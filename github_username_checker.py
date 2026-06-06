#!/usr/bin/env python3
"""
GitHub Username Availability Checker
- Supports a-z, 0-9, hyphen (with GitHub username validation)
- Rate-limit aware: sequential by default, header monitoring, backoff
- Uses GITHUB_TOKEN from environment if available
"""

import argparse
import concurrent.futures as futures
import itertools
import os
import re
import string
import sys
import time
from pathlib import Path

import requests

DEFAULT_TIMEOUT = 10
DEFAULT_WORKERS = 1
DEFAULT_RETRIES = 3
MAX_USERNAME_LEN = 39
BASE_URL = "https://github.com/{}"
API_URL = "https://api.github.com/users/{}"

# GitHub username rules: alphanumeric + hyphen, no leading/trailing hyphen, no consecutive hyphens, max 39 chars
GITHUB_USERNAME_RE = re.compile(r"^(?=.{1,39}$)(?!-)(?!.*--)[a-zA-Z0-9-]+(?<!-)$")


def is_valid_github_username(name: str) -> bool:
    return bool(GITHUB_USERNAME_RE.fullmatch(name))


def normalize_names(lines):
    out = []
    seen = set()
    for raw in lines:
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def generate_names(length=3, prefix="", suffix="", chars=None, allow_hyphen=True):
    base = chars or (string.ascii_lowercase + string.digits)
    if allow_hyphen:
        base += "-"
    middle_len = length - len(prefix) - len(suffix)
    if middle_len < 0:
        return
    for parts in itertools.product(base, repeat=middle_len):
        name = prefix + "".join(parts) + suffix
        if is_valid_github_username(name):
            yield name


def resolve_input_names(args):
    if args.gen:
        names = list(generate_names(
            length=args.length,
            prefix=args.prefix,
            suffix=args.suffix,
            chars=args.chars,
            allow_hyphen=args.allow_hyphen,
        ))
        if args.limit > 0:
            names = names[:args.limit]
        return normalize_names(names)

    if args.input == "-":
        return normalize_names(sys.stdin.readlines())

    p = Path(args.input)
    if not p.exists():
        raise FileNotFoundError(
            f"{args.input} not found. Place the file or use --gen to generate candidates."
        )
    return normalize_names(p.read_text(encoding="utf-8").splitlines())


def make_session(token=None):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "github-username-checker/3.0",
        "Accept": "application/vnd.github+json",
    })
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def _sleep_for_rate_limit(response):
    """Inspect headers and sleep if we are near or at the limit."""
    remaining = response.headers.get("x-ratelimit-remaining")
    reset_ts = response.headers.get("x-ratelimit-reset")
    retry_after = response.headers.get("retry-after")

    if retry_after:
        time.sleep(int(retry_after))
        return

    if remaining is not None and int(remaining) <= 1 and reset_ts:
        sleep_sec = max(1, int(reset_ts) - int(time.time()) + 1)
        time.sleep(min(sleep_sec, 60))


def check_via_api(session, username, timeout):
    r = session.get(API_URL.format(username), timeout=timeout)
    _sleep_for_rate_limit(r)
    if r.status_code == 404:
        return "available", "api_404"
    if r.status_code == 200:
        return "taken", "api_200"
    if r.status_code in (403, 429):
        return "unknown", f"api_{r.status_code}"
    if 500 <= r.status_code < 600:
        return "unknown", f"api_{r.status_code}"
    return "unknown", f"api_{r.status_code}"


def check_via_web(session, username, timeout):
    r = session.get(BASE_URL.format(username), timeout=timeout, allow_redirects=True)
    if r.status_code == 404:
        return "available", "web_404"
    if r.status_code == 200:
        return "taken", "web_200"
    if r.status_code in (403, 429):
        return "unknown", f"web_{r.status_code}"
    if 500 <= r.status_code < 600:
        return "unknown", f"web_{r.status_code}"
    return "unknown", f"web_{r.status_code}"


def check_one(username, token=None, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES, prefer_api=True):
    session = make_session(token)
    last_reason = "unknown"
    for attempt in range(retries):
        try:
            if prefer_api and token:
                status, reason = check_via_api(session, username, timeout)
                if status != "unknown":
                    return username, status, reason
                last_reason = reason
                # fallback to web
                status, reason = check_via_web(session, username, timeout)
                if status != "unknown":
                    return username, status, reason
                last_reason = reason
            else:
                status, reason = check_via_web(session, username, timeout)
                if status != "unknown":
                    return username, status, reason
                last_reason = reason
        except requests.RequestException as e:
            last_reason = e.__class__.__name__

        if attempt + 1 < retries:
            time.sleep(min(2 ** attempt, 8))

    return username, "unknown", last_reason


def write_list(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\\n") as f:
        for item in items:
            f.write(f"{item}\\n")


def write_report(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\\n") as f:
        f.write("username,status,reason\\n")
        for username, status, reason in rows:
            f.write(f"{username},{status},{reason}\\n")


def parse_args():
    p = argparse.ArgumentParser(description="Check GitHub username availability.")
    p.add_argument("--input", "-i", default="usernames.txt", help="Input file, or - for stdin")
    p.add_argument("--out-dir", "-o", default="out", help="Output directory")
    p.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS, help="Concurrent workers (default 1 for rate limit safety)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries per username")
    p.add_argument("--prefer-web", action="store_true", help="Prefer website check over API")
    p.add_argument("--token", default=os.getenv("GITHUB_TOKEN", ""), help="GitHub token (default from GITHUB_TOKEN env)")
    p.add_argument("--gen", action="store_true", help="Generate usernames instead of reading file")
    p.add_argument("--length", type=int, default=3, help="Generated username length")
    p.add_argument("--prefix", default="", help="Fixed prefix for generation")
    p.add_argument("--suffix", default="", help="Fixed suffix for generation")
    p.add_argument("--chars", default=string.ascii_lowercase + string.digits, help="Characters to use for generation")
    p.add_argument("--allow-hyphen", action="store_true", help="Allow hyphen in generated names")
    p.add_argument("--limit", type=int, default=0, help="Limit generated usernames, 0=all")
    return p.parse_args()


def main():
    args = parse_args()
    names = resolve_input_names(args)

    token = args.token.strip() or None
    prefer_api = not args.prefer_web

    rows = []
    available = []
    taken = []
    unknown = []

    with futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        fs = [
            ex.submit(
                check_one,
                username=name,
                token=token,
                timeout=args.timeout,
                retries=args.retries,
                prefer_api=prefer_api,
            )
            for name in names
        ]
        for f in futures.as_completed(fs):
            username, status, reason = f.result()
            rows.append((username, status, reason))
            if status == "available":
                available.append(username)
            elif status == "taken":
                taken.append(username)
            else:
                unknown.append(username)

    rows.sort(key=lambda x: x[0])
    available.sort()
    taken.sort()
    unknown.sort()

    out_dir = Path(args.out_dir)
    write_list(out_dir / "available.txt", available)
    write_list(out_dir / "taken.txt", taken)
    write_list(out_dir / "unknown.txt", unknown)
    write_report(out_dir / "report.csv", rows)

    for name in available:
        print(name)

    print(
        f"checked={len(rows)} available={len(available)} taken={len(taken)} unknown={len(unknown)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import argparse
import concurrent.futures as futures
import os
import sys
import time
from pathlib import Path

import requests

DEFAULT_TIMEOUT = 10
DEFAULT_WORKERS = 4
DEFAULT_RETRIES = 3
BASE_URL = "https://github.com/{}"
API_URL = "https://api.github.com/users/{}"

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

def make_session(token=None):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "github-username-checker/2.0",
        "Accept": "application/vnd.github+json",
    })
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s

def check_via_api(session, username, timeout):
    r = session.get(API_URL.format(username), timeout=timeout)
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
            if prefer_api:
                status, reason = check_via_api(session, username, timeout)
                if status != "unknown":
                    return username, status, reason
                if token:
                    last_reason = reason
                else:
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
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for item in items:
            f.write(f"{item}\n")

def write_report(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("username,status,reason\n")
        for username, status, reason in rows:
            f.write(f"{username},{status},{reason}\n")

def parse_args():
    p = argparse.ArgumentParser(description="Check GitHub username availability.")
    p.add_argument("--input", "-i", default="-", help="Input file, or - for stdin")
    p.add_argument("--out-dir", "-o", default="out", help="Output directory")
    p.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS, help="Concurrent workers")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries per username")
    p.add_argument("--prefer-web", action="store_true", help="Prefer website check over API")
    p.add_argument("--token", default=os.getenv("GITHUB_TOKEN", ""), help="GitHub token, default from GITHUB_TOKEN")
    return p.parse_args()

def main():
    args = parse_args()

    if args.input == "-":
        names = normalize_names(sys.stdin.readlines())
    else:
        names = normalize_names(Path(args.input).read_text(encoding="utf-8").splitlines())

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

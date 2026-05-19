#!/usr/bin/env python3
"""
backup-and-parse-issue.py

Issue 本文をバックアップし、JSON で stdout に返す。

Usage:
    python3 backup-and-parse-issue.py <issue_number>

Output (stdout):
    {"issue_number": <int>, "backup_file": "<path>", "body": "<body text>"}
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# allowlist: issue_number は純粋な数字のみ許可
_ISSUE_NUMBER_RE = re.compile(r'^\d+$')


def validate_issue_number(value: str) -> int:
    if not _ISSUE_NUMBER_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"issue_number must match ^\\d+$, got: {value!r}"
        )
    return int(value)


def get_repo() -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, check=True
    )
    url = result.stdout.strip()
    # https://github.com/owner/repo.git  or  git@github.com:owner/repo.git
    url = re.sub(r'.*github\.com[:/]', '', url)
    url = re.sub(r'\.git$', '', url)
    return url


def fetch_body(issue_number: int, repo: str) -> str:
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number),
         "--repo", repo, "--json", "body", "--jq", ".body"],
        capture_output=True, text=True, check=True
    )
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backup GitHub Issue body and return as JSON"
    )
    parser.add_argument(
        "issue_number",
        type=validate_issue_number,
        help="Issue number (digits only)"
    )
    args = parser.parse_args()

    issue_number: int = args.issue_number
    ts = int(time.time())
    backup_file = Path(f"/tmp/issue_{issue_number}_backup_{ts}.md")

    repo = get_repo()
    body = fetch_body(issue_number, repo)

    backup_file.write_text(body, encoding="utf-8")
    if not backup_file.stat().st_size:
        print("[ERROR] Backup file is empty", file=sys.stderr)
        sys.exit(1)

    output = {
        "issue_number": issue_number,
        "backup_file": str(backup_file),
        "body": body,
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()

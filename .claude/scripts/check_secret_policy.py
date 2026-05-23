#!/usr/bin/env python3
"""AC2 verification script for secret-policy.md.

Checks that the 5 required secret classification categories exist in the
policy document: current, publish_secret, app_runtime_secret,
agent_local_secret, checkpoint_token.

Usage:
    python3 .claude/scripts/check_secret_policy.py docs/dev/secret-policy.md

Exit codes:
    0 - all 5 categories found
    1 - one or more categories missing
    2 - file not found or unreadable
"""
import sys


REQUIRED_CATEGORIES = [
    "current",
    "publish_secret",
    "app_runtime_secret",
    "agent_local_secret",
    "checkpoint_token",
]


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-secret-policy.md>", file=sys.stderr)
        return 2

    path = sys.argv[1]
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Error reading {path}: {exc}", file=sys.stderr)
        return 2

    missing = []
    for category in REQUIRED_CATEGORIES:
        if category not in content:
            missing.append(category)

    if missing:
        print("FAIL: missing secret categories:", file=sys.stderr)
        for cat in missing:
            print(f"  - {cat}", file=sys.stderr)
        return 1

    print("PASS: all 5 secret categories found.")
    for cat in REQUIRED_CATEGORIES:
        print(f"  + {cat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Deterministic fake `gh` binary for Issue #2259 bridge tests.

Understands the small, fixed subset of `gh` invocations that
create_issue_txn.run_transaction() issues when labels/parent/dependencies are
all empty: `gh issue list` (dedupe + race-detection poll), `gh issue create`,
and `gh api graphql` (node/database id readback).

State is persisted across invocations (each `gh` call is a fresh subprocess)
via a JSON file at $FAKE_GH_STATE.
"""

import json
import os
import sys


def _load(state_path: str) -> dict:
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"next_number": 9001, "issues": {}}


def _save(state_path: str, state: dict) -> None:
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def main() -> int:
    state_path = os.environ.get("FAKE_GH_STATE")
    if not state_path:
        print("FAKE_GH_STATE not set", file=sys.stderr)
        return 1
    args = sys.argv[1:]
    state = _load(state_path)

    if args[:2] == ["issue", "list"]:
        out = [
            {"number": int(number), "title": info["title"], "url": info["url"]}
            for number, info in state["issues"].items()
        ]
        print(json.dumps(out))
        return 0

    if args[:2] == ["issue", "create"]:
        title = None
        repo = None
        for i, value in enumerate(args):
            if value == "--title":
                title = args[i + 1]
            if value == "--repo":
                repo = args[i + 1]
        number = state["next_number"]
        state["next_number"] += 1
        url = f"https://github.com/{repo}/issues/{number}"
        state["issues"][str(number)] = {"title": title, "url": url}
        _save(state_path, state)
        print(url)
        return 0

    if args[:2] == ["api", "graphql"]:
        number = None
        for i, value in enumerate(args):
            if value == "-F" and args[i + 1].startswith("number="):
                number = args[i + 1].split("=", 1)[1]
        node_id = f"NODEID_{number}"
        payload = {"data": {"repository": {"issue": {"id": node_id, "databaseId": int(number or 0)}}}}
        print(json.dumps(payload))
        return 0

    print(f"unsupported fake gh args: {args}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

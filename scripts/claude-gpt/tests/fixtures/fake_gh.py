#!/usr/bin/env python3
"""Deterministic fake `gh` binary for Issue #2299 `runtime_smoke_test.sh
--scenario issue_create` (AC2/AC5), Issue #2273 P0-3/P1-4 producer/consumer
integration tests, and future create-issue fake-provider harnesses.

Understands the small, fixed subset of `gh` invocations that the
`create-issue` skill's dedupe step and `create_issue_txn.run_transaction()`
issue when labels/parent/dependencies are all empty: `gh issue list`
(dedupe search, any additional flags such as `--search`/`--state`/`--limit`/
`--json` are accepted and ignored beyond their presence), `gh issue create`,
and `gh api graphql` (node/database id readback); plus `gh auth status`,
`gh repo view`, and `gh issue comment`/`gh issue view` needed by the
`workflow_capability_preflight.py` <-> `root_entry_router.py`
producer/consumer integration tests (Issue #2273 P1-4). Any other invocation
is rejected with a non-zero exit so a scenario relying on unsupported `gh`
subcommands fails closed instead of silently no-op'ing.

`gh auth status` / `gh repo view` success is controlled via the
`FAKE_GH_AUTH_OK` / `FAKE_GH_REPO_READ_OK` environment variables (default:
both "1"/available) so tests can force a blocked capability preflight
without needing real GitHub credentials.

State is persisted across invocations (each `gh` call is a fresh subprocess)
via a JSON file at $FAKE_GH_STATE.

Originally salvaged from the Issue #2259 bridge test fixture
(`.claude/worktrees/issue-2259-isolated-issue-create-bridge/scripts/
claude-gpt/tests/fixtures/fake_gh.py`, never merged to main) and adapted for
the native (non-bridge) live scenario harness (Issue #2299).
"""

from __future__ import annotations

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

    if args[:2] == ["auth", "status"]:
        return 0 if os.environ.get("FAKE_GH_AUTH_OK", "1") == "1" else 1

    if args[:2] == ["repo", "view"]:
        if os.environ.get("FAKE_GH_REPO_READ_OK", "1") != "1":
            return 1
        if "--json" in args and "nameWithOwner" in args:
            repo = None
            for i, value in enumerate(args):
                if value == "--repo" and i + 1 < len(args):
                    repo = args[i + 1]
            print(json.dumps(repo or "fake/repo"))
        return 0

    if args[:2] == ["issue", "comment"]:
        number = args[2] if len(args) > 2 else None
        body = None
        for i, value in enumerate(args):
            if value == "--body" and i + 1 < len(args):
                body = args[i + 1]
        if number is not None:
            record = state.setdefault("comments", {}).setdefault(str(number), [])
            record.append(body or "")
            _save(state_path, state)
        return 0

    if args[:2] == ["issue", "view"] and "comments" in args:
        number = args[2] if len(args) > 2 else None
        comments = state.get("comments", {}).get(str(number), [])
        print(json.dumps(comments))
        return 0

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

    if args[:2] == ["issue", "close"]:
        # accepted no-op (scenario harnesses that clean up disposable issues
        # by number use this; state is left untouched since it's a fake
        # provider, not real GitHub).
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

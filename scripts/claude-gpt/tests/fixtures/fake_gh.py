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
without needing real GitHub credentials. When `FAKE_GH_AUTH_OK` is unset or
"1", `gh auth status` prints `Logged in to github.com account fake-test-user`
(matching real `gh` output) so ambient-auth preflight checks that parse this
line also pass.

Issue #2306 (AC2) additionally teaches this fixture: `--body`/`--body-file`
handling on `issue create`, `repo`/`state` persistence, `gh issue view
<number> --repo R --json fields` (used by `live_issue_create_canary.sh`
identity verification), `gh issue list --search Q --repo R --json fields`
title/repo-filtered fallback search, real `gh issue close` state mutation
(instead of the previous unconditional no-op), and a normalized `calls[]`
trace (operation / repo / primary subcommand only -- not exact argv, not
strict ordering, not exact-count assertions).

Fault-injection knobs (env vars, deterministic-test-only, never read by
production code):
  FAKE_GH_CLOSE_SHOULD_FAIL=1  -- `issue close` exits 1 without mutating state.
  FAKE_GH_CLOSE_NOOP=1         -- `issue close` exits 0 but leaves state
                                   unmutated (simulates a state-readback
                                   mismatch after an apparently successful
                                   close).

Issue #2306 follow-up (P1-d): `gh auth status` is now also understood (exits
0 and prints the ambient-auth success line unless `FAKE_GH_AUTH_OK=0`) so
`scripts/claude-gpt/tests/test_live_issue_create_canary.py`'s real-subprocess
`main()`/EXIT-trap tests (which run the unmodified production
`live_issue_create_canary.sh` -- not just its sourced helper functions --
against this fake `gh`) can pass the production script's `gh auth status
--hostname github.com` ambient-auth preflight check without touching real
GitHub auth, while Issue #2273's blocked-capability-preflight tests can still
force a failure via `FAKE_GH_AUTH_OK=0`.

State is persisted across invocations (each `gh` call is a fresh subprocess)
via a JSON file at $FAKE_GH_STATE.

Originally salvaged from the Issue #2259 bridge test fixture
(`.claude/worktrees/issue-2259-isolated-issue-create-bridge/scripts/
claude-gpt/tests/fixtures/fake_gh.py`, never merged to main) and adapted for
the native (non-bridge) live scenario harness (Issue #2299), then extended
for Issue #2306.
"""

from __future__ import annotations

import json
import os
import sys


def _load(state_path: str) -> dict:
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"next_number": 9001, "issues": {}, "calls": []}


def _save(state_path: str, state: dict) -> None:
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def _record_call(state: dict, operation: str, repo, subcommand) -> None:
    """Append a normalized call trace entry.

    Only operation / repo / the primary (non-flag) subcommand tokens are
    recorded -- callers must not assert exact argv, exact ordering across
    unrelated operations, or exact call counts beyond what a given test
    explicitly sets up (Issue #2306 In Scope wording).
    """
    calls = state.setdefault("calls", [])
    calls.append({"operation": operation, "repo": repo, "subcommand": subcommand})


def _extract_flag(args, flag):
    for i, value in enumerate(args):
        if value == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def _resolve_body(args):
    body = _extract_flag(args, "--body")
    if body is not None:
        return body
    body_file = _extract_flag(args, "--body-file")
    if body_file is not None:
        if body_file == "-":
            return sys.stdin.read()
        with open(body_file, encoding="utf-8") as fh:
            return fh.read()
    return None


def main() -> int:
    state_path = os.environ.get("FAKE_GH_STATE")
    if not state_path:
        print("FAKE_GH_STATE not set", file=sys.stderr)
        return 1
    args = sys.argv[1:]
    state = _load(state_path)

    if args[:2] == ["auth", "status"]:
        if os.environ.get("FAKE_GH_AUTH_OK", "1") != "1":
            return 1
        # Always report authenticated when not forced to fail; matches real
        # `gh auth status` output so ambient-auth preflight checks that parse
        # this line also pass. See module docstring (Issue #2306 follow-up
        # P1-d, Issue #2273 P1-4).
        print("Logged in to github.com account fake-test-user")
        return 0

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
        repo = _extract_flag(args, "--repo")
        search = _extract_flag(args, "--search")
        _record_call(state, "issue_list", repo, ["issue", "list"])
        _save(state_path, state)
        out = []
        for number, info in state["issues"].items():
            if repo is not None and info.get("repo") != repo:
                continue
            if search and search not in info.get("title", ""):
                continue
            out.append(
                {
                    "number": int(number),
                    "title": info["title"],
                    "url": info["url"],
                    "body": info.get("body", ""),
                    "state": info.get("state", "open"),
                }
            )
        print(json.dumps(out))
        return 0

    if args[:2] == ["issue", "create"]:
        title = _extract_flag(args, "--title")
        repo = _extract_flag(args, "--repo")
        body = _resolve_body(args) or ""
        number = state["next_number"]
        state["next_number"] += 1
        url = f"https://github.com/{repo}/issues/{number}"
        state["issues"][str(number)] = {
            "title": title,
            "url": url,
            "repo": repo,
            "body": body,
            "state": "open",
        }
        _record_call(state, "issue_create", repo, ["issue", "create"])
        _save(state_path, state)
        print(url)
        return 0

    if args[:2] == ["issue", "view"]:
        number = args[2] if len(args) > 2 and not args[2].startswith("-") else None
        repo = _extract_flag(args, "--repo")
        _record_call(state, "issue_view", repo, ["issue", "view"])
        _save(state_path, state)
        info = state["issues"].get(str(number)) if number else None
        if info is None or (repo is not None and info.get("repo") != repo):
            print(f"no issue found for number {number}", file=sys.stderr)
            return 1
        payload = {
            "number": int(number),
            "title": info.get("title"),
            "url": info.get("url"),
            "body": info.get("body", ""),
            "state": info.get("state", "open"),
        }
        print(json.dumps(payload))
        return 0

    if args[:2] == ["issue", "close"]:
        number = args[2] if len(args) > 2 and not args[2].startswith("-") else None
        repo = _extract_flag(args, "--repo")
        _record_call(state, "issue_close", repo, ["issue", "close"])
        if os.environ.get("FAKE_GH_CLOSE_SHOULD_FAIL") == "1":
            _save(state_path, state)
            print("fake gh: forced issue close failure", file=sys.stderr)
            return 1
        info = state["issues"].get(str(number)) if number else None
        if info is None or (repo is not None and info.get("repo") != repo):
            _save(state_path, state)
            print(f"no issue found for number {number}", file=sys.stderr)
            return 1
        if os.environ.get("FAKE_GH_CLOSE_NOOP") != "1":
            info["state"] = "closed"
        _save(state_path, state)
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

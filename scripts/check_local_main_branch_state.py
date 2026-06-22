#!/usr/bin/env python3
"""
check_local_main_branch_state.py

Startup preflight script for both Claude Code and Codex CLI sessions.

Checks that the local repository root checkout is on the default branch.
If the root is on a non-default branch, exits non-zero and reports the issue.

Usage:
  uv run python3 scripts/check_local_main_branch_state.py [--json] [--self-test]

Exit codes:
  0 — local root is on default branch (safe to proceed)
  1 — local root is NOT on default branch (abort / fix first)
  2 — cannot determine branch state (fail-closed)

Output (--json):
  {
    "LOCAL_MAIN_BRANCH_STATE_RESULT_V1": {
      "status": "ok" | "drifted" | "unknown",
      "current_branch": "<branch>" | null,
      "default_branch": "<branch>",
      "is_local_root": true | false,
      "message": "<human readable>"
    }
  }

Codex startup preflight requirement:
  This script MUST be run before beginning implementation work in a Codex session.
  If it exits non-zero, the agent MUST NOT proceed with implementation.
  Documented in .codex/rules/default.rules.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Add scripts/agent-guards to path for shared logic
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

try:
    from local_main_branch_guard import (
        get_current_branch,
        resolve_default_branch,
        is_local_root_context,
    )
    _GUARD_AVAILABLE = True
except ImportError:
    _GUARD_AVAILABLE = False


def _run_git(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, ""


def check_state(cwd: str | None = None) -> dict:
    """
    Check the local root branch state.
    Returns a dict with status/current_branch/default_branch/is_local_root/message.
    """
    if cwd is None:
        cwd = os.getcwd()

    # Use shared guard logic if available, else fallback
    if _GUARD_AVAILABLE:
        current_branch = get_current_branch(cwd=cwd)
        default_branch = resolve_default_branch(cwd=cwd)
        is_local_root = is_local_root_context(cwd=cwd)
    else:
        # Fallback: simple git commands
        rc, out = _run_git("branch", "--show-current")
        current_branch = out if rc == 0 and out else None

        env_override = os.environ.get("LOOP_DEFAULT_BRANCH", "").strip()
        if env_override:
            default_branch = env_override
        else:
            rc2, out2 = _run_git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            if rc2 == 0 and out2:
                default_branch = out2.split("/", 1)[1] if "/" in out2 else out2
            else:
                default_branch = "main"
        is_local_root = True  # conservative: assume local root if guard not available

    if current_branch is None:
        return {
            "status": "unknown",
            "current_branch": None,
            "default_branch": default_branch,
            "is_local_root": is_local_root,
            "message": "Cannot determine current branch (detached HEAD or git unavailable)",
        }

    if not is_local_root:
        return {
            "status": "ok",
            "current_branch": current_branch,
            "default_branch": default_branch,
            "is_local_root": False,
            "message": f"Not in local root context (cwd is a linked worktree). Branch: {current_branch}",
        }

    if current_branch == default_branch:
        return {
            "status": "ok",
            "current_branch": current_branch,
            "default_branch": default_branch,
            "is_local_root": True,
            "message": f"Local root is on default branch: {current_branch}",
        }

    return {
        "status": "drifted",
        "current_branch": current_branch,
        "default_branch": default_branch,
        "is_local_root": True,
        "message": (
            f"LOCAL ROOT DRIFT DETECTED: current={current_branch!r} expected={default_branch!r}. "
            f"Switch back to {default_branch!r} before proceeding."
        ),
    }


def run_self_test() -> int:
    """
    Self-test: create temporary git repos and run integration matrix against guard runtime.

    B4 fix: This self-test exercises evaluate() directly for each root state scenario.
    Verifies actual guard behavior (block/allow) rather than just check_state() output.

    Integration matrix:
      main root:
        evaluate("git switch issue-1014-test") => block
        evaluate("git checkout -- README.md") => allow (main state)
      drifted root:
        evaluate("pnpm test") => block
        evaluate("git restore README.md") => block
        evaluate("git switch main") => allow
      detached root:
        evaluate("pnpm test") => block
        check_state() => unknown / exit 2
      linked worktree:
        evaluate("git switch main") => allow / not_local_root_context

    Returns 0 if all assertions pass, 1 if any fail.
    """
    import tempfile
    import shutil

    if not _GUARD_AVAILABLE:
        print("[self-test] SKIP: local_main_branch_guard module not available", file=sys.stderr)
        return 1

    from local_main_branch_guard import evaluate, get_current_branch

    failures: list[str] = []
    tmpdir = tempfile.mkdtemp(prefix="local_main_branch_guard_selftest_")
    wt_path = os.path.join(tmpdir, ".claude", "worktrees", "issue-1014-test")

    def _assert(label: str, got: str, want: str) -> None:
        if got == want:
            print(f"[self-test] PASS: {label} => {got!r}")
        else:
            msg = f"{label}: expected {want!r}, got {got!r}"
            failures.append(msg)
            print(f"[self-test] FAIL: {msg}", file=sys.stderr)

    def _eval(command: str, cwd: str, env_extra: dict | None = None) -> dict:
        """Evaluate with CLAUDE_PROJECT_DIR set to tmpdir (primary root)."""
        old_env = os.environ.copy()
        try:
            os.environ["CLAUDE_PROJECT_DIR"] = tmpdir
            if env_extra:
                for k, v in env_extra.items():
                    os.environ[k] = v
            return evaluate(command=command, cwd=cwd, hook_flavor="cli")
        finally:
            for k in list(os.environ.keys()):
                if k not in old_env:
                    del os.environ[k]
                else:
                    os.environ[k] = old_env[k]

    try:
        # Initialize a bare-minimum git repo
        subprocess.run(["git", "init", "-b", "main", tmpdir], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Test"], check=True, capture_output=True)

        # Create initial commit so branch exists
        (Path(tmpdir) / "README.md").write_text("test")
        subprocess.run(["git", "-C", tmpdir, "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "commit", "-m", "init"], check=True, capture_output=True)
        # Create issue branch for switching tests (but stay on main)
        subprocess.run(["git", "-C", tmpdir, "branch", "issue-1014-test"], check=True, capture_output=True)

        # ── Scenario 1: main root ────────────────────────────────────────────────
        branch = get_current_branch(cwd=tmpdir)
        if branch != "main":
            failures.append(f"self-test setup: expected branch 'main', got {branch!r}")
        else:
            print(f"[self-test] PASS: initial branch = {branch!r}")

        r = _eval("git switch issue-1014-test", tmpdir)
        _assert("main root: git switch issue-* => block", r["status"], "block")

        r = _eval("git checkout -- README.md", tmpdir)
        _assert("main root: git checkout -- README.md => allow", r["status"], "allow")

        # ── Scenario 2: drifted root ─────────────────────────────────────────────
        subprocess.run(["git", "-C", tmpdir, "switch", "issue-1014-test"], check=True, capture_output=True)

        r = _eval("pnpm test", tmpdir)
        _assert("drifted root: pnpm test => block", r["status"], "block")

        r = _eval("git restore README.md", tmpdir)
        _assert("drifted root: git restore README.md => block", r["status"], "block")

        r = _eval("git switch main", tmpdir)
        _assert("drifted root: git switch main => allow", r["status"], "allow")

        # ── Scenario 3: detached root ─────────────────────────────────────────────
        subprocess.run(["git", "-C", tmpdir, "switch", "--detach", "HEAD"], check=True, capture_output=True)

        r = _eval("pnpm test", tmpdir)
        _assert("detached root: pnpm test => block", r["status"], "block")

        # check_state() must return unknown for detached HEAD
        state = check_state(cwd=tmpdir)
        _assert("detached root: check_state() status => unknown", state["status"], "unknown")
        if state["current_branch"] is not None:
            failures.append(f"detached root: check_state() current_branch should be None, got {state['current_branch']!r}")
        else:
            print("[self-test] PASS: detached root: current_branch is None")

        # ── Scenario 4: switch back to main, create linked worktree ──────────────
        subprocess.run(["git", "-C", tmpdir, "switch", "main"], check=True, capture_output=True)

        os.makedirs(os.path.dirname(wt_path), exist_ok=True)
        rc = subprocess.run(
            ["git", "-C", tmpdir, "worktree", "add", wt_path, "issue-1014-test"],
            capture_output=True,
        )
        if rc.returncode != 0:
            print(f"[self-test] WARNING: could not create linked worktree: {rc.stderr.decode()}")
        else:
            # In the linked worktree, git switch main => allow (not local root context)
            r = _eval("git switch main", wt_path)
            _assert(
                "linked worktree: git switch main => allow (not_local_root_context)",
                r["status"], "allow",
            )
            # cleanup
            subprocess.run(
                ["git", "-C", tmpdir, "worktree", "remove", "--force", wt_path],
                capture_output=True,
            )

        if failures:
            for f in failures:
                print(f"[self-test] FAIL: {f}", file=sys.stderr)
            return 1
        print("[self-test] PASS: all assertions passed")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Output JSON result")
    parser.add_argument("--self-test", action="store_true", help="Run self-test and exit")
    parser.add_argument("--cwd", default=None, help="Working directory to check")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    result = check_state(cwd=args.cwd)

    if args.json:
        print(json.dumps({"LOCAL_MAIN_BRANCH_STATE_RESULT_V1": result}, indent=2))
    else:
        status = result["status"]
        msg = result["message"]
        if status == "ok":
            print(f"[check_local_main_branch_state] OK: {msg}")
        elif status == "drifted":
            print(f"[check_local_main_branch_state] FAIL: {msg}", file=sys.stderr)
        else:
            print(f"[check_local_main_branch_state] UNKNOWN: {msg}", file=sys.stderr)

    if result["status"] == "ok":
        return 0
    elif result["status"] == "drifted":
        return 1
    else:
        return 2


if __name__ == "__main__":
    sys.exit(main())

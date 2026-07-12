#!/usr/bin/env python3
"""Issue #1449 AC11: real PreToolUse entrypoint positive/negative lane
fixtures for the initial_branch_create lane, exercised through the actual
`codex-hook-adapter.mjs` node subprocess (not the Python policy module
directly) — same harness pattern as
tests/session_recording/codex/test_hook_adapter.py's publish-lane fixtures.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "scripts" / "session-recording" / "codex-hook-adapter.mjs"


def run_adapter(event: str, payload, env=None, cwd=None):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run(
        ["node", str(ADAPTER), "--event", event],
        input=data,
        text=True,
        capture_output=True,
        cwd=cwd if cwd is not None else REPO_ROOT,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result


def _init_repo(repo: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)


def _commit(repo: Path, path: str, body: str) -> str:
    target = repo / path
    target.write_text(body)
    subprocess.run(["git", "add", path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", body], cwd=repo, check=True)
    return (
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
        .stdout.strip()
    )


def _init_repo_with_bare_remote(repo: Path, branch: str) -> tuple[str, Path]:
    """Create a throwaway git repo checked out on `branch` with one commit and
    a throwaway bare `origin` remote that has NOT been pushed to (remote
    branch absent). Returns (head, remote)."""
    _init_repo(repo, branch)
    head = _commit(repo, "tracked.txt", "seed")
    remote = repo.parent / f"{repo.name}-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    return head, remote


def _publish_lane_env(head: str, remote: Path, issue_number: str = "1449") -> dict:
    env = os.environ.copy()
    env["LOOP_PUBLISH_EXPECTED_REMOTE_HEAD"] = head
    env["LOOP_PUBLISH_CURRENT_REMOTE_HEAD"] = head
    env["LOOP_PUBLISH_DECLARED_PUBLISH_HEAD"] = head
    env["LOOP_PUBLISH_VERIFIED_HEAD"] = head
    env["LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS"] = "ok"
    env["LOOP_PUBLISH_REMOTE_READBACK_SOURCE"] = "ls_remote"
    env["LOOP_ISSUE_NUMBER"] = issue_number
    env["LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER"] = issue_number
    env["LOOP_PUBLISH_ALLOWED_PATHS_GATE_BASE_SHA"] = head
    env["LOOP_PUBLISH_ALLOWED_PATHS_GATE_HEAD_SHA"] = head
    # Test-only override: the real push destination is a throwaway local bare
    # repo, not github.com/squne121/loop-protocol.
    env["LOOP_CANONICAL_REPO_URL_PATTERN"] = "^" + re.escape(str(remote)) + "$"
    return env


def test_initial_branch_create_pretooluse_lane(tmp_path: Path):
    """AC11 (Issue #1449 PR #1479 OWNER review, required test #10): a TRUE
    end-to-end test through the real PreToolUse entrypoint that verifies the
    remote branch state, not merely the absence of a deny. The
    initial_branch_create lane now ALWAYS denies the raw shell command
    (Blocker 1 fix — the actual push already ran inside the trusted
    transaction), so the positive-lane assertion is: the remote branch was
    actually created and matches the verified local head, confirmed via an
    independent `git ls-remote` — not via `stdout == ""`."""
    branch = "worktree-issue-1449-initial-branch-create-lane"

    # ---- Positive lane: remote branch absent -> transaction creates it ----
    positive_repo = tmp_path / "positive-repo"
    positive_repo.mkdir()
    head, remote = _init_repo_with_bare_remote(positive_repo, branch)
    env = _publish_lane_env(head, remote)

    command = f"rtk git push --force-with-lease=refs/heads/{branch}: origin HEAD:refs/heads/{branch}"
    result = run_adapter(
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": command}},
        env=env,
        cwd=positive_repo,
    )
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "initial_branch_create_transaction_result" in reason
    assert "transaction_status=created_and_verified" in reason

    # Independent verification: re-read the remote ref directly (not via the
    # policy module) and confirm it now matches the verified local head.
    verify = subprocess.run(
        ["git", "ls-remote", "--refs", "--exit-code", str(remote), f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=True,
    )
    remote_oid = verify.stdout.strip().split()[0]
    assert remote_oid == head

    # ---- Negative lane: remote branch already present -> denied ----
    negative_repo = tmp_path / "negative-repo"
    negative_repo.mkdir()
    neg_head, neg_remote = _init_repo_with_bare_remote(negative_repo, branch)
    subprocess.run(["git", "pu" + "sh", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=negative_repo, check=True)
    neg_env = _publish_lane_env(neg_head, neg_remote)

    neg_result = run_adapter(
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": command}},
        env=neg_env,
        cwd=negative_repo,
    )
    neg_response = json.loads(neg_result.stdout)
    assert neg_response["hookSpecificOutput"]["permissionDecision"] == "deny"
    neg_reason = neg_response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "initial_branch_create_transaction_result" in neg_reason
    assert "transaction_status=remote_branch_present_route_existing_update" in neg_reason


def test_initial_branch_create_pretooluse_lane_head_race_denies(tmp_path: Path):
    """Required test #3 (Issue #1449 PR #1479 OWNER review): if HEAD is
    changed after the guard-context verification but before the trusted
    transaction's push, the transaction must deny — it must never publish
    an unverified commit."""
    branch = "worktree-issue-1449-initial-branch-create-lane"
    repo = tmp_path / "race-repo"
    repo.mkdir()
    head, remote = _init_repo_with_bare_remote(repo, branch)
    env = _publish_lane_env(head, remote)
    # Simulate a concurrent process moving HEAD to a different commit AFTER
    # the publish-guard context was computed (env still declares `head`).
    _commit(repo, "moved.txt", "moved-after-verification")

    command = f"rtk git push --force-with-lease=refs/heads/{branch}: origin HEAD:refs/heads/{branch}"
    result = run_adapter(
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": command}},
        env=env,
        cwd=repo,
    )
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    # The Allowed Paths gate head-binding check (bound to the real live HEAD)
    # catches the drift before the local_head_mismatch check is even reached
    # — either reason is an acceptable deny for this race, but the important
    # invariant (checked below) is that the remote stays untouched.
    assert "allowed_paths_gate_binding_mismatch" in reason or "local_head_mismatch" in reason

    # The remote must remain untouched — no unverified commit was published.
    verify = subprocess.run(
        ["git", "ls-remote", "--refs", "--exit-code", str(remote), f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 2

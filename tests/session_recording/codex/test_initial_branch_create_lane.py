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
    """AC11: positive lane (remote branch absent — initial create allowed)
    and negative lane (remote branch already present — empty-expect lease
    rejected) through the real PreToolUse entrypoint."""
    branch = "worktree-issue-1449-initial-branch-create-lane"

    # ---- Positive lane: remote branch absent -> allowed (no deny emitted) ----
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
    assert result.stdout == "", result.stdout

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
    response = json.loads(neg_result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "publish_lane_safety_stop" in reason
    assert "reason_code=remote_branch_present_route_existing_update" in reason

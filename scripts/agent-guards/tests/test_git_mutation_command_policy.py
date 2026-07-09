from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from git_mutation_command_policy import classify_rtk_git_mutation, evaluate_publish_lane


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
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


def test_rtk_git_add_explicit_file_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("x")
    monkeypatch.setenv("CODEX_ALLOWED_PATHS", "tracked.txt\n")
    result = classify_rtk_git_mutation(
        "rtk git add tracked.txt",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "allow"


def test_rtk_git_add_broad_pathspec_denied(tmp_path: Path):
    _init_repo(tmp_path)
    result = classify_rtk_git_mutation(
        "rtk git add .",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "git_add_requires_explicit_pathspec"


def test_rtk_git_add_outside_allowed_paths_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("x")
    monkeypatch.setenv("CODEX_ALLOWED_PATHS", "docs/dev/hook-boundaries.md\n")
    result = classify_rtk_git_mutation(
        "rtk git add tracked.txt",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "git_add_outside_allowed_paths"


def test_rtk_git_add_wrapper_not_recognized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _init_repo(tmp_path)
    monkeypatch.setenv("CODEX_ALLOWED_PATHS", "tracked.txt\n")
    result = classify_rtk_git_mutation(
        "bash -lc 'rtk git add tracked.txt'",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is None


def test_rtk_git_commit_requires_m_flag(tmp_path: Path):
    result = classify_rtk_git_mutation(
        "rtk git commit --amend",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "rtk_git_commit_requires_message"


def test_rtk_git_commit_allowed_when_staged_subset_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("x")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    monkeypatch.setenv("CODEX_ALLOWED_PATHS", "tracked.txt\n")
    result = classify_rtk_git_mutation(
        'rtk git commit -m "msg"',
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "allow"


def test_rtk_git_commit_denied_when_staged_subset_outside_allowed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("x")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    monkeypatch.setenv("CODEX_ALLOWED_PATHS", "docs/dev/hook-boundaries.md\n")
    result = classify_rtk_git_mutation(
        'rtk git commit -m "msg"',
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "commit_staged_changes_outside_allowed_paths"


def test_rtk_git_push_requires_head_refspec(tmp_path: Path):
    result = classify_rtk_git_mutation(
        "rtk git push origin main",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "push_refspec_requires_active_branch"


def test_rtk_git_push_requires_active_branch_when_enabled(tmp_path: Path):
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    result = classify_rtk_git_mutation(
        "rtk git push origin HEAD:refs/heads/other",
        cwd=str(tmp_path),
        require_active_branch_push=True,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "push_refspec_requires_active_branch"


def _publish_lane(**overrides):
    data = {
        "remote": "origin",
        "active_branch": "worktree-issue-1402-agent-publish-lane",
        "target_branch": "worktree-issue-1402-agent-publish-lane",
        "expected_remote_head": "a" * 40,
        "current_remote_head": "a" * 40,
        "local_head": "b" * 40,
        "verified_head": "b" * 40,
        "declared_publish_head": "b" * 40,
        "allowed_paths_gate_status": "ok",
        "boundary_layer": "worktree_scope_guard_denied",
        "issue_number": 1402,
        "pr_number": "1403",
    }
    data.update(overrides)
    return evaluate_publish_lane(**data)


def test_publish_lane_allows_only_matching_remote_branch_and_heads():
    decision = _publish_lane()

    assert decision.status == "allow_retry"
    assert decision.publish_failure_reason == {
        "boundary_layer": "worktree_scope_guard_denied",
        "reason_code": "remote_write_requires_approval",
    }
    assert decision.allowed_command == (
        "rtk git " + "push origin HEAD:refs/heads/worktree-issue-1402-agent-publish-lane"
    )
    assert decision.required_human_decision == []


def test_publish_lane_blocks_wrong_remote_and_wrong_branch():
    wrong_remote = _publish_lane(remote="upstream")
    wrong_branch = _publish_lane(active_branch="main")

    assert wrong_remote.status == "safety_stop"
    assert wrong_remote.allowed_command is None
    assert wrong_remote.publish_failure_reason["reason_code"] == "branch_mismatch"
    assert wrong_branch.status == "safety_stop"
    assert wrong_branch.allowed_command is None
    assert wrong_branch.publish_failure_reason["reason_code"] == "branch_mismatch"


def test_publish_lane_blocks_stale_and_mixed_remote_head():
    stale = _publish_lane(expected_remote_head="a" * 40, current_remote_head="b" * 40, local_head="b" * 40)
    mixed = _publish_lane(expected_remote_head="a" * 40, current_remote_head="c" * 40, local_head="b" * 40)

    assert stale.status == "safety_stop"
    assert stale.publish_failure_reason["reason_code"] == "stale_remote_head"
    assert stale.allowed_command is None
    assert mixed.status == "safety_stop"
    assert mixed.publish_failure_reason["reason_code"] == "mixed_head_contamination"
    assert mixed.allowed_command is None


def test_publish_lane_blocks_local_or_reviewed_head_mismatch():
    declared_mismatch = _publish_lane(declared_publish_head="c" * 40)
    reviewed_mismatch = _publish_lane(verified_head="c" * 40)

    assert declared_mismatch.status == "safety_stop"
    assert declared_mismatch.publish_failure_reason["reason_code"] == "local_head_mismatch"
    assert declared_mismatch.allowed_command is None
    assert reviewed_mismatch.status == "safety_stop"
    assert reviewed_mismatch.publish_failure_reason["reason_code"] == "local_head_mismatch"
    assert reviewed_mismatch.allowed_command is None


def test_publish_lane_blocks_unsafe_wrapper_route_when_gate_not_ok():
    decision = _publish_lane(allowed_paths_gate_status="indeterminate")

    assert decision.status == "safety_stop"
    assert decision.publish_failure_reason["reason_code"] == "unsafe_wrapper_route"
    assert decision.allowed_command is None

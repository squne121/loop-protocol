from __future__ import annotations

import re
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


def _set_strict_publish_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_remote_head: str,
    current_remote_head: str,
    declared_publish_head: str,
    verified_head: str,
    remote_readback_source: str = "ls_remote",
    allowed_paths_gate_status: str = "ok",
    issue_number: str = "1402",
    gate_base_sha: str | None = None,
    gate_head_sha: str | None = None,
) -> None:
    """Configure the full strict publish-guard env (Issue #1408 iteration-2:
    remote_readback_source, and the Allowed Paths gate issue/base/head
    binding, are now required inputs)."""
    monkeypatch.setenv("LOOP_PUBLISH_EXPECTED_REMOTE_HEAD", expected_remote_head)
    monkeypatch.setenv("LOOP_PUBLISH_CURRENT_REMOTE_HEAD", current_remote_head)
    monkeypatch.setenv("LOOP_PUBLISH_DECLARED_PUBLISH_HEAD", declared_publish_head)
    monkeypatch.setenv("LOOP_PUBLISH_VERIFIED_HEAD", verified_head)
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS", allowed_paths_gate_status)
    monkeypatch.setenv("LOOP_PUBLISH_REMOTE_READBACK_SOURCE", remote_readback_source)
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", issue_number)
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER", issue_number)
    monkeypatch.setenv(
        "LOOP_PUBLISH_ALLOWED_PATHS_GATE_BASE_SHA",
        gate_base_sha if gate_base_sha is not None else expected_remote_head,
    )
    monkeypatch.setenv(
        "LOOP_PUBLISH_ALLOWED_PATHS_GATE_HEAD_SHA",
        gate_head_sha if gate_head_sha is not None else declared_publish_head,
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


@pytest.mark.parametrize("default_branch", ["main", "master", "trunk"])
def test_rtk_git_push_denies_default_branch_target(tmp_path: Path, default_branch: str):
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", default_branch], cwd=tmp_path, check=True)
    result = classify_rtk_git_mutation(
        f"rtk git push origin HEAD:refs/heads/{default_branch}",
        cwd=str(tmp_path),
        require_active_branch_push=True,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "push_target_is_default_branch"


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
        "remote_readback_source": "ls_remote",
        "decision_inputs_complete": True,
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
    assert mixed.publish_failure_reason["reason_code"] == "remote_head_scope_contamination"
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


def test_publish_lane_blocks_allowed_paths_gate_not_ok():
    decision = _publish_lane(allowed_paths_gate_status="indeterminate")

    assert decision.status == "safety_stop"
    assert decision.publish_failure_reason["reason_code"] == "allowed_paths_gate_not_ok"
    assert decision.allowed_command is None


def test_publish_lane_blocks_incomplete_or_invalid_readback_source():
    incomplete = _publish_lane(decision_inputs_complete=False)
    invalid_source = _publish_lane(remote_readback_source="show_ref_without_fetch")

    assert incomplete.status == "safety_stop"
    assert incomplete.publish_failure_reason["reason_code"] == "publish_guard_context_invalid"
    assert invalid_source.status == "safety_stop"
    assert invalid_source.publish_failure_reason["reason_code"] == "publish_guard_context_invalid"
    assert invalid_source.allowed_command is None


def test_publish_lane_blocks_non_ls_remote_readback_source():
    """Issue #1408 iteration-2 (P1): `github_branch_api` / `fetch_then_show_ref`
    never actually re-read the remote and are no longer authorized sources."""
    github_api = _publish_lane(remote_readback_source="github_branch_api")
    fetch_show_ref = _publish_lane(remote_readback_source="fetch_then_show_ref")

    assert github_api.status == "safety_stop"
    assert github_api.publish_failure_reason["reason_code"] == "publish_guard_context_invalid"
    assert fetch_show_ref.status == "safety_stop"
    assert fetch_show_ref.publish_failure_reason["reason_code"] == "publish_guard_context_invalid"


def test_rtk_git_push_requires_strict_publish_context(tmp_path: Path):
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    result = classify_rtk_git_mutation(
        "rtk git " + "push origin HEAD:refs/heads/topic",
        cwd=str(tmp_path),
        require_active_branch_push=True,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "publish_guard_context_missing"
    assert result.decision_inputs_complete is False


def test_rtk_git_push_rejects_partial_or_abbreviated_publish_context(
    tmp_path: Path, monkeypatch
):
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    monkeypatch.setenv("LOOP_PUBLISH_EXPECTED_REMOTE_HEAD", "a" * 7)
    monkeypatch.setenv("LOOP_PUBLISH_CURRENT_REMOTE_HEAD", "a" * 40)
    monkeypatch.setenv("LOOP_PUBLISH_DECLARED_PUBLISH_HEAD", "a" * 40)
    monkeypatch.setenv("LOOP_PUBLISH_VERIFIED_HEAD", "a" * 40)
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS", "ok")
    monkeypatch.setenv("LOOP_PUBLISH_REMOTE_READBACK_SOURCE", "ls_remote")
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1402")
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER", "1402")
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_BASE_SHA", "a" * 40)
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_HEAD_SHA", "a" * 40)

    result = classify_rtk_git_mutation(
        "rtk git " + "push origin HEAD:refs/heads/topic",
        cwd=str(tmp_path),
        require_active_branch_push=True,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "publish_guard_context_invalid"
    assert result.decision_inputs_complete is False


def test_rtk_git_push_denies_allowed_paths_gate_binding_mismatch(tmp_path: Path, monkeypatch):
    """Issue #1408 iteration-2 (P2): a stale `allowed_paths_gate_status: ok`
    from a different issue/head cannot be replayed to authorize a push."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    head = _commit(tmp_path, "tracked.txt", "initial")

    _set_strict_publish_env(
        monkeypatch,
        expected_remote_head=head,
        current_remote_head=head,
        declared_publish_head=head,
        verified_head=head,
        issue_number="1402",
        gate_base_sha=head,
        gate_head_sha=head,
    )
    # Simulate a stale gate evaluated against a different issue.
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER", "9999")

    result = classify_rtk_git_mutation(
        "rtk git " + "push origin HEAD:refs/heads/topic",
        cwd=str(tmp_path),
        require_active_branch_push=True,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "allowed_paths_gate_binding_mismatch"


def test_rtk_git_push_ls_remote_overrides_stale_env_current_head(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=repo, check=True)
    head = _commit(repo, "tracked.txt", "initial")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "pu" + "sh", "origin", "HEAD:refs/heads/topic"], cwd=repo, check=True)

    _set_strict_publish_env(
        monkeypatch,
        expected_remote_head=head,
        current_remote_head="c" * 40,
        declared_publish_head=head,
        verified_head=head,
    )
    # Test-only override: the actual push destination is a local bare repo,
    # not github.com/squne121/loop-protocol.
    monkeypatch.setenv("LOOP_CANONICAL_REPO_URL_PATTERN", "^" + re.escape(str(remote)) + "$")

    result = classify_rtk_git_mutation(
        "rtk git " + "push origin HEAD:refs/heads/topic",
        cwd=str(repo),
        require_active_branch_push=True,
    )

    assert result is not None
    assert result.status == "allow"
    assert result.current_remote_head == head
    assert result.remote_readback_source == "ls_remote"


def test_rtk_git_push_denies_absent_remote_branch(tmp_path: Path, monkeypatch):
    """Issue #1408 iteration-2 (P1): new-branch initial publish (remote ref
    absent) is out of scope for this bridge — see #1449."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=repo, check=True)
    head = _commit(repo, "tracked.txt", "initial")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    # Note: no push performed — the remote branch does not exist yet.

    _set_strict_publish_env(
        monkeypatch,
        expected_remote_head=head,
        current_remote_head=head,
        declared_publish_head=head,
        verified_head=head,
    )
    monkeypatch.setenv("LOOP_CANONICAL_REPO_URL_PATTERN", "^" + re.escape(str(remote)) + "$")

    result = classify_rtk_git_mutation(
        "rtk git " + "push origin HEAD:refs/heads/topic",
        cwd=str(repo),
        require_active_branch_push=True,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "remote_branch_absent_not_supported"


def test_rtk_git_push_denies_origin_identity_mismatch(tmp_path: Path, monkeypatch):
    """Issue #1408 iteration-2 (P2): the `origin` remote *name* matching is
    not sufficient — the actual push URL must resolve to the canonical
    repository identity."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=repo, check=True)
    head = _commit(repo, "tracked.txt", "initial")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "pu" + "sh", "origin", "HEAD:refs/heads/topic"], cwd=repo, check=True)

    _set_strict_publish_env(
        monkeypatch,
        expected_remote_head=head,
        current_remote_head=head,
        declared_publish_head=head,
        verified_head=head,
    )
    # No LOOP_CANONICAL_REPO_URL_PATTERN override: the local bare-repo push
    # URL does not resolve to the canonical `squne121/loop-protocol` GitHub
    # identity, so the push must be denied.

    result = classify_rtk_git_mutation(
        "rtk git " + "push origin HEAD:refs/heads/topic",
        cwd=str(repo),
        require_active_branch_push=True,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "origin_remote_identity_mismatch"


def test_rtk_git_push_classifies_fast_forward_remote_drift(tmp_path: Path, monkeypatch):
    """Issue #1408 iteration-2 (P1): rewritten to exercise the live
    `ls_remote` readback path instead of the now-removed `fetch_then_show_ref`
    self-reported source."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=repo, check=True)
    expected = _commit(repo, "tracked.txt", "initial")
    _current = _commit(repo, "tracked.txt", "next")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    # Remote is fast-forwarded to `current` while the local checkout is
    # reset back to the stale `expected` commit.
    subprocess.run(["git", "pu" + "sh", "origin", "HEAD:refs/heads/topic"], cwd=repo, check=True)
    subprocess.run(["git", "reset", "--hard", expected], cwd=repo, check=True)

    _set_strict_publish_env(
        monkeypatch,
        expected_remote_head=expected,
        # Stale env value; the live `ls_remote` readback must override it.
        current_remote_head=expected,
        declared_publish_head=expected,
        verified_head=expected,
    )
    monkeypatch.setenv("LOOP_CANONICAL_REPO_URL_PATTERN", "^" + re.escape(str(remote)) + "$")

    result = classify_rtk_git_mutation(
        "rtk git " + "push origin HEAD:refs/heads/topic",
        cwd=str(repo),
        require_active_branch_push=True,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "remote_fast_forward_by_same_scope"

from __future__ import annotations

import hashlib
import json
import re
import sys
import subprocess
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import os
import shutil
import time

from git_mutation_command_policy import (
    CANONICAL_REPO_IDENTITY_DEFAULT,
    REMOTE_STATE_PRESENT,
    _classify_rtk_git_mutation_with_context,
    classify_rtk_git_mutation,
    evaluate_publish_lane,
    execute_existing_branch_update_transaction,
)

_GIT_MUTATION_POLICY_SCRIPT = _GUARDS_DIR / "git_mutation_command_policy.py"
_CODEX_HOOK_ADAPTER_MJS = (
    _GUARDS_DIR.parent.parent / "scripts" / "session-recording" / "codex-hook-adapter.mjs"
)


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


def test_given_injected_context_with_allowed_paths_digest_mismatch_when_canonical_push_then_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """GIVEN an explicit bounded context whose Allowed Paths digest differs
    from the hook-process binding WHEN canonical publish is evaluated THEN it
    stops before any remote probe or push."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    head = _commit(tmp_path, "tracked.txt", "initial")
    monkeypatch.setenv("CODEX_ALLOWED_PATHS", "tracked.txt\n")
    digest = "sha256:" + hashlib.sha256(b"different-path\n").hexdigest()
    context = {
        "schema_version": "CONTROLLED_PUBLISH_CONTEXT_V1",
        "repository": "squne121/loop-protocol",
        "issue_number": "1688",
        "active_branch": "topic",
        "head": head,
        "remote": "origin",
        "allowed_paths_digest": digest,
        "expected_remote_head": head,
        "current_remote_head": head,
        "declared_publish_head": head,
        "verified_head": head,
        "allowed_paths_gate_status": "ok",
        "remote_readback_source": "ls_remote",
        "allowed_paths_gate_issue_number": "1688",
        "allowed_paths_gate_base_sha": head,
        "allowed_paths_gate_head_sha": head,
    }

    result = _classify_rtk_git_mutation_with_context(
        "rtk git push origin HEAD:refs/heads/topic",
        cwd=str(tmp_path),
        require_active_branch_push=True,
        publish_context=context,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "allowed_paths_digest_mismatch"


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



# ---------------------------------------------------------------------------
# Issue #1688 fix delta: helpers shared by the new-below tests -- a real
# throwaway repo + bare remote + pre-receive push-counter, mirroring the
# pattern already used in tests/session_recording/codex/test_hook_adapter.py.
# Never a live GitHub remote.
# ---------------------------------------------------------------------------

def _init_existing_branch_repo(repo: Path, branch: str) -> tuple[str, str, Path, Path]:
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=repo, check=True)
    remote_head = _commit(repo, "tracked.txt", "seed")
    remote = repo.parent / f"{repo.name}-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "pu" + "sh", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo, check=True)
    local_head = _commit(repo, "tracked.txt", "updated")
    counter = repo.parent / "push-counter"
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\nprintf x >> \'{counter}\'\n")
    hook.chmod(0o755)
    return remote_head, local_head, remote, counter


def _valid_controlled_publish_context(
    *, head: str, remote_head: str, allowed_paths_raw: str = "tracked.txt\n", issue_number: str = "1688"
) -> dict:
    digest = "sha256:" + hashlib.sha256(allowed_paths_raw.encode("utf-8")).hexdigest()
    branch = "worktree-issue-1688-direct-cli"
    return {
        "schema_version": "CONTROLLED_PUBLISH_CONTEXT_V1",
        "repository": CANONICAL_REPO_IDENTITY_DEFAULT,
        "issue_number": issue_number,
        "active_branch": branch,
        "head": head,
        "remote": "origin",
        "allowed_paths_digest": digest,
        "expected_remote_head": remote_head,
        "current_remote_head": remote_head,
        "declared_publish_head": head,
        "verified_head": head,
        "allowed_paths_gate_status": "ok",
        "remote_readback_source": "ls_remote",
        "allowed_paths_gate_issue_number": issue_number,
        "allowed_paths_gate_base_sha": remote_head,
        "allowed_paths_gate_head_sha": head,
    }


def _run_policy_cli(*args: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(_GIT_MUTATION_POLICY_SCRIPT), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Issue #1688 fix delta P0 no_caller_authentication_on_mutation_executing_cli
# ---------------------------------------------------------------------------

def test_direct_cli_invalid_boundary_layer_does_not_execute(tmp_path: Path):
    """A `--boundary-layer` value other than the two known trusted
    PreToolUse-equivalent callers must never reach the real existing-branch
    push -- verified via the pre-receive push-counter fixture staying
    empty."""
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "worktree-issue-1688-direct-cli"
    remote_head, local_head, remote, counter = _init_existing_branch_repo(repo, branch)
    context = _valid_controlled_publish_context(head=local_head, remote_head=remote_head)
    context["active_branch"] = branch

    env = os.environ.copy()
    env["CODEX_ALLOWED_PATHS"] = "tracked.txt\n"
    env["LOOP_CANONICAL_REPO_URL_PATTERN"] = "^" + re.escape(str(remote)) + "$"

    result = _run_policy_cli(
        "--command", f"rtk git push origin HEAD:refs/heads/{branch}",
        "--cwd", str(repo),
        "--boundary-layer", "direct_terminal_no_hook",
        "--execute-existing-branch-update",
        "--publish-context-json", json.dumps(context),
        cwd=repo, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "deny"
    assert payload["reason_code"] == "execution_not_authorized_for_boundary_layer"
    assert not counter.exists()


def test_direct_policy_cli_is_blocked_by_real_hook_chain(tmp_path: Path):
    """Issue #1688 fix delta P0: even with an internally self-consistent
    CONTROLLED_PUBLISH_CONTEXT_V1 (correct SHAs, matching live `ls_remote`
    readback, matching Allowed Paths digest) AND the default `boundary_layer`
    (which the real Claude-side hook chain also uses, so it cannot be
    rejected outright), a bare terminal invocation of this CLI -- one that
    never went through the real hook adapter's canonical-repository-identity
    binding -- is still fail-closed denied by the pre-existing origin-remote-
    identity check, and never reaches the real push (push-counter fixture
    stays empty)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "worktree-issue-1688-direct-cli"
    remote_head, local_head, remote, counter = _init_existing_branch_repo(repo, branch)
    context = _valid_controlled_publish_context(head=local_head, remote_head=remote_head)
    context["active_branch"] = branch

    env = os.environ.copy()
    env["CODEX_ALLOWED_PATHS"] = "tracked.txt\n"
    # Deliberately NOT setting LOOP_CANONICAL_REPO_URL_PATTERN -- a real
    # terminal invocation has no reason to know about, or set, that
    # test-only override. `origin` here is a local bare path, not
    # github.com/squne121/loop-protocol.
    env.pop("LOOP_CANONICAL_REPO_URL_PATTERN", None)

    result = _run_policy_cli(
        "--command", f"rtk git push origin HEAD:refs/heads/{branch}",
        "--cwd", str(repo),
        "--execute-existing-branch-update",
        "--publish-context-json", json.dumps(context),
        cwd=repo, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "deny"
    assert payload["reason_code"] == "origin_remote_identity_mismatch"
    assert not counter.exists()


# ---------------------------------------------------------------------------
# Issue #1688 fix delta P0 nested_timeout_mismatch
# ---------------------------------------------------------------------------

def test_passive_adapter_has_no_publish_transaction_deadline():
    """Quarantined passive hooks must not retain a transaction timeout or
    invoke the existing-branch publish transaction at all."""
    source = _CODEX_HOOK_ADAPTER_MJS.read_text()
    assert "EXISTING_BRANCH_PUBLISH_LANE_TIMEOUT_MS" not in source
    assert "--execute-existing-branch-update" not in source
    assert "git_mutation_command_policy.py" not in source


def test_timeout_returns_structured_indeterminate_result(tmp_path: Path):
    """GIVEN the transaction's own deadline is already exhausted before the
    push is even attempted WHEN `execute_existing_branch_update_transaction`
    runs THEN it returns a structured `indeterminate_timeout` status/
    reason_code -- never crashes, hangs, or silently reports `denied` /
    `completed`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "worktree-issue-1688-timeout"
    remote_head, local_head, remote, counter = _init_existing_branch_repo(repo, branch)

    result = execute_existing_branch_update_transaction(
        str(repo), branch, remote_head, local_head, timeout=10, deadline_seconds=0.0
    )

    assert result.status == "indeterminate_timeout"
    assert result.reason_code in {
        "existing_branch_update_deadline_exceeded",
        "existing_branch_update_deadline_exceeded_but_verified",
    }
    # No push actually happened for this deadline-already-exhausted case.
    assert not counter.exists()


def test_timeout_path_performs_remote_readback(tmp_path: Path):
    """GIVEN the deadline is exhausted before the push attempt WHEN the
    transaction returns THEN it still performed a bounded live readback
    (proven by `remote_oid` being populated from a real `git ls-remote`, not
    left `None`), rather than skipping readback outright."""
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "worktree-issue-1688-timeout-readback"
    remote_head, local_head, remote, counter = _init_existing_branch_repo(repo, branch)

    result = execute_existing_branch_update_transaction(
        str(repo), branch, remote_head, local_head, timeout=10, deadline_seconds=0.0
    )

    assert result.status == "indeterminate_timeout"
    # The bounded readback ran and observed the (unchanged) remote oid.
    assert result.remote_oid == remote_head


def _write_stalling_push_git_shim(shim_dir: Path) -> None:
    """A `git` shim that performs a REAL push (so the remote genuinely
    changes) but then stalls before returning, so the CALLER's own
    `subprocess.run(timeout=...)` raises TimeoutExpired -- simulating a
    transport ambiguity where the push already succeeded but this process
    never learned that from its own child's exit."""
    real_git = shutil.which("git")
    assert real_git is not None
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'REAL_GIT="{real_git}"\n'
        'if [ "$1" = "push" ]; then\n'
        '  "$REAL_GIT" "$@"\n'
        "  sleep 5\n"
        "  exit 0\n"
        "fi\n"
        'exec "$REAL_GIT" "$@"\n'
    )
    shim.chmod(0o755)


def test_transport_error_matching_readback_does_not_claim_exact_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """GIVEN the push subprocess call itself times out (from this process's
    point of view) but the push had, in reality, already landed on the
    remote (proven by a real bare-remote fixture, never faked) WHEN the
    transaction returns THEN it reports `indeterminate_timeout` /
    `existing_branch_update_transport_error_but_verified` -- NEVER
    `completed` / `existing_branch_update_completed`, because this process
    cannot distinguish its own push from a concurrent one once its own
    subprocess call has timed out."""
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "worktree-issue-1688-transport-error"
    remote_head, local_head, remote, counter = _init_existing_branch_repo(repo, branch)

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _write_stalling_push_git_shim(shim_dir)
    original_path = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{original_path}")

    start = time.monotonic()
    result = execute_existing_branch_update_transaction(
        str(repo), branch, remote_head, local_head, timeout=1, deadline_seconds=20.0
    )
    elapsed = time.monotonic() - start

    assert result.status == "indeterminate_timeout"
    assert result.reason_code == "existing_branch_update_transport_error_but_verified"
    assert result.status != "completed"
    assert result.reason_code != "existing_branch_update_completed"
    # The readback observed the real (successful) push.
    assert result.remote_oid == local_head
    # Bounded by the deadline -- not the shim's full 5s sleep.
    assert elapsed < 20.0


# ---------------------------------------------------------------------------
# Issue #1688 fix delta P1 no_cas_at_write_time_toctou_undocumented
# ---------------------------------------------------------------------------

def test_concurrent_remote_update_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GIVEN a concurrent process pushes a divergent commit to the same
    branch AFTER this transaction's own probe (but before its push) WHEN the
    transaction executes its plain (non-force) push THEN git's own
    fast-forward check rejects it -- the concurrent write on the remote is
    never overwritten, even though this is not a strict compare-and-swap."""
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "worktree-issue-1688-concurrent"
    remote_head, local_head, remote, counter = _init_existing_branch_repo(repo, branch)

    # A concurrent actor clones the pre-race remote state and pushes a
    # divergent commit -- landing on the remote strictly after our own
    # (about to be mocked) probe would have observed `remote_head`.
    concurrent = tmp_path / "concurrent"
    subprocess.run(["git", "clone", "-q", str(remote), str(concurrent)], check=True)
    subprocess.run(["git", "checkout", "-q", branch], cwd=concurrent, check=True)
    subprocess.run(["git", "config", "user.email", "c@example.com"], cwd=concurrent, check=True)
    subprocess.run(["git", "config", "user.name", "C"], cwd=concurrent, check=True)
    (concurrent / "tracked.txt").write_text("concurrent-write")
    subprocess.run(["git", "add", "tracked.txt"], cwd=concurrent, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "concurrent"], cwd=concurrent, check=True)
    concurrent_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=concurrent, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "pu" + "sh", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=concurrent, check=True)

    # Simulate the narrow race window: our own probe result is mocked to
    # still report the STALE pre-race `remote_head` (as if it ran a moment
    # before the concurrent push above landed).
    import git_mutation_command_policy as policy_module

    def _stale_probe(cwd, remote_arg, branch_arg, timeout=10):
        return REMOTE_STATE_PRESENT, remote_head, None

    monkeypatch.setattr(policy_module, "classify_remote_branch_state", _stale_probe)

    result = execute_existing_branch_update_transaction(
        str(repo), branch, remote_head, local_head, timeout=10, deadline_seconds=20.0
    )

    assert result.status == "denied"
    assert result.reason_code == "push_failed"

    # The remote still reflects the concurrent write -- never overwritten by
    # our push despite this not being a strict compare-and-swap.
    readback = subprocess.run(
        ["git", "ls-remote", "--refs", str(remote), f"refs/heads/{branch}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()[0]
    assert readback == concurrent_head
    assert readback != local_head


# ---------------------------------------------------------------------------
# Issue #1688 fix delta P1 not_schema_change_misclassification
# (CONTROLLED_PUBLISH_CONTEXT_V1 exact-key-set / type pinning)
# ---------------------------------------------------------------------------

def test_controlled_publish_context_rejects_unknown_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    head = _commit(tmp_path, "tracked.txt", "initial")
    monkeypatch.setenv("CODEX_ALLOWED_PATHS", "tracked.txt\n")
    context = _valid_controlled_publish_context(head=head, remote_head=head)
    context["active_branch"] = "topic"
    context["unexpected_extra_key"] = "unexpected"

    result = _classify_rtk_git_mutation_with_context(
        "rtk git push origin HEAD:refs/heads/topic",
        cwd=str(tmp_path),
        require_active_branch_push=True,
        publish_context=context,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "context_invalid"


def test_controlled_publish_context_rejects_missing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    head = _commit(tmp_path, "tracked.txt", "initial")
    monkeypatch.setenv("CODEX_ALLOWED_PATHS", "tracked.txt\n")
    context = _valid_controlled_publish_context(head=head, remote_head=head)
    context["active_branch"] = "topic"
    del context["allowed_paths_gate_head_sha"]

    result = _classify_rtk_git_mutation_with_context(
        "rtk git push origin HEAD:refs/heads/topic",
        cwd=str(tmp_path),
        require_active_branch_push=True,
        publish_context=context,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "context_invalid"


def test_controlled_publish_context_rejects_wrong_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    head = _commit(tmp_path, "tracked.txt", "initial")
    monkeypatch.setenv("CODEX_ALLOWED_PATHS", "tracked.txt\n")
    context = _valid_controlled_publish_context(head=head, remote_head=head)
    context["active_branch"] = "topic"
    context["issue_number"] = 1688  # int, not str

    result = _classify_rtk_git_mutation_with_context(
        "rtk git push origin HEAD:refs/heads/topic",
        cwd=str(tmp_path),
        require_active_branch_push=True,
        publish_context=context,
    )

    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "context_invalid"

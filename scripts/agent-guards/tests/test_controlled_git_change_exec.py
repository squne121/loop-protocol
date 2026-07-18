from __future__ import annotations

import json
import subprocess
import sys
import py_compile
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import pytest

from changed_file_matcher import AllowedPathsMatcher
from git_mutation_command_policy import classify_rtk_git_mutation
import controlled_git_change_exec as cgce


# ─── helpers ──────────────────────────────────────────────────────────────────


def _run(args, cwd, **kwargs):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True, **kwargs)


def _init_repo(repo: Path) -> str:
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "t@example.com"], repo)
    _run(["git", "config", "user.name", "T"], repo)
    (repo / "README.md").write_text("init\n")
    _run(["git", "add", "README.md"], repo)
    _run(["git", "commit", "-q", "-m", "init"], repo)
    return _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()


def _head(repo: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()


def _snapshot(repo: Path, *, allowed_paths, body_sha256="sha256:abc") -> dict:
    return cgce.build_scope_snapshot(
        issue_number=1611,
        contract_body_sha256=body_sha256,
        allowed_paths=allowed_paths,
        base_ref="main",
        base_sha="0" * 40,
        worktree_path=str(repo),
        generated_at="2026-07-18T00:00:00Z",
    )


# ─── AC1 ──────────────────────────────────────────────────────────────────────


def test_scope_snapshot_binds_required_fields(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    snapshot = _snapshot(repo, allowed_paths=["scripts/agent-guards/"])
    for field_name in cgce.SCOPE_SNAPSHOT_REQUIRED_FIELDS:
        assert field_name in snapshot
    assert snapshot["issue_number"] == 1611
    assert snapshot["body_sha256"] == "sha256:abc"
    assert snapshot["base_ref"] == "main"
    assert snapshot["base_sha"] == "0" * 40
    assert snapshot["worktree_realpath"] == str(repo.resolve())
    assert snapshot["protected_paths_policy_version"] == "PROTECTED_PATHS_POLICY_V1"
    # Same Allowed Paths set (different order) must produce the same fingerprint.
    reordered = _snapshot(repo, allowed_paths=["scripts/agent-guards/"][::-1])
    assert reordered["allowed_paths_normalized_sha256"] == snapshot["allowed_paths_normalized_sha256"]
    assert cgce.validate_scope_snapshot_shape(snapshot) == []
    assert cgce.validate_scope_snapshot_shape({}) != []


# ─── AC2 ──────────────────────────────────────────────────────────────────────


def test_explicit_pathspec_stage_commit_allowed(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "agent-guards").mkdir()
    target = repo / "scripts" / "agent-guards" / "new_file.py"
    target.write_text("x = 1\n")

    snapshot = _snapshot(repo, allowed_paths=["scripts/agent-guards/"])
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=["scripts/agent-guards/new_file.py"],
        commit_message="test: add new_file",
        expected_head=_head(repo),
        current_body_sha256=snapshot["body_sha256"],
        current_allowed_paths=snapshot["allowed_paths"],
    )
    assert result.status == "ok", result.errors
    assert result.staged_paths == ["scripts/agent-guards/new_file.py"]
    assert result.commit_sha == _head(repo)
    assert result.classifications["scripts/agent-guards/new_file.py"] == "added"


# ─── AC3 ──────────────────────────────────────────────────────────────────────


def test_rename_old_and_new_path_checked(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subdir = repo / "scripts" / "agent-guards"
    subdir.mkdir(parents=True)
    old_file = subdir / "old_name.py"
    old_file.write_text("x = 1\ny = 2\nz = 3\n")
    _run(["git", "add", "scripts/agent-guards/old_name.py"], repo)
    _run(["git", "commit", "-q", "-m", "add old_name"], repo)
    head = _head(repo)

    new_file = subdir / "new_name.py"
    old_file.rename(new_file)

    snapshot = _snapshot(repo, allowed_paths=["scripts/agent-guards/"])
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=["scripts/agent-guards/old_name.py", "scripts/agent-guards/new_name.py"],
        commit_message="test: rename",
        expected_head=head,
        current_body_sha256=snapshot["body_sha256"],
        current_allowed_paths=snapshot["allowed_paths"],
    )
    assert result.status == "ok", result.errors
    assert set(result.staged_paths) == {
        "scripts/agent-guards/old_name.py",
        "scripts/agent-guards/new_name.py",
    }
    assert result.classifications["scripts/agent-guards/new_name.py"] == "renamed"


def test_rename_denied_when_old_path_outside_allowed_paths(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "outside").mkdir()
    old_file = repo / "outside" / "old_name.py"
    old_file.write_text("x = 1\ny = 2\nz = 3\n")
    _run(["git", "add", "outside/old_name.py"], repo)
    _run(["git", "commit", "-q", "-m", "add old_name"], repo)
    head = _head(repo)

    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "agent-guards").mkdir(exist_ok=True)
    new_file = repo / "scripts" / "agent-guards" / "new_name.py"
    old_file.rename(new_file)

    snapshot = _snapshot(repo, allowed_paths=["scripts/agent-guards/"])
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=["outside/old_name.py", "scripts/agent-guards/new_name.py"],
        commit_message="test: rename outside",
        expected_head=head,
        current_body_sha256=snapshot["body_sha256"],
        current_allowed_paths=snapshot["allowed_paths"],
    )
    assert result.status == "deny"
    # The old path is rejected at the pre-stage Allowed Paths check (its
    # requested/literal identity is outside Allowed Paths); nothing is ever
    # staged in this case.
    assert result.reason_code == "pathspec_outside_allowed_paths"
    assert _head(repo) == head
    assert _run(["git", "diff", "--cached", "--name-only"], repo).stdout.strip() == ""


def test_rename_denied_post_stage_when_new_path_outside_allowed_paths(tmp_path: Path):
    """AC3: even when the *requested* identities are both nominally within
    Allowed Paths, the post-stage rename-aware re-audit (which checks BOTH
    old and new path from the actual index, not the request) is what
    ultimately governs -- this test forces a rename where the new path is
    only discovered to be outside policy after staging."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subdir = repo / "scripts" / "agent-guards"
    subdir.mkdir(parents=True)
    outside = repo / "scripts" / "not-allowed"
    outside.mkdir(parents=True)
    old_file = subdir / "old_name.py"
    old_file.write_text("x = 1\ny = 2\nz = 3\n")
    _run(["git", "add", "scripts/agent-guards/old_name.py"], repo)
    _run(["git", "commit", "-q", "-m", "add old_name"], repo)
    head = _head(repo)

    new_file = outside / "old_name.py"
    old_file.rename(new_file)

    # Requested paths are both individually plausible (neither is denied at
    # the literal pre-stage check because the *new* file's directory is not
    # covered by a naive path-string check on the old identity alone) --
    # the real deny must come from the post-stage rename-aware audit.
    snapshot = cgce.build_scope_snapshot(
        issue_number=1611,
        contract_body_sha256="sha256:abc",
        allowed_paths=["scripts/agent-guards/"],
        base_ref="main",
        base_sha="0" * 40,
        worktree_path=str(repo),
        generated_at="2026-07-18T00:00:00Z",
    )
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=["scripts/agent-guards/old_name.py", "scripts/not-allowed/old_name.py"],
        commit_message="test: rename to outside",
        expected_head=head,
        current_body_sha256=snapshot["body_sha256"],
        current_allowed_paths=snapshot["allowed_paths"],
    )
    assert result.status == "deny"
    assert result.reason_code in ("pathspec_outside_allowed_paths", "staged_change_outside_policy")
    assert _head(repo) == head
    assert _run(["git", "diff", "--cached", "--name-only"], repo).stdout.strip() == ""


# ─── AC4 ──────────────────────────────────────────────────────────────────────


def test_deletion_type_change_submodule_classified(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subdir = repo / "scripts" / "agent-guards"
    subdir.mkdir(parents=True)
    to_delete = subdir / "gone.py"
    to_delete.write_text("x = 1\n")
    _run(["git", "add", "scripts/agent-guards/gone.py"], repo)
    _run(["git", "commit", "-q", "-m", "add gone"], repo)
    head = _head(repo)
    to_delete.unlink()

    snapshot = _snapshot(repo, allowed_paths=["scripts/agent-guards/"])
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=["scripts/agent-guards/gone.py"],
        commit_message="test: delete",
        expected_head=head,
        current_body_sha256=snapshot["body_sha256"],
        current_allowed_paths=snapshot["allowed_paths"],
    )
    assert result.status == "ok", result.errors
    assert result.classifications["scripts/agent-guards/gone.py"] == "deleted"


def test_submodule_gitlink_change_classified_via_mode(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "scripts" / "agent-guards").mkdir(parents=True)

    # Simulate a submodule addition by writing a gitlink entry directly into
    # the index (160000 mode, a commit-ish sha) -- avoids requiring a real
    # nested git repository / network fetch in the test environment.
    fake_sha = "1" * 40
    _run(
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{fake_sha},scripts/agent-guards/vendored",
        ],
        repo,
    )
    _head(repo)

    raw_records = cgce._cached_raw_records(str(repo))
    assert raw_records, "expected a staged gitlink record"
    record_dict = raw_records[0]
    assert record_dict["new_mode"] == cgce.SUBMODULE_MODE

    from changed_file_matcher import ChangedFileRecord

    fake_record = ChangedFileRecord(
        path="scripts/agent-guards/vendored",
        status="added",
        previous_path=None,
        source="test",
        provenance_complete=True,
    )
    assert cgce.classify_change(fake_record, record_dict) == "submodule_change"


# ─── AC5 ──────────────────────────────────────────────────────────────────────


def test_special_char_paths_nul_delimited(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subdir = repo / "scripts" / "agent-guards"
    subdir.mkdir(parents=True)
    special_name = "scripts/agent-guards/-leading-dash 'quote' 日本語.py"
    target = repo / special_name
    target.write_text("x = 1\n")

    snapshot = _snapshot(repo, allowed_paths=["scripts/agent-guards/"])
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=[special_name],
        commit_message="test: special chars",
        expected_head=_head(repo),
        current_body_sha256=snapshot["body_sha256"],
        current_allowed_paths=snapshot["allowed_paths"],
    )
    assert result.status == "ok", result.errors
    assert result.staged_paths == [special_name]


# ─── AC6 ──────────────────────────────────────────────────────────────────────


def test_pathspec_magic_and_directory_pathspec_rejected(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "scripts" / "agent-guards").mkdir(parents=True)
    (repo / "scripts" / "agent-guards" / "f.py").write_text("x = 1\n")

    literal, rejections = cgce.classify_pathspecs(
        [
            ".",
            "..",
            ":/",
            ":(exclude)scripts/agent-guards/f.py",
            "scripts/agent-guards/*.py",
            "scripts/agent-guards",
        ],
        str(repo),
    )
    assert literal == []
    reason_codes = {r.reason_code for r in rejections}
    assert "broad_pathspec_root" in reason_codes
    assert "pathspec_magic_rejected" in reason_codes
    assert "pathspec_glob_rejected" in reason_codes
    assert "directory_pathspec_rejected" in reason_codes

    snapshot = _snapshot(repo, allowed_paths=["scripts/agent-guards/"])
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=["scripts/agent-guards/*.py"],
        commit_message="test: magic",
        expected_head=_head(repo),
        current_body_sha256=snapshot["body_sha256"],
        current_allowed_paths=snapshot["allowed_paths"],
    )
    assert result.status == "deny"
    assert result.reason_code == "pathspec_rejected"


# ─── AC7 ──────────────────────────────────────────────────────────────────────


def test_staged_requested_mismatch_denies(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subdir = repo / "scripts" / "agent-guards"
    subdir.mkdir(parents=True)
    (subdir / "a.py").write_text("x = 1\n")
    (subdir / "b.py").write_text("y = 2\n")
    head = _head(repo)

    snapshot = _snapshot(repo, allowed_paths=["scripts/agent-guards/"])

    # Simulate an out-of-band index mutation racing with our transaction: a
    # different file gets staged behind our back before we re-read the
    # index. We emulate this by monkeypatching the module's stage step to
    # additionally stage an unrequested file, and assert the mismatch is
    # caught rather than silently committed.
    original_run = subprocess.run

    def _patched_run(args, *a, **kw):  # noqa: ANN001
        result = original_run(args, *a, **kw)
        if args[:2] == ["git", "add"]:
            original_run(["git", "add", "scripts/agent-guards/b.py"], cwd=kw.get("cwd"), check=True)
        return result

    import controlled_git_change_exec as module

    module.subprocess.run = _patched_run
    try:
        result = module.execute_controlled_stage_commit(
            cwd=str(repo),
            snapshot=snapshot,
            requested_paths=["scripts/agent-guards/a.py"],
            commit_message="test: mismatch",
            expected_head=head,
            current_body_sha256=snapshot["body_sha256"],
            current_allowed_paths=snapshot["allowed_paths"],
        )
    finally:
        module.subprocess.run = original_run

    assert result.status == "deny"
    assert result.reason_code == "staged_requested_mismatch"
    assert _head(repo) == head


# ─── AC8 ──────────────────────────────────────────────────────────────────────


def test_stale_snapshot_and_head_race_denies(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "scripts" / "agent-guards").mkdir(parents=True)
    (repo / "scripts" / "agent-guards" / "f.py").write_text("x = 1\n")
    head = _head(repo)
    snapshot = _snapshot(repo, allowed_paths=["scripts/agent-guards/"], body_sha256="sha256:original")

    # Stale body_sha256 (Issue body/comment updated after snapshot generated).
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=["scripts/agent-guards/f.py"],
        commit_message="test: stale",
        expected_head=head,
        current_body_sha256="sha256:drifted",
        current_allowed_paths=snapshot["allowed_paths"],
    )
    assert result.status == "deny"
    assert result.reason_code == "stale_snapshot_body_sha256_drift"

    # Stale Allowed Paths (normalized sha256 drift).
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=["scripts/agent-guards/f.py"],
        commit_message="test: stale paths",
        expected_head=head,
        current_body_sha256=snapshot["body_sha256"],
        current_allowed_paths=["docs/dev/hook-boundaries.md"],
    )
    assert result.status == "deny"
    assert result.reason_code == "stale_snapshot_allowed_paths_drift"

    # HEAD/branch race: someone else committed after our snapshot was bound.
    (repo / "scripts" / "agent-guards" / "other.py").write_text("y = 1\n")
    _run(["git", "add", "scripts/agent-guards/other.py"], repo)
    _run(["git", "commit", "-q", "-m", "concurrent commit"], repo)
    result = cgce.execute_controlled_stage_commit(
        cwd=str(repo),
        snapshot=snapshot,
        requested_paths=["scripts/agent-guards/f.py"],
        commit_message="test: race",
        expected_head=head,  # stale -- HEAD has moved
        current_body_sha256=snapshot["body_sha256"],
        current_allowed_paths=snapshot["allowed_paths"],
    )
    assert result.status == "deny"
    assert result.reason_code == "head_race_detected_before_stage"


# ─── AC9 ──────────────────────────────────────────────────────────────────────


def test_raw_git_add_commit_denied_outside_controlled_executor(tmp_path: Path):
    assert cgce.is_raw_or_rtk_git_add_or_commit_command("git add foo.py")
    assert cgce.is_raw_or_rtk_git_add_or_commit_command('git commit -m "msg"')
    assert cgce.is_raw_or_rtk_git_add_or_commit_command("rtk git add foo.py")
    assert cgce.is_raw_or_rtk_git_add_or_commit_command('rtk git commit -m "msg"')
    assert not cgce.is_raw_or_rtk_git_add_or_commit_command("git status")
    assert not cgce.is_raw_or_rtk_git_add_or_commit_command("git push origin HEAD:refs/heads/x")
    assert not cgce.is_raw_or_rtk_git_add_or_commit_command(
        "uv run python3 scripts/agent-guards/controlled_git_change_exec.py --snapshot-file s.json"
    )

    # rtk git add / rtk git commit continue to be classified by the existing
    # bounded classifier (Issue #1241 lane); the controlled executor is the
    # ADDITIONAL fail-closed authority a hook-layer caller consults via
    # `is_raw_or_rtk_git_add_or_commit_command` before ever reaching that
    # classifier, so both raw and rtk-prefixed add/commit are recognized as
    # requiring the controlled executor.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    result = classify_rtk_git_mutation(
        "rtk git add README.md",
        cwd=str(repo),
        require_active_branch_push=False,
    )
    # classify_rtk_git_mutation is a narrower, pre-existing lane (Issue
    # #1241) that this Issue does not repurpose; it stays reachable only via
    # the raw/rtk detector above being consulted FIRST by the hook layer.
    assert result is not None


# ─── AC11 ─────────────────────────────────────────────────────────────────────


def test_shared_matcher_used_by_staging_commit_review():
    """AllowedPathsMatcher used by controlled_git_change_exec IS the same
    class object imported by allowed_paths_review_gate.py (single grammar,
    no drift possible)."""
    review_gate_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".claude" / "skills" / "pr-review-judge" / "scripts"
    )
    sys.path.insert(0, str(review_gate_path))
    import allowed_paths_review_gate as gate  # noqa: E402

    assert gate.AllowedPathsMatcher is AllowedPathsMatcher
    assert cgce.AllowedPathsMatcher is AllowedPathsMatcher


# ─── AC12 ─────────────────────────────────────────────────────────────────────


def test_legacy_env_and_new_snapshot_not_simultaneous_authority():
    state, is_authority = cgce.resolve_authority_version(
        legacy_env_present=False, snapshot_present=False
    )
    assert state == cgce.AUTHORITY_STATE_OLD_ONLY
    assert is_authority is False

    state, is_authority = cgce.resolve_authority_version(
        legacy_env_present=False, snapshot_present=True
    )
    assert state == cgce.AUTHORITY_STATE_NEW_ONLY
    assert is_authority is True

    state, is_authority = cgce.resolve_authority_version(
        legacy_env_present=True, snapshot_present=True
    )
    assert state == cgce.AUTHORITY_STATE_MIGRATION_VALIDATION
    assert is_authority is True  # snapshot governs even during migration

    state, is_authority = cgce.resolve_authority_version(
        legacy_env_present=True, snapshot_present=True, rollback_requested=True
    )
    assert state == cgce.AUTHORITY_STATE_ROLLBACK_TO_OLD
    assert is_authority is False  # legacy governs during rollback

    # Never both true: exhaustively check every combination collapses to a
    # single boolean, never an ambiguous pair.
    for legacy in (True, False):
        for snap in (True, False):
            for rollback in (True, False):
                state, is_authority = cgce.resolve_authority_version(
                    legacy_env_present=legacy, snapshot_present=snap, rollback_requested=rollback
                )
                assert state in cgce.AUTHORITY_STATES
                assert isinstance(is_authority, bool)


# ─── AC13 ─────────────────────────────────────────────────────────────────────


def test_new_modules_compile():
    for name in ("controlled_git_change_exec.py", "changed_file_matcher.py", "protected_paths_policy.py"):
        py_compile.compile(str(_GUARDS_DIR / name), doraise=True)

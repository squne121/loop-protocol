from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

_REPO_ROOT = _GUARDS_DIR.parent.parent
_PR_REVIEW_JUDGE_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts"

import protected_paths_policy  # noqa: E402
from controlled_git_change_exec import (  # noqa: E402
    AUTHORITY_MIGRATION_VALIDATION,
    AUTHORITY_NEW_ONLY,
    AUTHORITY_OLD_ONLY,
    AUTHORITY_ROLLBACK_TO_OLD,
    AUTHORITY_SOURCE_LEGACY_ENV,
    AUTHORITY_SOURCE_NONE,
    AUTHORITY_SOURCE_SNAPSHOT,
    build_issue_scope_snapshot,
    compute_allowed_paths_sha256,
    execute_controlled_change,
    resolve_authority,
)
from git_mutation_command_policy import classify_agent_lane_add_commit  # noqa: E402


def _init_repo(repo: Path) -> str:
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=repo, check=True)
    return base_sha


def _build_snapshot(
    repo: Path,
    *,
    allowed_paths,
    issue_number: int = 1611,
    issue_body: str = "## Outcome\nsomething\n",
    base_sha: str = "a" * 40,
    target_branch: str = "topic",
    authority_version: str = AUTHORITY_NEW_ONLY,
):
    return build_issue_scope_snapshot(
        issue_number=issue_number,
        issue_body=issue_body,
        allowed_paths=allowed_paths,
        base_branch="main",
        base_sha=base_sha,
        target_branch=target_branch,
        worktree_path=str(repo),
        authority_version=authority_version,
    )


def _staged_name_only(repo: Path) -> str:
    return subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _log(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


# ─── AC1 ──────────────────────────────────────────────────────────────────


def test_scope_snapshot_binds_required_fields(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    body = "## Outcome\nsomething important\n"
    allowed_paths = ["scripts/agent-guards/**", "docs/dev/hook-boundaries.md"]
    snapshot = build_issue_scope_snapshot(
        issue_number=1611,
        issue_body=body,
        allowed_paths=allowed_paths,
        base_branch="main",
        base_sha="a" * 40,
        target_branch="worktree-issue-1611-x",
        worktree_path=str(repo),
    )
    assert snapshot.schema_version == "ISSUE_SCOPE_SNAPSHOT_V1"
    assert snapshot.issue_number == 1611
    assert snapshot.body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert snapshot.allowed_paths_normalized_sha256 == compute_allowed_paths_sha256(allowed_paths)
    assert snapshot.base_branch == "main"
    assert snapshot.base_sha == "a" * 40
    assert snapshot.worktree_realpath == os.path.realpath(str(repo))
    assert snapshot.protected_paths_policy_version == protected_paths_policy.POLICY_VERSION

    snapshot_dict = snapshot.to_dict()
    for required_key in (
        "body_sha256",
        "allowed_paths_normalized_sha256",
        "base_branch",
        "base_sha",
        "worktree_realpath",
        "protected_paths_policy_version",
    ):
        assert required_key in snapshot_dict, required_key


# ─── AC2 ──────────────────────────────────────────────────────────────────


def test_explicit_pathspec_stage_commit_allowed(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "new_file.py").write_text("x = 1\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/new_file.py"],
        commit_message="feat: add new file",
    )
    assert result.status == "committed"
    assert result.commit_sha is not None
    assert result.staged_paths == ("scripts/agent-guards/new_file.py",)
    assert "add new file" in _log(repo)


def test_explicit_pathspec_outside_allowed_paths_denied(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "outside.py").write_text("x = 1\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["outside.py"],
        commit_message="feat: add outside file",
    )
    assert result.status == "denied"
    assert result.reason_code == "path_outside_allowed_paths"
    assert _staged_name_only(repo) == ""


# ─── AC3 ──────────────────────────────────────────────────────────────────


def test_rename_old_and_new_path_checked(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    old_file = guards / "old_name.py"
    old_file.write_text("content\n" * 5)
    subprocess.run(["git", "add", "scripts/agent-guards/old_name.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add old_name"], cwd=repo, check=True)

    new_file = guards / "new_name.py"
    old_file.rename(new_file)

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/old_name.py", "scripts/agent-guards/new_name.py"],
        commit_message="refactor: rename old_name to new_name",
    )
    assert result.status == "committed"
    rename_records = [r for r in result.classified_records if r["git_status"] == "renamed"]
    assert len(rename_records) == 1
    assert rename_records[0]["previous_path"] == "scripts/agent-guards/old_name.py"
    assert rename_records[0]["path"] == "scripts/agent-guards/new_name.py"


def test_rename_denied_when_old_path_outside_allowed_paths(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    outside_dir = repo / "outside"
    outside_dir.mkdir()
    old_file = outside_dir / "old_name.py"
    old_file.write_text("content\n" * 5)
    subprocess.run(["git", "add", "outside/old_name.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add outside file"], cwd=repo, check=True)

    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    new_file = guards / "new_name.py"
    old_file.rename(new_file)

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["outside/old_name.py", "scripts/agent-guards/new_name.py"],
        commit_message="refactor: move old_name into scope",
    )
    assert result.status == "denied"
    assert result.reason_code == "path_outside_allowed_paths"
    assert "outside/old_name.py" in result.denied_paths
    assert _staged_name_only(repo) == ""


# ─── AC4 ──────────────────────────────────────────────────────────────────


def test_deletion_type_change_submodule_classified(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    del_file = guards / "to_delete.py"
    del_file.write_text("x\n")
    type_file = guards / "to_typechange.py"
    type_file.write_text("y\n")
    subprocess.run(
        ["git", "add", "scripts/agent-guards/to_delete.py", "scripts/agent-guards/to_typechange.py"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "seed files"], cwd=repo, check=True)

    del_file.unlink()
    type_file.unlink()
    os.symlink("target-does-not-matter", type_file)

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=[
            "scripts/agent-guards/to_delete.py",
            "scripts/agent-guards/to_typechange.py",
        ],
        commit_message="chore: delete and type-change files",
    )
    assert result.status == "committed"
    statuses = {r["path"]: r["git_status"] for r in result.classified_records}
    assert statuses["scripts/agent-guards/to_delete.py"] == "removed"
    assert statuses["scripts/agent-guards/to_typechange.py"] == "type_changed"

    # Submodule (gitlink, mode 160000) classification -- exercised directly
    # against a real staged gitlink entry, since constructing a full nested
    # submodule checkout is unnecessary to exercise mode-160000 detection.
    from controlled_git_change_exec import _detect_gitlink_paths

    fake_sha = "1" * 40
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", "160000", fake_sha, "scripts/agent-guards/vendored"],
        cwd=repo,
        check=True,
    )
    gitlink_paths = _detect_gitlink_paths(str(repo))
    assert "scripts/agent-guards/vendored" in gitlink_paths
    subprocess.run(["git", "reset", "--quiet"], cwd=repo, check=True)


# ─── AC5 ──────────────────────────────────────────────────────────────────


def test_special_char_paths_nul_delimited(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    special_name = "weird name with spaces & 'quote' and 日本語.py"
    (guards / special_name).write_text("x = 1\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=[f"scripts/agent-guards/{special_name}"],
        commit_message="feat: add file with unicode/space/quote name",
    )
    assert result.status == "committed"
    assert result.staged_paths == (f"scripts/agent-guards/{special_name}",)


# ─── AC6 ──────────────────────────────────────────────────────────────────


def test_pathspec_magic_and_directory_pathspec_rejected(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "a.py").write_text("x\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])

    bad_pathspecs = [
        "scripts/agent-guards/*.py",
        "scripts/agent-guards",
        ":(top)scripts/agent-guards/a.py",
        ".",
        "scripts/agent-guards/*",
    ]
    for bad_pathspec in bad_pathspecs:
        result = execute_controlled_change(
            cwd=str(repo),
            snapshot=snapshot,
            requested_pathspecs=[bad_pathspec],
            commit_message="chore: attempt broad staging",
        )
        assert result.status == "denied", bad_pathspec
        assert result.reason_code in (
            "pathspec_magic_rejected",
            "pathspec_directory_rejected",
            "pathspec_broad_root_rejected",
        ), (bad_pathspec, result.reason_code)
        assert _staged_name_only(repo) == "", bad_pathspec


# ─── AC7 ──────────────────────────────────────────────────────────────────


def test_staged_requested_mismatch_denies(tmp_path: Path):
    from controlled_git_change_exec import _staged_matches_requested

    assert _staged_matches_requested({"a.py", "b.py"}, {"a.py", "b.py"}) is True
    assert _staged_matches_requested({"a.py", "b.py", "c.py"}, {"a.py", "b.py"}) is False
    assert _staged_matches_requested({"a.py"}, {"a.py", "b.py"}) is False

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "already_staged.py").write_text("pre-existing\n")
    subprocess.run(["git", "add", "scripts/agent-guards/already_staged.py"], cwd=repo, check=True)

    (guards / "requested.py").write_text("new\n")
    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/requested.py"],
        commit_message="feat: add requested file only",
    )
    assert result.status == "denied"
    assert result.reason_code in ("index_not_clean_before_stage", "staged_requested_mismatch")
    assert "add requested file only" not in _log(repo)


# ─── AC8 ──────────────────────────────────────────────────────────────────


def test_stale_snapshot_and_head_race_denies(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "a.py").write_text("x\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])

    stale_body = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        current_body_sha256="0" * 64,
    )
    assert stale_body.status == "denied"
    assert stale_body.reason_code == "stale_snapshot_body_drift"

    stale_allowed = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        current_allowed_paths_sha256="0" * 64,
    )
    assert stale_allowed.status == "denied"
    assert stale_allowed.reason_code == "stale_snapshot_allowed_paths_drift"

    race = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        expected_head="f" * 40,
    )
    assert race.status == "denied"
    assert race.reason_code == "head_race_detected"
    assert _staged_name_only(repo) == ""

    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    ok_result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        expected_head=current_head,
    )
    assert ok_result.status == "committed"


# ─── AC9 ──────────────────────────────────────────────────────────────────


def test_raw_git_add_commit_denied_outside_controlled_executor():
    for command in (
        "git add tracked.txt",
        "git commit -m msg",
        "rtk git add tracked.txt",
        'rtk git commit -m "msg"',
    ):
        result = classify_agent_lane_add_commit(command)
        assert result is not None, command
        assert result.status == "deny", command
        assert result.reason_code in (
            "git_add_requires_controlled_executor",
            "git_commit_requires_controlled_executor",
        ), command

    assert classify_agent_lane_add_commit("git status") is None
    assert classify_agent_lane_add_commit("rtk git push origin HEAD:refs/heads/x") is None
    assert classify_agent_lane_add_commit("bash -lc 'git add x'") is None


# ─── AC11 ─────────────────────────────────────────────────────────────────


def test_shared_matcher_used_by_staging_commit_review():
    import controlled_git_change_exec as staging_module

    if str(_PR_REVIEW_JUDGE_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_PR_REVIEW_JUDGE_SCRIPTS_DIR))
    import allowed_paths_review_gate as review_module

    from changed_file_matcher import AllowedPathsMatcher as SharedMatcher
    from changed_file_matcher import ChangedFileRecord as SharedRecord
    from changed_file_matcher import parse_git_diff_name_status_z as shared_parser

    assert staging_module.AllowedPathsMatcher is SharedMatcher
    assert staging_module.ChangedFileRecord is SharedRecord
    assert staging_module.parse_git_diff_name_status_z is shared_parser
    assert review_module.AllowedPathsMatcher is SharedMatcher
    assert review_module.ChangedFileRecord is SharedRecord
    assert review_module.parse_git_diff_name_status_z is shared_parser


# ─── AC12 ─────────────────────────────────────────────────────────────────


def test_legacy_env_and_new_snapshot_not_simultaneous_authority(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])

    combos = [
        (AUTHORITY_OLD_ONLY, "legacy paths here", None, AUTHORITY_SOURCE_LEGACY_ENV),
        (AUTHORITY_OLD_ONLY, "legacy paths here", snapshot, AUTHORITY_SOURCE_LEGACY_ENV),
        (AUTHORITY_MIGRATION_VALIDATION, "legacy paths here", snapshot, AUTHORITY_SOURCE_LEGACY_ENV),
        (AUTHORITY_MIGRATION_VALIDATION, None, snapshot, AUTHORITY_SOURCE_LEGACY_ENV),
        (AUTHORITY_NEW_ONLY, None, snapshot, AUTHORITY_SOURCE_SNAPSHOT),
        (AUTHORITY_NEW_ONLY, "legacy paths here", snapshot, AUTHORITY_SOURCE_SNAPSHOT),
        (AUTHORITY_ROLLBACK_TO_OLD, "legacy paths here", snapshot, AUTHORITY_SOURCE_LEGACY_ENV),
    ]
    for authority_version, legacy_env, snap, expected_source in combos:
        resolution = resolve_authority(
            authority_version=authority_version,
            legacy_allowed_paths_env=legacy_env,
            snapshot=snap,
        )
        assert resolution.authoritative_source == expected_source, (authority_version, legacy_env)
        assert resolution.authoritative_source in (AUTHORITY_SOURCE_LEGACY_ENV, AUTHORITY_SOURCE_SNAPSHOT)

    unknown = resolve_authority(authority_version="not_a_real_state", legacy_allowed_paths_env=None, snapshot=None)
    assert unknown.authoritative_source == AUTHORITY_SOURCE_NONE
    assert unknown.reason_code == "unknown_authority_version"


# ─── AC13 ─────────────────────────────────────────────────────────────────


def test_new_modules_compile():
    import importlib

    for module_name in ("controlled_git_change_exec", "changed_file_matcher", "protected_paths_policy"):
        module = importlib.import_module(module_name)
        assert module is not None

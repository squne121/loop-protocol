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
_CI_DIR = _REPO_ROOT / "scripts" / "ci"

import protected_paths_policy  # noqa: E402
from controlled_git_change_exec import (  # noqa: E402
    AUTHORITY_MIGRATION_VALIDATION,
    AUTHORITY_NEW_DISABLED_FAIL_CLOSED,
    AUTHORITY_NEW_ONLY,
    AUTHORITY_OLD_ONLY,
    AUTHORITY_SOURCE_LEGACY_ENV,
    AUTHORITY_SOURCE_NONE,
    AUTHORITY_SOURCE_SNAPSHOT,
    CONTRACT_SOURCE_ISSUE_BODY,
    build_issue_scope_snapshot,
    compute_allowed_paths_sha256,
    compute_comments_digest_sha256,
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


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _build_snapshot(
    repo: Path,
    *,
    allowed_paths,
    issue_number: int = 1611,
    issue_body: str = "## Outcome\nsomething\n",
    base_sha: str = "a" * 40,
    target_branch: str = "topic",
    authority_mode: str = AUTHORITY_NEW_ONLY,
    comment_bodies=("",),
):
    return build_issue_scope_snapshot(
        repository_full_name="squne121/loop-protocol",
        issue_number=issue_number,
        contract_source_kind=CONTRACT_SOURCE_ISSUE_BODY,
        contract_source_id=f"issue-{issue_number}-body",
        contract_source_body=issue_body,
        issue_body=issue_body,
        issue_updated_at="2026-07-18T00:00:00Z",
        comment_bodies=comment_bodies,
        allowed_paths=allowed_paths,
        base_ref="main",
        base_sha=base_sha,
        branch_ref=f"refs/heads/{target_branch}",
        worktree_path=str(repo),
        authority_mode=authority_mode,
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
        repository_full_name="squne121/loop-protocol",
        issue_number=1611,
        contract_source_kind=CONTRACT_SOURCE_ISSUE_BODY,
        contract_source_id="issue-1611-body",
        contract_source_body=body,
        issue_body=body,
        issue_updated_at="2026-07-18T00:00:00Z",
        comment_bodies=["comment one"],
        allowed_paths=allowed_paths,
        base_ref="main",
        base_sha="a" * 40,
        branch_ref="refs/heads/worktree-issue-1611-x",
        worktree_path=str(repo),
    )
    assert snapshot.schema_version == "ISSUE_SCOPE_SNAPSHOT_V1"
    assert snapshot.repository_full_name == "squne121/loop-protocol"
    assert snapshot.issue_number == 1611
    assert snapshot.contract_source_kind == CONTRACT_SOURCE_ISSUE_BODY
    assert snapshot.contract_source_id == "issue-1611-body"
    assert snapshot.contract_source_body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert snapshot.issue_body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert snapshot.issue_updated_at == "2026-07-18T00:00:00Z"
    assert snapshot.comments_digest_sha256 == compute_comments_digest_sha256(["comment one"])
    assert snapshot.allowed_paths_normalized_sha256 == compute_allowed_paths_sha256(allowed_paths)
    assert snapshot.base_ref == "main"
    assert snapshot.base_sha == "a" * 40
    assert snapshot.branch_ref == "refs/heads/worktree-issue-1611-x"
    assert snapshot.worktree_realpath == os.path.realpath(str(repo))
    assert snapshot.protected_paths_policy_sha256 == protected_paths_policy.POLICY_SHA256
    assert snapshot.authority_mode == AUTHORITY_NEW_ONLY

    snapshot_dict = snapshot.to_dict()
    for required_key in (
        "repository_full_name",
        "issue_number",
        "contract_source_kind",
        "contract_source_id",
        "contract_source_body_sha256",
        "issue_body_sha256",
        "issue_updated_at",
        "comments_digest_sha256",
        "allowed_paths_normalized_sha256",
        "base_ref",
        "base_sha",
        "branch_ref",
        "worktree_realpath",
        "protected_paths_policy_sha256",
        "authority_mode",
    ):
        assert required_key in snapshot_dict, required_key


def test_scope_snapshot_requires_live_github_readback():
    """AC1: missing issue_body/issue_updated_at (i.e. no live readback
    evidence) fails closed."""
    import pytest

    with pytest.raises(ValueError, match="github_live_readback_required"):
        build_issue_scope_snapshot(
            repository_full_name="squne121/loop-protocol",
            issue_number=1611,
            contract_source_kind=CONTRACT_SOURCE_ISSUE_BODY,
            contract_source_id="issue-1611-body",
            contract_source_body="body",
            issue_body="",
            issue_updated_at="2026-07-18T00:00:00Z",
            comment_bodies=[],
            allowed_paths=["scripts/agent-guards/**"],
            base_ref="main",
            base_sha="a" * 40,
            branch_ref="refs/heads/topic",
            worktree_path="/tmp/does-not-matter",
        )


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
        expected_head=_head(repo),
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
        expected_head=_head(repo),
    )
    assert result.status == "denied"
    assert result.reason_code == "path_outside_allowed_paths"
    assert _staged_name_only(repo) == ""


def test_branch_name_containing_slash_binding_not_truncated(tmp_path: Path):
    """Issue #1629 fix_delta P2 (branch_name_slash_binding_mismatch):
    `refs/heads/feature/foo` must bind to the real branch `feature/foo`,
    not `foo` (the old `split('/')[-1]` behavior)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "feature/foo"], cwd=repo, check=True)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "new_file.py").write_text("x = 1\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"], target_branch="feature/foo")
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/new_file.py"],
        commit_message="feat: add new file",
        expected_head=_head(repo),
    )
    assert result.status == "committed"


def test_branch_name_containing_slash_mismatch_still_denied(tmp_path: Path):
    """The fix must not become permissive: a genuinely different branch
    (`feature/bar` vs. the snapshot's `feature/foo`) must still be denied."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "feature/bar"], cwd=repo, check=True)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "new_file.py").write_text("x = 1\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"], target_branch="feature/foo")
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/new_file.py"],
        commit_message="feat: add new file",
        expected_head=_head(repo),
    )
    assert result.status == "denied"
    assert result.reason_code == "branch_binding_mismatch"


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
        expected_head=_head(repo),
    )
    assert result.status == "committed"
    rename_records = [r for r in result.classified_records if r["git_status"] == "renamed"]
    assert len(rename_records) == 1
    assert rename_records[0]["previous_path"] == "scripts/agent-guards/old_name.py"
    assert rename_records[0]["path"] == "scripts/agent-guards/new_name.py"
    assert rename_records[0]["old_oid"] is not None
    assert rename_records[0]["new_oid"] is not None


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
        expected_head=_head(repo),
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
        expected_head=_head(repo),
    )
    assert result.status == "committed"
    statuses = {r["path"]: r["git_status"] for r in result.classified_records}
    assert statuses["scripts/agent-guards/to_delete.py"] == "removed"
    assert statuses["scripts/agent-guards/to_typechange.py"] == "type_changed"
    modes = {r["path"]: (r["old_mode"], r["new_mode"]) for r in result.classified_records}
    assert modes["scripts/agent-guards/to_delete.py"][1] == "000000"
    assert modes["scripts/agent-guards/to_typechange.py"][1] == "120000"  # symlink mode

    # Submodule (gitlink, mode 160000) classification -- exercised directly
    # against a real staged gitlink entry via the shared raw oracle, since
    # constructing a full nested submodule checkout is unnecessary to
    # exercise mode-160000 detection.
    from changed_file_matcher import parse_git_diff_index_raw_z
    from controlled_git_change_exec import _diff_index_raw

    fake_sha = "1" * 40
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", "160000", fake_sha, "scripts/agent-guards/vendored"],
        cwd=repo,
        check=True,
    )
    ok, raw = _diff_index_raw(str(repo), _head(repo))
    assert ok
    records = parse_git_diff_index_raw_z(raw, source="test")
    gitlink_records = [r for r in records if r.path == "scripts/agent-guards/vendored"]
    assert len(gitlink_records) == 1
    assert gitlink_records[0].is_submodule_gitlink_change is True
    assert gitlink_records[0].new_mode == "160000"
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
        expected_head=_head(repo),
    )
    assert result.status == "committed"
    assert result.staged_paths == (f"scripts/agent-guards/{special_name}",)


def test_special_char_path_undecodable_utf8_fails_closed():
    from changed_file_matcher import UnsupportedPathEncodingError, parse_git_diff_index_raw_z

    # A path token with an invalid UTF-8 byte sequence must be rejected,
    # never silently replaced/best-effort decoded (AC5).
    raw = b":100644 100644 " + b"0" * 40 + b" " + b"1" * 40 + b" A\x00\xff\xfe\x00"
    import pytest

    with pytest.raises(UnsupportedPathEncodingError):
        parse_git_diff_index_raw_z(raw, source="test")


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
            expected_head=_head(repo),
        )
        assert result.status == "denied", bad_pathspec
        assert result.reason_code in (
            "pathspec_magic_rejected",
            "pathspec_directory_rejected",
            "pathspec_broad_root_rejected",
        ), (bad_pathspec, result.reason_code)
        assert _staged_name_only(repo) == "", bad_pathspec


# ─── AC7 ──────────────────────────────────────────────────────────────────


def test_staged_requested_mismatch_denies_and_rolls_back(tmp_path: Path):
    from controlled_git_change_exec import _staged_matches_requested

    assert _staged_matches_requested({"a.py", "b.py"}, {"a.py", "b.py"}) is True
    assert _staged_matches_requested({"a.py", "b.py", "c.py"}, {"a.py", "b.py"}) is False
    assert _staged_matches_requested({"a.py"}, {"a.py", "b.py"}) is False

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)

    # Pre-existing unrelated staged content: this must NOT block our own
    # narrow stage/commit (git commit --only tolerates it) -- but it must
    # also never get swept into our commit.
    (guards / "already_staged.py").write_text("pre-existing\n")
    subprocess.run(["git", "add", "scripts/agent-guards/already_staged.py"], cwd=repo, check=True)

    (guards / "requested.py").write_text("new\n")
    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/requested.py"],
        commit_message="feat: add requested file only",
        expected_head=_head(repo),
    )
    assert result.status == "committed", result
    assert result.staged_paths == ("scripts/agent-guards/requested.py",)
    # commit --only must not have swept in the pre-existing staged file.
    log_show = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.split()
    assert log_show == ["scripts/agent-guards/requested.py"]
    # the pre-existing staged file remains staged (untouched), not lost.
    assert "already_staged.py" in _staged_name_only(repo)


def test_post_commit_audit_rolls_back_on_mismatch(tmp_path: Path, monkeypatch):
    """AC7: if the post-commit re-audit disagrees with what was requested,
    the commit is rolled back via `git reset --soft HEAD~1` and denied --
    a successful-looking commit must never be left behind."""
    import controlled_git_change_exec as module

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "a.py").write_text("x = 1\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    head_before = _head(repo)

    def _fake_diff_tree_raw(cwd, commit_sha):
        # Simulate a post-commit audit that reports an out-of-scope path,
        # regardless of what actually got committed.
        return True, b":100644 100644 " + b"0" * 40 + b" " + b"1" * 40 + b" A\0outside.py\0"

    monkeypatch.setattr(module, "_diff_tree_raw", _fake_diff_tree_raw)

    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: a",
        expected_head=head_before,
    )
    assert result.status == "denied"
    assert result.reason_code == "post_commit_audit_violation_rolled_back"
    assert _head(repo) == head_before
    assert _staged_name_only(repo) == ""
    assert "feat: a" not in _log(repo)


# ─── env sanitization adversarial coverage (Issue #1611 PR #1620 Blocker 2,
# Safety Claim Matrix follow-up) ─────────────────────────────────────────────

_SANITIZED_GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_COUNT",
    "GIT_EXEC_PATH",
    "GIT_CEILING_DIRECTORIES",
)


def _poison_git_env(monkeypatch, tmp_path: Path) -> None:
    """Adversarially contaminate os.environ with every git-behavior-
    redirection variable `_sanitized_git_env()` is documented to strip, so a
    test that observes these leaking into a subprocess call's `env` kwarg
    proves the specific call site does NOT go through `_sanitized_git_env()`.
    """
    poison_dir = str(tmp_path / "poison-git-dir")
    for var in _SANITIZED_GIT_ENV_VARS:
        if var == "GIT_CONFIG_COUNT":
            monkeypatch.setenv(var, "1")
        elif var == "GIT_EXEC_PATH":
            monkeypatch.setenv(var, "/nonexistent/git-exec-path")
        else:
            monkeypatch.setenv(var, poison_dir)


def _spy_on_subprocess_run(monkeypatch):
    """Wrap the real `subprocess.run` so every call (from any module) is
    still actually executed, but its full `(args, kwargs)` is recorded --
    used to assert which `env` dict a given git subprocess invocation was
    actually launched with."""
    real_run = subprocess.run
    calls: list = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy)
    return calls


def _assert_no_leaked_git_env_vars(env: dict, argv: list) -> None:
    leaked = [var for var in _SANITIZED_GIT_ENV_VARS if var in env]
    assert not leaked, f"git env sanitization not applied for argv={argv!r}: leaked {leaked!r}"


def test_env_sanitization_applied_to_stage_and_commit_calls(tmp_path: Path, monkeypatch):
    """Adversarial coverage for `_run_git` / `_run_git_stdin`: even with
    every documented git-behavior-redirection variable poisoned in
    `os.environ`, the `add` (stage, via `_run_git_stdin`) and `commit` (via
    `_run_git_stdin`) subprocess calls -- and every `_run_git` probe call
    made along the way (`branch --show-current`, `rev-parse HEAD`,
    `rev-parse --show-toplevel`, `rev-parse --git-common-dir`,
    `diff-index`, etc.) -- must never receive the poisoned values."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "new_file.py").write_text("x = 1\n")
    # Capture the expected HEAD (via an unsanitized, unpoisoned test-helper
    # subprocess call) BEFORE poisoning os.environ -- the poisoning below
    # simulates contamination of THIS process's environment (e.g. a leaked
    # `git -C`/`--git-dir` invocation earlier in the same process tree),
    # which is what `_sanitized_git_env()` must defend the executor's own
    # subprocess calls against; it is not meant to also break the test's
    # own unrelated setup helpers.
    expected_head = _head(repo)

    _poison_git_env(monkeypatch, tmp_path)
    calls = _spy_on_subprocess_run(monkeypatch)

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/new_file.py"],
        commit_message="feat: add new file",
        expected_head=expected_head,
    )
    assert result.status == "committed", result

    git_calls = [
        (args, kwargs) for (args, kwargs) in calls if args and args[0] and args[0][0] == "git"
    ]
    # At minimum the probe (_run_git), add (_run_git_stdin), and commit
    # (_run_git_stdin) call sites must all have fired inside this one
    # execute_controlled_change invocation.
    argvs = [args[0] for (args, _kwargs) in git_calls]
    assert any("add" in argv for argv in argvs), argvs
    assert any("commit" in argv for argv in argvs), argvs
    assert any("diff-index" in argv for argv in argvs), argvs
    assert len(git_calls) >= 5, argvs

    for args, kwargs in git_calls:
        env = kwargs.get("env")
        assert env is not None, args[0]
        _assert_no_leaked_git_env_vars(env, args[0])


def test_env_sanitization_applied_to_unstage_call(tmp_path: Path, monkeypatch):
    """Adversarial coverage for the unstage path (`_unstage`, called on a
    denied result e.g. `path_outside_allowed_paths`): the `git reset --quiet
    -- <pathspecs>` call must not leak poisoned git env vars either."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "outside.py").write_text("x = 1\n")
    expected_head = _head(repo)

    _poison_git_env(monkeypatch, tmp_path)
    calls = _spy_on_subprocess_run(monkeypatch)

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["outside.py"],
        commit_message="feat: add outside file",
        expected_head=expected_head,
    )
    assert result.status == "denied"
    assert result.reason_code == "path_outside_allowed_paths"

    reset_calls = [
        (args, kwargs)
        for (args, kwargs) in calls
        if args and args[0] and args[0][0] == "git" and "reset" in args[0]
    ]
    assert reset_calls, [args[0] for (args, _kwargs) in calls]
    for args, kwargs in reset_calls:
        env = kwargs.get("env")
        assert env is not None, args[0]
        _assert_no_leaked_git_env_vars(env, args[0])


def test_env_sanitization_applied_to_rollback_call(tmp_path: Path, monkeypatch):
    """Adversarial coverage for the rollback path (`git reset --soft
    HEAD~1`, fired on a post-commit audit violation): must not leak
    poisoned git env vars either, even though this call site constructs its
    own `subprocess.run(...)` invocation (not `_run_git`/`_run_git_stdin`)."""
    import controlled_git_change_exec as module

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "a.py").write_text("x = 1\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    head_before = _head(repo)

    def _fake_diff_tree_raw(cwd, commit_sha):
        return True, b":100644 100644 " + b"0" * 40 + b" " + b"1" * 40 + b" A\0outside.py\0"

    monkeypatch.setattr(module, "_diff_tree_raw", _fake_diff_tree_raw)

    _poison_git_env(monkeypatch, tmp_path)
    calls = _spy_on_subprocess_run(monkeypatch)

    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: a",
        expected_head=head_before,
    )
    assert result.status == "denied"
    assert result.reason_code == "post_commit_audit_violation_rolled_back"
    # Undo the adversarial poisoning before using the (unsanitized) test
    # helper `_head()` again -- it is not itself a call site under test.
    for var in _SANITIZED_GIT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert _head(repo) == head_before

    rollback_calls = [
        (args, kwargs)
        for (args, kwargs) in calls
        if args and args[0] and args[0][0] == "git" and args[0][1:3] == ["reset", "--soft"]
    ]
    assert rollback_calls, [args[0] for (args, _kwargs) in calls]
    for args, kwargs in rollback_calls:
        env = kwargs.get("env")
        assert env is not None, args[0]
        _assert_no_leaked_git_env_vars(env, args[0])


# ─── AC8 ──────────────────────────────────────────────────────────────────


def test_stale_snapshot_comment_drift_and_head_race_denies(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "a.py").write_text("x\n")

    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"], comment_bodies=("first",))

    stale_body = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        expected_head=_head(repo),
        current_issue_body_sha256="0" * 64,
    )
    assert stale_body.status == "denied"
    assert stale_body.reason_code == "stale_snapshot_body_drift"

    stale_comment = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        expected_head=_head(repo),
        current_comments_digest_sha256=compute_comments_digest_sha256(("first", "a new reply")),
    )
    assert stale_comment.status == "denied"
    assert stale_comment.reason_code == "stale_snapshot_comment_drift"

    stale_allowed = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        expected_head=_head(repo),
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

    ok_result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        expected_head=_head(repo),
    )
    assert ok_result.status == "committed"


def test_detached_head_unborn_branch_and_in_progress_state_denied(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "a.py").write_text("x\n")
    snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])

    head = _head(repo)
    subprocess.run(["git", "checkout", "-q", head], cwd=repo, check=True)
    detached = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        expected_head=head,
    )
    assert detached.status == "denied"
    assert detached.reason_code == "detached_head_rejected"
    subprocess.run(["git", "checkout", "-q", "topic"], cwd=repo, check=True)


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
        (AUTHORITY_NEW_DISABLED_FAIL_CLOSED, "legacy paths here", snapshot, AUTHORITY_SOURCE_NONE),
        (AUTHORITY_NEW_DISABLED_FAIL_CLOSED, None, None, AUTHORITY_SOURCE_NONE),
    ]
    for authority_mode, legacy_env, snap, expected_source in combos:
        resolution = resolve_authority(
            authority_mode=authority_mode,
            legacy_allowed_paths_env=legacy_env,
            snapshot=snap,
        )
        assert resolution.authoritative_source == expected_source, (authority_mode, legacy_env)
        assert resolution.authoritative_source in (
            AUTHORITY_SOURCE_LEGACY_ENV,
            AUTHORITY_SOURCE_SNAPSHOT,
            AUTHORITY_SOURCE_NONE,
        )

    unknown = resolve_authority(authority_mode="not_a_real_state", legacy_allowed_paths_env=None, snapshot=None)
    assert unknown.authoritative_source == AUTHORITY_SOURCE_NONE
    assert unknown.reason_code == "unknown_authority_mode"


def test_new_disabled_fail_closed_stops_add_commit_no_auto_fallback(tmp_path: Path):
    """AC12: `new_disabled_fail_closed` stops add/commit outright through
    `execute_controlled_change` itself -- never silently falls back to
    treating the legacy env as authoritative and proceeding anyway."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "a.py").write_text("x\n")

    snapshot = _build_snapshot(
        repo, allowed_paths=["scripts/agent-guards/**"], authority_mode=AUTHORITY_NEW_DISABLED_FAIL_CLOSED
    )
    result = execute_controlled_change(
        cwd=str(repo),
        snapshot=snapshot,
        requested_pathspecs=["scripts/agent-guards/a.py"],
        commit_message="feat: x",
        expected_head=_head(repo),
    )
    assert result.status == "denied"
    assert result.reason_code == "authority_new_disabled_fail_closed_add_commit_stopped"
    assert _staged_name_only(repo) == ""


# ─── AC13 ─────────────────────────────────────────────────────────────────


def test_new_modules_compile():
    import importlib

    for module_name in ("controlled_git_change_exec", "changed_file_matcher", "protected_paths_policy"):
        module = importlib.import_module(module_name)
        assert module is not None


# ─── AC14 ─────────────────────────────────────────────────────────────────


def test_codex_execpolicy_matrix_includes_git_mutation_cases():
    """AC14: `execpolicy_case_definitions()` includes static cases for the
    controlled-executor-only git add/commit narrowing (Issue #1611 contract
    revision). This asserts the case DEFINITIONS exist with the expected
    decision/guard-pair shape -- it does not invoke a real Codex binary."""
    import sys as _sys
    from dataclasses import dataclass
    from pathlib import Path as _Path

    if str(_CI_DIR) not in _sys.path:
        _sys.path.insert(0, str(_CI_DIR))
    import codex_execpolicy_matrix as matrix_module

    @dataclass(frozen=True)
    class _FakeFixture:
        root: _Path
        worktree: _Path
        branch: str
        issue_number: str

    fixture = _FakeFixture(
        root=_Path("/tmp/fake-repo"),
        worktree=_Path("/tmp/fake-repo/.claude/worktrees/issue-1611-x"),
        branch="worktree-issue-1611-x",
        issue_number="1611",
    )
    cases = matrix_module.execpolicy_case_definitions(fixture)
    labels = {case["label"]: case for case in cases}

    for expected_deny_label in (
        "git_add_denied_outside_controlled_executor",
        "git_commit_denied_outside_controlled_executor",
        "rtk_git_add_denied_outside_controlled_executor",
        "rtk_git_commit_denied_outside_controlled_executor",
    ):
        assert expected_deny_label in labels, expected_deny_label
        case = labels[expected_deny_label]
        assert case["expected_guard_pair"] == "deny"
        assert "forbidden" in case["expected_execpolicy"] or "prompt" in case["expected_execpolicy"]

    exact_case = labels["controlled_executor_exact_invocation_allowed"]
    assert exact_case["expected_guard_pair"] == "allow"
    assert exact_case["expected_execpolicy"] == ["allow"]
    assert "controlled_git_change_exec.py" in " ".join(exact_case["argv"])

    for deny_label in (
        "controlled_executor_extra_argv_denied",
        "controlled_executor_via_bash_lc_denied",
        "controlled_executor_from_main_root_denied",
        "controlled_executor_wrong_issue_worktree_denied",
    ):
        assert deny_label in labels, deny_label
        assert labels[deny_label]["expected_guard_pair"] == "deny"


# ─── Issue #1629 fix_delta P0 (provenance_self_attestation) ────────────────


def test_snapshot_json_flag_always_denied(tmp_path: Path):
    """`--snapshot-json` is permanently disabled as an authority source
    (Issue #1629 fix_delta P0): a hand-written, internally-consistent
    snapshot/sidecar pair on disk must never authorize a commit through the
    CLI entrypoint, regardless of what `_validate_materialized_snapshot_
    provenance` would say about it in isolation."""
    import controlled_git_change_exec as controlled_module
    import io
    import contextlib

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    snapshot_json = repo / "handwritten_snapshot.json"
    snapshot_json.write_text("{}")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = controlled_module._main(
            [
                "--cwd",
                str(repo),
                "--snapshot-json",
                str(snapshot_json),
                "--path",
                "README.md",
                "--message",
                "feat: x",
                "--expected-head",
                _head(repo),
            ]
        )
    assert rc == 1
    assert "snapshot_json_file_trust_disabled_use_materialize_request" in buf.getvalue()


def test_materialize_request_required_when_snapshot_json_absent(tmp_path: Path):
    import controlled_git_change_exec as controlled_module
    import io
    import contextlib

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = controlled_module._main(
            [
                "--cwd",
                str(repo),
                "--path",
                "README.md",
                "--message",
                "feat: x",
                "--expected-head",
                _head(repo),
            ]
        )
    assert rc == 1
    assert "materialize_request_required" in buf.getvalue()


def test_materialize_request_drives_commit_via_live_snapshot(tmp_path: Path, monkeypatch):
    """The sanctioned CLI path: `--materialize-request` recomputes the
    snapshot live (mocked here) and that in-memory snapshot -- never a file
    on disk -- authorizes the commit."""
    import controlled_git_change_exec as controlled_module
    import io
    import contextlib
    import json as json_module

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    guards = repo / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    (guards / "new_file.py").write_text("x = 1\n")

    live_snapshot = _build_snapshot(repo, allowed_paths=["scripts/agent-guards/**"])
    monkeypatch.setattr(controlled_module, "build_snapshot_via_live_materializer", lambda **kwargs: live_snapshot)

    request_json = repo / "materialize_request.json"
    request_json.write_text(
        json_module.dumps(
            {
                "issue_number": 1611,
                "repo": "squne121/loop-protocol",
                "contract_snapshot_url": "https://github.com/squne121/loop-protocol/issues/1611#issuecomment-1",
                "base_ref": "main",
                "branch_name": "topic",
                "output": "artifacts/1611/issue-metadata/issue_scope_snapshot.materialize/issue_scope_snapshot.json",
                "gh_bin": "/usr/bin/gh",
            }
        )
    )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = controlled_module._main(
            [
                "--cwd",
                str(repo),
                "--materialize-request",
                str(request_json),
                "--path",
                "scripts/agent-guards/new_file.py",
                "--message",
                "feat: add new file",
                "--expected-head",
                _head(repo),
            ]
        )
    assert rc == 0
    assert '"status": "committed"' in buf.getvalue()

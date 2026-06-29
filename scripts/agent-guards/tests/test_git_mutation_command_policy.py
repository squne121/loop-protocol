from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from git_mutation_command_policy import classify_rtk_git_mutation


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


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

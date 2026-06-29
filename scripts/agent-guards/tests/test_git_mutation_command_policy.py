from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from git_mutation_command_policy import classify_rtk_git_mutation


def test_rtk_git_add_explicit_file_allowed(tmp_path: Path):
    target = tmp_path / "tracked.txt"
    target.write_text("x")
    result = classify_rtk_git_mutation(
        "rtk git add tracked.txt",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "allow"


def test_rtk_git_add_broad_pathspec_denied(tmp_path: Path):
    result = classify_rtk_git_mutation(
        "rtk git add .",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "git_add_requires_explicit_pathspec"


def test_rtk_git_commit_requires_m_flag(tmp_path: Path):
    result = classify_rtk_git_mutation(
        "rtk git commit --amend",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "rtk_git_commit_requires_message"


def test_rtk_git_push_requires_head_refspec(tmp_path: Path):
    result = classify_rtk_git_mutation(
        "rtk git push origin main",
        cwd=str(tmp_path),
        require_active_branch_push=False,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "push_refspec_requires_active_branch"

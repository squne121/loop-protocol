"""Regression coverage for Issue #1403 same-invocation cleanup fallback."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-ops"))

import cleanup_exec as _ce  # noqa: E402
from cleanup_contract_v3 import OP_BRANCH_DELETE, OP_WORKTREE_REMOVE, PR_NOT_MERGED  # noqa: E402


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def normal_cleanup_candidate(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.invalid", cwd=root)
    _git("config", "user.name", "Cleanup test", cwd=root)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=root)
    (root / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)

    worktree_path = root / ".claude" / "worktrees" / "issue-1403-fallback"
    branch_name = "issue-1403-fallback"
    worktree_path.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "-b", branch_name, str(worktree_path), "main", cwd=root)
    (worktree_path / "feature.txt").write_text("unmerged cleanup branch\n")
    _git("add", "feature.txt", cwd=worktree_path)
    _git("commit", "-q", "-m", "feature commit", cwd=worktree_path)
    branch_tip = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"refs/heads/{branch_name}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    yield {
        "root": str(root),
        "worktree_path": str(worktree_path),
        "branch_name": branch_name,
        "branch_tip": branch_tip,
    }
    if worktree_path.exists():
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree_path)],
            capture_output=True,
        )


def _request(candidate: dict[str, str]) -> dict[str, object]:
    return {
        "schema": "CLEANUP_EXEC_REQUEST_V1",
        "pr_number": 1403,
        "linked_issue_number": 1403,
        "worktree_path": candidate["worktree_path"],
        "branch_name": candidate["branch_name"],
    }


def _merged_pr(candidate: dict[str, str], *, state: str = "MERGED") -> dict[str, object]:
    return {
        "state": state,
        "mergedAt": "2026-07-13T00:00:00Z" if state == "MERGED" else None,
        "headRefName": candidate["branch_name"],
        "headRefOid": candidate["branch_tip"],
        "baseRefName": "main",
        "isCrossRepository": False,
        "headRepositoryOwner": {"login": "squne121"},
        "closingIssuesReferences": [{"number": 1403}],
    }


def test_same_run_branch_only_fallback(normal_cleanup_candidate: dict[str, str]) -> None:
    """GIVEN ancestry rejects branch -d WHEN reauthorization passes THEN internal -D completes."""
    candidate = normal_cleanup_candidate
    with (
        patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
        patch.object(_ce, "_pr_state", return_value=_merged_pr(candidate)),
    ):
        result = _ce.run(_request(candidate), project_root=candidate["root"])

    assert result["status"] == "ok"
    assert result["reason_code"] is None
    assert result["actions_taken"] == [OP_WORKTREE_REMOVE, OP_BRANCH_DELETE]
    assert not Path(candidate["worktree_path"]).exists()
    deleted = subprocess.run(
        ["git", "-C", candidate["root"], "rev-parse", "--verify", f"refs/heads/{candidate['branch_name']}"],
        capture_output=True,
    )
    assert deleted.returncode != 0


def test_same_run_fallback_reauthorization_fail_closed(
    normal_cleanup_candidate: dict[str, str],
) -> None:
    """GIVEN branch -d partial success WHEN fallback sees an unmerged PR THEN force delete is refused."""
    candidate = normal_cleanup_candidate
    with (
        patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
        patch.object(
            _ce,
            "_pr_state",
            side_effect=[_merged_pr(candidate), _merged_pr(candidate, state="OPEN")],
        ),
    ):
        result = _ce.run(_request(candidate), project_root=candidate["root"])

    assert result["status"] == "error"
    assert result["reason_code"] == PR_NOT_MERGED
    assert result["actions_taken"] == [OP_WORKTREE_REMOVE]
    assert not Path(candidate["worktree_path"]).exists()
    still_present = subprocess.run(
        ["git", "-C", candidate["root"], "rev-parse", "--verify", f"refs/heads/{candidate['branch_name']}"],
        capture_output=True,
    )
    assert still_present.returncode == 0


def test_same_run_fallback_preserves_partial_actions_and_result_schema(
    normal_cleanup_candidate: dict[str, str],
) -> None:
    """GIVEN fallback refusal WHEN result emitted THEN its public schema and partial action are retained."""
    candidate = normal_cleanup_candidate
    with (
        patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
        patch.object(
            _ce,
            "_pr_state",
            side_effect=[_merged_pr(candidate), _merged_pr(candidate, state="OPEN")],
        ),
    ):
        result = _ce.run(_request(candidate), project_root=candidate["root"])

    assert set(result) == {
        "schema",
        "status",
        "reason_code",
        "verified",
        "actions_taken",
        "stderr_line_count",
    }
    assert result["actions_taken"] == [OP_WORKTREE_REMOVE]
    assert isinstance(result["verified"], dict)

"""tests/codex/test_cleanup_exec_branch_only.py

Tests for the branch-only cleanup lane in cleanup_exec.py (Issue #1196).

AC1: normal cleanup (worktree exists) → worktree_remove + branch_delete
AC2: branch-only candidate → branch_delete with status ok
AC3: branch tip / PR head OID mismatch → pr_head_oid_mismatch refused
AC4: requested worktree path not under .claude/worktrees/ → refused
AC5: cross-repo PR / base-branch mismatch / linked-issue mismatch → refused
AC6: result includes worktree_absent_after_removal: True
AC7: branch_only_force_delete_denied reason code defined; actions_taken: ["branch_delete"]
AC8: bare git branch -D still denied by hook (regression gate in test_local_main_branch_guard.py)
AC9: CLI shape unchanged — test that cleanup_exec.py exists (separate VC: test -f ...)

Issue #1337 (squash-merge head-OID equivalence):
AC1: squash merge, path-scoped content match -> authorized (verify_cleanup_authorization)
AC2: squash merge, path-scoped content mismatch -> pr_head_oid_mismatch
AC6: unrelated base change outside the local delta path set must not cause a false match
AC7: extra unmerged local-only files (outside the squashed merge commit) -> rejected
AC8: missing/null mergeCommit -> fail-closed, no squash-equivalence fallback attempted
AC9: verified dict carries additive head_equivalence_* / pr_merge_commit_oid /
     local_delta_paths_count fields without changing head_oid_match semantics
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-ops"))

import cleanup_exec as _ce
from cleanup_exec import (
    run,
    verify_cleanup_authorization,
    verify_branch_only_cleanup_authorization,
    WORKTREE_STILL_IN_CATALOG,
    BRANCH_CHECKED_OUT_IN_WORKTREE,
    LOCAL_BRANCH_MISSING,
    HEAD_OID_MISMATCH,
    BRANCH_ONLY_FORCE_DELETE_DENIED,
    BRANCH_ONLY_MATERIALIZE_DENIED,
    HEAD_REPO_MISMATCH,
    BASE_BRANCH_MISMATCH,
    LINKED_ISSUE_MISMATCH,
)
from cleanup_contract_v3 import OP_BRANCH_DELETE, OP_WORKTREE_REMOVE, WORKTREE_NOT_IN_CATALOG
from worktree_catalog import Deadline


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_merged_pr(branch_name: str, branch_tip: str, *,
                    pr_number: int = 1234, linked_issue: int = 1196,
                    state: str = "MERGED", cross_repo: bool = False,
                    base_ref: str = "main") -> dict:
    return {
        "state": state,
        "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None,
        "headRefName": branch_name,
        "headRefOid": branch_tip,
        "baseRefName": base_ref,
        "isCrossRepository": cross_repo,
        "headRepositoryOwner": {"login": "squne121"},
        "closingIssuesReferences": [{"number": linked_issue}],
    }


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def repo_with_worktree(tmp_path):
    """Temp git repo with a linked worktree under .claude/worktrees/ (AC1 / normal cleanup)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=root)
    (root / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)

    wt_parent = root / ".claude" / "worktrees"
    wt_parent.mkdir(parents=True, exist_ok=True)
    wt_path = wt_parent / "issue-1196-test"
    _git("worktree", "add", "-q", "-b", "issue-1196-test", str(wt_path), "main", cwd=root)

    branch_tip = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "refs/heads/issue-1196-test"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    yield {
        "root": str(root),
        "worktree_path": str(wt_path),
        "branch_name": "issue-1196-test",
        "branch_tip": branch_tip,
    }

    # Cleanup: remove worktree if still present
    if wt_path.exists():
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
        )


@pytest.fixture
def repo_branch_only(tmp_path):
    """Temp git repo where worktree was removed but branch still exists (branch-only state)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=root)
    (root / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)

    wt_parent = root / ".claude" / "worktrees"
    wt_parent.mkdir(parents=True, exist_ok=True)
    wt_path = wt_parent / "issue-1196-branch-only"
    _git("worktree", "add", "-q", "-b", "issue-1196-branch-only", str(wt_path), "main", cwd=root)

    branch_tip = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "refs/heads/issue-1196-branch-only"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Remove worktree so branch exists but worktree is gone
    _git("worktree", "remove", str(wt_path), cwd=root)

    assert not wt_path.exists(), "worktree should have been removed"

    yield {
        "root": str(root),
        "worktree_path": str(wt_path),
        "branch_name": "issue-1196-branch-only",
        "branch_tip": branch_tip,
    }


def _make_req(repo: dict, *, linked_issue: int = 1196, pr_number: int = 1234) -> dict:
    return {
        "schema": "CLEANUP_EXEC_REQUEST_V1",
        "pr_number": pr_number,
        "linked_issue_number": linked_issue,
        "worktree_path": repo["worktree_path"],
        "branch_name": repo["branch_name"],
    }


# ─── AC1: normal cleanup still works ──────────────────────────────────────────


class TestNormalCleanup:
    """AC1: worktree exists → worktree_remove + branch_delete in order."""

    def test_normal_cleanup_actions_taken(self, repo_with_worktree):
        """GIVEN worktree exists WHEN run() called THEN actions include worktree_remove and branch_delete."""
        repo = repo_with_worktree
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "ok", f"Expected ok, got: {result}"
        assert OP_WORKTREE_REMOVE in result["actions_taken"]
        assert OP_BRANCH_DELETE in result["actions_taken"]
        assert result["actions_taken"].index(OP_WORKTREE_REMOVE) < result["actions_taken"].index(OP_BRANCH_DELETE)

    def test_normal_cleanup_no_branch_only_flag(self, repo_with_worktree):
        """GIVEN normal worktree cleanup WHEN ok THEN branch_only not set in result."""
        repo = repo_with_worktree
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result.get("branch_only") is not True

    def test_normal_cleanup_verify_returns_worktree_in_catalog(self, repo_with_worktree):
        """GIVEN worktree exists WHEN verify_cleanup_authorization called THEN worktree_in_catalog True."""
        repo = repo_with_worktree
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))

        assert ok is True
        assert verified["worktree_in_catalog"] is True
        assert verified["branch_match"] is True


# ─── AC2: branch-only candidate ───────────────────────────────────────────────


class TestBranchOnlyCandidate:
    """AC2: worktree absent, branch present, OID matches → status ok, actions: [branch_delete]."""

    def test_branch_only_candidate_status_ok(self, repo_branch_only):
        """GIVEN branch-only candidate WHEN run() called THEN status ok."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "ok", f"Expected ok, got: {result}"

    def test_branch_only_candidate_actions_branch_delete_only(self, repo_branch_only):
        """GIVEN branch-only candidate WHEN run() ok THEN actions_taken == ['branch_delete']."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["actions_taken"] == [OP_BRANCH_DELETE]
        assert OP_WORKTREE_REMOVE not in result["actions_taken"]

    def test_branch_only_candidate_verified_fields(self, repo_branch_only):
        """GIVEN branch-only candidate WHEN verify_branch_only_cleanup_authorization called THEN all 5 conditions True."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )

        assert ok is True, f"Expected ok, reason={reason}, verified={verified}"
        assert verified["worktree_path_under_worktrees_dir"] is True     # condition A
        assert verified["worktree_absent_on_disk"] is True               # condition B
        assert verified["worktree_absent_from_catalog"] is True          # condition C
        assert verified["branch_absent_from_worktree_catalog"] is True   # condition D
        assert verified["local_branch_exists"] is True                   # condition E
        assert verified["branch_only_candidate"] is True
        assert verified["head_oid_match"] is True

    def test_branch_only_candidate_branch_deleted_after_run(self, repo_branch_only):
        """GIVEN branch-only candidate WHEN run() ok THEN branch no longer exists locally."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "ok"
        # Verify branch is actually gone
        check = subprocess.run(
            ["git", "-C", repo["root"], "rev-parse", "--verify",
             f"refs/heads/{repo['branch_name']}"],
            capture_output=True,
        )
        assert check.returncode != 0, "Branch should have been deleted"


# ─── AC3: OID mismatch ────────────────────────────────────────────────────────


class TestOidMismatch:
    """AC3: branch tip != PR head OID → refused with pr_head_oid_mismatch."""

    def test_oid_mismatch_refused(self, repo_branch_only):
        """GIVEN OID mismatch WHEN run() called THEN refused with pr_head_oid_mismatch."""
        repo = repo_branch_only
        req = _make_req(repo)
        # Use a different OID than the actual branch tip
        fake_pr = _make_merged_pr(repo["branch_name"], "a" * 40)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "refused"
        assert result["reason_code"] == HEAD_OID_MISMATCH

    def test_oid_mismatch_verify_branch_only_returns_false(self, repo_branch_only):
        """GIVEN OID mismatch WHEN verify_branch_only_cleanup_authorization called THEN ok=False."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], "b" * 40)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )

        assert ok is False
        assert reason == HEAD_OID_MISMATCH
        assert verified["pr_head_oid"] == "b" * 40
        assert verified["head_oid_match"] is False


# ─── AC4: path constraint ─────────────────────────────────────────────────────


class TestPathConstraint:
    """AC4: worktree path not under .claude/worktrees/ → branch-only lane refused."""

    def test_path_constraint_outside_worktrees_dir_refused(self, tmp_path):
        """GIVEN worktree path outside .claude/worktrees/ WHEN verify_branch_only called THEN refused."""
        root = tmp_path / "repo"
        root.mkdir()
        _git("init", "-q", "-b", "main", cwd=root)
        _git("config", "user.email", "t@t.com", cwd=root)
        _git("config", "user.name", "T", cwd=root)
        (root / "README.md").write_text("seed\n")
        _git("add", "README.md", cwd=root)
        _git("commit", "-q", "-m", "seed", cwd=root)
        # Create branch
        _git("checkout", "-b", "issue-path-test", cwd=root)
        branch_tip = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "refs/heads/issue-path-test"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _git("checkout", "main", cwd=root)

        # Use a path OUTSIDE .claude/worktrees/
        outside_path = str(tmp_path / "some-other-dir" / "issue-path-test")

        req = {
            "schema": "CLEANUP_EXEC_REQUEST_V1",
            "pr_number": 1234,
            "linked_issue_number": None,
            "worktree_path": outside_path,
            "branch_name": "issue-path-test",
        }

        fake_pr = _make_merged_pr("issue-path-test", branch_tip)
        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(
                req, str(root), Deadline(30.0)
            )

        assert ok is False
        assert reason == BRANCH_ONLY_FORCE_DELETE_DENIED
        assert verified["worktree_path_under_worktrees_dir"] is False

    def test_path_constraint_run_refused_for_outside_path(self, tmp_path):
        """GIVEN worktree path outside .claude/worktrees/ WHEN run() called THEN refused."""
        root = tmp_path / "repo"
        root.mkdir()
        _git("init", "-q", "-b", "main", cwd=root)
        _git("config", "user.email", "t@t.com", cwd=root)
        _git("config", "user.name", "T", cwd=root)
        (root / "README.md").write_text("seed\n")
        _git("add", "README.md", cwd=root)
        _git("commit", "-q", "-m", "seed", cwd=root)
        _git("checkout", "-b", "issue-path-test2", cwd=root)
        branch_tip = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "refs/heads/issue-path-test2"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _git("checkout", "main", cwd=root)

        outside_path = str(tmp_path / "other" / "issue-path-test2")
        req = {
            "schema": "CLEANUP_EXEC_REQUEST_V1",
            "pr_number": 9999,
            "linked_issue_number": None,
            "worktree_path": outside_path,
            "branch_name": "issue-path-test2",
        }
        fake_pr = _make_merged_pr("issue-path-test2", branch_tip)
        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=str(root))

        # Normal verify returns WORKTREE_NOT_IN_CATALOG, then branch-only returns
        # BRANCH_ONLY_FORCE_DELETE_DENIED (path outside .claude/worktrees/)
        assert result["status"] == "refused"
        assert result["reason_code"] == BRANCH_ONLY_FORCE_DELETE_DENIED


# ─── AC5: cross-repo / base-branch / linked-issue mismatch ───────────────────


class TestCrossRepo:
    """AC5: cross-repo PR, base branch mismatch, linked issue mismatch → refused in branch-only lane."""

    def test_cross_repo_pr_refused_branch_only(self, repo_branch_only):
        """GIVEN cross-repo PR WHEN verify_branch_only called THEN refused with pr_head_repo_mismatch."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"], cross_repo=True)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )

        assert ok is False
        assert reason == HEAD_REPO_MISMATCH

    def test_base_branch_mismatch_refused_branch_only(self, repo_branch_only):
        """GIVEN PR base != default branch WHEN verify_branch_only called THEN refused with pr_base_branch_mismatch."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"], base_ref="develop")

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )

        assert ok is False
        assert reason == BASE_BRANCH_MISMATCH

    def test_linked_issue_mismatch_refused_branch_only(self, repo_branch_only):
        """GIVEN linked issue mismatch WHEN verify_branch_only called THEN refused with linked_issue_mismatch."""
        repo = repo_branch_only
        req = _make_req(repo, linked_issue=9999)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"], linked_issue=1196)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )

        assert ok is False
        assert reason == LINKED_ISSUE_MISMATCH

    def test_cross_repo_run_refused(self, repo_branch_only):
        """GIVEN cross-repo PR WHEN run() called THEN refused."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"], cross_repo=True)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "refused"
        assert result["reason_code"] == HEAD_REPO_MISMATCH


# ─── AC6: worktree_absent_after_removal field ─────────────────────────────────


class TestAbsentAfterRemoval:
    """AC6: branch-only result includes worktree_absent_after_removal: True."""

    def test_absent_after_removal_true_on_ok(self, repo_branch_only):
        """GIVEN branch-only ok WHEN result returned THEN worktree_absent_after_removal is True."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result.get("worktree_absent_after_removal") is True

    def test_absent_after_removal_true_on_refused(self, repo_branch_only):
        """GIVEN branch-only refused (OID mismatch) WHEN result returned THEN worktree_absent_after_removal is True."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], "c" * 40)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "refused"
        assert result.get("worktree_absent_after_removal") is True

    def test_absent_after_removal_not_in_normal_cleanup_result(self, repo_with_worktree):
        """GIVEN normal worktree cleanup WHEN result returned THEN worktree_absent_after_removal not set."""
        repo = repo_with_worktree
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        # Normal cleanup does not set worktree_absent_after_removal
        assert "worktree_absent_after_removal" not in result


# ─── AC7: force_delete reason code + actions_taken ────────────────────────────


class TestForceDelete:
    """AC7: branch_only_force_delete_denied and actions_taken: ['branch_delete'] defined."""

    def test_force_delete_reason_code_defined(self):
        """GIVEN reason code constants WHEN checked THEN branch_only_force_delete_denied is defined."""
        assert BRANCH_ONLY_FORCE_DELETE_DENIED == "branch_only_force_delete_denied"

    def test_branch_only_materialize_denied_constant(self):
        """GIVEN reason code constants WHEN checked THEN branch_only_materialize_denied is defined."""
        assert BRANCH_ONLY_MATERIALIZE_DENIED == "branch_only_materialize_denied"

    def test_force_delete_actions_taken_contains_branch_delete(self, repo_branch_only):
        """GIVEN authorized branch-only cleanup WHEN run() ok THEN actions_taken == ['branch_delete']."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "ok"
        assert result["actions_taken"] == ["branch_delete"]

    def test_force_delete_denied_when_path_outside_worktrees(self, repo_branch_only):
        """GIVEN path outside .claude/worktrees/ WHEN branch-only attempted THEN branch_only_force_delete_denied."""
        repo = repo_branch_only
        # Use a path outside .claude/worktrees/
        outside_req = {
            "schema": "CLEANUP_EXEC_REQUEST_V1",
            "pr_number": 1234,
            "linked_issue_number": 1196,
            "worktree_path": "/tmp/some-random-path/issue-1196-branch-only",
            "branch_name": repo["branch_name"],
        }
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(
                outside_req, repo["root"], Deadline(30.0)
            )

        assert ok is False
        assert reason == BRANCH_ONLY_FORCE_DELETE_DENIED

    def test_force_delete_authorized_uses_git_branch_D(self, repo_branch_only, monkeypatch):
        """GIVEN authorized branch-only WHEN _perform_branch_only called THEN uses 'branch -D'."""
        repo = repo_branch_only
        captured_args: list[list[str]] = []

        original_git = _ce._git

        def mock_git(args, deadline, maximum=10.0):
            captured_args.append(list(args))
            return original_git(args, deadline, maximum)

        monkeypatch.setattr(_ce, "_git", mock_git)

        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "ok"
        # Verify git branch -D was used (not -d)
        branch_cmds = [a for a in captured_args if "branch" in a]
        assert any("-D" in cmd for cmd in branch_cmds), (
            f"Expected 'branch -D' in git calls, got: {branch_cmds}"
        )
        assert not any("-d" in cmd and "-D" not in cmd for cmd in branch_cmds), (
            "branch -d (soft delete) must not be used in branch-only lane"
        )

    def test_branch_only_force_delete_used_field(self, repo_branch_only):
        """GIVEN authorized branch-only WHEN verify called THEN branch_only_force_delete_used True."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, _, verified = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )

        assert ok is True
        assert verified["branch_only_force_delete_used"] is True


# ─── Additional branch-only candidate condition tests ─────────────────────────


class TestBranchOnlyConditions:
    """Tests for individual branch-only candidacy conditions (A-E)."""

    def test_worktree_still_on_disk_refused(self, repo_with_worktree):
        """GIVEN worktree still on disk (condition B fails) WHEN verify_branch_only called THEN refused."""
        repo = repo_with_worktree
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        # worktree_path EXISTS on disk → condition B fails
        assert Path(repo["worktree_path"]).exists(), "Worktree should still exist for this test"

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )

        assert ok is False
        assert reason == WORKTREE_STILL_IN_CATALOG
        assert verified["worktree_absent_on_disk"] is False

    def test_branch_checked_out_in_another_worktree_refused(self, tmp_path):
        """GIVEN branch checked out in another worktree (condition D fails) WHEN verify called THEN refused."""
        root = tmp_path / "repo"
        root.mkdir()
        _git("init", "-q", "-b", "main", cwd=root)
        _git("config", "user.email", "t@t.com", cwd=root)
        _git("config", "user.name", "T", cwd=root)
        (root / "README.md").write_text("seed\n")
        _git("add", "README.md", cwd=root)
        _git("commit", "-q", "-m", "seed", cwd=root)

        wt_parent = root / ".claude" / "worktrees"
        wt_parent.mkdir(parents=True, exist_ok=True)

        # Create original worktree and branch
        wt1 = wt_parent / "issue-1196-wt1"
        _git("worktree", "add", "-q", "-b", "issue-1196-wt1", str(wt1), "main", cwd=root)
        branch_tip = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "refs/heads/issue-1196-wt1"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Remove the original worktree so condition B and C pass
        _git("worktree", "remove", str(wt1), cwd=root)

        # Create ANOTHER worktree with the SAME branch name (re-add it)
        wt2 = wt_parent / "issue-1196-wt2"
        _git("worktree", "add", "-q", str(wt2), "issue-1196-wt1", cwd=root)

        req = {
            "schema": "CLEANUP_EXEC_REQUEST_V1",
            "pr_number": 1234,
            "linked_issue_number": None,
            "worktree_path": str(wt1),  # original path (absent on disk)
            "branch_name": "issue-1196-wt1",
        }
        fake_pr = _make_merged_pr("issue-1196-wt1", branch_tip)

        try:
            with (
                patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
                patch.object(_ce, "_pr_state", return_value=fake_pr),
            ):
                ok, reason, verified = verify_branch_only_cleanup_authorization(
                    req, str(root), Deadline(30.0)
                )

            assert ok is False
            assert reason == BRANCH_CHECKED_OUT_IN_WORKTREE
            assert verified["branch_absent_from_worktree_catalog"] is False
        finally:
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(wt2)],
                capture_output=True,
            )

    def test_local_branch_missing_refused(self, tmp_path):
        """GIVEN local branch deleted (condition E fails) WHEN verify called THEN refused with local_branch_missing."""
        root = tmp_path / "repo"
        root.mkdir()
        _git("init", "-q", "-b", "main", cwd=root)
        _git("config", "user.email", "t@t.com", cwd=root)
        _git("config", "user.name", "T", cwd=root)
        (root / "README.md").write_text("seed\n")
        _git("add", "README.md", cwd=root)
        _git("commit", "-q", "-m", "seed", cwd=root)

        wt_parent = root / ".claude" / "worktrees"
        wt_parent.mkdir(parents=True, exist_ok=True)
        wt_path = wt_parent / "issue-1196-no-branch"
        _git("worktree", "add", "-q", "-b", "issue-1196-no-branch", str(wt_path), "main", cwd=root)
        branch_tip = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "refs/heads/issue-1196-no-branch"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Remove worktree and also delete the branch
        _git("worktree", "remove", str(wt_path), cwd=root)
        _git("branch", "-D", "issue-1196-no-branch", cwd=root)

        req = {
            "schema": "CLEANUP_EXEC_REQUEST_V1",
            "pr_number": 1234,
            "linked_issue_number": None,
            "worktree_path": str(wt_path),
            "branch_name": "issue-1196-no-branch",
        }
        fake_pr = _make_merged_pr("issue-1196-no-branch", branch_tip)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(
                req, str(root), Deadline(30.0)
            )

        assert ok is False
        assert reason == LOCAL_BRANCH_MISSING
        assert verified["local_branch_exists"] is False


# ─── AC9: CLI shape unchanged ─────────────────────────────────────────────────


class TestCliShape:
    """AC9: cleanup_exec.py CLI shape unchanged — no new flags."""

    def test_cleanup_exec_file_exists(self):
        """cleanup_exec.py must exist in scripts/agent-ops/."""
        path = REPO_ROOT / "scripts" / "agent-ops" / "cleanup_exec.py"
        assert path.exists(), f"cleanup_exec.py not found: {path}"

    def test_cli_no_new_flags(self, tmp_path):
        """CLI help must show only the original 5 flags — no new flags added."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "agent-ops" / "cleanup_exec.py"), "--help"],
            capture_output=True, text=True,
        )
        # Extract flag lines
        help_text = result.stdout + result.stderr
        # Must have exactly the original 5 flags
        for flag in ["--pr-number", "--linked-issue-number", "--worktree-path", "--branch-name", "--json"]:
            assert flag in help_text, f"Expected flag {flag!r} in help"
        # Must NOT have new flags
        forbidden = ["--allow-branch-only", "--branch-only", "--force-delete", "--mode"]
        for flag in forbidden:
            assert flag not in help_text, f"Forbidden new flag {flag!r} found in help"

    def test_run_function_accepts_existing_req_shape(self):
        """run() must accept the existing CLEANUP_EXEC_REQUEST_V1 shape without error on bad creds."""
        req = {
            "schema": "CLEANUP_EXEC_REQUEST_V1",
            "pr_number": 1234,
            "linked_issue_number": None,
            "worktree_path": "/nonexistent/path",
            "branch_name": "test-branch",
        }
        # Should not raise — just refuse/error
        with patch.object(_ce, "_repo_slug", return_value=None):
            result = run(req)
        assert "status" in result
        assert result["status"] in ("refused", "error")


# ─── B3: TestCatalogUnreadable ────────────────────────────────────────────────


class TestCatalogUnreadable:
    """B1 fix: list_worktrees() が None を返した場合は fail-closed。"""

    def test_catalog_none_refuses_branch_only(self, repo_branch_only):
        """list_worktrees() が None（catalog 読み取り不能）なら branch-only は拒否。"""
        repo = repo_branch_only
        req = _make_req(repo)
        with patch.object(_ce, "list_worktrees", return_value=None):
            result = run(req, project_root=repo["root"])
        assert result["status"] in ("refused", "error"), (
            f"Expected refused/error when catalog unreadable, got: {result}"
        )
        assert result["actions_taken"] == [], (
            f"No actions expected on refusal: {result['actions_taken']}"
        )
        # branch still exists — no destructive action taken
        out = subprocess.run(
            ["git", "-C", repo["root"], "rev-parse", "--verify",
             f"refs/heads/{repo['branch_name']}"],
            capture_output=True, text=True,
        )
        assert out.returncode == 0, "branch must still exist after refusal"


# ─── B3: TestWorktreeAbsentAfterRemoval ───────────────────────────────────────


class TestWorktreeAbsentAfterRemoval:
    """B2 fix: worktree_absent_after_removal は verified フィールドに基づく（拒否時は条件次第）。"""

    def test_refused_when_path_exists_has_false_absent(self, tmp_path):
        """disk 上に path が存在する場合の拒否: worktree_absent_after_removal は True ではない。"""
        root = tmp_path / "repo"
        root.mkdir()
        _git("init", "-q", "-b", "main", cwd=root)
        _git("config", "user.email", "t@t.com", cwd=root)
        _git("config", "user.name", "T", cwd=root)
        (root / "README.md").write_text("seed\n")
        _git("add", "README.md", cwd=root)
        _git("commit", "-q", "-m", "seed", cwd=root)

        wt_parent = root / ".claude" / "worktrees"
        wt_parent.mkdir(parents=True, exist_ok=True)
        wt_path = wt_parent / "issue-1196-b2-test"
        _git("worktree", "add", "-q", "-b", "issue-1196-b2-test", str(wt_path), "main", cwd=root)

        branch_tip = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "refs/heads/issue-1196-b2-test"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Remove worktree from catalog AND disk via git worktree remove
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(wt_path)],
            check=True, capture_output=True,
        )
        # Recreate directory on disk (absent from catalog, present on disk)
        wt_path.mkdir(parents=True, exist_ok=True)

        req = {
            "schema": "CLEANUP_EXEC_REQUEST_V1",
            "pr_number": 1234,
            "linked_issue_number": None,
            "worktree_path": str(wt_path),
            "branch_name": "issue-1196-b2-test",
        }
        fake_pr = _make_merged_pr("issue-1196-b2-test", branch_tip)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=str(root))

        assert result["status"] == "refused", f"Expected refused, got: {result}"
        # B2 fix: when worktree_absent_on_disk is False, must not claim True
        assert result.get("worktree_absent_after_removal") is not True, (
            f"refused result must not claim worktree_absent_after_removal=True "
            f"when path exists on disk: {result}"
        )
        assert result["actions_taken"] == []


# ─── B3: TestMaterializeRefusesBranchOnly ─────────────────────────────────────


class TestMaterializeRefusesBranchOnly:
    """B3: materialize_cleanup_contract が branch-only 状態で contract を発行しないことを確認。"""

    def test_materialize_refused_in_branch_only_state(self, repo_branch_only):
        """worktree が disk/catalog から消えている branch-only 状態では materialize は refused。"""
        import importlib.util

        repo = repo_branch_only
        mat_path = REPO_ROOT / "scripts" / "agent-ops" / "materialize_cleanup_contract.py"
        spec = importlib.util.spec_from_file_location(
            "materialize_cleanup_contract_b3_test", str(mat_path)
        )
        assert spec is not None and spec.loader is not None
        mat_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mat_mod)

        # branch-only state: worktree absent from disk+catalog, branch still present
        result = mat_mod.materialize(
            pr_number=1234,
            linked_issue_number=None,
            worktree_path=repo["worktree_path"],
            branch_name=repo["branch_name"],
            project_root=repo["root"],
        )

        assert result["status"] == "refused", (
            f"expected materialize to refuse in branch-only state, got: {result}"
        )
        # No contract file should be written
        contract_path = Path(repo["root"]) / "artifacts" / "agent-ops" / "cleanup_contract.json"
        assert not contract_path.exists(), (
            f"no contract should be written in branch-only state: {contract_path}"
        )
        # Branch is NOT deleted — materialize refused without action
        out = subprocess.run(
            ["git", "-C", repo["root"], "rev-parse", "--verify",
             f"refs/heads/{repo['branch_name']}"],
            capture_output=True, text=True,
        )
        assert out.returncode == 0, "branch must still exist after materialize refusal"


# ─── Issue #1337: squash-merge head-OID equivalence ───────────────────────────


@pytest.fixture
def repo_for_squash_equivalence(tmp_path):
    """Temp git repo with a linked worktree on a feature branch (Issue #1337).

    The feature branch is checked out as a REAL git worktree (under
    .claude/worktrees/) so verify_cleanup_authorization's catalog + clean-worktree
    checks pass, exactly like repo_with_worktree. The squash commit is then
    created directly on main in the ROOT repo (not the worktree) to represent
    what GitHub's squash-merge would have produced.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=root)
    (root / "shared.txt").write_text("base v1\n")
    _git("add", "shared.txt", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)

    branch_name = "issue-1337-feature"
    wt_parent = root / ".claude" / "worktrees"
    wt_parent.mkdir(parents=True, exist_ok=True)
    wt_path = wt_parent / "issue-1337-feature"
    _git("worktree", "add", "-q", "-b", branch_name, str(wt_path), "main", cwd=root)
    (wt_path / "feature.txt").write_text("feature-A\n")
    _git("add", "feature.txt", cwd=wt_path)
    _git("commit", "-q", "-m", "add feature", cwd=wt_path)
    local_tip = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"refs/heads/{branch_name}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    yield {
        "root": str(root),
        "branch_name": branch_name,
        "local_tip": local_tip,
        "worktree_path": str(wt_path),
    }

    if wt_path.exists():
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
        )


def _commit_on_main(root, filename, content, message):
    (Path(root) / filename).write_text(content)
    _git("add", filename, cwd=root)
    _git("commit", "-q", "-m", message, cwd=root)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()



def test_verify_cleanup_authorization_accepts_squash_merge_path_scoped_match(
    repo_for_squash_equivalence
):
    """GIVEN squash commit with identical feature.txt content WHEN verified THEN authorized."""
    repo = repo_for_squash_equivalence
    merge_commit_oid = _commit_on_main(
        repo["root"], "feature.txt", "feature-A\n", "squash: add feature (#1337)"
    )
    fake_pr = _make_merged_pr(repo["branch_name"], "f" * 40, linked_issue=1337)
    fake_pr["mergeCommit"] = {"oid": merge_commit_oid}
    req = _make_req(repo, linked_issue=1337)

    with (
        patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
        patch.object(_ce, "_pr_state", return_value=fake_pr),
    ):
        ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))

    assert ok is True, f"Expected authorized, reason={reason}, verified={verified}"
    assert verified["head_equivalence_authorized"] is True
    assert verified["head_equivalence_mode"] == "squash_merge_delta_match"
    assert verified["pr_merge_commit_oid"] == merge_commit_oid
    assert verified["local_delta_paths_count"] == 1

def test_verify_cleanup_authorization_rejects_squash_merge_content_mismatch(
    repo_for_squash_equivalence
):
    """GIVEN squash commit with DIFFERENT feature.txt content WHEN verified THEN pr_head_oid_mismatch."""
    repo = repo_for_squash_equivalence
    merge_commit_oid = _commit_on_main(
        repo["root"], "feature.txt", "feature-B (different)\n", "squash: add feature (#1337)"
    )
    fake_pr = _make_merged_pr(repo["branch_name"], "f" * 40)
    fake_pr["mergeCommit"] = {"oid": merge_commit_oid}
    req = _make_req(repo, linked_issue=1337)

    with (
        patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
        patch.object(_ce, "_pr_state", return_value=fake_pr),
    ):
        ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))

    assert ok is False
    assert reason == HEAD_OID_MISMATCH
    assert verified["head_equivalence_authorized"] is False

def test_squash_merge_unrelated_base_change_does_not_false_match(
    repo_for_squash_equivalence
):
    """GIVEN unrelated base change AND a real feature.txt mismatch WHEN verified THEN still rejected.

    The unrelated ``shared.txt`` diff (base evolved after the branch forked)
    must not mask a genuine content mismatch in the local branch's own path
    set — path-set-restricted comparison must not be fooled by noise.
    """
    repo = repo_for_squash_equivalence
    # Unrelated base change: shared.txt evolves on main, NOT touched by the
    # local branch at all (local branch never modified shared.txt).
    _commit_on_main(repo["root"], "shared.txt", "base v2 (unrelated change)\n", "unrelated base change")
    # The "squash commit" on top of that unrelated change has a MISMATCHED
    # feature.txt content relative to the local branch tip.
    merge_commit_oid = _commit_on_main(
        repo["root"], "feature.txt", "feature-B (mismatch)\n", "squash: add feature (#1337)"
    )
    fake_pr = _make_merged_pr(repo["branch_name"], "f" * 40)
    fake_pr["mergeCommit"] = {"oid": merge_commit_oid}
    req = _make_req(repo, linked_issue=1337)

    with (
        patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
        patch.object(_ce, "_pr_state", return_value=fake_pr),
    ):
        ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))

    assert ok is False, (
        f"unrelated base change noise must not mask the real feature.txt mismatch: {verified}"
    )
    assert reason == HEAD_OID_MISMATCH
    assert verified["head_equivalence_authorized"] is False

def test_squash_merge_rejects_extra_unmerged_local_files(repo_for_squash_equivalence):
    """GIVEN local branch has an extra file never captured by the squash commit WHEN verified THEN rejected."""
    repo = repo_for_squash_equivalence
    # The squash commit only captures the ORIGINAL feature.txt content.
    merge_commit_oid = _commit_on_main(
        repo["root"], "feature.txt", "feature-A\n", "squash: add feature (#1337)"
    )
    # Local branch keeps evolving AFTER what got merged: an extra local-only
    # file that was never part of the squashed PR content. The branch is
    # already checked out in the linked worktree (not root), so commit there.
    (Path(repo["worktree_path"]) / "extra.txt").write_text("local-only, never merged\n")
    _git("add", "extra.txt", cwd=repo["worktree_path"])
    _git("commit", "-q", "-m", "extra unmerged local commit", cwd=repo["worktree_path"])
    new_local_tip = subprocess.run(
        ["git", "-C", repo["root"], "rev-parse", f"refs/heads/{repo['branch_name']}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    fake_pr = _make_merged_pr(repo["branch_name"], "f" * 40)
    fake_pr["mergeCommit"] = {"oid": merge_commit_oid}
    req = _make_req(repo, linked_issue=1337)
    req["branch_name"] = repo["branch_name"]

    with (
        patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
        patch.object(_ce, "_pr_state", return_value=fake_pr),
    ):
        ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))

    assert ok is False, f"extra unmerged local file must cause rejection: {verified}"
    assert reason == HEAD_OID_MISMATCH
    assert verified["local_delta_paths_count"] == 2  # feature.txt + extra.txt
    assert verified["head_equivalence_authorized"] is False
    assert new_local_tip != repo["local_tip"]

def test_squash_merge_missing_merge_commit_falls_back_to_reject(
    repo_for_squash_equivalence
):
    """GIVEN mergeCommit missing/null WHEN OID mismatch THEN fail-closed without squash fallback."""
    repo = repo_for_squash_equivalence
    fake_pr = _make_merged_pr(repo["branch_name"], "f" * 40)
    fake_pr["mergeCommit"] = None
    req = _make_req(repo, linked_issue=1337)

    with (
        patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
        patch.object(_ce, "_pr_state", return_value=fake_pr),
    ):
        ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))

    assert ok is False
    assert reason == HEAD_OID_MISMATCH
    assert verified["head_equivalence_authorized"] is False
    assert verified["head_equivalence_mode"] is None
    assert verified["pr_merge_commit_oid"] is None
    assert verified["local_delta_paths_count"] is None

def test_verified_dict_contains_additive_head_equivalence_fields(
    repo_for_squash_equivalence
):
    """GIVEN squash-equivalence authorized WHEN verified THEN additive fields present, head_oid_match unchanged."""
    repo = repo_for_squash_equivalence
    merge_commit_oid = _commit_on_main(
        repo["root"], "feature.txt", "feature-A\n", "squash: add feature (#1337)"
    )
    fake_pr = _make_merged_pr(repo["branch_name"], "f" * 40, linked_issue=1337)
    fake_pr["mergeCommit"] = {"oid": merge_commit_oid}
    req = _make_req(repo, linked_issue=1337)

    with (
        patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
        patch.object(_ce, "_pr_state", return_value=fake_pr),
    ):
        ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))

    assert ok is True, f"reason={reason}, verified={verified}"
    # Additive fields present and correct.
    assert verified["head_equivalence_authorized"] is True
    assert verified["head_equivalence_mode"] == "squash_merge_delta_match"
    assert verified["pr_merge_commit_oid"] == merge_commit_oid
    assert verified["local_delta_paths_count"] == 1
    # Existing head_oid_match semantics unchanged: it reflects the LITERAL
    # exact-SHA comparison only, so it stays False for a squash-equivalence
    # authorized case (headRefOid != local branch tip by construction here).
    assert verified["head_oid_match"] is False

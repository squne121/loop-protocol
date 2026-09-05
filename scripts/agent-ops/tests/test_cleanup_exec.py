"""scripts/agent-ops/tests/test_cleanup_exec.py

Issue #1523: branch-only fallback compare-and-delete / structural ancestry /
verified-payload-schema / same-invocation refusal-matrix regression coverage.

AC7: branch-only fallback threads the expected branch-tip OID captured at
     authorization time through to the destructive path and performs an
     OID-bound compare-and-delete (``git update-ref -d refs/heads/<branch>
     <expected-old-oid>``) inside a shared repository mutation lock. A ref
     race (tip changed between authorization and delete) is refused with
     ``branch_tip_changed`` and the branch is left intact.
AC8: the compare-and-delete is gated by a STRUCTURAL ancestry check
     (``_verify_ancestry_for_force_delete``): the expected OID must resolve to
     a real, locally-present commit object AND ``git merge-base
     --is-ancestor`` must return a meaningful exit code (0 or 1). Invalid
     object, ref-lock/comparison error, or any other exit code fails closed to
     ``branch_only_non_ancestry_failure`` without ever invoking the
     destructive delete.
AC9: the ``verified`` payload's key set and per-key types are normalized
     across ALL verifier lanes (normal / branch-only / discard) via a shared
     ``_verified_template()`` union.
AC10: the same-invocation fallback (normal cleanup's worktree_remove succeeds
      but branch -d then fails) re-authorizes via the SAME branch-only
      verifier before attempting a force-delete; any refusal during
      re-authorization preserves the original reason code, keeps
      ``actions_taken`` to ``worktree_remove`` only, leaves the branch intact,
      and never invokes the force-delete subprocess.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import ANY, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-ops"))

import cleanup_exec as _ce  # noqa: E402
from cleanup_exec import (  # noqa: E402
    run,
    verify_branch_only_cleanup_authorization,
    verify_cleanup_authorization,
    _perform_branch_only,
    _pr_body_has_exact_refs,
    _research_fallback_authorized,
    _verify_ancestry_for_force_delete,
    _verify_linked_issue,
    _verified_template,
    BRANCH_TIP_CHANGED,
    BRANCH_ONLY_NON_ANCESTRY_FAILURE,
    LINKED_ISSUE_MISMATCH,
)
from cleanup_contract_v3 import OP_BRANCH_DELETE, OP_WORKTREE_REMOVE  # noqa: E402
from worktree_catalog import Deadline  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _rev_parse(root, ref) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", ref],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _make_merged_pr(branch_name: str, branch_tip: str, *,
                     pr_number: int = 1523, linked_issue: int = 1523,
                     state: str = "MERGED", cross_repo: bool = False,
                     base_ref: str = "main", body: str = "",
                     closing_issue_refs: list[int] | None = None) -> dict:
    """Build a fake ``gh pr view --json`` PR dict matching the real production
    shape (Issue #2508: production ``_pr_state()`` now also requests
    ``body``). ``closing_issue_refs`` defaults to ``[linked_issue]`` (existing
    canonical-linkage shape); pass ``[]`` to simulate the research-issue shape
    (closing keyword not used) that the fallback conditions must be evaluated
    for.
    """
    refs = [linked_issue] if closing_issue_refs is None else closing_issue_refs
    return {
        "state": state,
        "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None,
        "headRefName": branch_name,
        "headRefOid": branch_tip,
        "baseRefName": base_ref,
        "isCrossRepository": cross_repo,
        "headRepositoryOwner": {"login": "squne121"},
        "closingIssuesReferences": [{"number": n} for n in refs],
        "body": body,
    }


def _make_research_issue_mrc(issue_kind: str = "research") -> str:
    """Build a minimal Issue body carrying a valid Machine-Readable Contract
    block with the given ``issue_kind`` (Issue #2508 research fallback)."""
    return (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        f"issue_kind: {issue_kind}\n"
        "parent_issue: \"none\"\n"
        "goal_ref: \"test\"\n"
        "change_kind: code\n"
        "```\n"
    )


def _make_req(repo: dict, *, linked_issue: int = 1523, pr_number: int = 1523) -> dict:
    return {
        "schema": "CLEANUP_EXEC_REQUEST_V1",
        "pr_number": pr_number,
        "linked_issue_number": linked_issue,
        "worktree_path": repo["worktree_path"],
        "branch_name": repo["branch_name"],
    }


@pytest.fixture
def repo_branch_only(tmp_path):
    """Temp git repo where the worktree has been removed but the branch remains
    (branch-only fallback candidacy)."""
    root = tmp_path / "repo_branch_only"
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
    wt_path = wt_parent / "issue-1523-branch-only"
    _git("worktree", "add", "-q", "-b", "issue-1523-branch-only", str(wt_path), "main", cwd=root)
    branch_tip = _rev_parse(root, "refs/heads/issue-1523-branch-only")
    _git("worktree", "remove", str(wt_path), cwd=root)
    assert not wt_path.exists()

    yield {
        "root": str(root),
        "worktree_path": str(wt_path),
        "branch_name": "issue-1523-branch-only",
        "branch_tip": branch_tip,
    }


@pytest.fixture
def repo_normal_worktree(tmp_path):
    """Temp git repo with a dedicated worktree still present (normal-cleanup shape)."""
    root = tmp_path / "repo_normal"
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
    wt_path = wt_parent / "issue-1523-normal"
    _git("worktree", "add", "-q", "-b", "issue-1523-normal", str(wt_path), "main", cwd=root)
    branch_tip = _rev_parse(root, "refs/heads/issue-1523-normal")

    yield {
        "root": str(root),
        "worktree_path": str(wt_path),
        "branch_name": "issue-1523-normal",
        "branch_tip": branch_tip,
    }

    if wt_path.exists():
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
        )


# ─── AC7: branch-tip compare-and-delete race guard ────────────────────────────


class TestBranchOnlyCompareAndDeleteRaceGuard:
    def test_branch_only_fallback_compare_and_delete_refuses_tip_change(self, repo_branch_only):
        """GIVEN branch tip moved after authorization WHEN force-delete
        attempted THEN branch_tip_changed, branch survives."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok_b, reason_b, verified_b = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )
        assert ok_b is True, f"expected branch-only authorization to succeed: {reason_b} {verified_b}"
        expected_oid = verified_b["local_branch_tip_oid"]
        assert expected_oid == repo["branch_tip"]

        # Simulate a race: move the branch tip AFTER authorization captured the
        # expected OID, but BEFORE the destructive delete runs.
        _git("update-ref", f"refs/heads/{repo['branch_name']}",
             _rev_parse(repo["root"], "HEAD"), cwd=repo["root"])
        # (HEAD == the same seed commit here; force a genuinely different SHA
        # by committing an empty commit on a scratch ref and re-pointing.)
        _git("commit", "--allow-empty", "-q", "-m", "race: branch moved", cwd=repo["root"])
        moved_tip = _rev_parse(repo["root"], "HEAD")
        _git("update-ref", f"refs/heads/{repo['branch_name']}", moved_tip, cwd=repo["root"])
        assert moved_tip != expected_oid

        actions, error = _perform_branch_only(
            repo["branch_name"], expected_oid, "main", repo["root"], Deadline(30.0)
        )

        assert actions == []
        assert error == BRANCH_TIP_CHANGED
        live_tip = _rev_parse(repo["root"], f"refs/heads/{repo['branch_name']}")
        assert live_tip == moved_tip, "branch must survive the refused race"

    def test_branch_only_fallback_succeeds_when_tip_unchanged(self, repo_branch_only):
        """GIVEN branch tip unchanged WHEN compare-and-delete attempted THEN branch deleted via update-ref."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok_b, reason_b, verified_b = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )
        assert ok_b is True

        actions, error = _perform_branch_only(
            repo["branch_name"], verified_b["local_branch_tip_oid"], "main", repo["root"], Deadline(30.0)
        )

        assert error is None
        assert actions == [OP_BRANCH_DELETE]
        check = subprocess.run(
            ["git", "-C", repo["root"], "rev-parse", "--verify", f"refs/heads/{repo['branch_name']}"],
            capture_output=True, text=True,
        )
        assert check.returncode != 0, "branch must be deleted"


# ─── AC8: structural ancestry gate ─────────────────────────────────────────────


class TestBranchOnlyAncestryStructuralGate:
    def test_branch_only_fallback_refuses_invalid_object_and_non_ancestry_errors(self, repo_branch_only):
        """GIVEN an invalid/unresolvable expected OID WHEN ancestry gate runs THEN non_ancestry_failure."""
        repo = repo_branch_only
        deadline = Deadline(30.0)

        # (1) A syntactically SHA-like but non-existent commit object.
        bogus_oid = "f" * 40
        ok, reason = _verify_ancestry_for_force_delete(repo["root"], bogus_oid, "main", deadline)
        assert ok is False
        assert reason == BRANCH_ONLY_NON_ANCESTRY_FAILURE

        # (2) None expected OID.
        ok_none, reason_none = _verify_ancestry_for_force_delete(repo["root"], None, "main", deadline)
        assert ok_none is False
        assert reason_none == BRANCH_ONLY_NON_ANCESTRY_FAILURE

        # (3) A real commit object, but an invalid comparison ref (git error,
        # not a definite ancestor/non-ancestor exit code).
        real_oid = _rev_parse(repo["root"], "refs/heads/" + repo["branch_name"])
        ok_badref, reason_badref = _verify_ancestry_for_force_delete(
            repo["root"], real_oid, "refs/heads/does-not-exist-at-all", deadline
        )
        assert ok_badref is False
        assert reason_badref == BRANCH_ONLY_NON_ANCESTRY_FAILURE

        # And the perform-level function must never invoke the destructive
        # delete for any of these — actions_taken stays empty.
        actions, error = _perform_branch_only(
            repo["branch_name"], bogus_oid, "main", repo["root"], deadline
        )
        assert actions == []
        assert error == BRANCH_TIP_CHANGED  # live tip != bogus expected_oid, refused before ancestry check

    def test_ancestry_gate_accepts_ordinary_ancestor(self, repo_branch_only):
        """GIVEN expected OID is a plain ancestor of main WHEN gate runs THEN authorized (exit code 0)."""
        real_oid = _rev_parse(repo_branch_only["root"], "refs/heads/" + repo_branch_only["branch_name"])
        ok, reason = _verify_ancestry_for_force_delete(
            repo_branch_only["root"], real_oid, "main", Deadline(30.0)
        )
        assert ok is True
        assert reason is None


# ─── AC9: verified payload schema parity across lanes ──────────────────────────


class TestVerifiedPayloadSchemaParity:
    def test_branch_only_fallback_preserves_verified_payload_schema(self, repo_normal_worktree, repo_branch_only):
        """GIVEN normal-cleanup and branch-only verifiers WHEN both run THEN verified key set + types match."""
        template_keys = set(_verified_template().keys())

        normal_repo = repo_normal_worktree
        normal_req = _make_req(normal_repo)
        normal_pr = _make_merged_pr(normal_repo["branch_name"], normal_repo["branch_tip"])
        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=normal_pr),
        ):
            ok_n, _reason_n, verified_n = verify_cleanup_authorization(
                normal_req, normal_repo["root"], Deadline(30.0)
            )
        assert ok_n is True
        assert set(verified_n.keys()) == template_keys

        bo_repo = repo_branch_only
        bo_req = _make_req(bo_repo)
        bo_pr = _make_merged_pr(bo_repo["branch_name"], bo_repo["branch_tip"])
        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=bo_pr),
        ):
            ok_b, _reason_b, verified_b = verify_branch_only_cleanup_authorization(
                bo_req, bo_repo["root"], Deadline(30.0)
            )
        assert ok_b is True
        assert set(verified_b.keys()) == template_keys

        # Same key SET across both lanes (AC9's core assertion).
        assert set(verified_n.keys()) == set(verified_b.keys())

        # Same declared TYPE SHAPE per key across both lanes. Boolean flag
        # keys must always be exactly ``bool`` in both lanes (never
        # populated/unpopulated as None); optional str/int/list keys may
        # legitimately be their concrete type in one lane and ``None`` in the
        # other run (e.g. a key only ever populated by ONE lane), but must
        # never be a DIFFERENT concrete type across lanes.
        bool_keys = {k for k, v in _verified_template().items() if v is False}
        for key in bool_keys:
            assert isinstance(verified_n[key], bool), (
                f"{key!r} must be bool in normal lane, got {type(verified_n[key])}"
            )
            assert isinstance(verified_b[key], bool), (
                f"{key!r} must be bool in branch-only lane, got {type(verified_b[key])}"
            )
        optional_keys = template_keys - bool_keys
        for key in optional_keys:
            type_n = type(verified_n[key])
            type_b = type(verified_b[key])
            compatible = (
                type_n == type_b
                or verified_n[key] is None
                or verified_b[key] is None
            )
            assert compatible, f"incompatible type shape for {key!r}: normal={type_n} branch_only={type_b}"


# ─── AC10: same-invocation fallback refusal matrix ─────────────────────────────


class TestSameInvocationFallbackRefusalMatrix:
    def test_same_invocation_fallback_refusals_preserve_partial_actions_and_branch(
        self, repo_normal_worktree, monkeypatch
    ):
        """GIVEN worktree_remove succeeds but branch -d fails, and re-authorization is refused,
        WHEN run() called THEN actions_taken == [worktree_remove] only, branch survives,
        force-delete subprocess never invoked, original reason_code preserved."""
        repo = repo_normal_worktree
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        original_git = _ce._git
        captured_update_ref_calls: list[list[str]] = []

        def spy_git(args, deadline, maximum=10.0):
            if "update-ref" in args:
                captured_update_ref_calls.append(list(args))
            if args[:3] == ["-C", repo["root"], "branch"] and "-d" in args and "-D" not in args:
                # Force the plain (non-force) branch -d step to fail, as if git
                # refuses because it does not consider the branch merged
                # (e.g. squash-merge scenario).
                class _Fail:
                    returncode = 1
                    stdout = ""
                    stderr = "error: The branch is not fully merged."
                return _Fail()
            return original_git(args, deadline, maximum)

        monkeypatch.setattr(_ce, "_git", spy_git)

        # Force re-authorization (verify_branch_only_cleanup_authorization) to
        # be REFUSED (e.g. linked-issue mismatch) so the refusal-matrix path is
        # exercised — the ORIGINAL branch_delete_failed reason must be
        # preserved and no force-delete subprocess must run.
        def refuse_reauth(req_arg, root_arg, deadline_arg):
            return False, "linked_issue_mismatch", _verified_template()

        monkeypatch.setattr(_ce, "verify_branch_only_cleanup_authorization", refuse_reauth)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "error"
        assert result["reason_code"].startswith("branch_delete_failed")
        assert result["actions_taken"] == [OP_WORKTREE_REMOVE]
        assert captured_update_ref_calls == [], "force-delete subprocess must never be invoked on refusal"
        # Branch survives (worktree_remove already ran and removed the
        # worktree directory, but the branch ref itself must remain).
        check = subprocess.run(
            ["git", "-C", repo["root"], "rev-parse", "--verify", f"refs/heads/{repo['branch_name']}"],
            capture_output=True, text=True,
        )
        assert check.returncode == 0, "branch must survive when re-authorization is refused"

    def test_same_invocation_fallback_succeeds_when_reauthorized_and_non_ancestor(
        self, repo_normal_worktree, monkeypatch
    ):
        """GIVEN worktree_remove succeeds, branch -d fails, re-authorization SUCCEEDS, AND
        merge-base --is-ancestor structurally confirms NON-ancestry (exit 1, the genuine
        squash-merge shape this lane exists for) WHEN run() called THEN status ok and
        actions include worktree_remove + branch_delete (fix_delta P1-3)."""
        repo = repo_normal_worktree
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        original_git = _ce._git

        def spy_git(args, deadline, maximum=10.0):
            if args[:3] == ["-C", repo["root"], "branch"] and "-d" in args and "-D" not in args:
                class _Fail:
                    returncode = 1
                    stdout = ""
                    stderr = "error: The branch is not fully merged."
                return _Fail()
            if "merge-base" in args and "--is-ancestor" in args:
                # Simulate the genuine squash-merge shape: NOT a git-visible ancestor.
                class _NonAncestor:
                    returncode = 1
                    stdout = ""
                    stderr = ""
                return _NonAncestor()
            return original_git(args, deadline, maximum)

        monkeypatch.setattr(_ce, "_git", spy_git)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "ok", result
        assert result["actions_taken"] == [OP_WORKTREE_REMOVE, OP_BRANCH_DELETE]
        check = subprocess.run(
            ["git", "-C", repo["root"], "rev-parse", "--verify", f"refs/heads/{repo['branch_name']}"],
            capture_output=True, text=True,
        )
        assert check.returncode != 0, "branch must be deleted on successful re-authorization"

    def test_same_invocation_fallback_refused_when_ancestry_exit_code_zero(
        self, repo_normal_worktree, monkeypatch
    ):
        """Fix_delta P1-3: GIVEN worktree_remove succeeds, branch -d fails, AND
        merge-base --is-ancestor returns exit code 0 (mergedness IS structurally
        established) WHEN run() called THEN the SAME-INVOCATION path must NOT escalate
        to force-delete — the original branch -d failure was NOT "not fully merged" and
        must not be papered over. Result preserves the ORIGINAL branch_delete_failed
        reason code, actions_taken stays worktree_remove-only, the branch survives, and
        no update-ref -d subprocess is ever invoked."""
        repo = repo_normal_worktree
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        original_git = _ce._git
        captured_update_ref_calls: list[list[str]] = []

        def spy_git(args, deadline, maximum=10.0):
            if "update-ref" in args:
                captured_update_ref_calls.append(list(args))
            if args[:3] == ["-C", repo["root"], "branch"] and "-d" in args and "-D" not in args:
                class _Fail:
                    returncode = 1
                    stdout = ""
                    stderr = "error: The branch is not fully merged."
                return _Fail()
            # Default (real) git resolves merge-base --is-ancestor for this
            # fixture's real-merge branch tip to exit code 0 (ordinary ancestor).
            return original_git(args, deadline, maximum)

        monkeypatch.setattr(_ce, "_git", spy_git)

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            result = run(req, project_root=repo["root"])

        assert result["status"] == "error"
        assert result["reason_code"].startswith("branch_delete_failed")
        assert result["actions_taken"] == [OP_WORKTREE_REMOVE]
        assert captured_update_ref_calls == [], (
            "force-delete subprocess must never be invoked when ancestry exit code is 0"
        )

        check = subprocess.run(
            ["git", "-C", repo["root"], "rev-parse", "--verify", f"refs/heads/{repo['branch_name']}"],
            capture_output=True, text=True,
        )
        assert check.returncode == 0, "branch must survive when same-invocation ancestry is not confirmed"



# ─── P0-3: in-lock worktree catalog / branch-usage re-check ───────────────────


class TestBranchOnlyInLockCatalogRecheck:
    def test_branch_only_delete_refused_when_branch_checked_out_between_authorization_and_lock(
        self, repo_branch_only, monkeypatch
    ):
        """Fix_delta P0-3: GIVEN the pre-lock authorization check passed, but another
        worktree checks out the SAME branch immediately after the shared mutation lock is
        acquired (and before the delete), WHEN ``_perform_branch_only`` runs THEN the
        delete is refused, the branch ref survives, and the new worktree remains intact."""
        repo = repo_branch_only
        req = _make_req(repo)
        fake_pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])

        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=fake_pr),
        ):
            ok_b, reason_b, verified_b = verify_branch_only_cleanup_authorization(
                req, repo["root"], Deadline(30.0)
            )
        assert ok_b is True, f"expected branch-only authorization to succeed: {reason_b} {verified_b}"
        expected_oid = verified_b["local_branch_tip_oid"]

        other_wt = Path(repo["root"]) / ".claude" / "worktrees" / "issue-1523-branch-only-racer"

        def race_hook(project_root, branch_name):
            # Simulate another process checking the SAME branch out into a NEW
            # worktree in the window between pre-lock authorization and the
            # in-lock delete.
            _git("worktree", "add", "-q", str(other_wt), branch_name, cwd=project_root)

        monkeypatch.setattr(_ce, "_BRANCH_ONLY_RACE_HOOK", race_hook)
        try:
            actions, error = _perform_branch_only(
                repo["branch_name"], expected_oid, "main", repo["root"], Deadline(30.0)
            )
        finally:
            monkeypatch.setattr(_ce, "_BRANCH_ONLY_RACE_HOOK", None)

        assert actions == []
        assert error == _ce.BRANCH_CHECKED_OUT_IN_WORKTREE
        live_tip = _rev_parse(repo["root"], f"refs/heads/{repo['branch_name']}")
        assert live_tip == expected_oid, "branch ref must survive"
        assert other_wt.exists(), "the racing worktree must remain intact"


# ─── P1-1: fail-closed mutation lock ───────────────────────────────────────────


class TestMutationLockFailsClosed:
    def test_lock_raises_when_fcntl_unavailable(self, repo_branch_only, monkeypatch):
        """GIVEN fcntl is unavailable (simulated) WHEN the mutation lock is entered
        THEN MutationLockError is raised (fail-closed, never silently proceeds)."""
        monkeypatch.setattr(_ce, "fcntl", None)
        with pytest.raises(_ce.MutationLockError) as exc_info:
            with _ce._mutation_lock(repo_branch_only["root"]):
                pytest.fail("must not yield when fcntl is unavailable")
        assert exc_info.value.reason_code == _ce.MUTATION_LOCK_FAILED

    def test_lock_raises_when_lock_file_open_fails(self, repo_branch_only, monkeypatch):
        """GIVEN the lock file cannot be opened WHEN the mutation lock is entered
        THEN MutationLockError is raised (fail-closed)."""
        def boom(*args, **kwargs):
            raise OSError("simulated open failure")

        monkeypatch.setattr(_ce.os, "open", boom)
        with pytest.raises(_ce.MutationLockError):
            with _ce._mutation_lock(repo_branch_only["root"]):
                pytest.fail("must not yield when the lock file cannot be opened")

    def test_lock_raises_when_flock_exhausts_retry_budget(self, repo_branch_only, monkeypatch):
        """GIVEN flock() always raises (another holder has the lock) WHEN the bounded
        retry budget is exhausted THEN MutationLockError is raised (fail-closed, never
        proceeds without a held lock)."""
        def always_blocked(fd, flags):
            raise BlockingIOError("simulated: another process holds the lock")

        monkeypatch.setattr(_ce.fcntl, "flock", always_blocked)
        with pytest.raises(_ce.MutationLockError):
            with _ce._mutation_lock(repo_branch_only["root"], Deadline(0.2)):
                pytest.fail("must not yield when flock never succeeds")

    def test_lock_uses_git_common_dir_not_hardcoded_git_path(self, repo_branch_only):
        """Fix_delta P1-1: the lock path is resolved via
        ``git rev-parse --git-common-dir`` (repository-identity-correct, works for
        linked worktrees), not a hardcoded ``<root>/.git/...`` path."""
        path = _ce._resolve_mutation_lock_path(repo_branch_only["root"], Deadline(10.0))
        assert path == str(Path(repo_branch_only["root"]) / ".git" / _ce._MUTATION_LOCK_FILENAME)

    def test_normal_lock_acquire_and_release_succeeds(self, repo_branch_only):
        """GIVEN a healthy environment WHEN the mutation lock is entered and exited
        THEN no exception is raised and the body executes exactly once."""
        calls = []
        with _ce._mutation_lock(repo_branch_only["root"], Deadline(10.0)):
            calls.append(1)
        assert calls == [1]


# ─── Issue #2508: research-issue (closing-keyword-not-used) linked-issue
#     fallback regression coverage ──────────────────────────────────────────
#
# AC1: canonical ``closingIssuesReferences`` fast path unchanged + no extra fetch
# AC2/AC3: research fallback authorizes normal + branch-only lanes
# AC4: exact ``Refs #N`` match / missing / wrong number / prefix collision
# AC5: linked Issue OPEN / NOT_PLANNED / unknown stateReason rejected
# AC6: linked Issue issue_kind != research rejected (no fallback for implementation)
# AC7: missing/malformed MRC rejected
# AC8: gh command failure / malformed JSON fail-closed
# AC9: _pr_state() requests body; linked-issue fetch pins trusted repo_slug
# AC10: verify_cleanup_authorization / verify_branch_only_cleanup_authorization
#       consolidated onto the same shared _verify_linked_issue helper
# AC11: existing closing-keyword behavior unchanged (covered by the pre-existing
#       test classes above, which all still exercise the canonical fast path)


class TestPrBodyExactRefs:
    """AC4: small deterministic ``Refs #<N>`` matcher — exact match only."""

    def test_exact_refs_match(self):
        assert _pr_body_has_exact_refs("See also. Refs #2101\nmore text.", 2101) is True

    def test_missing_refs_rejected(self):
        assert _pr_body_has_exact_refs("no refs mentioned here", 2101) is False

    def test_wrong_issue_number_rejected(self):
        assert _pr_body_has_exact_refs("Refs #9999", 2101) is False

    def test_prefix_collision_rejected(self):
        """``Refs #21010`` must NOT match a target of ``#2101``."""
        assert _pr_body_has_exact_refs("Refs #21010", 2101) is False

    def test_empty_body_rejected(self):
        assert _pr_body_has_exact_refs("", 2101) is False


class TestLinkedIssueStateFailClosed:
    """AC8: gh command failure / malformed JSON fail-closed to None."""

    def test_gh_command_failure_returns_none(self, monkeypatch):
        def fake_run(args, cwd, capture_output, text, timeout):
            class _Result:
                returncode = 1
                stdout = ""
            return _Result()

        monkeypatch.setattr(_ce.subprocess, "run", fake_run)
        monkeypatch.setattr(_ce.shutil, "which", lambda name: "/usr/bin/gh")
        assert _ce._linked_issue_state(2101, "/root", "squne121/loop-protocol", Deadline(10.0)) is None

    def test_malformed_json_response_returns_none(self, monkeypatch):
        def fake_run(args, cwd, capture_output, text, timeout):
            class _Result:
                returncode = 0
                stdout = "{not valid json"
            return _Result()

        monkeypatch.setattr(_ce.subprocess, "run", fake_run)
        monkeypatch.setattr(_ce.shutil, "which", lambda name: "/usr/bin/gh")
        assert _ce._linked_issue_state(2101, "/root", "squne121/loop-protocol", Deadline(10.0)) is None

    def test_gh_not_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(_ce.shutil, "which", lambda name: None)
        assert _ce._linked_issue_state(2101, "/root", "squne121/loop-protocol", Deadline(10.0)) is None


class TestLinkedIssueFetchPinsTrustedRepoSlug:
    """AC9: the linked-issue fetch helper pins ``--repo`` to the trusted repo_slug."""

    def test_linked_issue_fetch_pins_repo_flag(self, monkeypatch):
        captured = {}

        def fake_run(args, cwd, capture_output, text, timeout):
            captured["args"] = args
            class _Result:
                returncode = 0
                stdout = json.dumps({"state": "CLOSED", "stateReason": "COMPLETED", "body": ""})
            return _Result()

        monkeypatch.setattr(_ce.subprocess, "run", fake_run)
        monkeypatch.setattr(_ce.shutil, "which", lambda name: "/usr/bin/gh")
        _ce._linked_issue_state(2101, "/root", "squne121/loop-protocol", Deadline(10.0))

        args = captured["args"]
        assert "--repo" in args
        repo_idx = args.index("--repo")
        assert args[repo_idx + 1] == "squne121/loop-protocol", (
            "the linked-issue fetch MUST pin --repo to the trusted repo_slug, "
            "never derive it from untrusted input"
        )


class TestPrStateRequestsBody:
    """AC9: production _pr_state() must request `body` via --json (real command args)."""

    def test_pr_state_requests_body_field(self, monkeypatch):
        captured = {}

        def fake_run(args, cwd, capture_output, text, timeout):
            captured["args"] = args
            class _Result:
                returncode = 0
                stdout = json.dumps({"state": "MERGED"})
            return _Result()

        monkeypatch.setattr(_ce.subprocess, "run", fake_run)
        monkeypatch.setattr(_ce.shutil, "which", lambda name: "/usr/bin/gh")
        _ce._pr_state(123, "/root", "squne121/loop-protocol", Deadline(10.0))

        args = captured["args"]
        json_idx = args.index("--json")
        json_fields = args[json_idx + 1].split(",")
        assert "body" in json_fields, (
            "production _pr_state() must request the real 'body' field, not "
            "just add it to test fixtures/mocks"
        )


class TestResearchFallbackAuthorized:
    """AC2/AC5/AC6/AC7: the 3 required research fallback conditions, all-must-hold."""

    @staticmethod
    def _pr(body: str) -> dict:
        return {"body": body, "closingIssuesReferences": []}

    def test_success_all_conditions_met(self, monkeypatch):
        monkeypatch.setattr(
            _ce, "_linked_issue_state",
            lambda *a, **k: {
                "state": "CLOSED", "stateReason": "COMPLETED",
                "body": _make_research_issue_mrc("research"),
            },
        )
        pr = self._pr("Refs #2101")
        assert _research_fallback_authorized(2101, pr, "/root", "squne121/loop-protocol", Deadline(10.0)) is True

    def test_open_state_rejected(self, monkeypatch):
        monkeypatch.setattr(
            _ce, "_linked_issue_state",
            lambda *a, **k: {
                "state": "OPEN", "stateReason": None,
                "body": _make_research_issue_mrc("research"),
            },
        )
        pr = self._pr("Refs #2101")
        assert _research_fallback_authorized(2101, pr, "/root", "s/r", Deadline(10.0)) is False

    def test_not_planned_state_reason_rejected(self, monkeypatch):
        monkeypatch.setattr(
            _ce, "_linked_issue_state",
            lambda *a, **k: {
                "state": "CLOSED", "stateReason": "NOT_PLANNED",
                "body": _make_research_issue_mrc("research"),
            },
        )
        pr = self._pr("Refs #2101")
        assert _research_fallback_authorized(2101, pr, "/root", "s/r", Deadline(10.0)) is False

    def test_unknown_state_reason_rejected(self, monkeypatch):
        monkeypatch.setattr(
            _ce, "_linked_issue_state",
            lambda *a, **k: {
                "state": "CLOSED", "stateReason": "DUPLICATE",
                "body": _make_research_issue_mrc("research"),
            },
        )
        pr = self._pr("Refs #2101")
        assert _research_fallback_authorized(2101, pr, "/root", "s/r", Deadline(10.0)) is False

    def test_implementation_issue_kind_rejected(self, monkeypatch):
        """AC6: issue_kind: implementation must not benefit from the fallback."""
        monkeypatch.setattr(
            _ce, "_linked_issue_state",
            lambda *a, **k: {
                "state": "CLOSED", "stateReason": "COMPLETED",
                "body": _make_research_issue_mrc("implementation"),
            },
        )
        pr = self._pr("Refs #2101")
        assert _research_fallback_authorized(2101, pr, "/root", "s/r", Deadline(10.0)) is False

    def test_missing_mrc_rejected(self, monkeypatch):
        monkeypatch.setattr(
            _ce, "_linked_issue_state",
            lambda *a, **k: {
                "state": "CLOSED", "stateReason": "COMPLETED",
                "body": "just prose, no MRC section at all",
            },
        )
        pr = self._pr("Refs #2101")
        assert _research_fallback_authorized(2101, pr, "/root", "s/r", Deadline(10.0)) is False

    def test_malformed_mrc_rejected(self, monkeypatch):
        malformed_body = (
            "## Machine-Readable Contract\n\n"
            "```yaml\n"
            "issue_kind: [unterminated\n"
            "```\n"
        )
        monkeypatch.setattr(
            _ce, "_linked_issue_state",
            lambda *a, **k: {
                "state": "CLOSED", "stateReason": "COMPLETED", "body": malformed_body,
            },
        )
        pr = self._pr("Refs #2101")
        assert _research_fallback_authorized(2101, pr, "/root", "s/r", Deadline(10.0)) is False

    def test_missing_refs_short_circuits_before_fetch(self, monkeypatch):
        """A body without a matching Refs must fail closed WITHOUT fetching the linked Issue."""
        calls = []

        def spy(*a, **k):
            calls.append((a, k))
            return {"state": "CLOSED", "stateReason": "COMPLETED", "body": _make_research_issue_mrc("research")}

        monkeypatch.setattr(_ce, "_linked_issue_state", spy)
        pr = self._pr("no refs here at all")
        assert _research_fallback_authorized(2101, pr, "/root", "s/r", Deadline(10.0)) is False
        assert calls == [], "the linked-issue fetch must not run when Refs #N is absent"

    def test_prefix_collision_short_circuits_before_fetch(self, monkeypatch):
        calls = []

        def spy(*a, **k):
            calls.append((a, k))
            return {"state": "CLOSED", "stateReason": "COMPLETED", "body": _make_research_issue_mrc("research")}

        monkeypatch.setattr(_ce, "_linked_issue_state", spy)
        pr = self._pr("Refs #21010")
        assert _research_fallback_authorized(2101, pr, "/root", "s/r", Deadline(10.0)) is False
        assert calls == []

    def test_issue_fetch_failure_rejected(self, monkeypatch):
        monkeypatch.setattr(_ce, "_linked_issue_state", lambda *a, **k: None)
        pr = self._pr("Refs #2101")
        assert _research_fallback_authorized(2101, pr, "/root", "s/r", Deadline(10.0)) is False


class TestVerifyLinkedIssueSharedHelper:
    """AC1/AC10: shared linked-issue determination helper semantics."""

    def test_canonical_fast_path_no_fallback_fetch(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            _ce, "_research_fallback_authorized",
            lambda *a, **k: calls.append(1) or True,
        )
        pr = {"closingIssuesReferences": [{"number": 2101}], "body": ""}
        req = {"linked_issue_number": 2101}
        ok, reason = _verify_linked_issue(req, pr, "/root", "s/r", Deadline(10.0))
        assert ok is True
        assert reason is None
        assert calls == [], "the canonical fast path must never attempt the research fallback"

    def test_research_fallback_used_when_canonical_absent(self, monkeypatch):
        monkeypatch.setattr(_ce, "_research_fallback_authorized", lambda *a, **k: True)
        pr = {"closingIssuesReferences": [], "body": "Refs #2101"}
        req = {"linked_issue_number": 2101}
        ok, reason = _verify_linked_issue(req, pr, "/root", "s/r", Deadline(10.0))
        assert ok is True
        assert reason is None

    def test_fail_closed_when_neither_canonical_nor_fallback(self, monkeypatch):
        monkeypatch.setattr(_ce, "_research_fallback_authorized", lambda *a, **k: False)
        pr = {"closingIssuesReferences": [], "body": ""}
        req = {"linked_issue_number": 2101}
        ok, reason = _verify_linked_issue(req, pr, "/root", "s/r", Deadline(10.0))
        assert ok is False
        assert reason == LINKED_ISSUE_MISMATCH

    def test_no_linked_issue_requested_is_a_noop(self):
        ok, reason = _verify_linked_issue({"linked_issue_number": None}, {}, "/root", "s/r", Deadline(10.0))
        assert ok is True
        assert reason is None

    def test_both_verifiers_invoke_same_shared_helper(self, repo_normal_worktree, repo_branch_only):
        """AC10: verify_cleanup_authorization and verify_branch_only_cleanup_authorization
        both invoke the SAME _verify_linked_issue helper (call-count check)."""
        normal_repo = repo_normal_worktree
        normal_req = _make_req(normal_repo)
        normal_pr = _make_merged_pr(normal_repo["branch_name"], normal_repo["branch_tip"])

        bo_repo = repo_branch_only
        bo_req = _make_req(bo_repo)
        bo_pr = _make_merged_pr(bo_repo["branch_name"], bo_repo["branch_tip"])

        with patch.object(_ce, "_verify_linked_issue", side_effect=_ce._verify_linked_issue) as spy:
            with (
                patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
                patch.object(_ce, "_pr_state", return_value=normal_pr),
            ):
                ok_n, reason_n, _verified_n = verify_cleanup_authorization(
                    normal_req, normal_repo["root"], Deadline(30.0)
                )
            with (
                patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
                patch.object(_ce, "_pr_state", return_value=bo_pr),
            ):
                ok_b, reason_b, _verified_b = verify_branch_only_cleanup_authorization(
                    bo_req, bo_repo["root"], Deadline(30.0)
                )

        assert ok_n is True, reason_n
        assert ok_b is True, reason_b
        assert spy.call_count == 2, "both verifiers must invoke the SAME shared _verify_linked_issue helper"


class TestResearchFallbackIntegration:
    """AC1/AC2/AC3: end-to-end authorization wiring through the real verifiers."""

    def test_canonical_fast_path_end_to_end_no_fallback_fetch(self, repo_normal_worktree):
        """AC1: the pre-existing closingIssuesReferences fast path is unchanged
        and performs NO additional linked-issue fetch."""
        repo = repo_normal_worktree
        req = _make_req(repo)
        pr = _make_merged_pr(repo["branch_name"], repo["branch_tip"])  # default: linked_issue in refs
        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=pr),
            patch.object(_ce, "_linked_issue_state") as fetch_mock,
        ):
            ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))
        assert ok is True, f"{reason} {verified}"
        assert verified["linked_issue_match"] is True
        fetch_mock.assert_not_called()

    def test_normal_lane_research_fallback_allows_cleanup(self, repo_normal_worktree):
        """AC2: normal cleanup lane authorizes via research fallback when
        closingIssuesReferences is empty but all 3 fallback conditions hold."""
        repo = repo_normal_worktree
        linked_issue = 2101
        req = _make_req(repo, linked_issue=linked_issue)
        pr = _make_merged_pr(
            repo["branch_name"], repo["branch_tip"],
            closing_issue_refs=[], body=f"Refs #{linked_issue}",
        )
        fake_issue = {
            "state": "CLOSED", "stateReason": "COMPLETED",
            "body": _make_research_issue_mrc("research"),
        }
        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=pr),
            patch.object(_ce, "_linked_issue_state", return_value=fake_issue) as fetch_mock,
        ):
            ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))
        assert ok is True, f"expected research fallback to authorize: {reason} {verified}"
        assert verified["linked_issue_match"] is True
        fetch_mock.assert_called_once_with(linked_issue, repo["root"], "squne121/loop-protocol", ANY)

    def test_branch_only_lane_research_fallback_allows_cleanup(self, repo_branch_only):
        """AC3: branch-only cleanup lane authorizes via the SAME research fallback condition."""
        repo = repo_branch_only
        linked_issue = 2101
        req = _make_req(repo, linked_issue=linked_issue)
        pr = _make_merged_pr(
            repo["branch_name"], repo["branch_tip"],
            closing_issue_refs=[], body=f"Refs #{linked_issue}",
        )
        fake_issue = {
            "state": "CLOSED", "stateReason": "COMPLETED",
            "body": _make_research_issue_mrc("research"),
        }
        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=pr),
            patch.object(_ce, "_linked_issue_state", return_value=fake_issue),
        ):
            ok, reason, verified = verify_branch_only_cleanup_authorization(req, repo["root"], Deadline(30.0))
        assert ok is True, f"expected research fallback to authorize: {reason} {verified}"
        assert verified["linked_issue_match"] is True

    def test_implementation_issue_kind_no_fallback_keeps_closing_reference_requirement(
        self, repo_normal_worktree
    ):
        """AC6: an issue_kind: implementation linked Issue does not benefit from the
        fallback even with Refs #N + CLOSED/COMPLETED — the existing closingIssuesReferences
        requirement is preserved (rejected)."""
        repo = repo_normal_worktree
        linked_issue = 2101
        req = _make_req(repo, linked_issue=linked_issue)
        pr = _make_merged_pr(
            repo["branch_name"], repo["branch_tip"],
            closing_issue_refs=[], body=f"Refs #{linked_issue}",
        )
        fake_issue = {
            "state": "CLOSED", "stateReason": "COMPLETED",
            "body": _make_research_issue_mrc("implementation"),
        }
        with (
            patch.object(_ce, "_repo_slug", return_value="squne121/loop-protocol"),
            patch.object(_ce, "_pr_state", return_value=pr),
            patch.object(_ce, "_linked_issue_state", return_value=fake_issue),
        ):
            ok, reason, verified = verify_cleanup_authorization(req, repo["root"], Deadline(30.0))
        assert ok is False
        assert reason == LINKED_ISSUE_MISMATCH
        assert verified["linked_issue_match"] is False

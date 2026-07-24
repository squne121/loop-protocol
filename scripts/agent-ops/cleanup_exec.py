#!/usr/bin/env python3
"""cleanup_exec.py — single narrow cleanup authorization boundary (Issue #1137).

Per the PR #1139 OWNER review (Blocker 5), cleanup is collapsed into ONE narrow
executor instead of a self-issuable contract. ``cleanup_exec`` verifies, on every
run, that:

  1. the local root checkout is on the default branch
  2. the target worktree exists in the real ``git worktree list --porcelain -z`` catalog
  3. the worktree's branch matches the requested branch
  4. the worktree is clean (porcelain=v1 -z empty)
  5. the PR is actually merged (``gh pr view`` state == MERGED)
  6. the PR head branch matches the requested branch
  7. the linked issue matches (when supplied)

and only then performs the exact ``git worktree remove`` + ``git branch -d`` via
internal subprocess arrays (which are NOT subject to the agent PreToolUse hook).
The agent never runs bare git cleanup; it runs only ``cleanup_exec``, which the
guard allows as an exact command class.

This module also exports ``verify_cleanup_authorization`` so ``materialize_cleanup_contract``
issues the defense-in-depth V3 contract only after the same checks pass.

Branch-only lane (Issue #1196): when ``verify_cleanup_authorization`` returns
``WORKTREE_NOT_IN_CATALOG``, ``run()`` checks whether the worktree is a partial-
cleanup state (worktree removed from both disk and catalog, branch still present)
and, if so, authorizes a ``git branch -D`` branch-only cleanup.
``verify_cleanup_authorization`` is NOT changed and ``materialize_cleanup_contract``
cannot reach the branch-only verifier.

Squash-merge head-OID equivalence (Issue #1337): GitHub squash merge always mints
a brand-new commit SHA for the default branch, so ``headRefOid`` never equals the
feature branch tip even when content is identical. ``_resolve_head_equivalence()``
authorizes cleanup via delta-equivalence ONLY when the candidate merge commit is
verified to be a genuine squash-shaped commit (object exists locally AND has
EXACTLY ONE parent). Normal merge commits (2+ parents) always fail-closed to the
existing exact-OID comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cleanup_contract_v3 import (  # noqa: E402
    OP_BRANCH_DELETE,
    OP_WORKTREE_REMOVE,
    PR_NOT_MERGED,
    WORKTREE_DIRTY,
    WORKTREE_NOT_IN_CATALOG,
    WORKTREE_PATH_MISMATCH,
)
from worktree_catalog import (  # noqa: E402
    Deadline,
    GuardDeadlineExceeded,
    branch_short_name,
    find_by_realpath,
    list_worktrees,
)

SCHEMA_REQUEST = "CLEANUP_EXEC_REQUEST_V1"
SCHEMA_RESULT = "CLEANUP_EXEC_RESULT_V1"

ROOT_NOT_DEFAULT = "root_not_default_branch"
BRANCH_MISMATCH = "worktree_branch_mismatch"
LINKED_ISSUE_MISMATCH = "linked_issue_mismatch"
HEAD_BRANCH_MISMATCH = "pr_head_branch_mismatch"
# Blocker 5: bind authorization to the same repository + commit + base + head repo.
HEAD_REPO_MISMATCH = "pr_head_repo_mismatch"          # fork / cross-repo PR
BASE_BRANCH_MISMATCH = "pr_base_branch_mismatch"      # PR base != default branch
HEAD_OID_MISMATCH = "pr_head_oid_mismatch"            # PR head sha != local branch tip
REPO_SLUG_UNRESOLVED = "repo_slug_unresolved"         # cannot pin gh to the trusted repo

# Branch-only lane reason codes (Issue #1196).
# These are specific to the branch-only cleanup path and are NOT reachable via
# verify_cleanup_authorization() or materialize_cleanup_contract.
WORKTREE_STILL_IN_CATALOG = "worktree_still_in_catalog"          # worktree still in git catalog or on disk
BRANCH_CHECKED_OUT_IN_WORKTREE = "branch_checked_out_in_worktree"  # branch used by another worktree
LOCAL_BRANCH_MISSING = "local_branch_missing"                     # refs/heads/<branch> not present
BRANCH_ONLY_FORCE_DELETE_DENIED = "branch_only_force_delete_denied"  # branch-only pre-checks failed
BRANCH_ONLY_MATERIALIZE_DENIED = "branch_only_materialize_denied"    # materialize attempted branch-only


def resolve_project_root() -> str:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return os.path.realpath(env_root)
    agent_ops = os.path.dirname(os.path.realpath(__file__))
    return os.path.realpath(os.path.dirname(os.path.dirname(agent_ops)))


def _git(args: list[str], deadline: Deadline, maximum: float = 10.0) -> subprocess.CompletedProcess:
    git = shutil.which("git") or "git"
    return subprocess.run(
        [git, *args],
        capture_output=True,
        text=True,
        timeout=deadline.subprocess_timeout(maximum),
    )


def _current_branch(project_root: str, deadline: Deadline) -> str | None:
    try:
        out = _git(["-C", project_root, "rev-parse", "--abbrev-ref", "HEAD"], deadline, 5.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _default_branch(project_root: str, deadline: Deadline) -> str:
    env = os.environ.get("LOOP_DEFAULT_BRANCH", "").strip()
    if env:
        return env
    try:
        out = _git(["-C", project_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], deadline, 5.0)
        if out.returncode == 0 and out.stdout.strip():
            ref = out.stdout.strip()
            return ref.split("/", 1)[1] if "/" in ref else ref
    except (OSError, subprocess.TimeoutExpired):
        pass
    for cand in ("main", "master", "trunk"):
        try:
            out = _git(["-C", project_root, "rev-parse", "--verify", cand], deadline, 5.0)
            if out.returncode == 0:
                return cand
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "main"


def _repo_slug(project_root: str, deadline: Deadline) -> str | None:
    """Resolve OWNER/REPO from the TRUSTED project root's git remote (Blocker 5).

    The agent never supplies the repo; it is derived from the trusted root so the
    PR being checked and the worktree being deleted are the same repository.
    """
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        out = subprocess.run(
            [gh, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=project_root, capture_output=True, text=True,
            timeout=deadline.subprocess_timeout(15.0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _local_branch_tip(project_root: str, branch_name: str, deadline: Deadline) -> str | None:
    """Return the local branch tip SHA for ``branch_name`` (Blocker 5 head-oid bind)."""
    try:
        out = _git(["-C", project_root, "rev-parse", "--verify", f"refs/heads/{branch_name}"], deadline, 5.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _pr_state(pr_number: int, project_root: str, repo_slug: str | None, deadline: Deadline) -> dict | None:
    """Fetch PR state from the TRUSTED repo (cwd=root, --repo pinned). Blocker 5."""
    gh = shutil.which("gh")
    if not gh:
        return None
    args = [gh, "pr", "view", str(pr_number), "--json",
            "state,mergedAt,headRefName,headRefOid,baseRefName,"
            "headRepositoryOwner,isCrossRepository,closingIssuesReferences,"
            "mergeCommit"]
    if repo_slug:
        args += ["--repo", repo_slug]
    try:
        out = subprocess.run(
            args, cwd=project_root, capture_output=True, text=True,
            timeout=deadline.subprocess_timeout(20.0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def _commit_object_exists(project_root: str, commit_oid: str, deadline: Deadline) -> bool:
    """Return True iff ``commit_oid`` resolves to a real commit object locally (Issue #1337 P1).

    Guards against treating an unresolvable/unknown ``mergeCommit.oid`` (e.g. the
    local clone does not have the object, or GitHub returned something that is
    not actually a commit) as a squash-equivalence candidate.
    """
    try:
        out = _git(["-C", project_root, "cat-file", "-e", f"{commit_oid}^{{commit}}"], deadline, 5.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


def _commit_parents(project_root: str, commit_oid: str, deadline: Deadline) -> list[str] | None:
    """Return the parent SHAs of ``commit_oid`` via ``git rev-list --parents -n 1`` (Issue #1337 P1).

    The output is ``"<commit> [parent...]"``; the first token is the commit
    itself. Returns ``None`` on git error so callers fail-closed. A squash
    commit (or any normal single-parent commit) has exactly ONE parent; a
    normal (non-squash) merge commit has TWO OR MORE.
    """
    try:
        out = _git(["-C", project_root, "rev-list", "--parents", "-n", "1", commit_oid], deadline, 10.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    tokens = out.stdout.strip().split()
    return tokens[1:]


def _merge_bases(project_root: str, ref_a: str, ref_b: str, deadline: Deadline) -> list[str] | None:
    """Return ALL merge-base commits between ``ref_a`` and ``ref_b`` (Issue #1337 P1).

    ``git merge-base`` can report more than one best common ancestor for
    criss-crossed histories. Callers must fail-closed unless there is EXACTLY
    ONE merge-base, since the path-set computation below assumes a single,
    unambiguous origin point.
    """
    try:
        out = _git(["-C", project_root, "merge-base", "--all", ref_a, ref_b], deadline, 10.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def _squash_equivalence_path_set(
    project_root: str, merge_base: str, local_tip: str, deadline: Deadline
) -> list[str] | None:
    """Return the path set changed by the local branch (``merge_base..local_tip``).

    Issue #1337 P1 fix: uses ``git diff --name-only -z`` (NUL-separated output)
    instead of ``--name-only`` + ``splitlines()`` so filenames containing
    newlines or other special characters are handled correctly.
    """
    try:
        out = _git(
            ["-C", project_root, "diff", "--name-only", "-z", merge_base, local_tip],
            deadline, 15.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.split("\0") if p]


def _squash_content_matches(
    project_root: str, merge_commit_oid: str, local_tip: str, paths: list[str], deadline: Deadline
) -> bool | None:
    """Return True iff ``local_tip`` and ``merge_commit_oid`` agree on ``paths``.

    Restricted to ``paths`` (the local branch's own delta) so unrelated base
    changes or unrelated other-PR content never affect the comparison — this is
    the fix for the squash-merge false-positive ``pr_head_oid_mismatch`` (Issue #1337).

    Issue #1337 P1 fix: uses ``git diff --quiet --no-ext-diff`` instead of
    ``--name-only`` + empty-stdout inspection. Exit code 0 means the paths
    match, 1 means they differ, and any other exit code is a git error — the
    caller must fail-closed (``None``) rather than treat it as a mismatch or
    a match.
    """
    if not paths:
        return False
    try:
        out = _git(
            ["-C", project_root, "diff", "--quiet", "--no-ext-diff", local_tip, merge_commit_oid, "--", *paths],
            deadline, 15.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    return None


def _resolve_head_equivalence(
    pr: dict, local_tip: str | None, project_root: str, default_branch: str, deadline: Deadline,
) -> tuple[bool, dict]:
    """Authorize head OID either by exact match or squash-merge delta equivalence.

    Issue #1337: GitHub squash merge always mints a brand-new commit SHA for the
    default branch, so ``headRefOid`` (really the merge commit) never equals the
    feature branch tip even when content is identical. This resolver keeps the
    existing exact-match fail-closed behavior for non-squash merges, and ONLY
    attempts the squash-equivalence fallback when ``mergeCommit`` is present AND
    verified to be a genuine squash-shaped commit (object exists locally, exactly
    ONE parent). A normal merge commit (2+ parents) always fails closed to
    ``pr_head_oid_mismatch``, even if its ``oid`` happens to be present.

    Returns ``(authorized, additive_fields)`` where ``additive_fields`` always
    carries the four additive ``verified`` keys.

    ``default_branch`` is retained in the signature for call-site compatibility
    but is no longer used to compute the path-set origin (Issue #1337 P1 —
    the origin is now the squash commit's own single parent, not the current
    default branch tip).
    """
    del default_branch  # no longer used — origin is the squash commit's own parent (P1 fix)
    additive: dict = {
        "head_equivalence_authorized": False,
        "head_equivalence_mode": None,
        "pr_merge_commit_oid": None,
        "local_delta_paths_count": None,
    }

    head_ref_oid = pr.get("headRefOid")
    if local_tip and head_ref_oid and head_ref_oid == local_tip:
        # Issue #1337 P2 fix: exact OID match is a literal comparison, not a
        # squash-equivalence authorization — keep head_equivalence_authorized
        # False and record the mode as exact_oid for diagnostics clarity.
        additive["head_equivalence_mode"] = "exact_oid"
        return True, additive

    # Exact match failed. Only attempt the squash-equivalence fallback when
    # mergeCommit is present — missing/null mergeCommit keeps the existing
    # fail-closed pr_head_oid_mismatch rejection (Issue #1337 AC8).
    merge_commit = pr.get("mergeCommit")
    merge_commit_oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    additive["pr_merge_commit_oid"] = merge_commit_oid
    if not local_tip or not merge_commit_oid:
        return False, additive

    # Issue #1337 P1 fix: verify the merge commit object actually exists
    # locally before treating it as a squash-equivalence candidate.
    if not _commit_object_exists(project_root, merge_commit_oid, deadline):
        return False, additive

    # Issue #1337 P1 fix: only commits with EXACTLY ONE parent are
    # squash-equivalence candidates. A normal merge commit (2+ parents) must
    # fail-closed rather than fall back to delta-equivalence.
    parents = _commit_parents(project_root, merge_commit_oid, deadline)
    if not parents or len(parents) != 1:
        return False, additive
    squash_parent = parents[0]

    # Issue #1337 P1 fix: compute the path-set origin from the squash commit's
    # own single parent, not the current default branch tip (which is
    # default-branch-dependent and ambiguous with rewritten/rebased history).
    # If more than one merge-base is reported, fail-closed rather than guess.
    bases = _merge_bases(project_root, squash_parent, local_tip, deadline)
    if not bases or len(bases) != 1:
        return False, additive
    merge_base = bases[0]

    paths = _squash_equivalence_path_set(project_root, merge_base, local_tip, deadline)
    if paths is None:
        return False, additive
    additive["local_delta_paths_count"] = len(paths)
    if not paths:
        # No local delta relative to merge_base — nothing to authorize on.
        return False, additive
    content_match = _squash_content_matches(project_root, merge_commit_oid, local_tip, paths, deadline)
    if content_match is not True:
        return False, additive

    additive["head_equivalence_authorized"] = True
    additive["head_equivalence_mode"] = "squash_merge_delta_match"
    return True, additive


def verify_cleanup_authorization(req: dict, project_root: str, deadline: Deadline) -> tuple[bool, str | None, dict]:
    """Run all authorization checks. Returns (ok, reason_code, verified).

    This function is intentionally NOT changed to support branch-only cleanup.
    materialize_cleanup_contract calls this function; it must never reach the
    branch-only verifier.  Branch-only logic is in run() only.
    """
    verified = {
        "root_default": False,
        "worktree_in_catalog": False,
        "branch_match": False,
        "worktree_clean": False,
        "pr_merged": False,
        "head_branch_match": False,
        "linked_issue_match": False,
        "head_repo_match": False,
        "base_branch_match": False,
        "head_oid_match": False,
        # Additive squash-merge equivalence fields (Issue #1337).
        "head_equivalence_authorized": False,
        "head_equivalence_mode": None,
        "pr_merge_commit_oid": None,
        "local_delta_paths_count": None,
    }
    branch_name = req["branch_name"]
    worktree_real = os.path.realpath(req["worktree_path"])

    # 1. root default branch
    cur = _current_branch(project_root, deadline)
    default = _default_branch(project_root, deadline)
    if cur is None or cur != default:
        return False, ROOT_NOT_DEFAULT, verified
    verified["root_default"] = True

    # 2/3. worktree in catalog + branch match
    catalog = list_worktrees(project_root, deadline)
    if catalog is None:
        return False, WORKTREE_NOT_IN_CATALOG, verified
    entry = find_by_realpath(catalog, worktree_real)
    if entry is None:
        return False, WORKTREE_NOT_IN_CATALOG, verified
    verified["worktree_in_catalog"] = True
    if branch_short_name(entry.get("branch_ref")) != branch_name:
        return False, BRANCH_MISMATCH, verified
    verified["branch_match"] = True

    # also reject when the worktree path is outside the project's worktrees dir
    worktrees_dir = os.path.realpath(os.path.join(project_root, ".claude", "worktrees"))
    if not worktree_real.startswith(worktrees_dir + os.sep):
        return False, WORKTREE_PATH_MISMATCH, verified

    # 4. worktree clean
    try:
        st = _git(["-C", worktree_real, "status", "--porcelain=v1", "-z"], deadline, 10.0)
    except (OSError, subprocess.TimeoutExpired):
        return False, WORKTREE_DIRTY, verified
    if st.returncode != 0 or st.stdout:
        return False, WORKTREE_DIRTY, verified
    verified["worktree_clean"] = True

    # 5/6/7. PR merged + head branch + linked issue, bound to THIS repo + commit.
    # Blocker 5: resolve the repo slug from the trusted root so gh is pinned to the
    # same repository whose worktree we are about to delete (no confused deputy).
    repo_slug = _repo_slug(project_root, deadline)
    if repo_slug is None:
        return False, REPO_SLUG_UNRESOLVED, verified
    pr = _pr_state(int(req["pr_number"]), project_root, repo_slug, deadline)
    if pr is None or pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        return False, PR_NOT_MERGED, verified
    verified["pr_merged"] = True
    if pr.get("headRefName") != branch_name:
        return False, HEAD_BRANCH_MISMATCH, verified
    verified["head_branch_match"] = True
    # Blocker 5: reject fork / cross-repo PRs — a same-named branch in another repo
    # must not authorize deleting our local worktree.
    if pr.get("isCrossRepository"):
        return False, HEAD_REPO_MISMATCH, verified
    owner = (pr.get("headRepositoryOwner") or {}).get("login")
    if owner and repo_slug and owner != repo_slug.split("/", 1)[0]:
        return False, HEAD_REPO_MISMATCH, verified
    verified["head_repo_match"] = True
    # Blocker 5: the PR base must be the default branch (not some side branch).
    if pr.get("baseRefName") != default:
        return False, BASE_BRANCH_MISMATCH, verified
    verified["base_branch_match"] = True
    # Blocker 5: the PR head sha must equal the LOCAL branch tip — a same-named
    # branch at a different commit must not authorize the deletion.
    local_tip = _local_branch_tip(project_root, branch_name, deadline)
    verified["head_oid_match"] = bool(local_tip and pr.get("headRefOid") == local_tip)
    head_authorized, equivalence_fields = _resolve_head_equivalence(
        pr, local_tip, project_root, default, deadline
    )
    verified.update(equivalence_fields)
    if not head_authorized:
        return False, HEAD_OID_MISMATCH, verified
    linked = req.get("linked_issue_number")
    if linked is not None:
        refs = {r.get("number") for r in (pr.get("closingIssuesReferences") or [])}
        if int(linked) not in refs:
            return False, LINKED_ISSUE_MISMATCH, verified
    verified["linked_issue_match"] = True

    return True, None, verified


def verify_branch_only_cleanup_authorization(
    req: dict, project_root: str, deadline: Deadline
) -> tuple[bool, str | None, dict]:
    """Authorize branch-only cleanup for partial-cleanup state (Issue #1196).

    Called by run() ONLY when verify_cleanup_authorization returns WORKTREE_NOT_IN_CATALOG.
    This function is intentionally NOT exported for materialize_cleanup_contract use
    (BRANCH_ONLY_MATERIALIZE_DENIED guards against that).

    Checks 5 conditions (A-E) for branch-only candidacy, then full PR authorization:
      (A) worktree realpath under <repo>/.claude/worktrees/
      (B) worktree path does not exist on filesystem
      (C) git worktree catalog has no entry at this path
      (D) git worktree catalog has no other worktree on this branch
      (E) refs/heads/<branch_name> exists locally

    On success returns verified fields that include all Verified Fields from the Issue
    contract plus standard PR authorization fields.
    """
    branch_name = req["branch_name"]
    worktree_real = os.path.realpath(req["worktree_path"])
    worktrees_dir = os.path.realpath(os.path.join(project_root, ".claude", "worktrees"))

    verified: dict = {
        "root_default": False,
        "branch_only_candidate": False,
        "worktree_path_under_worktrees_dir": False,
        "worktree_absent_on_disk": False,
        "worktree_absent_from_catalog": False,
        "branch_absent_from_worktree_catalog": False,
        "local_branch_exists": False,
        "local_branch_tip_oid": None,
        "pr_head_oid": None,
        "head_oid_match": False,
        "branch_only_force_delete_used": False,
        # Additive squash-merge equivalence fields (Issue #1337).
        "head_equivalence_authorized": False,
        "head_equivalence_mode": None,
        "pr_merge_commit_oid": None,
        "local_delta_paths_count": None,
        # Standard PR authorization fields (AC5 coverage)
        "pr_merged": False,
        "head_branch_match": False,
        "head_repo_match": False,
        "base_branch_match": False,
        "linked_issue_match": False,
    }

    # 1. root default branch
    cur = _current_branch(project_root, deadline)
    default = _default_branch(project_root, deadline)
    if cur is None or cur != default:
        return False, ROOT_NOT_DEFAULT, verified
    verified["root_default"] = True

    # Condition (A): worktree realpath must be under .claude/worktrees/
    if not worktree_real.startswith(worktrees_dir + os.sep):
        return False, BRANCH_ONLY_FORCE_DELETE_DENIED, verified
    verified["worktree_path_under_worktrees_dir"] = True

    # Condition (B): worktree path must not exist on filesystem
    if os.path.lexists(worktree_real):
        return False, WORKTREE_STILL_IN_CATALOG, verified
    verified["worktree_absent_on_disk"] = True

    # Fetch catalog once for conditions C and D
    catalog = list_worktrees(project_root, deadline)
    if catalog is None:
        return False, WORKTREE_NOT_IN_CATALOG, verified

    # Condition (C): git catalog must have no entry at this path
    entry = find_by_realpath(catalog, worktree_real)
    if entry is not None:
        return False, WORKTREE_STILL_IN_CATALOG, verified
    verified["worktree_absent_from_catalog"] = True

    # Condition (D): no OTHER worktree may use this branch
    for e in catalog:
        if branch_short_name(e.get("branch_ref")) == branch_name:
            return False, BRANCH_CHECKED_OUT_IN_WORKTREE, verified
    verified["branch_absent_from_worktree_catalog"] = True

    # Condition (E): local refs/heads/<branch_name> must exist
    local_tip = _local_branch_tip(project_root, branch_name, deadline)
    if local_tip is None:
        return False, LOCAL_BRANCH_MISSING, verified
    verified["local_branch_exists"] = True
    verified["local_branch_tip_oid"] = local_tip

    # All 5 conditions met — this is a branch-only candidate.
    verified["branch_only_candidate"] = True

    # Full PR authorization (same rigor as verify_cleanup_authorization).
    repo_slug = _repo_slug(project_root, deadline)
    if repo_slug is None:
        return False, REPO_SLUG_UNRESOLVED, verified
    pr = _pr_state(int(req["pr_number"]), project_root, repo_slug, deadline)
    if pr is None or pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        return False, PR_NOT_MERGED, verified
    verified["pr_merged"] = True
    if pr.get("headRefName") != branch_name:
        return False, HEAD_BRANCH_MISMATCH, verified
    verified["head_branch_match"] = True
    # Reject fork / cross-repo PRs (AC5)
    if pr.get("isCrossRepository"):
        return False, HEAD_REPO_MISMATCH, verified
    owner = (pr.get("headRepositoryOwner") or {}).get("login")
    if owner and repo_slug and owner != repo_slug.split("/", 1)[0]:
        return False, HEAD_REPO_MISMATCH, verified
    verified["head_repo_match"] = True
    # PR base must be default branch (AC5)
    if pr.get("baseRefName") != default:
        return False, BASE_BRANCH_MISMATCH, verified
    verified["base_branch_match"] = True
    # PR head OID must match local branch tip (AC3)
    pr_head_oid = pr.get("headRefOid")
    verified["pr_head_oid"] = pr_head_oid
    verified["head_oid_match"] = bool(local_tip and pr_head_oid == local_tip)
    head_authorized, equivalence_fields = _resolve_head_equivalence(
        pr, local_tip, project_root, default, deadline
    )
    verified.update(equivalence_fields)
    if not head_authorized:
        return False, HEAD_OID_MISMATCH, verified
    # Linked issue check (AC5)
    linked = req.get("linked_issue_number")
    if linked is not None:
        refs = {r.get("number") for r in (pr.get("closingIssuesReferences") or [])}
        if int(linked) not in refs:
            return False, LINKED_ISSUE_MISMATCH, verified
    verified["linked_issue_match"] = True

    # All authorization conditions met — mark force-delete as authorized.
    verified["branch_only_force_delete_used"] = True
    return True, None, verified


def _perform(branch_name: str, worktree_real: str, project_root: str,
             deadline: Deadline) -> tuple[list[str], str | None]:
    """Execute exact worktree remove + branch -d via internal subprocess arrays.

    Blocker 6: returns ``(actions_taken, error)``. If the worktree is removed but
    ``branch -d`` then fails (e.g. PR squash-merged so git does not see the branch
    as merged, or local default is stale), the PARTIAL success is preserved in
    ``actions_taken`` instead of being discarded — the caller must not report an
    empty ``actions_taken`` after a destructive step already ran.
    """
    actions: list[str] = []
    rm = _git(["-C", project_root, "worktree", "remove", worktree_real], deadline, 15.0)
    if rm.returncode != 0:
        return actions, f"worktree_remove_failed: {rm.stderr.strip()[:120]}"
    actions.append(OP_WORKTREE_REMOVE)
    bd = _git(["-C", project_root, "branch", "-d", branch_name], deadline, 10.0)
    if bd.returncode != 0:
        return actions, f"branch_delete_failed: {bd.stderr.strip()[:120]}"
    actions.append(OP_BRANCH_DELETE)
    return actions, None


def _perform_branch_only(
    branch_name: str, project_root: str, deadline: Deadline
) -> tuple[list[str], str | None]:
    """Execute branch-only force delete via internal subprocess array (Issue #1196 AC7).

    Uses ``git branch -D`` (force delete) because squash-merges leave the branch
    undetectable by ``git branch -d`` even when the PR is merged.  Authorization
    has already been verified by ``verify_branch_only_cleanup_authorization``.

    Returns ``(actions_taken, error)`` following the same Blocker 6 pattern as
    ``_perform`` — error is non-None on failure.
    """
    actions: list[str] = []
    bd = _git(["-C", project_root, "branch", "-D", branch_name], deadline, 10.0)
    if bd.returncode != 0:
        return actions, f"branch_delete_failed: {bd.stderr.strip()[:120]}"
    actions.append(OP_BRANCH_DELETE)
    return actions, None


def run(req: dict, project_root: str | None = None, budget_seconds: float = 60.0) -> dict:
    # Blocker 5: project_root is a TRUSTED-CALLER argument (internal API), not an
    # agent-facing flag. The CLI no longer exposes --project-root; it always uses
    # the canonical root resolved from CLAUDE_PROJECT_DIR / the script location.
    root = os.path.realpath(project_root) if project_root else resolve_project_root()
    deadline = Deadline(budget_seconds)
    try:
        ok, reason, verified = verify_cleanup_authorization(req, root, deadline)
    except GuardDeadlineExceeded as e:
        return _result("error", str(e), {}, [])

    # Branch-only lane (Issue #1196): when the worktree is not in the catalog,
    # check whether this is a partial-cleanup state (worktree removed, branch still
    # present) and, if so, authorize a branch-only cleanup.
    if not ok and reason == WORKTREE_NOT_IN_CATALOG:
        try:
            ok_b, reason_b, verified_b = verify_branch_only_cleanup_authorization(req, root, deadline)
        except GuardDeadlineExceeded as e:
            return _result("error", str(e), {}, [])
        if not ok_b:
            return _branch_only_result("refused", reason_b, verified_b, [])
        try:
            actions, perform_error = _perform_branch_only(req["branch_name"], root, deadline)
        except (GuardDeadlineExceeded, OSError, subprocess.TimeoutExpired) as e:
            return _branch_only_result("error", str(e)[:160], verified_b, [])
        if perform_error is not None:
            return _branch_only_result("error", perform_error, verified_b, actions)
        return _branch_only_result("ok", None, verified_b, actions)

    if not ok:
        return _result("refused", reason, verified, [])
    try:
        actions, perform_error = _perform(
            req["branch_name"], os.path.realpath(req["worktree_path"]), root, deadline
        )
    except (GuardDeadlineExceeded, OSError, subprocess.TimeoutExpired) as e:
        return _result("error", str(e)[:160], verified, [])
    if perform_error is not None:
        # Blocker 6: keep the partial actions that DID run (e.g. worktree_remove).
        #
        # Issue #1403: after the normal executor removed the dedicated worktree,
        # ``git branch -d`` can still reject a squash-merged branch by ancestry.
        # Re-enter the existing branch-only verifier in this *same* invocation;
        # it retains every destructive-path predicate before the executor uses
        # its internal ``git branch -D`` subprocess. Do not expose a new agent
        # command or broaden any hook/execpolicy allowlist.
        if (
            actions == [OP_WORKTREE_REMOVE]
            and perform_error.startswith("branch_delete_failed:")
        ):
            try:
                fallback_ok, fallback_reason, fallback_verified = (
                    verify_branch_only_cleanup_authorization(req, root, deadline)
                )
            except GuardDeadlineExceeded as e:
                return _result("error", str(e), verified, actions)
            if not fallback_ok:
                # The worktree removal already happened, so retain that partial
                # action while surfacing the branch-only verifier's reason code.
                return _result("error", fallback_reason, fallback_verified, actions)
            try:
                fallback_actions, fallback_error = _perform_branch_only(
                    req["branch_name"], root, deadline
                )
            except (GuardDeadlineExceeded, OSError, subprocess.TimeoutExpired) as e:
                return _result("error", str(e)[:160], fallback_verified, actions)
            if fallback_error is not None:
                return _result(
                    "error", fallback_error, fallback_verified, actions + fallback_actions
                )
            return _result("ok", None, fallback_verified, actions + fallback_actions)
        return _result("error", perform_error, verified, actions)
    return _result("ok", None, verified, actions)


def _result(status: str, reason: str | None, verified: dict, actions: list[str]) -> dict:
    return {
        "schema": SCHEMA_RESULT,
        "status": status,
        "reason_code": reason,
        "verified": verified,
        "actions_taken": actions,
        "stderr_line_count": 0,
    }


def _branch_only_result(status: str, reason: str | None, verified: dict, actions: list[str]) -> dict:
    """Result dict for the branch-only cleanup lane (Issue #1196 AC6)."""
    return {
        "schema": SCHEMA_RESULT,
        "status": status,
        "reason_code": reason,
        "verified": verified,
        "actions_taken": actions,
        "stderr_line_count": 0,
        "worktree_absent_after_removal": bool(
            verified.get("worktree_absent_on_disk")
            and verified.get("worktree_absent_from_catalog")
        ),
        "branch_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verified single cleanup executor.")
    p.add_argument("--pr-number", type=int, required=True)
    p.add_argument("--linked-issue-number", type=int, default=None)
    p.add_argument("--worktree-path", required=True)
    p.add_argument("--branch-name", required=True)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    req = {
        "schema": SCHEMA_REQUEST,
        "pr_number": a.pr_number,
        "linked_issue_number": a.linked_issue_number,
        "worktree_path": a.worktree_path,
        "branch_name": a.branch_name,
    }
    # Blocker 5: the executor always resolves the trusted root itself; there is no
    # agent-facing --project-root retargeting.
    result = run(req)
    if a.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"status: {result['status']}")
        if result["reason_code"]:
            print(f"reason_code: {result['reason_code']}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

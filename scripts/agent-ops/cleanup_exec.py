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

Branch-only compare-and-delete + local-only discard lane (Issue #1523):

  * ``_perform_branch_only`` now threads the ``expected`` branch-tip OID
    captured at authorization time through to the destructive call. Inside a
    shared repository mutation lock (``_mutation_lock``) it re-reads the LIVE
    branch tip immediately before deleting and refuses (``branch_tip_changed``)
    if it no longer matches — the git-native compare-and-delete equivalent of
    ``git update-ref -d refs/heads/<branch> <expected-old-oid>``.
  * Ancestry-failure classification is now structural
    (``_verify_ancestry_for_force_delete``): the expected OID must resolve to a
    real commit object AND ``git merge-base --is-ancestor`` must return a
    structurally meaningful exit code — 0 (ordinary ancestor) or 1
    (squash-shaped / not-``git``-visible-ancestor history, already authorized
    via PR-head equivalence). Any OTHER exit code, invalid object, ref-lock
    error, I/O error, or git/subprocess error fails closed to
    ``branch_only_non_ancestry_failure`` rather than silently promoting to
    force-delete.
  * ``run()`` also handles the same-invocation fallback: when normal cleanup's
    ``worktree remove`` succeeds but ``git branch -d`` then fails, it attempts
    to RE-AUTHORIZE (not re-execute) via
    ``verify_branch_only_cleanup_authorization`` and only proceeds to the
    compare-and-delete force path when that re-authorization succeeds. Any
    refusal during re-authorization preserves the ORIGINAL ``branch_delete_failed``
    reason code, the ``actions_taken`` list unchanged (worktree_remove only),
    and never invokes the force-delete subprocess.
  * A brand-new local-only unpublished commit discard lane
    (``verify_discard_authorization`` / ``run_discard_check`` /
    ``run_discard_consume``) authorizes discarding a dedicated worktree's
    local-only commits (commits beyond the merged PR's head SHA) ONLY after a
    human runs the executor-issued, target+SHA-bound, one-shot, expiring
    confirmation contract (``cleanup_contract_v3`` primitives, extended with
    ``OP_LOCAL_ONLY_DISCARD``). ``verify_cleanup_authorization`` and
    ``verify_branch_only_cleanup_authorization`` are UNCHANGED by this lane.
  * ``verified`` payload keys/types are normalized across all three verifier
    functions via a shared ``_verified_template()`` union (additive only — see
    Issue #1523 AC9), so ``normal`` / ``branch-only`` / ``discard`` lanes all
    populate the SAME key set with the SAME types.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cleanup_contract_v3 import (  # noqa: E402
    CLEANUP_CONTRACT_CONSUMED,
    CLEANUP_OPERATION_MISMATCH,
    OP_BRANCH_DELETE,
    OP_LOCAL_ONLY_DISCARD,
    OP_WORKTREE_REMOVE,
    PR_NOT_MERGED,
    WORKTREE_DIRTY,
    WORKTREE_NOT_IN_CATALOG,
    WORKTREE_PATH_MISMATCH,
    claim_contract,
    discard_claimed,
    read_claimed_contract,
    write_consume_tombstone,
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

# Branch-only compare-and-delete reason codes (Issue #1523 AC7/AC8).
BRANCH_TIP_CHANGED = "branch_tip_changed"                          # live tip != expected OID at delete time
BRANCH_ONLY_NON_ANCESTRY_FAILURE = "branch_only_non_ancestry_failure"  # invalid object / non-1 merge-base exit / git error

# Local-only unpublished commit discard lane reason codes (Issue #1523 AC1-AC6).
DISCARD_PR_HEAD_NOT_ANCESTOR = "discard_pr_head_not_ancestor"      # PR head SHA not an ancestor of local tip
DISCARD_NO_LOCAL_ONLY_COMMITS = "discard_no_local_only_commits"    # no commits beyond PR head SHA
DISCARD_SHA_BINDING_CHANGED = "discard_sha_binding_changed"        # live SHAs no longer match claimed contract
DISCARD_CONTRACT_OPERATION_MISMATCH = CLEANUP_OPERATION_MISMATCH   # re-exported for discard-consume callers
DISCARD_CONTRACT_NOT_CLAIMABLE = CLEANUP_CONTRACT_CONSUMED         # re-exported for discard-consume callers

# Repository mutation lock (Issue #1523 AC7): scoped ONLY to the branch-only
# compare-and-delete and discard destructive paths. No repo-wide mutation lock
# / serialization primitive existed anywhere under scripts/agent-ops before
# this Issue (verified via grep for mutation_lock/repo_lock/flock/FileLock).
_MUTATION_LOCK_REL_PATH = os.path.join(".git", "loop-protocol-cleanup.lock")


@contextlib.contextmanager
def _mutation_lock(project_root: str):
    """Serialize destructive branch-only-fallback / discard mutations (Issue #1523 AC7).

    A local POSIX advisory ``flock`` on a lock file under ``<root>/.git/``. This
    is intentionally narrow (scoped to the two destructive paths that call it,
    not a general-purpose repository lock) and degrades to a no-op when the
    platform lacks ``fcntl`` or the lock file cannot be opened — callers still
    get the compare-and-delete primitive's own atomicity in that case, but lose
    the additional serialization against concurrent local invocations.
    """
    lock_path = os.path.join(project_root, _MUTATION_LOCK_REL_PATH)
    fd = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        fd = None
    if fd is None or fcntl is None:
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            pass
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _verified_template() -> dict:
    """Canonical union of ``verified`` keys/types shared by ALL verifier lanes
    (normal / branch-only / discard — Issue #1523 AC9).

    Every ``verify_*_authorization`` function starts from a fresh copy of this
    dict and only ever ADDS/overwrites values for the checks it performs — the
    key SET and each key's TYPE are therefore identical across lanes, even
    though which keys end up ``True``/populated differs by lane. This is
    additive relative to the pre-#1523 per-lane dicts (AC4 / AC9): no existing
    key was removed or retyped.
    """
    return {
        "root_default": False,
        "worktree_in_catalog": False,
        "branch_match": False,
        "worktree_clean": False,
        "branch_only_candidate": False,
        "worktree_path_under_worktrees_dir": False,
        "worktree_absent_on_disk": False,
        "worktree_absent_from_catalog": False,
        "branch_absent_from_worktree_catalog": False,
        "local_branch_exists": False,
        "local_branch_tip_oid": None,
        "pr_head_oid": None,
        "pr_merged": False,
        "head_branch_match": False,
        "head_repo_match": False,
        "base_branch_match": False,
        "head_oid_match": False,
        "linked_issue_match": False,
        "head_equivalence_authorized": False,
        "head_equivalence_mode": None,
        "pr_merge_commit_oid": None,
        "local_delta_paths_count": None,
        "branch_only_force_delete_used": False,
        # Discard-lane additive fields (Issue #1523 AC1).
        "discard_candidate": False,
        "pr_head_sha": None,
        "local_tip_sha": None,
        "pr_head_is_ancestor": False,
        "local_only_commit_count": None,
        "local_only_commit_shas": None,
    }


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
    verified = _verified_template()
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

    verified: dict = _verified_template()

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


def _verify_ancestry_for_force_delete(
    project_root: str, expected_oid: str | None, comparison_ref: str, deadline: Deadline
) -> tuple[bool, str | None]:
    """Structural ancestry gate for branch-only force-delete (Issue #1523 AC8).

    Requires (1) ``expected_oid`` resolves to a real, locally-present commit
    object, AND (2) ``git merge-base --is-ancestor expected_oid comparison_ref``
    returns a STRUCTURALLY MEANINGFUL result — exit code 0 (the ordinary case:
    the branch tip is a plain ancestor of ``comparison_ref``, e.g. an
    already-merged or content-identical branch) or exit code 1 (the
    squash-merge case this force-delete lane exists for: the tip is genuinely
    NOT a ``git``-visible ancestor even though authorization already confirmed
    PR-head equivalence). ANY OTHER exit code, an invalid/unresolvable object, a
    ref-lock error, an I/O error, or a git/subprocess failure of any kind is a
    non-ancestry FAILURE (as opposed to a definite ancestor/non-ancestor
    result) and fails closed to ``BRANCH_ONLY_NON_ANCESTRY_FAILURE`` — none of
    these promote to force-delete.
    """
    if not expected_oid or not _commit_object_exists(project_root, expected_oid, deadline):
        return False, BRANCH_ONLY_NON_ANCESTRY_FAILURE
    try:
        out = _git(
            ["-C", project_root, "merge-base", "--is-ancestor", expected_oid, comparison_ref],
            deadline, 10.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, BRANCH_ONLY_NON_ANCESTRY_FAILURE
    if out.returncode not in (0, 1):
        return False, BRANCH_ONLY_NON_ANCESTRY_FAILURE
    return True, None


def _perform_branch_only(
    branch_name: str,
    expected_oid: str | None,
    comparison_ref: str,
    project_root: str,
    deadline: Deadline,
) -> tuple[list[str], str | None]:
    """Execute branch-only force delete via compare-and-delete (Issue #1523 AC7/AC8).

    Uses ``git branch -D`` (force delete) because squash-merges leave the branch
    undetectable by ``git branch -d`` even when the PR is merged. Authorization
    has already been verified by ``verify_branch_only_cleanup_authorization``,
    which captured ``expected_oid`` (the branch tip at authorization time).

    Runs inside the shared repository ``_mutation_lock`` and, immediately before
    deleting, (1) re-reads the LIVE branch tip and refuses with
    ``BRANCH_TIP_CHANGED`` if it no longer equals ``expected_oid`` (a race
    guard equivalent to ``git update-ref -d refs/heads/<branch> <expected-old-oid>``),
    and (2) re-runs the structural ancestry check
    (``_verify_ancestry_for_force_delete``) so an ancestry-comparison failure at
    delete time also fails closed rather than promoting to force-delete.

    Returns ``(actions_taken, error)`` following the same Blocker 6 pattern as
    ``_perform`` — error is non-None on failure. ``error`` is one of the bare
    reason-code constants (``BRANCH_TIP_CHANGED`` /
    ``BRANCH_ONLY_NON_ANCESTRY_FAILURE``) for pre-delete refusals, or a
    ``"branch_delete_failed: ..."``-prefixed diagnostic string for a genuine
    subprocess failure of the delete itself.
    """
    actions: list[str] = []
    with _mutation_lock(project_root):
        live_tip = _local_branch_tip(project_root, branch_name, deadline)
        if live_tip is None or expected_oid is None or live_tip != expected_oid:
            return actions, BRANCH_TIP_CHANGED
        ancestry_ok, ancestry_reason = _verify_ancestry_for_force_delete(
            project_root, expected_oid, comparison_ref, deadline
        )
        if not ancestry_ok:
            return actions, ancestry_reason
        # Compare-and-delete equivalent of
        # ``git update-ref -d refs/heads/<branch> <expected-old-oid>``: passing
        # the expected old value makes the ref update atomically fail if the ref
        # no longer points at ``expected_oid`` (concurrent race after the check
        # above, closed by the same primitive rather than a second read+delete).
        bd = _git(
            ["-C", project_root, "update-ref", "-d", f"refs/heads/{branch_name}", expected_oid],
            deadline, 10.0,
        )
        if bd.returncode != 0:
            return actions, f"branch_delete_failed: {bd.stderr.strip()[:120]}"
        actions.append(OP_BRANCH_DELETE)
        return actions, None


def _perform_discard(
    branch_name: str, worktree_real: str, project_root: str, deadline: Deadline
) -> tuple[list[str], str | None]:
    """Execute the local-only discard's force worktree remove + force branch delete.

    Only reachable from ``run_discard_consume`` after a claimed, SHA-bound,
    non-expired, non-replayed one-shot contract has been re-validated against
    LIVE state (Issue #1523 AC1-AC3). Runs inside ``_mutation_lock``.
    """
    actions: list[str] = []
    with _mutation_lock(project_root):
        rm = _git(["-C", project_root, "worktree", "remove", "--force", worktree_real], deadline, 15.0)
        if rm.returncode != 0:
            return actions, f"worktree_remove_failed: {rm.stderr.strip()[:120]}"
        actions.append(OP_WORKTREE_REMOVE)
        bd = _git(["-C", project_root, "branch", "-D", branch_name], deadline, 10.0)
        if bd.returncode != 0:
            return actions, f"branch_delete_failed: {bd.stderr.strip()[:120]}"
        actions.append(OP_BRANCH_DELETE)
        return actions, None


def verify_discard_authorization(req: dict, project_root: str, deadline: Deadline) -> tuple[bool, str | None, dict]:
    """Authorize the local-only unpublished commit discard lane (Issue #1523 AC1).

    Candidate conditions (all must hold):
      * the target worktree is a catalog-registered dedicated worktree under
        ``.claude/worktrees/`` whose branch matches the requested branch,
      * the worktree has no uncommitted changes,
      * the PR is merged, same-repo, correct head branch, correct base branch,
        and (when supplied) the linked issue matches,
      * the merged PR's head SHA resolves to a real, locally-present commit
        object AND is an ancestor of the local branch tip (``pr_head_is_ancestor``),
      * the local branch tip is strictly ahead of the PR head SHA by at least
        one commit (the "local-only commits").

    On success, ``verified["discard_candidate"]`` is ``True`` and
    ``verified["local_only_commit_shas"]`` carries the exact SHA list of the
    local-only commits (PR head SHA exclusive .. local tip inclusive) — this is
    the structured "confirmation needed" output AC1 requires. This function
    performs NO destructive action; it is called both by ``run_discard_check``
    (pure verification) and re-called by ``run_discard_consume`` immediately
    before the destructive step (fresh live re-verification, not a cache).
    """
    verified = _verified_template()
    branch_name = req["branch_name"]
    worktree_real = os.path.realpath(req["worktree_path"])

    cur = _current_branch(project_root, deadline)
    default = _default_branch(project_root, deadline)
    if cur is None or cur != default:
        return False, ROOT_NOT_DEFAULT, verified
    verified["root_default"] = True

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

    worktrees_dir = os.path.realpath(os.path.join(project_root, ".claude", "worktrees"))
    if not worktree_real.startswith(worktrees_dir + os.sep):
        return False, WORKTREE_PATH_MISMATCH, verified

    try:
        st = _git(["-C", worktree_real, "status", "--porcelain=v1", "-z"], deadline, 10.0)
    except (OSError, subprocess.TimeoutExpired):
        return False, WORKTREE_DIRTY, verified
    if st.returncode != 0 or st.stdout:
        return False, WORKTREE_DIRTY, verified
    verified["worktree_clean"] = True

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
    if pr.get("isCrossRepository"):
        return False, HEAD_REPO_MISMATCH, verified
    owner = (pr.get("headRepositoryOwner") or {}).get("login")
    if owner and repo_slug and owner != repo_slug.split("/", 1)[0]:
        return False, HEAD_REPO_MISMATCH, verified
    verified["head_repo_match"] = True
    if pr.get("baseRefName") != default:
        return False, BASE_BRANCH_MISMATCH, verified
    verified["base_branch_match"] = True

    linked = req.get("linked_issue_number")
    if linked is not None:
        refs = {r.get("number") for r in (pr.get("closingIssuesReferences") or [])}
        if int(linked) not in refs:
            return False, LINKED_ISSUE_MISMATCH, verified
    verified["linked_issue_match"] = True

    local_tip = _local_branch_tip(project_root, branch_name, deadline)
    if local_tip is None:
        return False, LOCAL_BRANCH_MISSING, verified
    verified["local_tip_sha"] = local_tip

    pr_head_sha = pr.get("headRefOid")
    verified["pr_head_sha"] = pr_head_sha
    if not pr_head_sha or not _commit_object_exists(project_root, pr_head_sha, deadline):
        return False, DISCARD_PR_HEAD_NOT_ANCESTOR, verified

    if pr_head_sha == local_tip:
        # PR head IS the local tip — no local-only commits to discard.
        return False, DISCARD_NO_LOCAL_ONLY_COMMITS, verified

    try:
        anc = _git(["-C", project_root, "merge-base", "--is-ancestor", pr_head_sha, local_tip], deadline, 10.0)
    except (OSError, subprocess.TimeoutExpired):
        return False, DISCARD_PR_HEAD_NOT_ANCESTOR, verified
    if anc.returncode != 0:
        return False, DISCARD_PR_HEAD_NOT_ANCESTOR, verified
    verified["pr_head_is_ancestor"] = True

    try:
        log = _git(["-C", project_root, "rev-list", f"{pr_head_sha}..{local_tip}"], deadline, 10.0)
    except (OSError, subprocess.TimeoutExpired):
        return False, DISCARD_NO_LOCAL_ONLY_COMMITS, verified
    if log.returncode != 0:
        return False, DISCARD_NO_LOCAL_ONLY_COMMITS, verified
    commit_shas = [line for line in log.stdout.splitlines() if line.strip()]
    verified["local_only_commit_count"] = len(commit_shas)
    verified["local_only_commit_shas"] = commit_shas
    if not commit_shas:
        return False, DISCARD_NO_LOCAL_ONLY_COMMITS, verified

    verified["discard_candidate"] = True
    return True, None, verified


def run_discard_check(req: dict, project_root: str | None = None, budget_seconds: float = 60.0) -> dict:
    """Verify-only entry point for the discard lane (Issue #1523 AC1).

    Performs NO destructive action and issues NO confirmation contract; it only
    reports whether the target is a discard candidate and, if so, the exact
    local-only commit SHAs a human would be confirming the discard of.
    """
    root = os.path.realpath(project_root) if project_root else resolve_project_root()
    deadline = Deadline(budget_seconds)
    try:
        ok, reason, verified = verify_discard_authorization(req, root, deadline)
    except GuardDeadlineExceeded as e:
        return _discard_result("error", str(e), _verified_template(), [])
    if not ok:
        return _discard_result("refused", reason, verified, [])
    return _discard_result("confirmation_required", None, verified, [])


def run_discard_consume(req: dict, project_root: str | None = None, budget_seconds: float = 60.0) -> dict:
    """Claim-first consume of a one-shot ``OP_LOCAL_ONLY_DISCARD`` contract (Issue #1523 AC2/AC3).

    This is the ONLY entry point that performs the destructive discard, and it
    is reachable ONLY when a human has already run the executor-issued
    confirmation (i.e. ``materialize_cleanup_contract`` with
    ``--operation local_only_discard``) so a claimable contract exists on disk.

    Sequence (fail-closed at every step, never performing a partial destructive
    action on refusal):
      1. ``claim_contract`` — atomic rename; loses the race / already-consumed /
         absent → refused (``cleanup_contract_consumed``). This is the one-shot /
         replay-refusal guarantee (a second invocation cannot re-claim).
      2. ``read_claimed_contract`` — schema/expiry validation of the CLAIMED
         copy (symlink-safe IO); any failure (including expiry) → refused +
         discard the claim.
      3. Contract ``operation`` must be ``OP_LOCAL_ONLY_DISCARD`` → else refused.
      4. Contract's target fields (PR number, linked issue, worktree realpath,
         branch name) must equal the REQUEST's fields exactly (target binding).
      5. Fresh LIVE re-verification via ``verify_discard_authorization`` — the
         confirmation contract does not itself substitute for live
         authorization; every original candidacy condition (clean worktree, PR
         still merged, etc.) is re-checked at consume time.
      6. The contract's bound ``pr_head_sha`` / ``local_tip_sha`` must equal the
         FRESH live-verified SHAs (Issue #1523 AC2/AC3 SHA-binding race guard) —
         a mismatch (branch moved, or a different PR head was merged in the
         interim) refuses with ``DISCARD_SHA_BINDING_CHANGED`` rather than
         performing a stale-binding discard.
      7. Only then, inside ``_mutation_lock``, does the destructive
         ``git worktree remove --force`` + ``git branch -D`` run.

    A durable consume tombstone is written on every claimed-and-validated
    attempt (success or failure past step 3) so replay is denied even if the
    destructive step itself later fails.
    """
    root = os.path.realpath(project_root) if project_root else resolve_project_root()
    deadline = Deadline(budget_seconds)

    claimed_name = claim_contract(root)
    if claimed_name is None:
        return _discard_result("refused", CLEANUP_CONTRACT_CONSUMED, _verified_template(), [])

    ok_c, contract, reason_c = read_claimed_contract(root, claimed_name)
    if not ok_c:
        discard_claimed(root, claimed_name)
        return _discard_result("refused", reason_c, _verified_template(), [])

    if contract.get("operation") != OP_LOCAL_ONLY_DISCARD:
        write_consume_tombstone(root, contract)
        discard_claimed(root, claimed_name)
        return _discard_result("refused", CLEANUP_OPERATION_MISMATCH, _verified_template(), [])

    worktree_real = os.path.realpath(req["worktree_path"])
    if (
        contract.get("pr_number") != req.get("pr_number")
        or contract.get("linked_issue_number") != req.get("linked_issue_number")
        or contract.get("worktree_path") != worktree_real
        or contract.get("branch_name") != req["branch_name"]
    ):
        write_consume_tombstone(root, contract)
        discard_claimed(root, claimed_name)
        return _discard_result("refused", "cleanup_command_hash_mismatch", _verified_template(), [])

    try:
        ok, reason, verified = verify_discard_authorization(req, root, deadline)
    except GuardDeadlineExceeded as e:
        write_consume_tombstone(root, contract)
        discard_claimed(root, claimed_name)
        return _discard_result("error", str(e), _verified_template(), [])
    if not ok:
        write_consume_tombstone(root, contract)
        discard_claimed(root, claimed_name)
        return _discard_result("refused", reason, verified, [])

    if (
        contract.get("pr_head_sha") != verified.get("pr_head_sha")
        or contract.get("local_tip_sha") != verified.get("local_tip_sha")
    ):
        write_consume_tombstone(root, contract)
        discard_claimed(root, claimed_name)
        return _discard_result("refused", DISCARD_SHA_BINDING_CHANGED, verified, [])

    try:
        actions, perform_error = _perform_discard(
            req["branch_name"], worktree_real, root, deadline
        )
    except (GuardDeadlineExceeded, OSError, subprocess.TimeoutExpired) as e:
        write_consume_tombstone(root, contract)
        discard_claimed(root, claimed_name)
        return _discard_result("error", str(e)[:160], verified, [])

    write_consume_tombstone(root, contract)
    discard_claimed(root, claimed_name)

    if perform_error is not None:
        return _discard_result("error", perform_error, verified, actions)
    return _discard_result("ok", None, verified, actions)


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
        default_branch = _default_branch(root, deadline)
        expected_oid = verified_b.get("local_branch_tip_oid")
        try:
            actions, perform_error = _perform_branch_only(
                req["branch_name"], expected_oid, default_branch, root, deadline
            )
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
        # Same-invocation branch-only-fallback re-authorization (Issue #1523 AC10):
        # when worktree_remove SUCCEEDED but the (non-force) ``branch -d`` then
        # FAILED, the worktree is now genuinely absent from disk+catalog, which
        # is exactly the branch-only lane's candidacy shape. Re-authorize (NOT
        # re-execute) via the SAME branch-only verifier used by the standalone
        # branch-only lane above; only on a fresh, independent authorization
        # success do we proceed to the compare-and-delete force path. ANY
        # refusal during re-authorization (confirmation absent/expired/replay,
        # target-SHA mismatch, PR-not-merged, cross-repo, base/linked-issue/
        # path/catalog/branch mismatch, not-local-only history, uncommitted
        # changes) preserves the ORIGINAL ``branch_delete_failed`` reason code,
        # leaves ``actions_taken`` as the already-succeeded ``worktree_remove``
        # ONLY, leaves the branch intact, and NEVER invokes the force-delete
        # subprocess.
        if OP_WORKTREE_REMOVE in actions and perform_error.startswith("branch_delete_failed"):
            try:
                ok_b, reason_b, verified_b = verify_branch_only_cleanup_authorization(req, root, deadline)
            except GuardDeadlineExceeded:
                return _result("error", perform_error, verified, actions)
            if ok_b:
                default_branch = _default_branch(root, deadline)
                expected_oid = verified_b.get("local_branch_tip_oid")
                try:
                    actions_b, perform_error_b = _perform_branch_only(
                        req["branch_name"], expected_oid, default_branch, root, deadline
                    )
                except (GuardDeadlineExceeded, OSError, subprocess.TimeoutExpired):
                    return _result("error", perform_error, verified, actions)
                if perform_error_b is None:
                    return _result("ok", None, verified, actions + actions_b)
                # Re-authorized but the force-delete step itself failed/refused:
                # preserve the original branch_delete_failed reason and keep
                # actions_taken to worktree_remove only (force-delete subprocess
                # WAS invoked here, but did not succeed).
                return _result("error", perform_error, verified, actions)
            # Re-authorization refused: preserve ORIGINAL reason code and
            # actions_taken; the force-delete subprocess is NEVER invoked.
            return _result("error", perform_error, verified, actions)
        # Blocker 6: keep the partial actions that DID run (e.g. worktree_remove).
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


def _discard_result(status: str, reason: str | None, verified: dict, actions: list[str]) -> dict:
    """Result dict for the local-only unpublished commit discard lane (Issue #1523 AC4).

    Additive-only relative to ``CLEANUP_EXEC_RESULT_V1``: adds ``discard_lane``
    (mirroring the existing ``branch_only`` flag pattern) without changing any
    pre-existing required key or type.
    """
    return {
        "schema": SCHEMA_RESULT,
        "status": status,
        "reason_code": reason,
        "verified": verified,
        "actions_taken": actions,
        "stderr_line_count": 0,
        "discard_lane": True,
    }


def main(argv: list[str] | None = None) -> int:
    # Issue #1523: this CLI shape is intentionally UNCHANGED (AC6) — the
    # discard lane (``verify_discard_authorization`` / ``run_discard_check`` /
    # ``run_discard_consume``) is a Python-level entry point only, invoked from
    # ``materialize_cleanup_contract.py``'s ``--check`` / ``--consume`` CLI
    # surface (which already carries the ``--operation local_only_discard``
    # choice), never from a new flag on THIS executor's argparse.
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

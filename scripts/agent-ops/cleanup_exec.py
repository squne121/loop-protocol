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
            "headRepositoryOwner,isCrossRepository,closingIssuesReferences"]
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


def verify_cleanup_authorization(req: dict, project_root: str, deadline: Deadline) -> tuple[bool, str | None, dict]:
    """Run all authorization checks. Returns (ok, reason_code, verified)."""
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
    if not local_tip or pr.get("headRefOid") != local_tip:
        return False, HEAD_OID_MISMATCH, verified
    verified["head_oid_match"] = True
    linked = req.get("linked_issue_number")
    if linked is not None:
        refs = {r.get("number") for r in (pr.get("closingIssuesReferences") or [])}
        if int(linked) not in refs:
            return False, LINKED_ISSUE_MISMATCH, verified
    verified["linked_issue_match"] = True

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

#!/usr/bin/env python3
"""guard_preflight.py — machine-decide guard arbitration via the REAL catalog (Issue #1137).

Rewritten per the PR #1139 OWNER review (Blocker 3): the preflight now resolves
state from the real ``git worktree list --porcelain -z`` catalog and actually
uses ``cwd``. ``active_worktree_state: matches`` requires a real catalog entry for
the active issue (not merely a ``LOOP_ISSUE_NUMBER`` env var), and the contract
state is the three-valued loader result so a present-but-invalid contract is never
advertised as runnable. Recovery deadlock (policy B): root drift + active worktree
mismatch returns ``human_required`` with structured hints and performs no mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cleanup_contract_v3 import (  # noqa: E402
    ROOT_DRIFT_ACTIVE_WORKTREE_MISMATCH,
    SAFE_SCRATCH_CONTRACT_PATH,
    STATE_ABSENT,
    STATE_PRESENT_BUT_INVALID,
    STATE_VALID_V3,
    is_expired,
    load_contract_state,
)
from worktree_catalog import (  # noqa: E402
    ACTIVE_ISSUE_CATALOG_CONFLICT,
    Deadline,
    GuardDeadlineExceeded,
    best_effort_root,
    find_containing_entry,
    issue_number_from_entry,
    resolve_canonical_root,
    root_candidate,
    select_issue_worktree,
)

SCHEMA = "AGENT_GUARD_PREFLIGHT_V1"


def resolve_project_root() -> str:
    """Best-effort canonical root (never raises).

    Priority: ``CLAUDE_PROJECT_DIR`` -> script location, each normalised to the
    repository's primary root through the shared ``worktree_catalog`` helper
    (Issue #2979). When normalisation fails the raw candidate is returned;
    ``build_preflight`` re-runs the identity check and reports the structured
    failure, so the raw candidate is never silently trusted as the root.
    """
    return best_effort_root(None, __file__)


def _session_cwd() -> str:
    """The session's current directory (an observation, never the root authority).

    ``PWD`` is only used when it still names the same directory as the process
    cwd (a stale inherited ``PWD`` must not override the real cwd); if the cwd
    was deleted, ``PWD`` is the only remaining hint.
    """
    pwd = os.environ.get("PWD")
    try:
        real_cwd = os.path.realpath(os.getcwd())
    except OSError:
        return os.path.realpath(pwd) if pwd else os.getcwd()
    if pwd and os.path.realpath(pwd) == real_cwd:
        return os.path.realpath(pwd)
    return real_cwd


def _current_branch(project_root: str, deadline: Deadline) -> str | None:
    import shutil
    import subprocess
    git = shutil.which("git")
    if not git:
        return None
    try:
        out = subprocess.run(
            [git, "-C", project_root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=deadline.subprocess_timeout(5.0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _default_branch(project_root: str, deadline: Deadline) -> str:
    import shutil
    import subprocess
    env = os.environ.get("LOOP_DEFAULT_BRANCH", "").strip()
    if env:
        return env
    git = shutil.which("git")
    if git:
        try:
            out = subprocess.run(
                [git, "-C", project_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                capture_output=True, text=True, timeout=deadline.subprocess_timeout(5.0),
            )
            if out.returncode == 0 and out.stdout.strip():
                ref = out.stdout.strip()
                return ref.split("/", 1)[1] if "/" in ref else ref
        except (OSError, subprocess.TimeoutExpired):
            pass
        for cand in ("main", "master", "trunk"):
            try:
                out = subprocess.run(
                    [git, "-C", project_root, "rev-parse", "--verify", cand],
                    capture_output=True, text=True, timeout=deadline.subprocess_timeout(5.0),
                )
                if out.returncode == 0:
                    return cand
            except (OSError, subprocess.TimeoutExpired):
                pass
    return "main"


def _classify_root(current: str | None, default: str) -> str:
    if current is None or current == "HEAD":
        return "detached_or_unknown"
    if current == default:
        return "default"
    return "drifted"


def _active_issue(cwd: str, current_branch: str | None, catalog_issue: str | None = None) -> str | None:
    """Active Issue: ``LOOP_ISSUE_NUMBER`` -> catalog identity of the cwd -> cwd basename -> root branch.

    ``catalog_issue`` is the Issue bound to the catalog entry that contains the
    cwd (Issue #2979 AC9) so nested cwds such as ``<worktree>/src`` are identified
    without relying on the cwd basename. Conflicts between the environment value
    and the catalog identity are detected by the caller.
    """
    env = os.environ.get("LOOP_ISSUE_NUMBER")
    if env and env.strip().isdigit():
        return env.strip()
    if catalog_issue:
        return catalog_issue
    base = os.path.basename(os.path.normpath(cwd))
    m = re.match(r"^issue-(\d+)-", base)
    if m:
        return m.group(1)
    if current_branch:
        m = re.match(r"^(?:worktree-)?issue-(\d+)-", current_branch)
        if m:
            return m.group(1)
    return None


def _entry_for_issue(catalog: list[dict], issue: str, root_real: str) -> dict | None:
    # Blocker 7: use the SAME shared strict selector as worktree_scope_guard so
    # preflight and the runtime guard never disagree on which worktree an issue maps to.
    return select_issue_worktree(catalog, issue, root_real)


def _cwd_classification(catalog: list[dict], cwd_real: str, root_real: str) -> str:
    for e in catalog:
        wt = e.get("worktree_realpath")
        if not wt or wt == root_real:
            continue
        if cwd_real == wt or cwd_real.startswith(wt + os.sep):
            return "inside_worktree"
    if cwd_real == root_real:
        return "outside_worktree"
    return "unknown"


def _contract_state(root: str) -> str:
    state, contract, _reason = load_contract_state(root)
    if state == STATE_ABSENT:
        return "absent"
    if state == STATE_PRESENT_BUT_INVALID:
        return "present_but_invalid"
    if state == STATE_VALID_V3:
        return "expired" if is_expired(contract) else "valid_v3"
    return "present_but_invalid"


def _contract_binds_to(root: str, entry: dict | None) -> bool:
    """True iff a valid V3 contract's worktree_path + branch match ``entry`` (Blocker 7)."""
    if entry is None:
        return False
    state, contract, _reason = load_contract_state(root)
    if state != STATE_VALID_V3 or not isinstance(contract, dict):
        return False
    try:
        wt_match = os.path.realpath(contract.get("worktree_path", "")) == entry.get("worktree_realpath")
    except (OSError, TypeError):
        return False
    branch_match = contract.get("branch_name") == (
        entry.get("branch_ref", "").replace("refs/heads/", "") if entry.get("branch_ref") else None
    )
    return bool(wt_match and branch_match)


def build_preflight(project_root: str | None = None, cwd: str | None = None, budget_seconds: float = 30.0) -> dict:
    # Issue #2979: the root CANDIDATE (--project-root -> CLAUDE_PROJECT_DIR -> script
    # location) is normalised to the repository's canonical primary root from Git
    # identity. cwd is only an observation of "which worktree am I in".
    candidate = root_candidate(project_root, __file__)
    cwd_real = os.path.realpath(cwd) if cwd else _session_cwd()
    deadline = Deadline(budget_seconds)

    try:
        resolution = resolve_canonical_root(candidate, deadline)
        if not resolution.ok:
            return _blocked_root_resolution(resolution.reason_code)
        root = resolution.primary_root
        catalog = resolution.catalog
        current = _current_branch(root, deadline)
        default = _default_branch(root, deadline)
    except GuardDeadlineExceeded:
        return _blocked_deadline(candidate)

    root_branch_state = _classify_root(current, default)
    cwd_entry = find_containing_entry(catalog, cwd_real)
    # Only a LINKED worktree entry carries an Issue identity for the session; the
    # primary root is a prefix of every worktree and never identifies one.
    catalog_issue = issue_number_from_entry(cwd_entry) if cwd_entry is not catalog[0] else None
    active_issue = _active_issue(cwd_real, current, catalog_issue)
    issue_conflict = bool(catalog_issue and active_issue and active_issue != catalog_issue)
    entry = _entry_for_issue(catalog, active_issue, root) if active_issue else None
    cwd_class = _cwd_classification(catalog, cwd_real, root)

    if active_issue is None:
        active_worktree_state = "none"
    elif issue_conflict:
        active_worktree_state = "mismatch"
    elif entry is not None and root_branch_state == "default":
        active_worktree_state = "matches"
    else:
        active_worktree_state = "mismatch"

    cleanup_contract_state = _contract_state(root)

    resolved = {
        "issue_number": int(active_issue) if active_issue else None,
        "worktree_realpath": entry.get("worktree_realpath") if entry else None,
        "branch_ref": entry.get("branch_ref") if entry else None,
        "cwd_classification": cwd_class,
        "git_common_dir": entry.get("git_common_dir") if entry else None,
    }

    blocked: list[str] = []
    hints: list[dict] = []
    if issue_conflict:
        # LOOP_ISSUE_NUMBER contradicts the catalog identity of the cwd's worktree.
        status = "human_required"
        blocked.append(ACTIVE_ISSUE_CATALOG_CONFLICT)
        hints.append({
            "action": "resolve_active_issue_conflict",
            "requires_human_override": True,
            "detail": "LOOP_ISSUE_NUMBER contradicts the Issue bound to the current worktree in the catalog",
        })
    elif root_branch_state in ("drifted", "detached_or_unknown") and active_worktree_state == "mismatch":
        status = "human_required"
        blocked.append(ROOT_DRIFT_ACTIVE_WORKTREE_MISMATCH)
        hints.append({
            "action": "recover_root_to_default_branch",
            "guard": "local_main_branch_guard",
            "requires_human_override": True,
            "detail": "root drifted while an issue worktree is active; human override required",
        })
    elif root_branch_state != "default":
        status = "blocked"
        blocked.append("root_branch_drift")
        hints.append({
            "action": "recover_root_to_default_branch",
            "guard": "local_main_branch_guard",
            "requires_human_override": False,
            "detail": "switch the local root checkout back to the default branch",
        })
    else:
        status = "ok"
        # Blocker 7: only advertise the gated bare-git route when a valid V3 contract
        # actually BINDS to the resolved active worktree (path + branch). Otherwise the
        # runtime guard would deny a route the preflight claimed was runnable.
        if cleanup_contract_state == "valid_v3" and _contract_binds_to(root, entry):
            hints.append({"action": "run_gated_cleanup", "guard": "worktree_scope_guard",
                          "detail": "a valid one-shot V3 contract is present and bound to the active worktree"})
        else:
            hints.append({"action": "run_cleanup_exec",
                          "detail": "use the cleanup_exec authorization boundary"})

    return {
        "schema": SCHEMA,
        "status": status,
        "root_branch_state": root_branch_state,
        "active_worktree_state": active_worktree_state,
        "cleanup_contract_state": cleanup_contract_state,
        "resolved_worktree": resolved,
        "safe_scratch_contract_path": SAFE_SCRATCH_CONTRACT_PATH,
        "allowed_next_commands": hints,
        "blocked_reason_codes": blocked,
    }


def _blocked_root_resolution(reason_code: str | None) -> dict:
    """Structured failure when the canonical primary root cannot be verified (Issue #2979).

    The candidate (possibly an issue worktree) is never silently used as the root.
    Status is never ``ok``; the fixed reason literal is carried on the existing
    ``blocked_reason_codes`` field.
    """
    return {
        "schema": SCHEMA,
        "status": "human_required",
        "root_branch_state": "detached_or_unknown",
        "active_worktree_state": "mismatch",
        "cleanup_contract_state": "present_but_invalid",
        "resolved_worktree": {"issue_number": None, "worktree_realpath": None, "branch_ref": None,
                              "cwd_classification": "unknown", "git_common_dir": None},
        "safe_scratch_contract_path": SAFE_SCRATCH_CONTRACT_PATH,
        "allowed_next_commands": [{
            "action": "resolve_canonical_root",
            "requires_human_override": True,
            "detail": "canonical primary root could not be verified; no cleanup was attempted",
        }],
        "blocked_reason_codes": [reason_code or "primary_root_unresolved"],
    }


def _blocked_deadline(root: str) -> dict:
    return {
        "schema": SCHEMA,
        "status": "human_required",
        "root_branch_state": "detached_or_unknown",
        "active_worktree_state": "mismatch",
        "cleanup_contract_state": "present_but_invalid",
        "resolved_worktree": {"issue_number": None, "worktree_realpath": None, "branch_ref": None,
                              "cwd_classification": "unknown", "git_common_dir": None},
        "safe_scratch_contract_path": SAFE_SCRATCH_CONTRACT_PATH,
        "allowed_next_commands": [{"action": "retry_preflight", "detail": "guard deadline exceeded"}],
        "blocked_reason_codes": ["guard_deadline_exceeded"],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Machine-decide guard arbitration preflight.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--project-root", default=None)
    p.add_argument("--cwd", default=None)
    a = p.parse_args(argv)
    result = build_preflight(project_root=a.project_root, cwd=a.cwd)
    if a.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"status: {result['status']}")
        print(f"root_branch_state: {result['root_branch_state']}")
        print(f"active_worktree_state: {result['active_worktree_state']}")
        print(f"cleanup_contract_state: {result['cleanup_contract_state']}")
    return {"ok": 0, "blocked": 1, "human_required": 2}.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())

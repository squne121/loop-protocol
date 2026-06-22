#!/usr/bin/env python3
"""guard_preflight.py — machine-decide guard arbitration state before cleanup.

Issue #1137 (AC1). Emits ``AGENT_GUARD_PREFLIGHT_V1`` describing the interaction
between ``local_main_branch_guard`` (root-branch drift) and
``worktree_scope_guard`` (worktree mutation scope) so post-merge-cleanup can
decide its next step *without* an agent running ``git -C <worktree>`` clean
probes (Design Decision 6).

Recovery deadlock policy B (Design Decision 4): when the root checkout has
drifted away from the default branch *and* an active issue worktree marker is
present, this preflight does NOT perform an automatic ``git switch main``
mutation. It returns ``status: human_required`` with structured
``allowed_next_commands`` recovery hints and never emits raw shell commands,
raw env values, or secret-like paths to stderr.
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
_GUARDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "agent-guards")
if _GUARDS_DIR not in sys.path:
    sys.path.insert(0, _GUARDS_DIR)

from cleanup_contract_v3 import (  # noqa: E402
    ROOT_DRIFT_ACTIVE_WORKTREE_MISMATCH,
    SAFE_SCRATCH_CONTRACT_PATH,
    is_expired,
    validate_v3_contract,
)

try:  # reuse the canonical branch-state helpers; fail-closed if unavailable.
    from local_main_branch_guard import (  # noqa: E402
        classify_root_state,
        get_current_branch,
        resolve_default_branch,
    )
    _GUARD_IMPORT_OK = True
except Exception:  # pragma: no cover - defensive
    _GUARD_IMPORT_OK = False


_ISSUE_RE = re.compile(r"^(?:worktree-)?issue-(\d+)\b")

SCHEMA = "AGENT_GUARD_PREFLIGHT_V1"


def resolve_project_root() -> str:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return os.path.realpath(env_root)
    agent_ops = os.path.dirname(os.path.realpath(__file__))
    scripts = os.path.dirname(agent_ops)
    return os.path.realpath(os.path.dirname(scripts))


def _active_issue(current_branch: str | None) -> str | None:
    """Resolve the active issue marker: LOOP_ISSUE_NUMBER, else issue-like branch."""
    env_issue = os.environ.get("LOOP_ISSUE_NUMBER")
    if env_issue and env_issue.strip().isdigit():
        return env_issue.strip()
    if current_branch:
        m = _ISSUE_RE.match(current_branch)
        if m:
            return m.group(1)
    return None


def _read_cleanup_contract_state(project_root: str) -> str:
    """Classify the safe-scratch cleanup contract: absent | valid | invalid | expired."""
    path = os.path.join(project_root, SAFE_SCRATCH_CONTRACT_PATH)
    if not os.path.isfile(path):
        return "absent"
    try:
        with open(path, encoding="utf-8") as f:
            contract = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "invalid"
    ok, _reason = validate_v3_contract(contract)
    if not ok:
        return "invalid"
    if is_expired(contract):
        return "expired"
    return "valid"


def build_preflight(project_root: str | None = None, cwd: str | None = None) -> dict:
    """Compute the AGENT_GUARD_PREFLIGHT_V1 decision (no mutation)."""
    root = os.path.realpath(project_root) if project_root else resolve_project_root()
    probe_cwd = cwd or root

    if not _GUARD_IMPORT_OK:
        # Fail-closed: cannot evaluate branch state → require human.
        return {
            "schema": SCHEMA,
            "status": "human_required",
            "root_branch_state": "detached_or_unknown",
            "active_worktree_state": "mismatch",
            "cleanup_contract_state": _read_cleanup_contract_state(root),
            "safe_scratch_contract_path": SAFE_SCRATCH_CONTRACT_PATH,
            "allowed_next_commands": [
                {"action": "inspect_guard_module", "detail": "local_main_branch_guard import failed"}
            ],
            "blocked_reason_codes": ["guard_module_unavailable"],
        }

    current_branch = get_current_branch(cwd=root)
    default_branch = resolve_default_branch(cwd=root)
    root_branch_state = classify_root_state(current_branch, default_branch)

    active_issue = _active_issue(current_branch)
    if active_issue is not None and root_branch_state == "default":
        active_worktree_state = "matches"
    elif active_issue is not None and root_branch_state != "default":
        active_worktree_state = "mismatch"
    else:
        active_worktree_state = "none"

    cleanup_contract_state = _read_cleanup_contract_state(root)

    blocked_reason_codes: list[str] = []
    allowed_next_commands: list[dict] = []

    # Deadlock: root drift + active worktree marker — policy B (no auto mutation).
    if root_branch_state in ("drifted", "detached_or_unknown") and active_worktree_state == "mismatch":
        status = "human_required"
        blocked_reason_codes.append(ROOT_DRIFT_ACTIVE_WORKTREE_MISMATCH)
        allowed_next_commands.append(
            {
                "action": "recover_root_to_default_branch",
                "guard": "local_main_branch_guard",
                "requires_human_override": True,
                "detail": "root checkout drifted while an issue worktree is active; "
                "human must approve the existing override path before recovery",
            }
        )
    elif root_branch_state != "default":
        status = "blocked"
        blocked_reason_codes.append("root_branch_drift")
        allowed_next_commands.append(
            {
                "action": "recover_root_to_default_branch",
                "guard": "local_main_branch_guard",
                "requires_human_override": False,
                "detail": "switch the local root checkout back to the default branch",
            }
        )
    else:
        status = "ok"
        if cleanup_contract_state == "valid":
            allowed_next_commands.append(
                {
                    "action": "run_gated_cleanup",
                    "guard": "worktree_scope_guard",
                    "detail": "exact cleanup argv is gated by the materialized V3 contract",
                }
            )
        elif cleanup_contract_state in ("absent", "expired", "invalid"):
            allowed_next_commands.append(
                {
                    "action": "materialize_cleanup_contract",
                    "detail": "regenerate a fresh V3 contract at the safe scratch path",
                }
            )

    return {
        "schema": SCHEMA,
        "status": status,
        "root_branch_state": root_branch_state,
        "active_worktree_state": active_worktree_state,
        "cleanup_contract_state": cleanup_contract_state,
        "safe_scratch_contract_path": SAFE_SCRATCH_CONTRACT_PATH,
        "allowed_next_commands": allowed_next_commands,
        "blocked_reason_codes": blocked_reason_codes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Machine-decide guard arbitration preflight.")
    parser.add_argument("--json", action="store_true", help="emit AGENT_GUARD_PREFLIGHT_V1 as JSON")
    parser.add_argument("--project-root", default=None)
    args = parser.parse_args(argv)

    result = build_preflight(project_root=args.project_root)

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"status: {result['status']}")
        print(f"root_branch_state: {result['root_branch_state']}")
        print(f"active_worktree_state: {result['active_worktree_state']}")
        print(f"cleanup_contract_state: {result['cleanup_contract_state']}")
    # exit 0 for ok, 1 for blocked, 2 for human_required (deterministic for callers)
    return {"ok": 0, "blocked": 1, "human_required": 2}.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())

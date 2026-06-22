#!/usr/bin/env python3
"""cleanup_contract_v3.py — shared POST_MERGE_CLEANUP_REQUEST_V3 validator + canonicalization.

Issue #1137. Single source of truth for cleanup-contract V3 logic so that
``materialize_cleanup_contract.py`` and ``.claude/hooks/worktree_scope_guard.py``
do not re-implement validation / hashing (Design Decision 3).

Responsibilities:
  - V3 contract schema validation (``expires_at`` / ``command_hash`` / required fields)
  - ``command_hash`` canonicalization — canonical argv JSON SHA-256
  - ``SHARED_CLEANUP_REASON_CODES`` — the Claude / Codex parity vocabulary
  - the safe-scratch contract path constant (gitignored, local-only, expiring)

The module is import-safe (no side effects) so a PreToolUse hook can import it
without spawning subprocesses or touching the filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

# POST_MERGE_CLEANUP_REQUEST_V3 schema marker.
SCHEMA_V3 = "POST_MERGE_CLEANUP_REQUEST_V3"

# Repo-relative safe scratch path. Gitignored (``artifacts/``), local-only,
# expiring, raw-path-not-displayed contract. NOT a distributed CI artifact.
SAFE_SCRATCH_CONTRACT_PATH = "artifacts/agent-ops/cleanup_contract.json"

# ── Shared cleanup reason codes (Claude / Codex parity target — AC6/AC9) ───────
NO_CLEANUP_CONTRACT = "no_cleanup_contract"
CLEANUP_CONTRACT_EXPIRED = "cleanup_contract_expired"
CLEANUP_COMMAND_HASH_MISMATCH = "cleanup_command_hash_mismatch"
WORKTREE_PATH_MISMATCH = "worktree_path_mismatch"
WORKTREE_DIRTY = "worktree_dirty"
BRANCH_FORCE_DELETE_DENIED = "branch_force_delete_denied"
ROOT_DRIFT_ACTIVE_WORKTREE_MISMATCH = "root_drift_active_worktree_mismatch"

# The canonical, ordered tuple both runtimes (Claude worktree_scope_guard and
# Codex local_main_branch_guard) must agree on. Parity tests assert that each
# runtime returns reason codes drawn from exactly this set.
SHARED_CLEANUP_REASON_CODES = (
    NO_CLEANUP_CONTRACT,
    CLEANUP_CONTRACT_EXPIRED,
    CLEANUP_COMMAND_HASH_MISMATCH,
    WORKTREE_PATH_MISMATCH,
    WORKTREE_DIRTY,
    BRANCH_FORCE_DELETE_DENIED,
    ROOT_DRIFT_ACTIVE_WORKTREE_MISMATCH,
)

# Characters that must never appear in a branch_name (shell-control / whitespace).
_BRANCH_FORBIDDEN_CHARS = " \t\n\r;|&<>$`(){}!"


def canonical_command_hash(
    argv: list[str],
    project_root: str,
    worktree_path: str,
    branch_name: str,
    require_clean: bool,
) -> str:
    """Return the SHA-256 of the canonical argv JSON (Design Decision 3).

    The canonical object binds an *exact* argv to the cleanup target identity,
    so a stale / replayed contract cannot authorize a different command. The
    JSON is serialized deterministically with
    ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` and hashed as
    UTF-8 bytes.
    """
    if not isinstance(argv, (list, tuple)):
        raise TypeError("argv must be a list/tuple of strings")
    payload = {
        "argv": [str(a) for a in argv],
        "project_root": project_root,
        "worktree_path": worktree_path,
        "branch_name": branch_name,
        "require_clean": bool(require_clean),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def worktree_remove_argv(worktree_path: str) -> list[str]:
    """Canonical exact argv for the gated ``git worktree remove`` command."""
    return ["git", "worktree", "remove", worktree_path]


def command_hash_for_worktree_remove(
    worktree_path: str,
    project_root: str,
    branch_name: str,
    require_clean: bool,
) -> str:
    """Convenience: command_hash bound to the ``git worktree remove <path>`` argv.

    ``git worktree remove`` is the destructive cleanup command, so the contract's
    ``command_hash`` is bound to it. ``git branch -d`` (safe delete; refuses on
    unmerged) is gated on ``branch_name`` match + V3 validity instead.
    """
    return canonical_command_hash(
        worktree_remove_argv(worktree_path),
        project_root,
        worktree_path,
        branch_name,
        require_clean,
    )


def parse_iso8601(value: object) -> datetime | None:
    """Parse an ISO8601 timestamp to an aware UTC datetime; None on failure."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_expired(contract: dict, now: datetime | None = None) -> bool:
    """True iff ``expires_at`` is absent / unparseable / in the past (fail-closed)."""
    if not isinstance(contract, dict):
        return True
    exp = parse_iso8601(contract.get("expires_at"))
    if exp is None:
        # Missing or unparseable expiry is treated as expired (Design Decision 1).
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= exp


def _is_hex_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def validate_v3_contract(contract: object) -> tuple[bool, str | None]:
    """Validate a POST_MERGE_CLEANUP_REQUEST_V3 contract (schema only, not expiry).

    Returns ``(is_valid, reason)``. ``reason`` is a machine-readable failure code
    when invalid, ``None`` when valid. Expiry is intentionally NOT checked here so
    callers can distinguish ``invalid`` (schema) from ``expired`` (use
    :func:`is_expired`). ``require_clean`` must be exactly ``True``.
    """
    if not isinstance(contract, dict):
        return False, "contract_not_object"
    if contract.get("schema") != SCHEMA_V3:
        return False, "schema_mismatch"

    wt_path = contract.get("worktree_path")
    if not isinstance(wt_path, str) or not os.path.isabs(wt_path):
        return False, "worktree_path_invalid"

    branch = contract.get("branch_name")
    if not isinstance(branch, str) or not branch:
        return False, "branch_name_invalid"
    if any(c in branch for c in _BRANCH_FORBIDDEN_CHARS):
        return False, "branch_name_invalid"

    if contract.get("require_clean") is not True:
        return False, "require_clean_not_true"

    if not _is_hex_sha256(contract.get("command_hash")):
        return False, "command_hash_invalid"

    # expires_at must be present and parseable (the *time* check is is_expired).
    if parse_iso8601(contract.get("expires_at")) is None:
        return False, "expires_at_invalid"

    pr_number = contract.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        return False, "pr_number_invalid"

    linked = contract.get("linked_issue_number")
    if linked is not None and (not isinstance(linked, int) or isinstance(linked, bool)):
        return False, "linked_issue_number_invalid"

    return True, None


def build_v3_contract(
    *,
    pr_number: int,
    linked_issue_number: int | None,
    worktree_path: str,
    branch_name: str,
    project_root: str,
    expires_at: str,
    require_clean: bool = True,
) -> dict:
    """Build a fully-populated, valid POST_MERGE_CLEANUP_REQUEST_V3 dict.

    ``command_hash`` is bound to the ``git worktree remove <worktree_path>`` argv.
    """
    command_hash = command_hash_for_worktree_remove(
        worktree_path, project_root, branch_name, require_clean
    )
    return {
        "schema": SCHEMA_V3,
        "pr_number": pr_number,
        "linked_issue_number": linked_issue_number,
        "worktree_path": worktree_path,
        "branch_name": branch_name,
        "require_clean": bool(require_clean),
        "command_hash": command_hash,
        "expires_at": expires_at,
    }

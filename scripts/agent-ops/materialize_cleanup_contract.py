#!/usr/bin/env python3
"""materialize_cleanup_contract.py — deliver a cleanup contract to a safe scratch path.

Issue #1137 (AC2). Writes a POST_MERGE_CLEANUP_REQUEST_V3 contract to
``artifacts/agent-ops/cleanup_contract.json`` (gitignored, local-only, expiring).

Why this exists: the legacy ``CLAUDE_WORKTREE_CLEANUP_CONTRACT=... git ...``
env-prefix form is blocked by ``local_main_branch_guard`` *before* arbitration
(``unparseable_branch_mutation`` / inline-env-override), and direct Write to
``.claude/artifacts/`` is permission-denied from the main session. Materializing
the contract to a safe scratch path under ``artifacts/`` removes both
dependencies: this script can run from ``cwd=local main root`` on the default
branch with an active issue worktree present and is not blocked by the
PreToolUse guards (a plain ``python3`` invocation is not a branch mutation).

Safety (Design Decisions 2 / 6):
  - atomic write: temp file + ``os.replace``
  - fail-closed if any existing parent component is a symlink
  - ``chmod 0600`` best-effort on the materialized file
  - no env-prefix dependency, no ``.claude/artifacts`` dependency
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cleanup_contract_v3 import (  # noqa: E402
    SAFE_SCRATCH_CONTRACT_PATH,
    build_v3_contract,
    is_expired,
    validate_v3_contract,
)


def resolve_project_root() -> str:
    """Resolve project root: CLAUDE_PROJECT_DIR, else walk up from this file.

    ``scripts/agent-ops/materialize_cleanup_contract.py`` → repo root is two
    directories up. ``git rev-parse --show-toplevel`` is intentionally NOT used
    so worktree isolation does not redirect us to the main repo root.
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return os.path.realpath(env_root)
    agent_ops = os.path.dirname(os.path.realpath(__file__))
    scripts = os.path.dirname(agent_ops)
    return os.path.realpath(os.path.dirname(scripts))


def _has_symlink_component(target_path: str, root_real: str) -> bool:
    """True (fail-closed) iff any existing component from root down to target is a symlink.

    Non-existent intermediate directories are OK (they will be created with
    ``makedirs``); an existing symlink anywhere on the path is a block.
    """
    if os.path.islink(root_real):
        return True
    try:
        rel = os.path.relpath(target_path, root_real)
    except ValueError:
        return True
    if rel.startswith(".."):
        return True
    current = root_real
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False


def _atomic_write_json(target_path: str, data: dict) -> None:
    """Write ``data`` as JSON to ``target_path`` atomically with mode 0600."""
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".cleanup_contract.", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, separators=(",", ":"))
            f.write("\n")
        try:
            os.chmod(tmp_path, 0o600)  # best-effort
        except OSError:
            pass
        os.replace(tmp_path, target_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def materialize(
    *,
    pr_number: int,
    linked_issue_number: int | None,
    worktree_path: str,
    branch_name: str,
    expires_in_seconds: int,
    project_root: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Build, validate, and atomically write the V3 cleanup contract.

    Returns a result dict with ``status`` (``ok`` | ``error``), the resolved
    ``contract_path`` (repo-relative), and either the ``contract`` or an
    ``error`` reason. Raises nothing for expected fail-closed conditions —
    callers inspect ``status``.
    """
    root = os.path.realpath(project_root) if project_root else resolve_project_root()
    abs_wt = worktree_path if os.path.isabs(worktree_path) else os.path.join(root, worktree_path)
    abs_wt = os.path.abspath(abs_wt)

    if now is None:
        now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=expires_in_seconds)).isoformat().replace("+00:00", "Z")

    contract = build_v3_contract(
        pr_number=pr_number,
        linked_issue_number=linked_issue_number,
        worktree_path=abs_wt,
        branch_name=branch_name,
        project_root=root,
        expires_at=expires_at,
    )

    ok, reason = validate_v3_contract(contract)
    if not ok:
        return {"status": "error", "error": f"built_contract_invalid:{reason}"}
    if is_expired(contract, now=now):
        return {"status": "error", "error": "expires_in_seconds_must_be_positive"}

    target_path = os.path.join(root, SAFE_SCRATCH_CONTRACT_PATH)

    # Symlink fail-closed: anchor the component walk on the *unresolved*
    # artifacts/ safe-prefix dir so a symlinked safe_root or intermediate
    # component is detected (realpath() would silently resolve it away).
    safe_root = os.path.join(root, SAFE_SCRATCH_CONTRACT_PATH.split("/", 1)[0])
    if os.path.exists(safe_root) and _has_symlink_component(target_path, safe_root):
        return {"status": "error", "error": "symlink_component_denied"}

    _atomic_write_json(target_path, contract)
    return {
        "status": "ok",
        "contract_path": SAFE_SCRATCH_CONTRACT_PATH,
        "command_hash": contract["command_hash"],
        "expires_at": contract["expires_at"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize a V3 cleanup contract to a safe scratch path.")
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--linked-issue-number", type=int, default=None)
    parser.add_argument("--worktree-path", required=True)
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--expires-in-seconds", type=int, default=3600)
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    args = parser.parse_args(argv)

    if args.expires_in_seconds <= 0:
        result = {"status": "error", "error": "expires_in_seconds_must_be_positive"}
    else:
        result = materialize(
            pr_number=args.pr_number,
            linked_issue_number=args.linked_issue_number,
            worktree_path=args.worktree_path,
            branch_name=args.branch_name,
            expires_in_seconds=args.expires_in_seconds,
            project_root=args.project_root,
        )

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(result.get("status", "error"))
        if result.get("status") == "ok":
            print(f"contract_path: {result['contract_path']}")
        else:
            print(f"error: {result.get('error')}")
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

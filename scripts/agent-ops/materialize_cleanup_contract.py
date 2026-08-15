#!/usr/bin/env python3
"""materialize_cleanup_contract.py — issue a verified one-shot V3 contract (Issue #1137).

Defense-in-depth path for bare ``git`` cleanup. Per the PR #1139 OWNER review
(Blocker 5), this no longer issues a self-asserted capability: it runs the SAME
``verify_cleanup_authorization`` checks ``cleanup_exec`` does (PR merged / head
branch / linked issue / catalog / branch / root default / clean) and ONLY then
materializes a single-operation, nonce-bound, short-TTL contract to the
gitignored safe scratch path using durable + symlink-safe IO.

The primary cleanup path is ``cleanup_exec`` (single executor). This contract
exists for the bare-git route the guard arbitrates.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cleanup_contract_v3 import (  # noqa: E402
    MAX_TTL_SECONDS,
    OP_LOCAL_ONLY_DISCARD,
    OP_WORKTREE_REMOVE,
    OPERATIONS,
    SAFE_SCRATCH_CONTRACT_PATH,
    SCHEMA_V3,
    canonical_command_hash,
    expected_argv,
    validate_v3_contract,
    write_json_durably,
)
from cleanup_exec import (  # noqa: E402
    resolve_project_root,
    run_discard_check,
    run_discard_consume,
    verify_cleanup_authorization,
    verify_discard_authorization,
)
from worktree_catalog import Deadline, GuardDeadlineExceeded  # noqa: E402


def materialize(
    *,
    pr_number: int,
    linked_issue_number: int | None,
    worktree_path: str,
    branch_name: str,
    operation: str = OP_WORKTREE_REMOVE,
    ttl_seconds: int = 300,
    project_root: str | None = None,
    now: datetime | None = None,
    verify: bool = True,
    budget_seconds: float = 60.0,
) -> dict:
    if operation not in OPERATIONS:
        return {"status": "error", "error": f"operation_invalid:{operation}"}
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        return {"status": "error", "error": "ttl_out_of_bounds"}

    root = os.path.realpath(project_root) if project_root else resolve_project_root()
    worktree_real = os.path.realpath(
        worktree_path if os.path.isabs(worktree_path) else os.path.join(root, worktree_path)
    )

    req = {
        "pr_number": pr_number,
        "linked_issue_number": linked_issue_number,
        "worktree_path": worktree_real,
        "branch_name": branch_name,
    }

    pr_head_sha = None
    local_tip_sha = None
    if verify:
        try:
            if operation == OP_LOCAL_ONLY_DISCARD:
                # Issue #1523: the discard lane uses its OWN authorization
                # (worktree still on disk, PR-head-ancestor + local-only-commit
                # candidacy) rather than the normal-cleanup verifier.
                ok, reason, verified_fields = verify_discard_authorization(
                    req, root, Deadline(budget_seconds)
                )
                if ok:
                    pr_head_sha = verified_fields.get("pr_head_sha")
                    local_tip_sha = verified_fields.get("local_tip_sha")
            else:
                ok, reason, _verified = verify_cleanup_authorization(req, root, Deadline(budget_seconds))
        except GuardDeadlineExceeded as e:
            return {"status": "error", "error": str(e)}
        if not ok:
            return {"status": "refused", "reason_code": reason}

    if now is None:
        now = datetime.now(timezone.utc)
    nonce = secrets.token_hex(16)
    issued_at = now.isoformat().replace("+00:00", "Z")
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
    command_hash = canonical_command_hash(
        expected_argv(operation, worktree_real, branch_name), operation, root, nonce
    )
    contract = {
        "schema": SCHEMA_V3,
        "pr_number": pr_number,
        "linked_issue_number": linked_issue_number,
        "worktree_path": worktree_real,
        "branch_name": branch_name,
        "require_clean": True,
        "operation": operation,
        "command_hash": command_hash,
        "nonce": nonce,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    if operation == OP_LOCAL_ONLY_DISCARD:
        # Issue #1523 AC2: bind the confirmation to the EXACT PR head SHA and
        # local branch tip SHA observed at authorization time — the consume
        # step (``run_discard_consume``) re-verifies both against fresh live
        # state before performing the destructive discard.
        contract["pr_head_sha"] = pr_head_sha
        contract["local_tip_sha"] = local_tip_sha

    valid, vreason = validate_v3_contract(contract, now=now)
    if not valid:
        return {"status": "error", "error": f"built_contract_invalid:{vreason}"}

    try:
        write_json_durably(root, SAFE_SCRATCH_CONTRACT_PATH, contract)
    except (OSError, ValueError) as e:
        return {"status": "error", "error": f"write_failed:{type(e).__name__}:{str(e)[:80]}"}

    return {
        "status": "ok",
        "contract_path": SAFE_SCRATCH_CONTRACT_PATH,
        "operation": operation,
        "expires_at": expires_at,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Materialize a verified one-shot V3 cleanup contract.")
    p.add_argument("--pr-number", type=int, required=True)
    p.add_argument("--linked-issue-number", type=int, default=None)
    p.add_argument("--worktree-path", required=True)
    p.add_argument("--branch-name", required=True)
    p.add_argument("--operation", default=OP_WORKTREE_REMOVE, choices=list(OPERATIONS))
    p.add_argument("--ttl-seconds", type=int, default=300)
    # Issue #1523: --check / --consume are the EXPLICIT human confirmation
    # commands for the local-only discard lane (AC2/AC5). Neither performs any
    # destructive action on its own:
    #   (no flag)   issue the target+SHA-bound one-shot confirmation contract
    #               (the ONLY way a discard-consume can later succeed)
    #   --check     non-destructive: report discard candidacy + local-only
    #               commit SHAs a human would be confirming (AC1)
    #   --consume   claim-first consume of a PREVIOUSLY issued contract; this
    #               is the ONLY invocation that performs the destructive
    #               discard, and only succeeds if a human already ran the
    #               plain (no-flag) issuance invocation first
    p.add_argument("--check", action="store_true", help="Discard lane: verify-only, no contract issued.")
    p.add_argument("--consume", action="store_true", help="Discard lane: consume a previously issued contract.")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.check and a.consume:
        result = {"status": "error", "error": "check_and_consume_mutually_exclusive"}
    elif a.check or a.consume:
        if a.operation != OP_LOCAL_ONLY_DISCARD:
            result = {"status": "error", "error": "check_and_consume_require_local_only_discard_operation"}
        else:
            req = {
                "pr_number": a.pr_number,
                "linked_issue_number": a.linked_issue_number,
                "worktree_path": a.worktree_path,
                "branch_name": a.branch_name,
            }
            result = run_discard_check(req) if a.check else run_discard_consume(req)
    else:
        # Blocker 1 / Blocker 5: the public CLI exposes neither --no-verify
        # (skipping authorization) nor --project-root (retargeting).
        # Authorization always runs and the trusted root is resolved
        # internally. ``verify`` / ``project_root`` remain function parameters
        # for dependency-injected unit tests only.
        result = materialize(
            pr_number=a.pr_number,
            linked_issue_number=a.linked_issue_number,
            worktree_path=a.worktree_path,
            branch_name=a.branch_name,
            operation=a.operation,
            ttl_seconds=a.ttl_seconds,
        )
    if a.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(result.get("status", "error"))
    return 0 if result.get("status") in ("ok", "confirmation_required") else 1


if __name__ == "__main__":
    raise SystemExit(main())

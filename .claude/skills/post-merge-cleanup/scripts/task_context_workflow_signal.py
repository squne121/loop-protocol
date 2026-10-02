#!/usr/bin/env python3
"""Apply post-merge cleanup's trusted Task Context commit points (Issue #2565).

The caller supplies a fresh, already-fetched GraphQL merged-PR snapshot.  This
adapter performs no GitHub I/O, so snapshot acquisition remains outside the
Task Context SQLite transaction.

Issue #2817 adds two additive, explicit phases next to ``merged`` / ``completed``
(whose output is frozen and unchanged):

* ``--phase recover``: explicit retroactive claim recovery. It re-validates a
  fresh snapshot and the merge identity, then asks ``task_contextctl.py signal
  recover`` to attach the missing Issue/PR claims in one transaction.
* ``--phase local-only``: a Task-Context-free permit for the local cleanup of a
  merged PR that Task Context cannot record. It never calls ``task_contextctl``
  and writes nothing; the caller-declared ``--task-context-outcome`` is checked
  against a closed enum, which is a fail-closed local guardrail, not a security
  boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_ROOT / "scripts"))
from check_post_merge_cleanup_boundary import validate_report_v1  # noqa: E402

_CTL = _ROOT / "scripts" / "task-context" / "task_contextctl.py"

_HEX40 = re.compile(r"^[0-9a-f]{40}$")

# Issue #2817: a snapshot is "fresh" for `--phase recover` / `--phase local-only`
# only when its file mtime is within this many seconds of adapter start. The
# adapter performs no GitHub I/O, so this is the verifiable definition of
# "fetched immediately before this invocation".
SNAPSHOT_MAX_AGE_SECONDS = 300

# Issue #2817: the single source of the local-only permitted outcomes -- every
# case where Task Context cannot record the lifecycle. `<reason>` of
# `unbound/<reason>` is one of the six diagnose-origin causes that remain
# recordable-by-neither; `unbound/origin_ambiguous` and `unbound/resolved` are
# deliberately excluded. This is a caller-declared, fail-closed local
# guardrail, NOT a security boundary: the adapter cannot verify the outcome.
LOCAL_ONLY_UNBOUND_REASONS = (
    "origin_session_missing",
    "origin_run_not_found",
    "origin_run_ended",
    "origin_run_kind_mismatch",
    "origin_task_unattached",
    "origin_binding_session_mismatch",
)
LOCAL_ONLY_PERMITTED_OUTCOMES = (
    "deferred/IMPLEMENTATION_NOT_READY",
    "conflict/FACT_TASK_IDENTITY_CONFLICT",
    "conflict/OUT_OF_ORDER_SIGNAL",
    "recovery_rejected",
    *(f"unbound/{reason}" for reason in LOCAL_ONLY_UNBOUND_REASONS),
)


def _final_success_receipt_reason(receipt_file: Path | None) -> str | None:
    """Return a fail-closed reason unless the worker report proves final success."""
    if receipt_file is None:
        return "CLEANUP_FINAL_SUCCESS_RECEIPT_REQUIRED"
    try:
        receipt_text = receipt_file.read_text(encoding="utf-8")
    except OSError:
        return "CLEANUP_FINAL_SUCCESS_RECEIPT_INVALID"
    try:
        payload = json.loads(receipt_text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError:
            return "CLEANUP_FINAL_SUCCESS_RECEIPT_INVALID"
        try:
            payload = yaml.safe_load(receipt_text)
        except yaml.YAMLError:
            return "CLEANUP_FINAL_SUCCESS_RECEIPT_INVALID"
    if isinstance(payload, dict) and set(payload) == {"POST_MERGE_CLEANUP_REPORT_V1"}:
        payload = payload["POST_MERGE_CLEANUP_REPORT_V1"]
    validation = validate_report_v1(payload)
    if not validation.valid:
        return "CLEANUP_FINAL_SUCCESS_RECEIPT_INVALID"
    assert isinstance(payload, dict)
    if (
        payload["status"] != "ok"
        or payload["human_review_required"] is not False
        or payload["unresolved_cleanup_items"]
        or payload["errors"]
    ):
        return "CLEANUP_NOT_FINAL_SUCCESS"
    return None


def _merged_evidence(snapshot: object, issue_number: int, pr_number: int) -> tuple[dict | None, str]:
    # A GraphQL response may carry plausible partial data alongside top-level
    # errors. It is not authoritative merged-PR evidence and must never reach
    # either signal application or cleanup selection.
    if not isinstance(snapshot, dict) or "errors" in snapshot:
        return None, "RELATION_UNAVAILABLE"
    try:
        pr = snapshot["data"]["repository"]["pullRequest"]
        repo = snapshot["data"]["repository"]["nameWithOwner"]
        nodes = pr["closingIssuesReferences"]["nodes"]
        oid = pr["mergeCommit"]["oid"]
    except (KeyError, TypeError):
        return None, "RELATION_UNAVAILABLE"
    if pr.get("merged") is not True or pr.get("number") != pr_number or not isinstance(repo, str):
        return None, "MERGED_SNAPSHOT_INVALID"
    if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
        return None, "RELATION_ISSUE_MISMATCH"
    relation_repository = nodes[0].get("repository")
    relation_repo = relation_repository.get("nameWithOwner") if isinstance(relation_repository, dict) else None
    if (
        nodes[0].get("number") != issue_number
        or not isinstance(relation_repo, str)
        or relation_repo.lower() != repo.lower()
    ):
        return None, "RELATION_ISSUE_MISMATCH"
    if not isinstance(oid, str):
        return None, "MERGE_OID_INVALID"
    return {"repo": repo.lower(), "issue_number": issue_number, "pr_number": pr_number, "merge_commit_oid": oid}, "OK"


def _invoke_ctl(argv: list[str], body: dict, *, origin_session_id: str | None = None) -> dict | None:
    """Invoke ``task_contextctl`` and return its parsed result envelope
    (``{"status": ..., "data"/"code"/"message": ...}``), or ``None`` if the
    subprocess itself could not be run or its stdout could not be parsed as
    one JSON object.

    Issue #2719 AC3: ``task_contextctl.py`` reads its origin session id from
    its *own* process environment (``os.environ["CLAUDE_CODE_SESSION_ID"]``,
    see ``task_contextctl._dispatch``). This subprocess call mirrors the
    explicit override pattern ``.claude/hooks/task_context/ctl_client.py``'s
    ``call()`` already uses for the hook transport: build ``child_env`` from
    a copy of this process's environment, then explicitly set the
    caller-supplied ``origin_session_id`` into it before starting the child
    process, so the origin session provenance is never left to implicit
    inheritance.

    PR #2795 review fix_delta P2-A (comment 5852749710, finding 3): this is
    the single shared subprocess-invocation primitive both ``_run`` (the
    ``signal apply``/``cleanup begin`` disposition-oriented callers) and
    ``diagnose_origin`` (the read-only diagnostic caller) build on, so a
    caller that passed an explicit ``origin_session_id`` for a failed
    ``signal apply``/``cleanup begin`` call can pass that exact same value
    to the following diagnose-origin call -- the two invocations must never
    silently drift onto different effective origins (one reading this
    adapter's own ambient ``CLAUDE_CODE_SESSION_ID``, the other honoring the
    explicit override)."""
    child_env = dict(os.environ)
    if origin_session_id is not None:
        child_env["CLAUDE_CODE_SESSION_ID"] = origin_session_id
    try:
        proc = subprocess.run(
            [sys.executable, str(_CTL), *argv],
            input=json.dumps(body),
            text=True,
            capture_output=True,
            timeout=15,
            env=child_env,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if not proc.stdout.splitlines():
        return None
    try:
        result = json.loads(proc.stdout.splitlines()[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict):
        return None
    return result


def _run(argv: list[str], body: dict, *, origin_session_id: str | None = None) -> dict:
    """Invoke ``task_contextctl`` and normalize its result to the typed
    ``{"disposition": ..., "reason_code": ...}`` shape every other producer
    adapter (e.g. ``open_pr.emit_implementation_pr_observed``) already
    returns.

    A subprocess timeout/OSError, an unparsable/empty stdout, or a
    well-formed ``status: error`` result envelope (whose ``data`` carries
    ``{message, details}``, not a typed disposition) are all normalized to a
    diagnosable ``deferred`` outcome instead of an uncaught exception or a
    bare ``{}``. This is a read normalization only -- it never rolls back or
    fail-closes any already-completed post-merge cleanup work.

    fix_delta P2-A: this ``{"disposition": ...}``-shaped normalization only
    ever fits ``signal apply``/``cleanup begin`` results. ``diagnose_origin``
    below (which returns a ``{"resolved": ...}``-shaped result with no
    ``disposition`` key at all) is deliberately NOT routed through this
    function -- doing so used to silently coerce a genuine ``resolved: true``
    diagnosis into ``{"disposition": "deferred", "reason_code": "OK"}``."""
    envelope = _invoke_ctl(argv, body, origin_session_id=origin_session_id)
    if envelope is None:
        return {"disposition": "deferred", "reason_code": "ADAPTER_UNAVAILABLE"}
    data = envelope.get("data", {})
    if isinstance(data, dict) and "disposition" in data:
        return data
    # status: error (or any other envelope shape lacking a typed
    # disposition) -- surface the envelope's own error code rather than
    # silently returning {message, details} or {}.
    return {"disposition": "deferred", "reason_code": str(envelope.get("code", "ADAPTER_UNAVAILABLE"))}


def diagnose_origin(origin_session_id: str | None) -> dict:
    """PR #2795 review fix_delta P2-A/P2-C (comment 5852749710, findings 3
    and 5): read-only ``signal diagnose-origin`` counterpart used by the
    orchestrator (never by the dispatch-time worker -- see
    ``.claude/skills/post-merge-cleanup/SKILL.md``'s "unbound の原因別
    フォールバック" section) to diagnose the *same effective origin session*
    that a preceding failed ``signal apply``/``cleanup begin`` call used --
    pass the identical ``origin_session_id`` value here that was passed to
    that preceding call (``None`` falls back to this process's own ambient
    ``CLAUDE_CODE_SESSION_ID``, exactly like ``_run`` above).

    Returns the diagnostic's own ``{"resolved": bool, "reason_code": ...}``
    shape verbatim (never coerced through ``_run``'s disposition-oriented
    normalization -- see that function's docstring) on success, or
    ``{"resolved": False, "reason_code": "ADAPTER_UNAVAILABLE"}`` if the
    subprocess itself could not be run/parsed, or
    ``{"resolved": False, "reason_code": str(<envelope error code>)}`` for a
    well-formed ``status: error`` envelope."""
    envelope = _invoke_ctl(["signal", "diagnose-origin"], {}, origin_session_id=origin_session_id)
    if envelope is None:
        return {"resolved": False, "reason_code": "ADAPTER_UNAVAILABLE"}
    data = envelope.get("data", {})
    if isinstance(data, dict) and "resolved" in data:
        return data
    return {"resolved": False, "reason_code": str(envelope.get("code", "ADAPTER_UNAVAILABLE"))}


def _print(payload: dict) -> int:
    print(json.dumps(payload))
    return 0


def _missing_argument() -> int:
    return _print({"disposition": "rejected_evidence", "reason_code": "MISSING_REQUIRED_ARGUMENT"})


def _fresh_merged_evidence(args: argparse.Namespace) -> tuple[dict | None, dict | None]:
    """Shared snapshot/identity gate of `--phase recover` and `--phase local-only`.

    Returns ``(evidence, None)`` or ``(None, <typed rejection>)``. Order:
    snapshot validity -> freshness (mtime) -> merge identity binding. Nothing
    here touches Task Context.
    """
    try:
        snapshot = json.loads(args.snapshot_file.read_text(encoding="utf-8"))
        snapshot_mtime = args.snapshot_file.stat().st_mtime
    except (OSError, json.JSONDecodeError):
        return None, {"disposition": "deferred", "reason_code": "RELATION_UNAVAILABLE"}
    evidence, reason = _merged_evidence(snapshot, args.issue_number, args.pr_number)
    if evidence is None:
        return None, {"disposition": "deferred", "reason_code": reason}
    if time.time() - snapshot_mtime > SNAPSHOT_MAX_AGE_SECONDS:
        return None, {"disposition": "deferred", "reason_code": "SNAPSHOT_STALE"}
    oid = evidence["merge_commit_oid"]
    if (
        not _HEX40.fullmatch(oid)
        or not _HEX40.fullmatch(args.merge_identity)
        or oid != args.merge_identity
    ):
        return None, {"disposition": "rejected_evidence", "reason_code": "MERGE_IDENTITY_MISMATCH"}
    return evidence, None


def _phase_recover(args: argparse.Namespace) -> int:
    """`--phase recover`: explicit retroactive claim recovery (Issue #2817)."""
    if not args.explicit_recovery:
        return _print({"disposition": "rejected_evidence", "reason_code": "EXPLICIT_RECOVERY_REQUIRED"})
    # The origin must be named explicitly; recovery never falls back to the
    # ambient CLAUDE_CODE_SESSION_ID of this process.
    if not args.merge_identity or not args.origin_session_id:
        return _missing_argument()
    evidence, rejection = _fresh_merged_evidence(args)
    if rejection is not None:
        return _print(rejection)
    assert evidence is not None
    result = _run(
        ["signal", "recover"],
        {
            "schema_version": "task-context-request/v1",
            "operation": "signal_recover",
            "request_id": "post-merge-cleanup-recover",
            "payload": {**evidence, "explicit_recovery": True},
        },
        origin_session_id=args.origin_session_id,
    )
    return _print(result)


def _phase_local_only(args: argparse.Namespace) -> int:
    """`--phase local-only`: caller-declared, Task-Context-free cleanup permit.

    Writes nothing to Task Context, never starts or completes a lifecycle and
    emits no `cleanup_exec` argv (the worker runs its own executor procedure).
    """
    if not (
        args.merge_identity
        and args.task_context_outcome
        and args.worktree_path
        and args.worktree_path.strip()
        and args.branch_name
        and args.branch_name.strip()
    ):
        return _missing_argument()
    evidence, rejection = _fresh_merged_evidence(args)
    if rejection is not None:
        return _print(rejection)
    assert evidence is not None
    if args.task_context_outcome not in LOCAL_ONLY_PERMITTED_OUTCOMES:
        return _print({"disposition": "refused", "reason_code": "LOCAL_ONLY_NOT_PERMITTED"})
    return _print(
        {
            "disposition": "local_only",
            "reason_code": "LOCAL_ONLY_PERMITTED",
            "task_context": "unrecorded",
            "authority": {"cleanup_completed": False, "parent_issue_close": False, "superseded_pr_close": False},
            "repo": evidence["repo"],
            "issue_number": evidence["issue_number"],
            "pr_number": evidence["pr_number"],
            "merge_commit_oid": evidence["merge_commit_oid"],
            "worktree_path": args.worktree_path,
            "branch_name": args.branch_name,
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-file", type=Path, required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--phase", choices=("merged", "completed", "recover", "local-only"), required=True)
    parser.add_argument(
        "--merge-identity",
        default=None,
        help="40-hex merge commit OID the caller expects (required for --phase recover / local-only)",
    )
    parser.add_argument(
        "--explicit-recovery",
        action="store_true",
        help="explicit recovery request (required for --phase recover)",
    )
    parser.add_argument(
        "--task-context-outcome",
        default=None,
        help="caller-declared outcome that justifies local-only (required for --phase local-only; closed enum)",
    )
    parser.add_argument("--worktree-path", default=None, help="cleanup target worktree (--phase local-only)")
    parser.add_argument("--branch-name", default=None, help="cleanup target branch (--phase local-only)")
    parser.add_argument(
        "--cleanup-receipt-file",
        type=Path,
        help="final cleanup worker result (required for --phase completed)",
    )
    parser.add_argument(
        "--origin-session-id",
        default=None,
        help=(
            "origin Claude session id to set explicitly into the "
            "task_contextctl.py child process's CLAUDE_CODE_SESSION_ID "
            "(Issue #2719 AC3). Falls back to this adapter's own "
            "CLAUDE_CODE_SESSION_ID when omitted."
        ),
    )
    args = parser.parse_args(argv)
    if args.phase == "recover":
        return _phase_recover(args)
    if args.phase == "local-only":
        return _phase_local_only(args)
    origin_session_id = args.origin_session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    try:
        snapshot = json.loads(args.snapshot_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(json.dumps({"disposition": "deferred", "reason_code": "RELATION_UNAVAILABLE"}))
        return 0
    evidence, reason = _merged_evidence(snapshot, args.issue_number, args.pr_number)
    if evidence is None:
        print(json.dumps({"disposition": "deferred", "reason_code": reason}))
        return 0
    if args.phase == "merged":
        signal = {
            "signal_kind": "pr_merged_observed",
            "source": "post-merge-cleanup",
            "source_schema_version": "v1",
            "evidence": evidence,
        }
        result = _run(["signal", "apply"], signal, origin_session_id=origin_session_id)
        if result.get("disposition") not in {"applied", "duplicate_noop"}:
            print(json.dumps(result))
            return 0
        begin = {
            "repo": evidence["repo"],
            "issue_number": evidence["issue_number"],
            "pr_number": evidence["pr_number"],
            "merge_identity": evidence["merge_commit_oid"],
        }
        result = _run(
            ["cleanup", "begin"],
            {
                "schema_version": "task-context-request/v1",
                "operation": "cleanup_begin",
                "request_id": "post-merge-cleanup",
                "payload": begin,
            },
            origin_session_id=origin_session_id,
        )
        print(json.dumps(result))
        return 0
    receipt_reason = _final_success_receipt_reason(args.cleanup_receipt_file)
    if receipt_reason:
        print(json.dumps({"disposition": "deferred", "reason_code": receipt_reason}))
        return 0
    completed_evidence = {
        "repo": evidence["repo"],
        "issue_number": evidence["issue_number"],
        "pr_number": evidence["pr_number"],
        "merge_identity": evidence["merge_commit_oid"],
    }
    completed = {
        "signal_kind": "cleanup_completed",
        "source": "post-merge-cleanup",
        "source_schema_version": "v1",
        "evidence": completed_evidence,
    }
    result = _run(["signal", "apply"], completed, origin_session_id=origin_session_id)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

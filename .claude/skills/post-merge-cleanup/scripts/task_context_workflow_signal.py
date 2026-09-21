#!/usr/bin/env python3
"""Apply post-merge cleanup's trusted Task Context commit points (Issue #2565).

The caller supplies a fresh, already-fetched GraphQL merged-PR snapshot.  This
adapter performs no GitHub I/O, so snapshot acquisition remains outside the
Task Context SQLite transaction.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_ROOT / "scripts"))
from check_post_merge_cleanup_boundary import validate_report_v1  # noqa: E402

_CTL = _ROOT / "scripts" / "task-context" / "task_contextctl.py"


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

    Issue #2719 AC3: ``task_contextctl.py`` reads its origin session id from
    its *own* process environment (``os.environ["CLAUDE_CODE_SESSION_ID"]``,
    see ``task_contextctl._dispatch``). Previously this subprocess call
    passed no ``env=`` at all and unconditionally inherited whatever
    ``CLAUDE_CODE_SESSION_ID`` happened to already be set in *this*
    adapter's own ambient environment -- an implicit, uncontrolled
    passthrough. This now mirrors the explicit override pattern
    ``.claude/hooks/task_context/ctl_client.py``'s ``call()`` already uses
    for the hook transport: build ``child_env`` from a copy of this
    process's environment, then explicitly set the caller-supplied
    ``origin_session_id`` into it before starting the child process, so the
    origin session provenance is never left to implicit inheritance.
    """
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
        return {"disposition": "deferred", "reason_code": "ADAPTER_UNAVAILABLE"}
    if not proc.stdout.splitlines():
        return {"disposition": "deferred", "reason_code": "ADAPTER_UNAVAILABLE"}
    try:
        envelope = json.loads(proc.stdout.splitlines()[-1])
    except json.JSONDecodeError:
        return {"disposition": "deferred", "reason_code": "ADAPTER_UNAVAILABLE"}
    if not isinstance(envelope, dict):
        return {"disposition": "deferred", "reason_code": "ADAPTER_UNAVAILABLE"}
    data = envelope.get("data", {})
    if isinstance(data, dict) and "disposition" in data:
        return data
    # status: error (or any other envelope shape lacking a typed
    # disposition) -- surface the envelope's own error code rather than
    # silently returning {message, details} or {}.
    return {"disposition": "deferred", "reason_code": str(envelope.get("code", "ADAPTER_UNAVAILABLE"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-file", type=Path, required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--phase", choices=("merged", "completed"), required=True)
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

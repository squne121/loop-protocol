"""Trusted workflow-signal validation and Task Context application (Issue #2565).

This module deliberately keeps the public signal envelope small.  Producer
adapters provide an origin session internally; public callers never select a
Task, Activity, Binding, or session identity.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import task_context_db as db
import task_context_service as service

_SIGNAL_SOURCES = {
    "refinement_approved": "issue-refinement-loop",
    "implementation_pr_observed": "open-pr",
    "pr_merged_observed": "post-merge-cleanup",
    "cleanup_completed": "post-merge-cleanup",
}
_TOP_LEVEL = frozenset({"signal_kind", "source", "source_schema_version", "evidence"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9._-]*$")


class DuplicateMemberError(ValueError):
    def __init__(self, location: str, key: str):
        super().__init__(f"duplicate member {key!r} in {location}")
        self.location = location
        self.key = key


class _StrictObject(dict[str, Any]):
    """A decoded object retaining duplicate-member evidence until classified."""

    def __init__(self, pairs: list[tuple[str, Any]]):
        super().__init__()
        self.duplicate_keys: list[str] = []
        for key, value in pairs:
            if key in self:
                self.duplicate_keys.append(key)
            self[key] = value


def strict_json_loads(raw: str) -> object:
    """Decode JSON while retaining duplicate members at every object level."""
    return json.loads(raw, object_pairs_hook=_StrictObject)


def _outcome(disposition: str, reason_code: str, **extra: Any) -> dict[str, Any]:
    return {"disposition": disposition, "reason_code": reason_code, **extra}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_evidence(kind: str, evidence: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(evidence, dict):
        return None, _outcome("rejected_evidence", "WRONG_EVIDENCE_TYPE")
    required: dict[str, frozenset[str]] = {
        "refinement_approved": frozenset({"repo", "issue_number", "approved_body_sha256"}),
        "implementation_pr_observed": frozenset({"repo", "issue_number", "pr_number"}),
        "pr_merged_observed": frozenset({"repo", "issue_number", "pr_number", "merge_commit_oid"}),
        "cleanup_completed": frozenset({"repo", "issue_number", "pr_number", "merge_identity"}),
    }
    expected = required[kind]
    if set(evidence) != expected:
        return None, _outcome("rejected_evidence", "INVALID_EVIDENCE_FIELDS")
    repo = evidence["repo"]
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo.endswith(".git"):
        return None, _outcome("rejected_evidence", "INVALID_REPOSITORY")
    if not _is_int(evidence["issue_number"]) or evidence["issue_number"] <= 0:
        return None, _outcome("rejected_evidence", "INVALID_ISSUE_NUMBER")
    if "pr_number" in evidence and (not _is_int(evidence["pr_number"]) or evidence["pr_number"] <= 0):
        return None, _outcome("rejected_evidence", "INVALID_PR_NUMBER")
    digest_field = (
        "approved_body_sha256"
        if kind == "refinement_approved"
        else (
            "merge_commit_oid"
            if kind == "pr_merged_observed"
            else "merge_identity"
            if kind == "cleanup_completed"
            else None
        )
    )
    if digest_field:
        value = evidence[digest_field]
        pattern = _HEX64 if digest_field == "approved_body_sha256" else _HEX40
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return None, _outcome("rejected_evidence", f"INVALID_{digest_field.upper()}")
    return evidence, None


def validate_public_signal(
    payload: Any, *, duplicate_evidence_member: bool = False
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate exactly the frozen v1 public payload, without mutating state."""
    if not isinstance(payload, dict):
        return None, _outcome("rejected_envelope", "NON_OBJECT_ROOT")
    if isinstance(payload, _StrictObject) and payload.duplicate_keys:
        return None, _outcome("rejected_envelope", "DUPLICATE_TOP_LEVEL_MEMBER")
    if isinstance(payload.get("evidence"), _StrictObject) and payload["evidence"].duplicate_keys:
        return None, _outcome("rejected_evidence", "DUPLICATE_EVIDENCE_MEMBER")
    keys = set(payload)
    if any(k in payload for k in ("session_id", "task_id", "activity_id", "binding_id")):
        return None, _outcome("rejected_envelope", "FORBIDDEN_CALLER_IDENTITY")
    if keys != _TOP_LEVEL:
        return None, _outcome("rejected_envelope", "UNKNOWN_TOP_LEVEL_FIELD")
    if (
        not isinstance(payload.get("signal_kind"), str)
        or not isinstance(payload.get("source"), str)
        or not isinstance(payload.get("source_schema_version"), str)
        or not isinstance(payload.get("evidence"), dict)
    ):
        return None, _outcome("rejected_envelope", "WRONG_TOP_LEVEL_TYPE")
    kind = payload["signal_kind"]
    source = payload["source"]
    if kind not in _SIGNAL_SOURCES:
        return None, _outcome("rejected_envelope", "UNKNOWN_SIGNAL_KIND")
    if source not in set(_SIGNAL_SOURCES.values()):
        return None, _outcome("rejected_envelope", "UNKNOWN_SOURCE")
    if _SIGNAL_SOURCES[kind] != source:
        return None, _outcome("rejected_envelope", "SOURCE_SIGNAL_MISMATCH")
    if payload["source_schema_version"] != "v1":
        return None, _outcome("rejected_envelope", "UNSUPPORTED_SCHEMA_VERSION")
    if duplicate_evidence_member:
        return None, _outcome("rejected_evidence", "DUPLICATE_EVIDENCE_MEMBER")
    evidence, rejection = _validate_evidence(kind, payload["evidence"])
    if rejection:
        return None, rejection
    normalized = dict(payload)
    normalized["evidence"] = evidence
    return normalized, None


def parse_public_signal(raw: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        payload = strict_json_loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _outcome("rejected_envelope", "MALFORMED_JSON")
    if isinstance(payload, _StrictObject) and payload.duplicate_keys:
        return None, _outcome("rejected_envelope", "DUPLICATE_TOP_LEVEL_MEMBER")
    if isinstance(payload, dict) and isinstance(payload.get("evidence"), _StrictObject):
        if payload["evidence"].duplicate_keys:
            return None, _outcome("rejected_evidence", "DUPLICATE_EVIDENCE_MEMBER")
    return validate_public_signal(payload)


def dedupe_key_for(payload: dict[str, Any]) -> str:
    evidence = payload["evidence"]
    kind = payload["signal_kind"]
    prefix = f"task-context-v1:{kind}:{evidence['repo']}"
    if kind == "refinement_approved":
        return f"{prefix}:{evidence['issue_number']}:{evidence['approved_body_sha256']}"
    if kind == "implementation_pr_observed":
        return f"{prefix}:{evidence['pr_number']}"
    if kind == "pr_merged_observed":
        return f"{prefix}:{evidence['pr_number']}:{evidence['merge_commit_oid']}"
    return f"{prefix}:{evidence['pr_number']}:{evidence['merge_identity']}"


def _event_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload["evidence"]
    metadata: dict[str, Any] = {
        "signal_kind": payload["signal_kind"],
        "source": payload["source"],
        "source_schema_version": payload["source_schema_version"],
        "repo": evidence["repo"],
        "issue_number": evidence["issue_number"],
    }
    for key in ("pr_number", "approved_body_sha256", "merge_commit_oid", "merge_identity"):
        if key in evidence:
            metadata[key] = evidence[key]
    return metadata


def _metadata_matches(row: sqlite3.Row, **wanted: Any) -> bool:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return all(metadata.get(key) == value for key, value in wanted.items())


_MANAGED_ORIGIN_RUN_KINDS = ("native_operator", "claude_gpt")
_ORIGIN_UNBOUND_REASON = "unbound"
_ORIGIN_RESOLUTION_FAILURE_EVENT_TYPE = "workflow:origin_resolution_failed"


def _classify_origin_candidates(
    candidates: list[Any], origin_session_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Evaluate raw LEFT JOIN origin candidates predicate-by-predicate.

    Issue #2719 AC1: rather than collapsing every failure mode into the
    single opaque ``unbound`` the previous 6-predicate-ANDed SQL query
    produced, each candidate row is walked through the same predicates in a
    fixed order so the specific cause is distinguishable:
    ``origin_run_ended`` -> ``origin_run_kind_mismatch`` ->
    ``origin_task_unattached`` -> ``origin_binding_session_mismatch``.
    ``origin_ambiguous`` covers >1 candidate passing every predicate; this
    is unreachable through any real write path today (the
    ``ux_execution_runs_open_managed_session`` partial unique index already
    forbids two simultaneously open+managed ExecutionRuns sharing one
    ``claude_session_id`` -- the pre-decomposition code carried the exact
    same ``len(rows) != 1`` defensive check), but is kept as a distinct,
    directly testable reason-code for defense-in-depth against a future
    schema change or a raw-SQL bypass of the typed service layer.

    ``candidates`` rows only need mapping-style ``row["column"]`` access
    (a plain ``dict`` or a ``sqlite3.Row`` both work), which lets tests
    exercise this pure classification independently of the SQL fetch.
    """
    if not candidates:
        return None, _outcome("deferred", "origin_run_not_found")
    matched: list[Any] = []
    last_failure: dict[str, Any] | None = None
    for row in candidates:
        if row["ended_at"] is not None:
            reason = "origin_run_ended"
        elif row["run_kind"] not in _MANAGED_ORIGIN_RUN_KINDS:
            reason = "origin_run_kind_mismatch"
        elif row["task_id"] is None:
            reason = "origin_task_unattached"
        elif row["binding_session_id"] != origin_session_id:
            reason = "origin_binding_session_mismatch"
        else:
            matched.append(row)
            continue
        last_failure = _outcome(
            "deferred",
            reason,
            execution_run_id=row["execution_run_id"],
            task_id=row["task_id"],
            activity_id=row["activity_id"],
            binding_id=row["binding_id"],
        )
    if len(matched) > 1:
        anchor = matched[0]
        return None, _outcome(
            "deferred",
            "origin_ambiguous",
            execution_run_id=anchor["execution_run_id"],
            task_id=anchor["task_id"],
            activity_id=anchor["activity_id"],
            binding_id=anchor["binding_id"],
            count=len(matched),
        )
    if len(matched) == 1:
        row = matched[0]
        return {
            "execution_run_id": row["execution_run_id"],
            "task_id": row["task_id"],
            "activity_id": row["activity_id"],
            "binding_id": row["binding_id"],
        }, None
    assert last_failure is not None
    return None, last_failure


def _resolve_origin_tx(
    conn: sqlite3.Connection, origin_session_id: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve the one live, managed-operator ExecutionRun bound to
    ``origin_session_id``.

    On failure this returns the *specific* reason_code (one of the 7 Issue
    #2719 codes) as internal diagnostic detail -- callers that only need
    the frozen public ``unbound`` disposition (Issue #2565 contract) call
    ``_unbound_outcome()`` themselves instead of returning this function's
    outcome verbatim; ``apply_workflow_signal`` additionally persists this
    specific reason_code into the ``events`` journal (AC2) before doing so.
    """
    if not origin_session_id:
        return None, _outcome("deferred", "origin_session_missing")
    candidates = conn.execute(
        "SELECT er.id AS execution_run_id, er.task_id, er.activity_id, er.binding_id, "
        "er.ended_at, er.run_kind, tb.current_claude_session_id AS binding_session_id "
        "FROM execution_runs er LEFT JOIN tab_bindings tb ON tb.id = er.binding_id "
        "WHERE er.claude_session_id = ? "
        "ORDER BY er.started_at ASC, er.id ASC",
        (origin_session_id,),
    ).fetchall()
    return _classify_origin_candidates(candidates, origin_session_id)


def _unbound_outcome() -> dict[str, Any]:
    """The frozen public disposition for every origin-resolution failure
    (Issue #2565 contract, preserved by Issue #2719). ``_resolve_origin_tx``'s
    specific reason_code is a diagnostic-only detail persisted separately
    (see ``_record_origin_resolution_failure_tx``); it is never surfaced as
    this outcome's own ``reason_code`` so every existing
    ``{"disposition": "deferred", "reason_code": "unbound"}`` consumer
    contract keeps working unchanged."""
    return _outcome("deferred", _ORIGIN_UNBOUND_REASON)


def _record_origin_resolution_failure_tx(
    conn: sqlite3.Connection, payload: dict[str, Any], failure: dict[str, Any]
) -> None:
    """Persist AC1's specific reason_code into the append-only ``events``
    journal (AC2) whenever at least one ExecutionRun candidate could be
    identified for the failure.

    ``origin_session_missing`` / ``origin_run_not_found`` carry no
    resolvable ExecutionRun/Task at all and are deliberately NOT persisted
    here: this preserves the pre-existing "an unbound origin resolution is
    fully non-mutating" regression guarantee those two reason codes'
    fixtures exercise (Issue #2690/#2692 fix_delta;
    ``test_given_unbound_origin_when_fact_applied_then_it_is_deferred_before_dedupe_or_claim_mutation``
    and its ``mutation_counts`` siblings)."""
    execution_run_id = failure.get("execution_run_id")
    if execution_run_id is None:
        return
    metadata: dict[str, Any] = {
        "reason_code": failure["reason_code"],
        "signal_kind": payload["signal_kind"],
        "source": payload["source"],
    }
    if "count" in failure:
        metadata["count"] = failure["count"]
    service._append_event_tx(
        conn,
        event_type=_ORIGIN_RESOLUTION_FAILURE_EVENT_TYPE,
        task_id=failure.get("task_id"),
        activity_id=failure.get("activity_id"),
        binding_id=failure.get("binding_id"),
        execution_run_id=execution_run_id,
        metadata=metadata,
    )


def _claim_tx(conn: sqlite3.Connection, task_id: str, repo: str, kind: str, number: int) -> None:
    service._claim_task_ref_tx(conn, task_id, repo, kind, number)


def _claim_for_tx(conn: sqlite3.Connection, repo: str, kind: str, number: int) -> dict[str, Any] | None:
    return service._find_live_claim_tx(conn, repo, kind, number)


def _activity_for_tx(conn: sqlite3.Connection, task_id: str, kind: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM activities WHERE task_id = ? AND kind = ? ORDER BY started_at DESC LIMIT 1", (task_id, kind)
    ).fetchone()
    return dict(row) if row else None


def _has_cleanup_started_tx(conn: sqlite3.Connection, task_id: str, repo: str, pr_number: int) -> bool:
    rows = conn.execute(
        "SELECT metadata_json FROM events WHERE task_id = ? AND event_type = 'workflow:cleanup_started'", (task_id,)
    ).fetchall()
    return any(_metadata_matches(row, repo=repo, pr_number=pr_number) for row in rows)


def _accepted_event_tx(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM events WHERE dedupe_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def _append_signal_tx(
    conn: sqlite3.Connection, payload: dict[str, Any], origin: dict[str, Any], activity_id: str | None
) -> str:
    return service._append_event_tx(
        conn,
        event_type=f"workflow:{payload['signal_kind']}",
        task_id=origin["task_id"],
        activity_id=activity_id,
        binding_id=origin["binding_id"],
        execution_run_id=origin["execution_run_id"],
        metadata=_event_metadata(payload),
        dedupe_key=dedupe_key_for(payload),
    )


def _validate_implementation_claims_tx(
    conn: sqlite3.Connection, task_id: str, evidence: dict[str, Any]
) -> dict[str, Any] | None:
    """Validate a proposed implementation attachment without writing claims."""
    repo, issue, pr = evidence["repo"], evidence["issue_number"], evidence["pr_number"]
    issue_claim, pr_claim = _claim_for_tx(conn, repo, "issue", issue), _claim_for_tx(conn, repo, "pr", pr)
    if (issue_claim and issue_claim["task_id"] != task_id) or (pr_claim and pr_claim["task_id"] != task_id):
        return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
    # A Task has one primary Issue identity.  An open-pr producer may remain
    # successful, but it must not attach an unrelated Issue/PR fact to the
    # origin Task that an ACTIVE hook deliberately kept unchanged.
    other_issue = conn.execute(
        "SELECT 1 FROM task_ref_claims WHERE task_id = ? AND ref_kind = 'issue' AND released_at IS NULL "
        "AND (repo != ? OR ref_number != ?) LIMIT 1",
        (task_id, repo, issue),
    ).fetchone()
    if other_issue:
        return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
    # An Issue already tied to another implementation PR is an explicit
    # conflict; do not let a new PR silently become equivalent.
    other_pr = conn.execute(
        "SELECT 1 FROM task_ref_claims WHERE task_id = ? AND repo = ? AND ref_kind = 'pr' "
        "AND ref_number != ? AND released_at IS NULL LIMIT 1",
        (task_id, repo, pr),
    ).fetchone()
    if other_pr:
        return _outcome("conflict", "OUT_OF_ORDER_SIGNAL")
    return None


def _attach_implementation_claims_tx(conn: sqlite3.Connection, task_id: str, evidence: dict[str, Any]) -> None:
    """Attach only an already-admissible implementation fact."""
    repo, issue, pr = evidence["repo"], evidence["issue_number"], evidence["pr_number"]
    if _claim_for_tx(conn, repo, "issue", issue) is None:
        _claim_tx(conn, task_id, repo, "issue", issue)
    if _claim_for_tx(conn, repo, "pr", pr) is None:
        _claim_tx(conn, task_id, repo, "pr", pr)


def _validate_required_claims_tx(
    conn: sqlite3.Connection, task_id: str, evidence: dict[str, Any]
) -> dict[str, Any] | None:
    issue = _claim_for_tx(conn, evidence["repo"], "issue", evidence["issue_number"])
    if not issue or issue["task_id"] != task_id:
        return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
    if "pr_number" in evidence:
        pr = _claim_for_tx(conn, evidence["repo"], "pr", evidence["pr_number"])
        if not pr or pr["task_id"] != task_id:
            return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
    return None


def _validate_merged_prerequisites_tx(
    conn: sqlite3.Connection, task_id: str, evidence: dict[str, Any]
) -> dict[str, Any] | None:
    """Classify absent implementation claims as not-ready, not identity conflicts."""
    issue = _claim_for_tx(conn, evidence["repo"], "issue", evidence["issue_number"])
    pr = _claim_for_tx(conn, evidence["repo"], "pr", evidence["pr_number"])
    if issue is None or pr is None:
        return _outcome("deferred", "IMPLEMENTATION_NOT_READY")
    if issue["task_id"] != task_id or pr["task_id"] != task_id:
        return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
    return None


def _matching_accepted_merge_tx(
    conn: sqlite3.Connection, task_id: str, evidence: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Find the exact accepted merge fact required by cleanup completion."""
    merge_payload = {
        "signal_kind": "pr_merged_observed",
        "source": "post-merge-cleanup",
        "source_schema_version": "v1",
        "evidence": {
            "repo": evidence["repo"],
            "issue_number": evidence["issue_number"],
            "pr_number": evidence["pr_number"],
            "merge_commit_oid": evidence["merge_identity"],
        },
    }
    accepted = _accepted_event_tx(conn, dedupe_key_for(merge_payload))
    if accepted is None:
        return None, None
    if accepted["task_id"] != task_id or not _metadata_matches(
        accepted,
        repo=evidence["repo"],
        issue_number=evidence["issue_number"],
        pr_number=evidence["pr_number"],
        merge_commit_oid=evidence["merge_identity"],
    ):
        return None, _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
    return accepted, None


def apply_workflow_signal(
    conn: sqlite3.Connection, payload: Any, *, origin_session_id: str | None = None
) -> dict[str, Any]:
    """Apply one validated workflow fact in one BEGIN IMMEDIATE transaction."""
    valid, rejected = validate_public_signal(payload)
    if rejected:
        return rejected
    assert valid is not None
    key = dedupe_key_for(valid)
    try:
        with db.write_transaction(conn):
            origin, origin_failure = _resolve_origin_tx(conn, origin_session_id)
            if origin_failure:
                _record_origin_resolution_failure_tx(conn, valid, origin_failure)
                return _unbound_outcome()
            assert origin is not None
            task_id, kind, evidence = origin["task_id"], valid["signal_kind"], valid["evidence"]
            if kind == "implementation_pr_observed":
                # Per AC3's precedence (claim/Task consistency -> same-fact
                # dedupe -> phase/terminal judgment), cross-Task claim/Task
                # consistency must be judged before *any* local Activity
                # state (missing or terminal), and before this dedupe key's
                # own accepted-fact identity check. A different Task already
                # holding a *live claim* on this Issue (or PR) is always
                # conflict/FACT_TASK_IDENTITY_CONFLICT regardless of whether
                # this specific dedupe key has ever been accepted by anyone,
                # and regardless of whether this Task's own implementation
                # Activity is ACTIVE, terminal, or missing entirely (Issue
                # #2690 fix_delta / PR #2692 OWNER review). Only once
                # claim/Task consistency is confirmed clean do we evaluate
                # this dedupe key's own accepted-fact identity: a replay
                # whose dedupe key was already accepted by a *different*
                # Task is conflict/FACT_TASK_IDENTITY_CONFLICT; an exact
                # replay of the accepted fact by the *same* Task falls
                # through to the common SAME_TASK_SAME_FACT dedupe below
                # regardless of Activity status. Only once the fact is
                # confirmed genuinely new for this dedupe key (no accepted
                # event for it belongs to any Task) may local Activity state
                # (`activity_missing` / `activity_terminal`) be evaluated.
                claim_conflict = _validate_implementation_claims_tx(conn, task_id, evidence)
                if claim_conflict:
                    return claim_conflict
                accepted_implementation = _accepted_event_tx(conn, key)
                if accepted_implementation is not None and accepted_implementation["task_id"] != task_id:
                    return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
                if accepted_implementation is not None:
                    if not _metadata_matches(
                        accepted_implementation,
                        repo=evidence["repo"],
                        issue_number=evidence["issue_number"],
                        pr_number=evidence["pr_number"],
                    ):
                        return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
                else:
                    implementation = _activity_for_tx(conn, task_id, "implementation")
                    if implementation is None:
                        return _outcome("deferred", "activity_missing")
                    if implementation["status"] != "ACTIVE":
                        return _outcome("duplicate_noop", "activity_terminal", task_id=task_id)
                # claim/Task consistency was already validated above; do not
                # re-run it here (outcome stays None unless a later branch
                # sets it).
                outcome = None
            elif kind == "pr_merged_observed":
                outcome = _validate_merged_prerequisites_tx(conn, task_id, evidence)
            elif kind == "cleanup_completed":
                accepted_merge, outcome = _matching_accepted_merge_tx(conn, task_id, evidence)
                if outcome:
                    return outcome
                if accepted_merge is None:
                    return _outcome("conflict", "OUT_OF_ORDER_SIGNAL")
                outcome = _validate_required_claims_tx(conn, task_id, evidence)
            else:
                outcome = _validate_required_claims_tx(conn, task_id, evidence)
            if outcome:
                return outcome
            existing = _accepted_event_tx(conn, key)
            if existing:
                if existing["task_id"] == task_id:
                    return _outcome("duplicate_noop", "SAME_TASK_SAME_FACT", task_id=task_id)
                return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
            if kind == "pr_merged_observed" and _has_cleanup_started_tx(
                conn, task_id, evidence["repo"], evidence["pr_number"]
            ):
                return _outcome("late_noop", "CLEANUP_ALREADY_BEGUN", task_id=task_id)
            activity_kind = {
                "refinement_approved": "refine",
                "implementation_pr_observed": "implementation",
                "pr_merged_observed": "implementation",
            }.get(kind)
            activity = _activity_for_tx(conn, task_id, activity_kind) if activity_kind else None
            if kind == "implementation_pr_observed":
                if activity is None:
                    return _outcome("deferred", "activity_missing")
                _attach_implementation_claims_tx(conn, task_id, evidence)
                event_id = _append_signal_tx(conn, valid, origin, activity["id"])
            elif kind == "refinement_approved":
                if activity is None:
                    return _outcome("deferred", "activity_missing")
                if activity["status"] != "ACTIVE":
                    return _outcome("duplicate_noop", "activity_terminal", task_id=task_id)
                conn.execute(
                    "UPDATE activities SET status = 'DONE', ended_at = ? WHERE id = ?",
                    (service.now_iso(), activity["id"]),
                )
                # An ordinary Issue-prompt hook starts refine. Its approved
                # handoff is the canonical implementation phase start, and
                # the current managed origin must follow that phase atomically.
                implementation_id = service._transition_activity_tx(conn, task_id, "implementation")
                service._attach_execution_run_tx(
                    conn,
                    origin["execution_run_id"],
                    task_id=task_id,
                    activity_id=implementation_id,
                    binding_id=origin["binding_id"],
                )
                event_id = _append_signal_tx(conn, valid, origin, activity["id"])
            elif kind == "pr_merged_observed":
                if activity is None or activity["status"] != "ACTIVE":
                    return _outcome("deferred", "IMPLEMENTATION_NOT_READY")
                conn.execute(
                    "UPDATE activities SET status = 'DONE', ended_at = ? WHERE id = ?",
                    (service.now_iso(), activity["id"]),
                )
                event_id = _append_signal_tx(conn, valid, origin, activity["id"])
            else:  # cleanup_completed
                cleanup = _find_cleanup_instance_tx(
                    conn, task_id, evidence["repo"], evidence["pr_number"], evidence["merge_identity"]
                )
                if cleanup is None:
                    return _outcome("deferred", "CLEANUP_NOT_ELIGIBLE")
                if cleanup["status"] != "ACTIVE":
                    return _outcome("duplicate_noop", "activity_terminal", task_id=task_id)
                conn.execute(
                    "UPDATE activities SET status = 'DONE', ended_at = ? WHERE id = ?",
                    (service.now_iso(), cleanup["id"]),
                )
                event_id = _append_signal_tx(conn, valid, origin, cleanup["id"])
            service._bump_projection_tx(conn, origin["binding_id"])
            return _outcome("applied", "APPLIED", task_id=task_id, event_id=event_id)
    except sqlite3.IntegrityError as exc:
        # The partial unique index is the physical race backstop. Readback is
        # intentionally not treated as applied unless same-origin Task wins.
        if "dedupe" in str(exc).lower() or "unique" in str(exc).lower():
            return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
        raise


def _find_cleanup_instance_tx(
    conn: sqlite3.Connection, task_id: str, repo: str, pr_number: int, merge_identity: str
) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT * FROM events WHERE task_id = ? AND event_type = 'workflow:cleanup_started' ORDER BY occurred_at DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        if _metadata_matches(row, repo=repo, pr_number=pr_number, merge_identity=merge_identity):
            activity_id = json.loads(row["metadata_json"])["activity_id"]
            activity = conn.execute(
                "SELECT * FROM activities WHERE id = ? AND task_id = ?", (activity_id, task_id)
            ).fetchone()
            return dict(activity) if activity else None
    return None


def begin_cleanup_lifecycle(
    conn: sqlite3.Connection,
    *,
    origin_session_id: str | None,
    repo: str,
    issue_number: int,
    pr_number: int,
    merge_identity: str,
) -> dict[str, Any]:
    """Durably select/resume the one eligible cleanup Activity after merge acceptance."""
    if (
        not isinstance(repo, str)
        or not _REPO.fullmatch(repo)
        or repo.endswith(".git")
        or not all(_is_int(v) and v > 0 for v in (issue_number, pr_number))
        or not isinstance(merge_identity, str)
        or not _HEX40.fullmatch(merge_identity)
    ):
        return _outcome("rejected_evidence", "INVALID_CLEANUP_LIFECYCLE_EVIDENCE")
    merge_payload = {
        "signal_kind": "pr_merged_observed",
        "source": "post-merge-cleanup",
        "source_schema_version": "v1",
        "evidence": {
            "repo": repo,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "merge_commit_oid": merge_identity,
        },
    }
    with db.write_transaction(conn):
        origin, origin_failure = _resolve_origin_tx(conn, origin_session_id)
        if origin_failure:
            return _unbound_outcome()
        assert origin is not None
        task_id = origin["task_id"]
        accepted = _accepted_event_tx(conn, dedupe_key_for(merge_payload))
        if accepted is None:
            return _outcome("conflict", "OUT_OF_ORDER_SIGNAL")
        # The accepted merge fact is globally deduplicated. Cleanup selection
        # may proceed only for the Task that owns that accepted fact; otherwise
        # this origin would create a second lifecycle for the same merge.
        if accepted["task_id"] != task_id:
            return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
        found = _find_cleanup_instance_tx(conn, task_id, repo, pr_number, merge_identity)
        if found is not None:
            if found["status"] == "ACTIVE":
                service._attach_execution_run_tx(
                    conn,
                    origin["execution_run_id"],
                    task_id=task_id,
                    activity_id=found["id"],
                    binding_id=origin["binding_id"],
                )
                service._bump_projection_tx(conn, origin["binding_id"])
                return _outcome("selected", "CLEANUP_ALREADY_SELECTED", task_id=task_id, activity_id=found["id"])
            # A terminal historical cleanup instance is not resumable and must
            # never be reported as selected to a caller that could redispatch
            # work from that result.
            return _outcome("duplicate_noop", "activity_terminal", task_id=task_id)
        active = conn.execute(
            "SELECT id FROM activities WHERE task_id = ? AND status = 'ACTIVE'", (task_id,)
        ).fetchone()
        if active:
            return _outcome("conflict", "OUT_OF_ORDER_SIGNAL")
        activity_id = service._transition_activity_tx(conn, task_id, "cleanup")
        service._attach_execution_run_tx(
            conn,
            origin["execution_run_id"],
            task_id=task_id,
            activity_id=activity_id,
            binding_id=origin["binding_id"],
        )
        service._append_event_tx(
            conn,
            event_type="workflow:cleanup_started",
            task_id=task_id,
            activity_id=activity_id,
            binding_id=origin["binding_id"],
            execution_run_id=origin["execution_run_id"],
            metadata={
                "repo": repo,
                "issue_number": issue_number,
                "pr_number": pr_number,
                "merge_identity": merge_identity,
                "activity_id": activity_id,
            },
        )
        service._bump_projection_tx(conn, origin["binding_id"])
        return _outcome("selected", "CLEANUP_STARTED", task_id=task_id, activity_id=activity_id)


def cleanup_pending_for_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Derived state: accepted merge plus nonterminal eligible cleanup."""
    rows = db.execute_readonly(
        conn, "SELECT * FROM events WHERE task_id = ? AND event_type = 'workflow:pr_merged_observed'", (task_id,)
    ).fetchall()
    for row in rows:
        data = json.loads(row["metadata_json"] or "{}")
        cleanup = _find_cleanup_instance_tx(
            conn, task_id, data.get("repo"), data.get("pr_number"), data.get("merge_commit_oid")
        )
        if cleanup is None or cleanup["status"] != "DONE":
            return True
    return False

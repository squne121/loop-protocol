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


def _resolve_origin_tx(
    conn: sqlite3.Connection, origin_session_id: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not origin_session_id:
        return None, _outcome("deferred", "unbound")
    rows = conn.execute(
        "SELECT er.id AS execution_run_id, er.task_id, er.activity_id, er.binding_id "
        "FROM execution_runs er JOIN tab_bindings tb ON tb.id = er.binding_id "
        "WHERE er.claude_session_id = ? AND er.ended_at IS NULL "
        "AND er.run_kind IN ('native_operator', 'claude_gpt') "
        "AND tb.current_claude_session_id = ? AND er.task_id IS NOT NULL",
        (origin_session_id, origin_session_id),
    ).fetchall()
    if len(rows) != 1:
        return None, _outcome("deferred", "unbound")
    return dict(rows[0]), None


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


def _validate_claims_or_attach_implementation_tx(
    conn: sqlite3.Connection, task_id: str, evidence: dict[str, Any]
) -> dict[str, Any] | None:
    repo, issue, pr = evidence["repo"], evidence["issue_number"], evidence["pr_number"]
    issue_claim, pr_claim = _claim_for_tx(conn, repo, "issue", issue), _claim_for_tx(conn, repo, "pr", pr)
    if (issue_claim and issue_claim["task_id"] != task_id) or (pr_claim and pr_claim["task_id"] != task_id):
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
    # Validate every legal attachment before inserting either row.  The single
    # transaction makes the two inserts all-or-nothing if an unexpected DB
    # constraint wins a race.
    if issue_claim is None:
        _claim_tx(conn, task_id, repo, "issue", issue)
    if pr_claim is None:
        _claim_tx(conn, task_id, repo, "pr", pr)
    return None


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
            origin, outcome = _resolve_origin_tx(conn, origin_session_id)
            if outcome:
                return outcome
            assert origin is not None
            task_id, kind, evidence = origin["task_id"], valid["signal_kind"], valid["evidence"]
            if kind == "implementation_pr_observed":
                implementation = _activity_for_tx(conn, task_id, "implementation")
                if implementation is None:
                    return _outcome("deferred", "activity_missing")
                if implementation["status"] != "ACTIVE":
                    return _outcome("duplicate_noop", "activity_terminal", task_id=task_id)
                # The implementation dedupe key deliberately identifies a PR,
                # not an Issue. A replay with that PR but another Issue is a
                # conflict, never an opportunity to attach a second Issue claim.
                accepted_implementation = _accepted_event_tx(conn, key)
                if accepted_implementation and accepted_implementation["task_id"] == task_id and not _metadata_matches(
                    accepted_implementation,
                    repo=evidence["repo"],
                    issue_number=evidence["issue_number"],
                    pr_number=evidence["pr_number"],
                ):
                    return _outcome("conflict", "FACT_TASK_IDENTITY_CONFLICT")
                outcome = _validate_claims_or_attach_implementation_tx(conn, task_id, evidence)
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
        origin, outcome = _resolve_origin_tx(conn, origin_session_id)
        if outcome:
            return outcome
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

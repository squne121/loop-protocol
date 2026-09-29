"""Issue #2826: refinement_approved separates target-claim absence from Task identity conflict.

Matrix under test (target Issue B, origin Task = the managed session's Task):

- origin claims B                                  -> applied (existing normal path)
- B unclaimed, origin holds no live Issue claim    -> deferred / TARGET_ISSUE_UNCLAIMED (non-mutating)
- B unclaimed, origin claims a different Issue A   -> conflict / FACT_TASK_IDENTITY_CONFLICT
                                                      (#2810 stale-origin negative regression)
- B claimed by a different Task C                  -> conflict / FACT_TASK_IDENTITY_CONFLICT
"""

from __future__ import annotations

import task_context_service as service
import task_context_workflow_signals as signals
from workflow_signal_test_support import (
    REPO,
    SHA64,
    cleanup_completed_payload,
    create_origin,
    implementation_payload,
    merged_payload,
)

ISSUE_A = 10
ISSUE_B = 20
CONFLICT = {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
UNCLAIMED = {"disposition": "deferred", "reason_code": "TARGET_ISSUE_UNCLAIMED"}


def _payload(issue_number: int = ISSUE_B):
    return {
        "signal_kind": "refinement_approved",
        "source": "issue-refinement-loop",
        "source_schema_version": "v1",
        "evidence": {"repo": REPO, "issue_number": issue_number, "approved_body_sha256": SHA64},
    }


def _snapshot(conn) -> dict[str, list[tuple]]:
    """Full content of EVERY Task Context table, so a mutation anywhere is detected."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    assert {"events", "tasks", "task_ref_claims", "activities", "projection_outbox"} <= set(tables)
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
        for table in tables
    }


def _apply(conn, session="session-1", issue_number: int = ISSUE_B):
    return signals.apply_workflow_signal(conn, _payload(issue_number), origin_session_id=session)


def _assert_non_mutating(conn, session="session-1", issue_number: int = ISSUE_B):
    before = _snapshot(conn)
    result = _apply(conn, session, issue_number)
    assert _snapshot(conn) == before
    return result


def test_given_origin_claims_target_when_approved_then_applied_and_refine_terminalizes(conn):
    task, refine, _, _ = create_origin(conn, kind="refine")
    service.claim_task_ref(conn, task["id"], REPO, "issue", ISSUE_B)
    result = _apply(conn)
    assert result["disposition"] == "applied"
    assert result["reason_code"] == "APPLIED"
    assert service.get_activity(conn, refine["id"])["status"] == "DONE"


def test_given_no_issue_claim_anywhere_when_approved_then_deferred_unclaimed_and_every_table_unchanged(conn):
    task, refine, _, _ = create_origin(conn, kind="refine")
    result = _assert_non_mutating(conn)
    assert result == UNCLAIMED
    assert service.get_activity(conn, refine["id"])["status"] == "ACTIVE"
    assert service.find_live_claim(conn, REPO, "issue", ISSUE_B) is None
    assert service.count_live_task_ref_claims(conn, task["id"]) == 0


def test_given_stale_origin_claims_other_issue_when_target_unclaimed_then_identity_conflict_not_deferred(conn):
    """#2810 failure-class negative regression: the origin Task is bound to a
    different Issue (A), the target (B) has no live claim. This is a genuine
    identity conflict and must never be downgraded to TARGET_ISSUE_UNCLAIMED."""
    task, refine, _, _ = create_origin(conn, kind="refine")
    service.claim_task_ref(conn, task["id"], REPO, "issue", ISSUE_A)
    result = _assert_non_mutating(conn)
    assert result == CONFLICT
    assert result["reason_code"] != "TARGET_ISSUE_UNCLAIMED"
    assert service.get_activity(conn, refine["id"])["status"] == "ACTIVE"
    assert service.find_live_claim(conn, REPO, "issue", ISSUE_B) is None


def test_given_other_task_claims_target_when_origin_has_no_issue_claim_then_identity_conflict(conn):
    create_origin(conn, kind="refine")
    other = service.create_task(conn, title="other task C")
    service.claim_task_ref(conn, other["id"], REPO, "issue", ISSUE_B)
    result = _assert_non_mutating(conn)
    assert result == CONFLICT
    assert service.find_live_claim(conn, REPO, "issue", ISSUE_B)["task_id"] == other["id"]


def test_given_other_task_claims_target_when_origin_also_claims_other_issue_then_identity_conflict(conn):
    task, _, _, _ = create_origin(conn, kind="refine")
    service.claim_task_ref(conn, task["id"], REPO, "issue", ISSUE_A)
    other = service.create_task(conn, title="other task C")
    service.claim_task_ref(conn, other["id"], REPO, "issue", ISSUE_B)
    result = _assert_non_mutating(conn)
    assert result == CONFLICT
    assert service.find_live_claim(conn, REPO, "issue", ISSUE_B)["task_id"] == other["id"]


def _accept_then_release(conn):
    """Task X (session-1) claims B and gets the approved fact accepted, then
    its claim on B is released. Returns Task X."""
    task_x, _, _, _ = create_origin(conn, kind="refine", session="session-1")
    claim = service.claim_task_ref(conn, task_x["id"], REPO, "issue", ISSUE_B)
    assert _apply(conn, "session-1")["disposition"] == "applied"
    service.release_task_ref_claim(conn, claim["claim_id"])
    assert service.find_live_claim(conn, REPO, "issue", ISSUE_B) is None
    return task_x


def test_given_accepted_event_by_task_x_when_task_y_without_claim_replays_then_conflict_not_deferred(conn):
    task_x = _accept_then_release(conn)
    task_y, _, _, _ = create_origin(conn, kind="refine", session="session-2")
    assert task_y["id"] != task_x["id"]
    assert service.count_live_task_ref_claims(conn, task_y["id"]) == 0
    result = _assert_non_mutating(conn, "session-2")
    assert result == CONFLICT
    assert result["reason_code"] != "TARGET_ISSUE_UNCLAIMED"


def test_given_accepted_event_by_task_x_when_same_task_x_replays_after_release_then_current_conflict_is_kept(conn):
    _accept_then_release(conn)
    result = _assert_non_mutating(conn, "session-1")
    assert result == CONFLICT
    assert result["reason_code"] != "TARGET_ISSUE_UNCLAIMED"


def test_given_live_claim_and_accepted_event_when_same_task_replays_then_duplicate_noop(conn):
    task, _, _, _ = create_origin(conn, kind="refine")
    service.claim_task_ref(conn, task["id"], REPO, "issue", ISSUE_B)
    assert _apply(conn)["disposition"] == "applied"
    result = _assert_non_mutating(conn)
    assert result == {"disposition": "duplicate_noop", "reason_code": "SAME_TASK_SAME_FACT", "task_id": task["id"]}


def test_given_cleanup_completed_when_issue_claim_released_after_merge_then_conflict_not_unclaimed(conn):
    """cleanup_completed non-regression: accepted merge first (production
    shape), then the Issue claim is released -> strict identity conflict."""
    task, _, _, _ = create_origin(conn)
    assert signals.apply_workflow_signal(conn, implementation_payload(issue_number=ISSUE_B), origin_session_id="session-1")[
        "disposition"
    ] == "applied"
    assert signals.apply_workflow_signal(conn, merged_payload(issue_number=ISSUE_B), origin_session_id="session-1")[
        "disposition"
    ] == "applied"
    issue_claim = service.find_live_claim(conn, REPO, "issue", ISSUE_B)
    assert issue_claim["task_id"] == task["id"]
    service.release_task_ref_claim(conn, issue_claim["id"])
    before = _snapshot(conn)
    result = signals.apply_workflow_signal(
        conn, cleanup_completed_payload(issue_number=ISSUE_B), origin_session_id="session-1"
    )
    assert result == CONFLICT
    assert result["reason_code"] != "TARGET_ISSUE_UNCLAIMED"
    assert _snapshot(conn) == before


def test_given_refine_activity_missing_or_terminal_when_claim_state_differs_then_precedence_is_claim_first(conn):
    # Origin claims B but has no refine Activity (implementation only) -> activity_missing.
    task, _, _, _ = create_origin(conn, kind="implementation", session="session-1")
    service.claim_task_ref(conn, task["id"], REPO, "issue", ISSUE_B)
    assert _assert_non_mutating(conn, "session-1") == {"disposition": "deferred", "reason_code": "activity_missing"}

    # Origin claims B and refine is terminal -> activity_terminal duplicate_noop.
    task2, refine2, _, _ = create_origin(conn, kind="refine", session="session-2")
    service.claim_task_ref(conn, task2["id"], REPO, "issue", ISSUE_B + 1)
    conn.execute("UPDATE activities SET status = 'DONE', ended_at = '2026-01-01T00:00:00Z' WHERE id = ?", (refine2["id"],))
    result = _assert_non_mutating(conn, "session-2", ISSUE_B + 1)
    assert result == {"disposition": "duplicate_noop", "reason_code": "activity_terminal", "task_id": task2["id"]}

    # Claim precedes Activity: an unclaimed target reports the claim state even
    # when the origin has no refine Activity (implementation-only origin).
    create_origin(conn, kind="implementation", session="session-3")
    assert _assert_non_mutating(conn, "session-3", ISSUE_B + 2) == UNCLAIMED

    # ... and a stale origin (claims other Issue) with terminal refine is still a conflict.
    task4, refine4, _, _ = create_origin(conn, kind="refine", session="session-4")
    service.claim_task_ref(conn, task4["id"], REPO, "issue", ISSUE_A)
    conn.execute("UPDATE activities SET status = 'DONE', ended_at = '2026-01-01T00:00:00Z' WHERE id = ?", (refine4["id"],))
    assert _assert_non_mutating(conn, "session-4", ISSUE_B + 3) == CONFLICT


def test_given_target_unclaimed_no_other_claim_when_refine_terminal_then_deferred_unclaimed_not_activity_terminal(conn):
    """Claim state precedes Activity state: with no live Issue claim anywhere
    (target B unclaimed, origin holds no other Issue claim) a terminal refine
    Activity must NOT surface as activity_terminal; it stays TARGET_ISSUE_UNCLAIMED
    and the terminal Activity row (and every other table) is left untouched."""
    task, refine, _, _ = create_origin(conn, kind="refine")
    conn.execute(
        "UPDATE activities SET status = 'DONE', ended_at = '2026-01-01T00:00:00Z' WHERE id = ?", (refine["id"],)
    )
    assert service.count_live_task_ref_claims(conn, task["id"]) == 0
    result = _assert_non_mutating(conn)
    assert result == UNCLAIMED
    assert result["reason_code"] != "activity_terminal"
    assert service.get_activity(conn, refine["id"])["status"] == "DONE"
    assert service.find_live_claim(conn, REPO, "issue", ISSUE_B) is None
    assert service.count_live_task_ref_claims(conn, task["id"]) == 0

"""Issue #2817: retroactive claim recovery for a merged PR (``--phase recover``).

Every behavioral test uses a temporary SQLite DB, a snapshot fixture and the
REAL adapter CLI as a subprocess (adapter -> ``task_contextctl.py``). Nothing
patches ``_run``. DB assertions compare full row CONTENTS, not counts.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

import pytest

import task_context_config as config
import task_context_db as db
import task_context_service as service
import task_context_workflow_signals as workflow_signals
from workflow_signal_test_support import merged_payload
from retroactive_claim_support import (
    DOCS,
    ISSUE,
    OID,
    PR,
    REPO,
    REPO_ROOT,
    SESSION,
    SKILL,
    add_subagent_run,
    base_args,
    build_origin,
    claim,
    cleanup_report,
    dump_db,
    local_only_args,
    merged_args,
    merged_snapshot,
    new_rows,
    plain_task,
    read_all,
    recover_args,
    rows_of_tasks,
    run_adapter,
    write_snapshot,
)

RECOVERY_KEYS = {
    "operation",
    "source",
    "source_schema_version",
    "repo",
    "issue_number",
    "pr_number",
    "merge_commit_oid",
    "task_id",
    "execution_run_id",
    "activity_id",
    "prior_activity_id",
    "prior_activity_kind",
    "claims_attached",
    "activity_action",
}
RECOVERY_DEDUPE_KEY = f"task-context-v1:retroactive_claim_recovery:{REPO}:{PR}:{OID}"


def _recover(tmp_path, state_root, **overrides):
    snapshot = write_snapshot(tmp_path)
    return run_adapter(recover_args(snapshot, **overrides), state_root=state_root)


def _recovery_events():
    return read_all("SELECT * FROM events WHERE event_type = 'recovery:implementation_claims' ORDER BY rowid")


def _live_claim_owner(kind: str, number: int):
    rows = read_all(
        "SELECT task_id FROM task_ref_claims "
        "WHERE repo = ? AND ref_kind = ? AND ref_number = ? AND released_at IS NULL",
        (REPO, kind, number),
    )
    return rows[0]["task_id"] if rows else None


# ===========================================================================
# AC1: snapshot / identity / argument negatives (recover AND local-only)
# ===========================================================================


def _wrong_repo_relation():
    return merged_snapshot(relation_repo="owner/other")


NEGATIVE_CASES = {
    "unmerged": (lambda: merged_snapshot(merged=False), {}, ("deferred", "MERGED_SNAPSHOT_INVALID")),
    "same_number_other_repository": (_wrong_repo_relation, {}, ("deferred", "RELATION_ISSUE_MISMATCH")),
    "closing_references_zero": (lambda: merged_snapshot(nodes=[]), {}, ("deferred", "RELATION_ISSUE_MISMATCH")),
    "closing_references_two": (
        lambda: merged_snapshot(
            nodes=[
                {"number": ISSUE, "repository": {"nameWithOwner": REPO}},
                {"number": 99, "repository": {"nameWithOwner": REPO}},
            ]
        ),
        {},
        ("deferred", "RELATION_ISSUE_MISMATCH"),
    ),
    "partial_data_plus_top_level_errors": (
        lambda: merged_snapshot(errors=[{"message": "partial resolver failure"}]),
        {},
        ("deferred", "RELATION_UNAVAILABLE"),
    ),
    "merge_oid_key_missing": (
        lambda: merged_snapshot(with_merge_commit=False),
        {},
        ("deferred", "RELATION_UNAVAILABLE"),
    ),
    "merge_commit_null": (lambda: _null_merge_commit(), {}, ("deferred", "RELATION_UNAVAILABLE")),
    "merge_oid_not_a_string": (lambda: merged_snapshot(oid=12345), {}, ("deferred", "MERGE_OID_INVALID")),
    "merge_oid_not_hex40": (
        lambda: merged_snapshot(oid="B" * 40),
        {"merge_identity": "B" * 40},
        ("rejected_evidence", "MERGE_IDENTITY_MISMATCH"),
    ),
    "merge_oid_short": (
        lambda: merged_snapshot(oid="abc123"),
        {"merge_identity": "abc123"},
        ("rejected_evidence", "MERGE_IDENTITY_MISMATCH"),
    ),
    "merge_identity_differs_from_snapshot": (
        lambda: merged_snapshot(),
        {"merge_identity": "c" * 40},
        ("rejected_evidence", "MERGE_IDENTITY_MISMATCH"),
    ),
    "merge_identity_not_hex40": (
        lambda: merged_snapshot(),
        {"merge_identity": "not-a-sha"},
        ("rejected_evidence", "MERGE_IDENTITY_MISMATCH"),
    ),
    "target_issue_differs": (lambda: merged_snapshot(), {"issue": 99}, ("deferred", "RELATION_ISSUE_MISMATCH")),
    "target_pr_differs": (lambda: merged_snapshot(), {"pr": 22}, ("deferred", "MERGED_SNAPSHOT_INVALID")),
    "closes_keyword_in_body_only": (
        lambda: merged_snapshot(nodes=[], body=f"Closes #{ISSUE}"),
        {},
        ("deferred", "RELATION_ISSUE_MISMATCH"),
    ),
    "stale_snapshot": (lambda: merged_snapshot(), {"age": 400}, ("deferred", "SNAPSHOT_STALE")),
}


def _null_merge_commit():
    snapshot = merged_snapshot()
    snapshot["data"]["repository"]["pullRequest"]["mergeCommit"] = None
    return snapshot


def _recover_ready_state(conn):
    """Everything is in place for a successful recovery: only the snapshot or
    the arguments can make the adapter refuse."""
    origin = build_origin(conn, active_kind="refine")
    claim(conn, origin["task"]["id"], "issue", ISSUE)
    return origin


@pytest.mark.parametrize("phase", ["recover", "local-only"])
@pytest.mark.parametrize("case", sorted(NEGATIVE_CASES))
def test_snapshot_negative_cases_leave_every_db_row_untouched(tmp_path, state_root, conn, phase, case):
    build, overrides, (disposition, reason) = NEGATIVE_CASES[case]
    _recover_ready_state(conn)
    conn.close()
    before = dump_db()
    snapshot = write_snapshot(tmp_path, build(), age_seconds=overrides.get("age", 0))
    common = {k: v for k, v in overrides.items() if k in ("merge_identity", "issue", "pr")}
    args = recover_args(snapshot, **common) if phase == "recover" else local_only_args(snapshot, **common)

    result = run_adapter(args, state_root=state_root)

    assert result == {"disposition": disposition, "reason_code": reason}
    assert dump_db() == before


def test_snapshot_negative_unreadable_snapshot_file_is_deferred(tmp_path, state_root, conn):
    _recover_ready_state(conn)
    conn.close()
    before = dump_db()
    missing = tmp_path / "does-not-exist.json"

    assert run_adapter(recover_args(missing), state_root=state_root) == {
        "disposition": "deferred",
        "reason_code": "RELATION_UNAVAILABLE",
    }
    assert run_adapter(local_only_args(missing), state_root=state_root) == {
        "disposition": "deferred",
        "reason_code": "RELATION_UNAVAILABLE",
    }
    assert dump_db() == before


def test_snapshot_negative_missing_explicit_recovery_flag_is_rejected_before_any_write(tmp_path, state_root, conn):
    _recover_ready_state(conn)
    conn.close()
    before = dump_db()
    snapshot = write_snapshot(tmp_path)

    result = run_adapter(recover_args(snapshot, explicit=False), state_root=state_root)

    assert result == {"disposition": "rejected_evidence", "reason_code": "EXPLICIT_RECOVERY_REQUIRED"}
    assert dump_db() == before


@pytest.mark.parametrize(
    "overrides",
    [{"merge_identity": None}, {"merge_identity": ""}, {"session": None}, {"session": ""}],
    ids=["no_merge_identity", "empty_merge_identity", "no_origin_session", "empty_origin_session"],
)
def test_snapshot_negative_missing_required_recover_argument_is_rejected_before_any_write(
    tmp_path, state_root, conn, overrides
):
    _recover_ready_state(conn)
    conn.close()
    before = dump_db()
    snapshot = write_snapshot(tmp_path)

    # An ambient session is present on purpose: recovery must never fall back to it.
    result = run_adapter(recover_args(snapshot, **overrides), state_root=state_root, ambient_session=SESSION)

    assert result == {"disposition": "rejected_evidence", "reason_code": "MISSING_REQUIRED_ARGUMENT"}
    assert dump_db() == before


def test_snapshot_negative_recover_never_uses_the_ambient_session_as_origin(tmp_path, state_root, conn):
    _recover_ready_state(conn)
    conn.close()
    before = dump_db()
    snapshot = write_snapshot(tmp_path)

    # Explicit origin names an unbound session while the ambient one is the real origin.
    result = run_adapter(
        recover_args(snapshot, session="explicit-but-unbound"), state_root=state_root, ambient_session=SESSION
    )

    assert result == {"disposition": "deferred", "reason_code": "unbound"}
    assert dump_db() == before


# ===========================================================================
# AC2: real-CLI end-to-end (recover -> merged -> cleanup begin selected)
# ===========================================================================


def _reported_order(conn):
    """PR #2856-like order: the origin session started in another Task, the
    Issue's Task already exists (``/task`` bootstrap: Issue claim + operator
    Activity) and the origin was rebound to it; the PR claim never existed."""
    other = build_origin(conn, session="session-before-rebind", title="other-origin", active_kind="refine")
    issue_task = plain_task(conn, "issue-task")
    claim(conn, issue_task, "issue", ISSUE)
    operator_activity = service.transition_activity(conn, issue_task, "native_operator")
    with db.write_transaction(conn):
        service._attach_execution_run_tx(
            conn,
            other["run"]["id"],
            task_id=issue_task,
            activity_id=operator_activity["id"],
            binding_id=other["binding"]["id"],
        )
    return {
        "task": {"id": issue_task},
        "binding": other["binding"],
        "run": other["run"],
        "session": "session-before-rebind",
    }


def _issue_task_with_active_implementation(conn):
    origin = build_origin(conn, active_kind="implementation")
    claim(conn, origin["task"]["id"], "issue", ISSUE)
    return {"task": origin["task"], "run": origin["run"], "session": SESSION}


def _nothing_recorded(conn):
    origin = build_origin(conn, active_kind="none")
    return {"task": origin["task"], "run": origin["run"], "session": SESSION}


@pytest.mark.parametrize(
    "builder,expected_action,expected_claims",
    [
        (_reported_order, "transitioned", "pr"),
        (_issue_task_with_active_implementation, "reused", "pr"),
        (_nothing_recorded, "started", "issue,pr"),
    ],
    ids=["reported_order_rebound_origin", "implementation_already_active", "no_claims_no_activity"],
)
def test_e2e_merged_selected_after_explicit_recovery_via_real_cli(
    tmp_path, state_root, conn, builder, expected_action, expected_claims
):
    state = builder(conn)
    session, tid = state["session"], state["task"]["id"]
    bystander = build_origin(conn, session="bystander-session", title="bystander", active_kind="refine")
    claim(conn, bystander["task"]["id"], "issue", 77)
    claim(conn, bystander["task"]["id"], "pr", 78)
    add_subagent_run(conn, bystander["task"]["id"])
    conn.close()
    unrelated_before = rows_of_tasks([bystander["task"]["id"]])
    snapshot = write_snapshot(tmp_path)

    # Baseline: before recovery the merge fact is refused and nothing is written.
    before_merged = dump_db()
    refused = run_adapter(merged_args(snapshot, session=session), state_root=state_root)
    assert refused == {"disposition": "deferred", "reason_code": "IMPLEMENTATION_NOT_READY"}
    assert dump_db() == before_merged

    recovered = run_adapter(recover_args(snapshot, session=session), state_root=state_root)
    assert recovered == {
        "disposition": "applied",
        "reason_code": "RECOVERED",
        "task_id": tid,
        "activity_action": expected_action,
        "claims_attached": expected_claims,
    }
    assert _live_claim_owner("issue", ISSUE) == tid
    assert _live_claim_owner("pr", PR) == tid

    # Same effective origin, ambient session deliberately different.
    selected = run_adapter(merged_args(snapshot, session=session), state_root=state_root)
    assert selected["disposition"] == "selected"
    assert selected["reason_code"] == "CLEANUP_STARTED"
    assert selected["task_id"] == tid
    cleanup_activity_id = selected["activity_id"]

    merge_events = read_all("SELECT * FROM events WHERE event_type = 'workflow:pr_merged_observed'")
    assert len(merge_events) == 1
    assert merge_events[0]["task_id"] == tid
    assert merge_events[0]["dedupe_key"] == f"task-context-v1:pr_merged_observed:{REPO}:{PR}:{OID}"
    assert json.loads(merge_events[0]["metadata_json"])["merge_commit_oid"] == OID
    cleanup_events = read_all("SELECT * FROM events WHERE event_type = 'workflow:cleanup_started'")
    assert [row["task_id"] for row in cleanup_events] == [tid]
    cleanup_row = read_all("SELECT * FROM activities WHERE id = ?", (cleanup_activity_id,))[0]
    assert (cleanup_row["task_id"], cleanup_row["kind"], cleanup_row["status"]) == (tid, "cleanup", "ACTIVE")
    # The recovery itself never records a lifecycle completion.
    assert read_all("SELECT 1 FROM events WHERE event_type = 'workflow:cleanup_completed'") == []

    # Unrelated Task / Activity / claim / run rows are byte-identical.
    assert rows_of_tasks([bystander["task"]["id"]]) == unrelated_before

    # An unfinished cleanup is resumed (selected again), same Activity.
    resumed = run_adapter(merged_args(snapshot, session=session), state_root=state_root)
    assert (resumed["disposition"], resumed["reason_code"]) == ("selected", "CLEANUP_ALREADY_SELECTED")
    assert resumed["activity_id"] == cleanup_activity_id

    # Complete it through the real final-success receipt path.
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(cleanup_report()), encoding="utf-8")
    completed = run_adapter(
        [*base_args(snapshot, "completed"), "--cleanup-receipt-file", str(receipt), "--origin-session-id", session],
        state_root=state_root,
    )
    assert completed["disposition"] == "applied"

    # An already-completed cleanup is never `selected` again => no worker dispatch.
    rerun = run_adapter(merged_args(snapshot, session=session), state_root=state_root)
    assert rerun["disposition"] != "selected"
    assert (rerun["disposition"], rerun["reason_code"]) == ("duplicate_noop", "activity_terminal")


def test_e2e_merged_selected_recovery_accepts_a_snapshot_inside_the_freshness_window(tmp_path, state_root, conn):
    state = _nothing_recorded(conn)
    conn.close()
    snapshot = write_snapshot(tmp_path, age_seconds=200)

    result = run_adapter(recover_args(snapshot, session=state["session"]), state_root=state_root)

    assert (result["disposition"], result["reason_code"]) == ("applied", "RECOVERED")


# ===========================================================================
# AC3: claim ownership matrix, real CLI
# ===========================================================================


def _matrix_state(conn, *, issue=None, pr=None, t_other_issue=None, t_other_pr=None, released=()):
    """``issue`` / ``pr``: owner of the target claim (``T`` origin, ``X``, ``Y`` or None)."""
    origin = build_origin(conn, active_kind="refine")
    t = origin["task"]["id"]
    owners = {"T": t, "X": plain_task(conn, "X"), "Y": plain_task(conn, "Y")}
    for kind, number, who in (("issue", ISSUE, issue), ("pr", PR, pr)):
        if who:
            claim(conn, owners[who], kind, number)
    if t_other_issue:
        claim(conn, t, "issue", t_other_issue)
    if t_other_pr:
        claim(conn, t, "pr", t_other_pr)
    for kind, number, who in released:
        service.release_task_ref_claim(conn, claim(conn, owners[who], kind, number))
    return owners


MATRIX_REJECTS = {
    # order 1: a target claim is owned by a Task other than the origin
    "issue_owned_by_other_pr_missing": (
        dict(issue="X"),
        "FACT_TASK_IDENTITY_CONFLICT",
        {"issue_claim_owner_task_id": "X"},
    ),
    "pr_owned_by_other_issue_missing": (dict(pr="X"), "FACT_TASK_IDENTITY_CONFLICT", {"pr_claim_owner_task_id": "X"}),
    "issue_and_pr_owned_by_two_other_tasks": (
        dict(issue="X", pr="Y"),
        "FACT_TASK_IDENTITY_CONFLICT",
        {"issue_claim_owner_task_id": "X", "pr_claim_owner_task_id": "Y"},
    ),
    "issue_is_origin_pr_is_other_no_partial_repair": (
        dict(issue="T", pr="X"),
        "FACT_TASK_IDENTITY_CONFLICT",
        {"issue_claim_owner_task_id": "T", "pr_claim_owner_task_id": "X"},
    ),
    "issue_is_other_pr_is_origin": (
        dict(issue="X", pr="T"),
        "FACT_TASK_IDENTITY_CONFLICT",
        {"issue_claim_owner_task_id": "X", "pr_claim_owner_task_id": "T"},
    ),
    # order 2: the origin Task already holds a live claim on another Issue
    "origin_holds_another_issue": (
        dict(t_other_issue=99),
        "FACT_TASK_IDENTITY_CONFLICT",
        {"origin_other_issue_number": 99},
    ),
    # order 3: the origin Task already holds a live claim on another PR
    "origin_holds_another_pr": (dict(t_other_pr=98), "OUT_OF_ORDER_SIGNAL", {"origin_other_pr_number": 98}),
    # judgment order 1 -> 2 -> 3
    "order_1_wins_over_2": (
        dict(issue="X", t_other_issue=99),
        "FACT_TASK_IDENTITY_CONFLICT",
        {"issue_claim_owner_task_id": "X"},
    ),
    "order_1_wins_over_3": (
        dict(pr="X", t_other_pr=98),
        "FACT_TASK_IDENTITY_CONFLICT",
        {"pr_claim_owner_task_id": "X"},
    ),
    "order_2_wins_over_3": (
        dict(t_other_issue=99, t_other_pr=98),
        "FACT_TASK_IDENTITY_CONFLICT",
        {"origin_other_issue_number": 99},
    ),
}


@pytest.mark.parametrize("case", sorted(MATRIX_REJECTS))
def test_claim_split_rows_1_to_3_reject_without_moving_or_adding_anything(tmp_path, state_root, conn, case):
    setup, reason, diagnostics = MATRIX_REJECTS[case]
    owners = _matrix_state(conn, **setup)
    conn.close()
    before = dump_db()
    id_of = {name: task_id for name, task_id in owners.items()}
    expected_diag = {k: id_of.get(v, v) for k, v in diagnostics.items()}

    result = _recover(tmp_path, state_root)

    assert result == {
        "disposition": "conflict",
        "reason_code": reason,
        "task_id": owners["T"],
        "activity_action": "none",
        "claims_attached": "none",
        **expected_diag,
    }
    assert dump_db() == before


MATRIX_APPLIED = {
    "both_missing": (dict(), "issue,pr"),
    "issue_is_origin_pr_missing": (dict(issue="T"), "pr"),
    "pr_is_origin_issue_missing": (dict(pr="T"), "issue"),
    "both_are_origin": (dict(issue="T", pr="T"), "none"),
    "other_owner_claim_was_released": (dict(released=(("issue", ISSUE, "X"),)), "issue,pr"),
    "other_owner_pr_claim_was_released_issue_is_origin": (dict(issue="T", released=(("pr", PR, "X"),)), "pr"),
    "origin_other_issue_claim_was_released": (dict(released=(("issue", 99, "T"),)), "issue,pr"),
    "origin_other_pr_claim_was_released": (dict(released=(("pr", 98, "T"),)), "issue,pr"),
}


@pytest.mark.parametrize("case", sorted(MATRIX_APPLIED))
def test_claim_split_row_4_attaches_only_the_missing_claims(tmp_path, state_root, conn, case):
    setup, expected_attached = MATRIX_APPLIED[case]
    if case.startswith("origin_other"):
        # a released claim of the origin on ANOTHER ref: build it explicitly
        origin = build_origin(conn, active_kind="refine")
        owners = {"T": origin["task"]["id"]}
        kind, number, _ = setup["released"][0]
        service.release_task_ref_claim(conn, claim(conn, owners["T"], kind, number))
    else:
        owners = _matrix_state(conn, **setup)
    conn.close()
    before = dump_db()

    result = _recover(tmp_path, state_root)

    after = dump_db()
    assert (result["disposition"], result["reason_code"]) == ("applied", "RECOVERED")
    assert result["claims_attached"] == expected_attached
    # claims_attached matches the claim rows that were actually inserted
    inserted = {(r[4], r[5]) for r in new_rows(before, after).get("task_ref_claims", [])}  # (ref_kind, ref_number)
    expected_rows = set()
    if "issue" in expected_attached.split(","):
        expected_rows.add(("issue", ISSUE))
    if "pr" in expected_attached.split(","):
        expected_rows.add(("pr", PR))
    assert inserted == expected_rows
    assert _live_claim_owner("issue", ISSUE) == owners["T"]
    assert _live_claim_owner("pr", PR) == owners["T"]
    # no claim row that existed before was modified or removed
    for old_row in before["task_ref_claims"]:
        assert old_row in after["task_ref_claims"]


def test_claim_split_released_claim_row_is_kept_as_history_not_revived(tmp_path, state_root, conn):
    owners = _matrix_state(conn, released=(("issue", ISSUE, "X"),))
    conn.close()
    released_before = read_all("SELECT * FROM task_ref_claims WHERE task_id = ?", (owners["X"],))
    assert released_before[0]["released_at"] is not None

    result = _recover(tmp_path, state_root)

    assert result["reason_code"] == "RECOVERED"
    released_after = read_all("SELECT * FROM task_ref_claims WHERE task_id = ?", (owners["X"],))
    assert [tuple(r) for r in released_after] == [tuple(r) for r in released_before]


def test_claim_split_unbound_origin_is_reported_with_the_frozen_wire_only(tmp_path, state_root, conn):
    _matrix_state(conn)
    conn.close()
    before = dump_db()

    result = _recover(tmp_path, state_root, session="no-such-session")

    assert result == {"disposition": "deferred", "reason_code": "unbound"}
    assert dump_db() == before


# ===========================================================================
# AC4: implementation Activity decision (real DB fixtures + pure function)
# ===========================================================================

ACTIVE_KINDS = ["none", "implementation", "refine", "native_operator", "cleanup", "other"]
IMPL_STATUSES = ["none", "ACTIVE", "terminal"]
TERMINAL_REJECT = ("conflict", "IMPLEMENTATION_ACTIVITY_TERMINAL")
TERMINAL_NOOP = ("duplicate_noop", "MERGE_FACT_ALREADY_ACCEPTED")
NOT_RECOVERABLE = ("conflict", "ACTIVITY_KIND_NOT_RECOVERABLE")
INCONSISTENT = ("conflict", "ACTIVITY_STATE_INCONSISTENT")


def _pure_expected(active: str, impl: str, merge_accepted: bool):
    """Independent oracle for the 18-cell Cartesian product (Issue #2817
    In Scope section 3). Second element ``None`` = a write decision."""
    if (active == "implementation") != (impl == "ACTIVE"):
        return "reject", INCONSISTENT  # not DB-constructible: ux_activities_active_per_task
    if active == "implementation":
        return "reuse", None
    if impl == "terminal":
        return ("noop", TERMINAL_NOOP) if merge_accepted else ("reject", TERMINAL_REJECT)
    if active == "none":
        return "start", None
    if active in ("refine", "native_operator"):
        return "transition", None
    return "reject", NOT_RECOVERABLE


@pytest.mark.parametrize("merge_accepted", [False, True])
@pytest.mark.parametrize("impl", IMPL_STATUSES)
@pytest.mark.parametrize("active", ACTIVE_KINDS)
def test_activity_state_pure_function_covers_all_18_cells(active, impl, merge_accepted):
    action, reason = _pure_expected(active, impl, merge_accepted)

    decision = workflow_signals.decide_activity_action(active, impl, merge_accepted)

    assert decision["action"] == action
    if reason is None:
        assert set(decision) == {"action"}
    else:
        assert (decision["disposition"], decision["reason_code"]) == reason


def test_activity_state_pure_function_has_exactly_7_non_constructible_cells():
    inconsistent = [
        (a, i)
        for a in ACTIVE_KINDS
        for i in IMPL_STATUSES
        if workflow_signals.decide_activity_action(a, i, False).get("reason_code") == "ACTIVITY_STATE_INCONSISTENT"
    ]
    assert len(ACTIVE_KINDS) * len(IMPL_STATUSES) == 18
    assert sorted(inconsistent) == sorted(
        [("implementation", "none"), ("implementation", "terminal")]
        + [(a, "ACTIVE") for a in ("none", "refine", "native_operator", "cleanup", "other")]
    )


def test_activity_state_pure_function_fails_closed_on_values_outside_the_closed_domains():
    for active, impl in (("smoke", "none"), ("none", "DONE"), ("", "none"), ("none", "")):
        decision = workflow_signals.decide_activity_action(active, impl, False)
        assert (decision["action"], decision["disposition"], decision["reason_code"]) == ("reject", *INCONSISTENT)


def test_activity_state_non_constructible_cells_really_are_not_constructible_in_the_db(conn):
    origin = build_origin(conn, active_kind="refine")
    # (refine ACTIVE, implementation ACTIVE): two ACTIVE rows for one Task.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO activities (id, task_id, kind, status, started_at, ended_at) "
            "VALUES ('raw-impl', ?, 'implementation', 'ACTIVE', '2026-01-01T00:00:00+00:00', NULL)",
            (origin["task"]["id"],),
        )


# combo -> (active kind, history, merge fact already accepted, DB cell, outcome, activity_action)
DB_COMBOS = {
    "none_no_history": ("none", "none", False, ("none", "none"), ("applied", "RECOVERED"), "started"),
    "none_terminal_history": ("none", "terminal", False, ("none", "terminal"), TERMINAL_REJECT, None),
    "none_terminal_history_merge_accepted": (
        "implementation",
        "none",
        True,
        ("none", "terminal"),
        TERMINAL_NOOP,
        None,
    ),
    "refine_no_history": ("refine", "none", False, ("refine", "none"), ("applied", "RECOVERED"), "transitioned"),
    "native_operator_no_history": (
        "native_operator",
        "none",
        False,
        ("native_operator", "none"),
        ("applied", "RECOVERED"),
        "transitioned",
    ),
    "cleanup_no_history": ("cleanup", "none", False, ("cleanup", "none"), NOT_RECOVERABLE, None),
    "other_no_history": ("smoke", "none", False, ("other", "none"), NOT_RECOVERABLE, None),
    "refine_terminal_history": ("refine", "terminal", False, ("refine", "terminal"), TERMINAL_REJECT, None),
    "native_operator_terminal_history": (
        "native_operator",
        "terminal",
        False,
        ("native_operator", "terminal"),
        TERMINAL_REJECT,
        None,
    ),
    "cleanup_terminal_history": ("cleanup", "terminal", False, ("cleanup", "terminal"), TERMINAL_REJECT, None),
    "other_terminal_history": ("smoke", "terminal", False, ("other", "terminal"), TERMINAL_REJECT, None),
    "implementation_active": (
        "implementation",
        "none",
        False,
        ("implementation", "ACTIVE"),
        ("applied", "RECOVERED"),
        "reused",
    ),
    "implementation_active_with_older_terminal_rows": (
        "implementation",
        "terminal",
        False,
        ("implementation", "ACTIVE"),
        ("applied", "RECOVERED"),
        "reused",
    ),
}


def test_activity_state_db_fixture_set_is_the_11_constructible_cells():
    cells = {combo[3] for combo in DB_COMBOS.values()}
    assert len(cells) == 11
    assert cells == {
        (a, i) for a in ("none", "refine", "native_operator", "cleanup", "other") for i in ("none", "terminal")
    } | {("implementation", "ACTIVE")}


@pytest.mark.parametrize("combo", sorted(DB_COMBOS))
def test_activity_state_db_combination_follows_the_decision_table(tmp_path, state_root, conn, combo):
    active_kind, history, merge_accepted, cell, (disposition, reason), action = DB_COMBOS[combo]
    origin = build_origin(conn, active_kind=active_kind, history=history)
    tid = origin["task"]["id"]
    stranger_run = add_subagent_run(conn, tid)
    if merge_accepted:
        # The merge fact was already accepted for this Task: the claims exist and
        # the implementation Activity ended together with it.
        claim(conn, tid, "issue", ISSUE)
        claim(conn, tid, "pr", PR)
        applied = workflow_signals.apply_workflow_signal(conn, merged_payload(), origin_session_id=SESSION)
        assert applied["disposition"] == "applied", applied
    # The DB state really sits in the expected cell of the decision table.
    state = workflow_signals._task_activity_state_tx(conn, tid)
    assert (state[0], state[1]) == cell
    conn.close()
    before = dump_db()

    result = _recover(tmp_path, state_root)

    after = dump_db()
    assert (result["disposition"], result["reason_code"]) == (disposition, reason)
    assert result["task_id"] == tid
    if action is None:
        assert result["activity_action"] == "none" and result["claims_attached"] == "none"
        assert after == before  # reject / no-op write nothing; terminal rows are never revived
        return

    assert result["activity_action"] == action
    created = new_rows(before, after)
    # no refinement_approved / implementation_pr_observed (or any workflow:) event is invented
    assert [row[5] for row in created["events"]] == ["recovery:implementation_claims"]
    activities = {r["id"]: r for r in read_all("SELECT * FROM activities WHERE task_id = ?", (tid,))}
    active_rows = [r for r in activities.values() if r["status"] == "ACTIVE"]
    assert len(active_rows) == 1 and active_rows[0]["kind"] == "implementation"
    for row in before["activities"]:  # rows that were terminal are exactly as they were
        if row[1] == tid and row[3] in ("DONE", "ABANDONED"):
            assert row in after["activities"]
    runs = {r["id"]: r for r in read_all("SELECT * FROM execution_runs WHERE task_id = ?", (tid,))}
    assert [tuple(r) for r in read_all("SELECT * FROM execution_runs WHERE id = ?", (stranger_run["id"],))] == [
        row for row in before["execution_runs"] if row[0] == stranger_run["id"]
    ]  # another ExecutionRun of the Task is never touched
    (event,) = _recovery_events()
    metadata = json.loads(event["metadata_json"])
    assert event["activity_id"] == active_rows[0]["id"] == metadata["activity_id"]
    if action == "reused":
        assert active_rows[0]["id"] == origin["activity"]["id"]
        assert runs[origin["run"]["id"]]["activity_id"] == origin["run"]["activity_id"] != active_rows[0]["id"]
        assert metadata["prior_activity_id"] is None and metadata["prior_activity_kind"] is None
    else:
        assert runs[origin["run"]["id"]]["activity_id"] == active_rows[0]["id"]  # origin follows the phase
        if action == "transitioned":
            assert metadata["prior_activity_id"] == origin["activity"]["id"]
            assert metadata["prior_activity_kind"] == active_kind
            assert activities[origin["activity"]["id"]]["status"] == "DONE"
        else:
            assert metadata["prior_activity_id"] is None and metadata["prior_activity_kind"] is None


# ===========================================================================
# AC5: atomicity, idempotency, race, journal
# ===========================================================================


def _two_origin_state(conn):
    first = build_origin(conn, session="first-origin", title="first", active_kind="refine")
    second = build_origin(conn, session="second-origin", title="second", active_kind="refine")
    return first, second


def _evidence():
    return {"repo": REPO, "issue_number": ISSUE, "pr_number": PR, "merge_commit_oid": OID}


def _recover_in_process(conn, session=SESSION):
    return workflow_signals.recover_implementation_claims(
        conn, origin_session_id=session, evidence=_evidence(), explicit_recovery=True
    )


def test_atomic_idempotent_second_identical_recovery_is_a_noop(tmp_path, state_root, conn):
    origin = build_origin(conn, active_kind="refine")
    conn.close()

    first = _recover(tmp_path, state_root)
    after_first = dump_db()
    second = _recover(tmp_path, state_root)

    assert (first["disposition"], first["reason_code"]) == ("applied", "RECOVERED")
    assert second == {
        "disposition": "duplicate_noop",
        "reason_code": "SAME_TASK_SAME_FACT",
        "task_id": origin["task"]["id"],
        "activity_action": "none",
        "claims_attached": "none",
    }
    assert dump_db() == after_first  # claims, Activities, events: no growth, no mutation
    assert len(_recovery_events()) == 1


def test_atomic_idempotent_a_writer_that_claimed_first_wins_and_nothing_is_reassigned(tmp_path, state_root, conn):
    first, second = _two_origin_state(conn)
    conn.close()

    won = run_adapter(recover_args(write_snapshot(tmp_path), session="first-origin"), state_root=state_root)
    after_first = dump_db()
    lost = run_adapter(recover_args(write_snapshot(tmp_path), session="second-origin"), state_root=state_root)

    assert (won["disposition"], won["reason_code"]) == ("applied", "RECOVERED")
    assert lost == {
        "disposition": "conflict",
        "reason_code": "FACT_TASK_IDENTITY_CONFLICT",
        "task_id": second["task"]["id"],
        "activity_action": "none",
        "claims_attached": "none",
        "issue_claim_owner_task_id": first["task"]["id"],
        "pr_claim_owner_task_id": first["task"]["id"],
    }
    assert dump_db() == after_first
    assert _live_claim_owner("issue", ISSUE) == _live_claim_owner("pr", PR) == first["task"]["id"]


def test_atomic_idempotent_concurrent_recoveries_leave_exactly_one_owner(tmp_path, state_root, conn):
    first, second = _two_origin_state(conn)
    conn.close()
    from concurrent.futures import ThreadPoolExecutor

    def attempt(session):
        for _ in range(3):  # BEGIN IMMEDIATE contention is a typed, retryable deferral
            result = run_adapter(
                recover_args(write_snapshot(tmp_path, name=f"{session}.json"), session=session),
                state_root=state_root,
            )
            if result.get("reason_code") != "TEMPORARILY_UNAVAILABLE":
                return result
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["first-origin", "second-origin"]))

    assert sorted(r["disposition"] for r in results) == ["applied", "conflict"]
    winner = first["task"]["id"] if results[0]["disposition"] == "applied" else second["task"]["id"]
    assert _live_claim_owner("issue", ISSUE) == winner
    assert _live_claim_owner("pr", PR) == winner
    assert len(_recovery_events()) == 1


def _release_all_live_claims(connection):
    for row in connection.execute("SELECT id FROM task_ref_claims WHERE released_at IS NULL").fetchall():
        service.release_task_ref_claim(connection, row["id"])


def test_atomic_idempotent_dedupe_key_accepted_by_another_task_rejects_after_the_claims_were_released(
    tmp_path, state_root, conn
):
    first, second = _two_origin_state(conn)
    conn.close()
    run_adapter(recover_args(write_snapshot(tmp_path), session="first-origin"), state_root=state_root)
    connection = db.connect(config.db_path())
    _release_all_live_claims(connection)
    connection.close()
    before = dump_db()

    result = run_adapter(recover_args(write_snapshot(tmp_path), session="second-origin"), state_root=state_root)

    assert result == {
        "disposition": "conflict",
        "reason_code": "FACT_TASK_IDENTITY_CONFLICT",
        "task_id": second["task"]["id"],
        "activity_action": "none",
        "claims_attached": "none",
    }
    assert dump_db() == before  # zero writes: no claim for the second Task, no second event
    assert _live_claim_owner("issue", ISSUE) is None


@pytest.mark.parametrize("injection", ["bypassed_precheck_real_unique_index", "raw_sqlite_integrity_error"])
def test_atomic_idempotent_unique_index_violation_is_normalized_to_the_same_conflict(
    state_root, conn, monkeypatch, injection
):
    _two_origin_state(conn)
    if injection == "bypassed_precheck_real_unique_index":
        assert _recover_in_process(conn, "first-origin")["disposition"] == "applied"
        _release_all_live_claims(conn)
    before = dump_db(conn)
    if injection == "bypassed_precheck_real_unique_index":
        real = workflow_signals._accepted_event_tx
        monkeypatch.setattr(
            workflow_signals,
            "_accepted_event_tx",
            lambda c, key: None if key == RECOVERY_DEDUPE_KEY else real(c, key),
        )
    else:

        def raise_integrity(*_args, **_kwargs):
            raise sqlite3.IntegrityError("UNIQUE constraint failed: events.dedupe_key")

        monkeypatch.setattr(service, "_append_event_tx", raise_integrity)

    result = _recover_in_process(conn, "second-origin")

    assert result == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert dump_db(conn) == before  # rolled back completely, never `applied`


@pytest.mark.parametrize(
    "target",
    [
        ("workflow_signals", "_claim_tx", 2),  # first claim inserted, second fails
        ("service", "_transition_activity_tx", 1),
        ("service", "_attach_execution_run_tx", 1),
        ("service", "_append_event_tx", 1),
        ("service", "_bump_projection_tx", 1),
    ],
    ids=["second_claim", "activity_transition", "origin_run_attach", "recovery_record", "projection_update"],
)
def test_atomic_idempotent_late_failure_restores_every_row_exactly(state_root, conn, monkeypatch, target):
    module_name, attribute, fail_on_call = target
    build_origin(conn, active_kind="refine")
    add_subagent_run(conn, plain_task(conn, "bystander"))
    before = dump_db(conn)
    module = {"workflow_signals": workflow_signals, "service": service}[module_name]
    real = getattr(module, attribute)
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == fail_on_call:
            raise RuntimeError("injected failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, attribute, flaky)
    with pytest.raises(RuntimeError, match="injected failure"):
        _recover_in_process(conn)

    assert dump_db(conn) == before  # row CONTENTS identical, not merely counts
    monkeypatch.setattr(module, attribute, real)
    again = _recover_in_process(conn)  # and the state is not poisoned
    assert (again["disposition"], again["reason_code"]) == ("applied", "RECOVERED")


PRE_EXISTING_ALLOWLIST = frozenset(
    {
        "reason_code",
        "status",
        "count",
        "ac_id",
        "operation",
        "ref_kind",
        "ref_number",
        "repo",
        "binding_id",
        "task_id",
        "activity_id",
        "execution_run_id",
        "runtime_health",
        "duration_ms",
        "exit_code",
        "run_kind",
        "signal_kind",
        "source",
        "source_schema_version",
        "issue_number",
        "pr_number",
        "approved_body_sha256",
        "merge_commit_oid",
        "merge_identity",
        "transport",
        "target_kind",
        "source_task_id",
        "destination_task_id",
        "decision",
    }
)


def test_atomic_idempotent_allowlist_gains_exactly_the_four_new_keys():
    added = service.ALLOWED_EVENT_METADATA_KEYS - PRE_EXISTING_ALLOWLIST
    assert added == {"prior_activity_id", "prior_activity_kind", "claims_attached", "activity_action"}
    assert PRE_EXISTING_ALLOWLIST <= service.ALLOWED_EVENT_METADATA_KEYS
    assert RECOVERY_KEYS <= service.ALLOWED_EVENT_METADATA_KEYS
    assert set(workflow_signals.RECOVERY_METADATA_KEYS) == RECOVERY_KEYS and len(RECOVERY_KEYS) == 14


def test_atomic_idempotent_no_new_public_signal_kind_or_wire_value():
    assert set(workflow_signals._SIGNAL_SOURCES) == {
        "refinement_approved",
        "implementation_pr_observed",
        "pr_merged_observed",
        "cleanup_completed",
    }
    rejected = workflow_signals.validate_public_signal(
        {
            "signal_kind": "retroactive_claim_recovery",
            "source": "post-merge-cleanup",
            "source_schema_version": "v1",
            "evidence": _evidence(),
        }
    )[1]
    assert rejected == {"disposition": "rejected_envelope", "reason_code": "UNKNOWN_SIGNAL_KIND"}


def test_atomic_idempotent_recovery_journal_record_shape(tmp_path, state_root, conn):
    origin = build_origin(conn, active_kind="refine")
    conn.close()

    result = _recover(tmp_path, state_root)

    (event,) = _recovery_events()
    metadata = json.loads(event["metadata_json"])
    assert set(metadata) == RECOVERY_KEYS  # exactly the 14 closed keys
    assert metadata == {
        "operation": "retroactive_claim_recovery",
        "source": "post-merge-cleanup",
        "source_schema_version": "v1",
        "repo": REPO,
        "issue_number": ISSUE,
        "pr_number": PR,
        "merge_commit_oid": OID,
        "task_id": origin["task"]["id"],
        "execution_run_id": origin["run"]["id"],
        "activity_id": event["activity_id"],
        "prior_activity_id": origin["activity"]["id"],
        "prior_activity_kind": "refine",
        "claims_attached": "issue,pr",
        "activity_action": "transitioned",
    }
    assert event["dedupe_key"] == RECOVERY_DEDUPE_KEY
    assert event["event_type"] == "recovery:implementation_claims" and not event["event_type"].startswith("workflow:")
    assert event["task_id"] == origin["task"]["id"] and event["execution_run_id"] == origin["run"]["id"]
    assert (result["claims_attached"], result["activity_action"]) == ("issue,pr", "transitioned")
    # the projection marker for the origin Binding advanced in the same transaction
    assert read_all(
        "SELECT 1 FROM projection_outbox WHERE projection_key = ?", (f"tab_binding:{origin['binding']['id']}",)
    )
    # recovery never shows up as a workflow phase signal
    assert read_all("SELECT 1 FROM events WHERE event_type LIKE 'workflow:%'") == []


# ===========================================================================
# AC6: explicit-only reachability and non-interference
# ===========================================================================

RECOVERY_SYMBOLS = (
    "recover_implementation_claims",
    "signal_recover",
    "retroactive_claim_recovery",
    "recovery:implementation_claims",
)
ALLOWED_SYMBOL_FILES = {
    "scripts/task-context/task_context_workflow_signals.py",
    "scripts/task-context/task_contextctl.py",
    ".claude/skills/post-merge-cleanup/scripts/task_context_workflow_signal.py",
}


def test_explicit_only_no_implicit_path_imports_or_calls_the_recovery_operation():
    offenders = []
    scanned = 0
    for root in ("scripts", ".claude/hooks", ".claude/skills", "plugins"):
        for path in (REPO_ROOT / root).rglob("*.py"):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if "/tests/" in relative or "__pycache__" in relative or relative in ALLOWED_SYMBOL_FILES:
                continue
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(symbol in text for symbol in RECOVERY_SYMBOLS):
                offenders.append(relative)
    assert scanned > 50  # the scan really covered hooks / hook flows / prompt rebind modules
    assert offenders == []


def test_explicit_only_adapter_recover_phase_is_the_single_runtime_entry():
    adapter = (REPO_ROOT / ".claude/skills/post-merge-cleanup/scripts/task_context_workflow_signal.py").read_text(
        encoding="utf-8"
    )
    assert adapter.count('["signal", "recover"]') == 1
    assert 'if args.phase == "recover":' in adapter


def _ctl(state_root, payload):
    import subprocess
    import sys

    child_env = dict(os.environ)
    child_env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    child_env["CLAUDE_CODE_SESSION_ID"] = SESSION
    request = {
        "schema_version": "task-context-request/v1",
        "operation": "signal_recover",
        "request_id": "t",
        "payload": payload,
    }
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/task-context/task_contextctl.py"), "signal", "recover"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=child_env,
        timeout=60,
    )
    return proc.returncode, json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])


def test_explicit_only_machine_cli_also_refuses_a_request_without_the_explicit_flag(state_root, conn):
    build_origin(conn, active_kind="refine")
    conn.close()
    before = dump_db()

    code, envelope = _ctl(state_root, {**_evidence(), "explicit_recovery": False})
    assert code == 0
    assert envelope["data"] == {"disposition": "rejected_evidence", "reason_code": "EXPLICIT_RECOVERY_REQUIRED"}
    _, envelope = _ctl(state_root, {**_evidence(), "explicit_recovery": "yes"})
    assert envelope["data"]["reason_code"] == "EXPLICIT_RECOVERY_REQUIRED"  # only the JSON boolean true counts
    code, envelope = _ctl(state_root, {**_evidence(), "explicit_recovery": True, "task_id": "task_x"})
    assert envelope["status"] == "error" and code != 0  # no caller-selected Task / identity field
    assert dump_db() == before


@pytest.mark.parametrize("phase", ["merged", "completed"])
def test_explicit_only_normal_phases_never_perform_recovery(tmp_path, state_root, conn, phase):
    build_origin(conn, active_kind="refine")
    claim(conn, plain_task(conn, "someone"), "issue", 55)
    conn.close()
    before = dump_db()
    snapshot = write_snapshot(tmp_path)
    args = (
        merged_args(snapshot)
        if phase == "merged"
        else [*base_args(snapshot, "completed"), "--origin-session-id", SESSION]
    )

    result = run_adapter(args, state_root=state_root)

    assert result["disposition"] == "deferred"
    assert dump_db() == before
    assert read_all("SELECT 1 FROM events WHERE event_type LIKE 'recovery:%'") == []


def test_explicit_only_recovery_does_not_call_github_or_git_and_names_its_own_source(tmp_path, state_root, conn):
    origin = build_origin(conn, active_kind="refine")
    bystander = build_origin(conn, session="bystander", title="bystander", active_kind="refine")
    claim(conn, bystander["task"]["id"], "issue", 77)
    conn.close()
    unrelated_before = rows_of_tasks([bystander["task"]["id"]])
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "forbidden-tool-called"
    for tool in ("gh", "git"):
        script = fake_bin / tool
        script.write_text(f"#!/bin/sh\necho called >> {marker}\nexit 1\n", encoding="utf-8")
        script.chmod(0o755)

    result = run_adapter(
        recover_args(write_snapshot(tmp_path)),
        state_root=state_root,
        extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
    )

    assert result["reason_code"] == "RECOVERED" and result["task_id"] == origin["task"]["id"]
    assert not marker.exists()
    metadata = json.loads(_recovery_events()[0]["metadata_json"])
    assert metadata["source"] == "post-merge-cleanup" and metadata["source"] != "open-pr"
    assert "signal_kind" not in metadata  # not a workflow signal
    assert rows_of_tasks([bystander["task"]["id"]]) == unrelated_before


# ===========================================================================
# AC9: SKILL.md contract (parses the tables; never greps a single phrase)
# ===========================================================================


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def _section(text: str, heading_fragment: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("#") and heading_fragment in line)
    depth = len(lines[start]) - len(lines[start].lstrip("#"))
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("#") and len(lines[j]) - len(lines[j].lstrip("#")) <= depth:
            end = j
            break
    return "\n".join(lines[start:end])


def _table(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        rows.append(cells)
    return rows[1:]  # drop the header row


def _load_adapter():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "post_merge_signal_adapter_issue_2817",
        REPO_ROOT / ".claude/skills/post-merge-cleanup/scripts/task_context_workflow_signal.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PERMITTED_10 = [
    "deferred/IMPLEMENTATION_NOT_READY",
    "conflict/FACT_TASK_IDENTITY_CONFLICT",
    "conflict/OUT_OF_ORDER_SIGNAL",
    "recovery_rejected",
    "unbound/origin_session_missing",
    "unbound/origin_run_not_found",
    "unbound/origin_run_ended",
    "unbound/origin_run_kind_mismatch",
    "unbound/origin_task_unattached",
    "unbound/origin_binding_session_mismatch",
]
UNBOUND_LOCAL_ONLY = [value.split("/", 1)[1] for value in PERMITTED_10 if value.startswith("unbound/")]


def test_skill_contract_three_routes_are_separated_under_their_own_headings():
    text = _skill()
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    for fragment in ("3 本の経路", "通常経路", "復旧経路", "local-only 経路"):
        assert any(fragment in h for h in headings), fragment
    table = _table(_section(text, "3 本の経路"))
    assert [row[0] for row in table] == ["通常経路", "復旧経路", "local-only 経路"]
    assert "`merged` → `completed`" in table[0][1] and "`recover`" in table[1][1] and "`local-only`" in table[2][1]
    assert "一切書かない" in table[2][2]


def test_skill_contract_decision_table_rows_are_all_present_and_routed():
    rows = _table(_section(_skill(), "決定表（`--phase merged`"))
    assert len(rows) == 6 and all(len(r) == 3 for r in rows)

    def row_for(*tokens):
        (found,) = [r for r in rows if all(t in r[0] for t in tokens)]
        return found

    selected = row_for("`selected`", "CLEANUP_STARTED", "CLEANUP_ALREADY_SELECTED")
    assert "通常経路" in selected[1] and "worker dispatch" in selected[1] and "復旧不要" in selected[2]
    stopped = row_for("duplicate_noop/activity_terminal", "late_noop/CLEANUP_ALREADY_BEGUN")
    assert "停止" in stopped[1] and "local-only なし" in stopped[1] and "停止" in stopped[2]
    assert "local-only" not in stopped[2].replace("local-only なし", "")
    not_ready = row_for("deferred/IMPLEMENTATION_NOT_READY")
    assert "local-only" in not_ready[1] and "outcome `deferred/IMPLEMENTATION_NOT_READY`" in not_ready[1]
    for token in ("--phase recover", "1 回", "--phase merged", "recovery_rejected"):
        assert token in not_ready[2], token
    conflicts = row_for("conflict/FACT_TASK_IDENTITY_CONFLICT", "conflict/OUT_OF_ORDER_SIGNAL")
    assert "local-only" in conflicts[1] and "--phase recover" in conflicts[2] and "recovery_rejected" in conflicts[2]
    unbound = row_for("deferred/unbound")
    assert "diagnose-origin" in unbound[1] and "reject" in unbound[2]
    invalid = row_for(
        "RELATION_UNAVAILABLE",
        "MERGED_SNAPSHOT_INVALID",
        "RELATION_ISSUE_MISMATCH",
        "MERGE_OID_INVALID",
        "ADAPTER_UNAVAILABLE",
    )
    assert all(t in invalid[1] for t in ("local-only なし", "削除なし", "bounded retry"))
    assert "recover も呼ばない" in invalid[2]


def test_skill_contract_diagnose_origin_result_space_is_fully_enumerated():
    rows = _table(_section(_skill(), "diagnose-origin 結果表"))
    keys = ["resolved: true", "ADAPTER_UNAVAILABLE", "origin_ambiguous", *UNBOUND_LOCAL_ONLY]
    assert len(rows) == 9
    by_key = {}
    for key in keys:
        (matching,) = [r for r in rows if key in r[0]]
        by_key[key] = matching[1]
    assert "local-only に入らない" in by_key["resolved: true"] and "--phase merged" in by_key["resolved: true"]
    assert "local-only に入らない" in by_key["ADAPTER_UNAVAILABLE"] and "bounded retry" in by_key["ADAPTER_UNAVAILABLE"]
    assert "human_review_required: true" in by_key["origin_ambiguous"]
    assert "local-only に入らない" in by_key["origin_ambiguous"]
    for reason in UNBOUND_LOCAL_ONLY:
        assert f"local-only（outcome `unbound/{reason}`）" in by_key[reason], reason
    assert "/task <target>" in by_key["origin_run_not_found"]


def test_skill_contract_existing_cause_table_is_kept_as_guidance_and_composed_with_local_only():
    text = _skill()
    cause_rows = _table(_section(text, "`unbound` の原因別フォールバック"))
    assert [r[0] for r in cause_rows] == [
        f"`{c}`"
        for c in (
            "origin_session_missing",
            "origin_run_not_found",
            "origin_run_ended",
            "origin_run_kind_mismatch",
            "origin_task_unattached",
            "origin_binding_session_mismatch",
            "origin_ambiguous",
        )
    ]
    assert "診断・報告の指針として維持し、local-only は" in text
    assert "local-only に入らない" in cause_rows[-1][2]


def test_skill_contract_local_only_permitted_set_matches_section_6_and_the_adapter_enum():
    text = _skill()
    section = _section(text, "local-only 許可集合")
    bullets = [re.match(r"- `([^`]+)`", line).group(1) for line in section.splitlines() if line.startswith("- `")]
    adapter = _load_adapter()
    assert bullets == PERMITTED_10
    assert len(set(bullets)) == 10
    assert list(adapter.LOCAL_ONLY_PERMITTED_OUTCOMES) == PERMITTED_10
    assert "10 値" in section and "9 値" not in text
    refused = section.split("許可集合外（`refused` になる例）", 1)[1].split("。", 1)[0]
    for example in (
        "selected",
        "duplicate_noop/activity_terminal",
        "late_noop/CLEANUP_ALREADY_BEGUN",
        "unbound/origin_ambiguous",
        "unbound/resolved",
        "adapter_unavailable",
        "未知の値",
    ):
        assert example in refused, example
    assert "LOCAL_ONLY_NOT_PERMITTED" in section
    assert "caller 申告値" in section and "security boundary ではない" in section


def test_skill_contract_recovery_rejected_is_limited_to_recover_conflicts():
    text = _skill()
    assert "`recovery_rejected` は `--phase recover` が `conflict/*` を返した場合だけを指す" in text
    tail = text.split("`recovery_rejected` は `--phase recover` が `conflict/*`", 1)[1].split("\n\n", 1)[0]
    for stop_class in ("deferred/*", "rejected_evidence/*", "ADAPTER_UNAVAILABLE", "停止"):
        assert stop_class in tail, stop_class
    assert "`selected` でない場合" in tail and "その結果を outcome として" in tail


def test_skill_contract_fresh_snapshot_rule_and_merge_identity_are_stated():
    section = _section(_skill(), "fresh snapshot 規則")
    for token in (
        "300 秒",
        "SNAPSHOT_STALE",
        "前回 invocation の snapshot の再利用は禁止",
        "mtime",
        "MERGE_IDENTITY_MISMATCH",
        "40 hex",
        "closingIssuesReferences",
    ):
        assert token in section, token
    assert "`--phase recover`" in section and "`--phase local-only`" in section


def test_skill_contract_dispatch_condition_is_two_way_and_nothing_else_dispatches():
    text = _skill()
    paragraph = next(p for p in text.split("\n\n") if "dispatch 条件は 2 本立て" in p)
    assert "(a) 通常経路 = cleanup begin が `selected`" in paragraph
    assert "(b) local-only 経路 = `--phase local-only` が `LOCAL_ONLY_PERMITTED`" in paragraph
    assert "いずれでもなければ" in paragraph and "dispatch しない" in paragraph
    # the legacy unconditional stop rule is scoped to the normal route, not left contradicting local-only
    assert "dispatch せずこの invocation を停止する。最終 cleanup work" not in text
    assert "通常経路としては dispatch せず" in text
    assert "Task Context の cleanup Activity 経路（通常経路）に限定した規約" in text


def test_skill_contract_local_only_section_rules_and_step3_suppression():
    section = _section(_skill(), "local-only 経路: `--phase local-only`")
    code_blocks = re.findall(r"```bash\n(.*?)```", section, flags=re.S)
    assert len(code_blocks) == 1 and "--phase local-only" in code_blocks[0]
    assert "--phase completed" not in code_blocks[0]
    for flag in ("--merge-identity", "--task-context-outcome", "--worktree-path", "--branch-name"):
        assert flag in code_blocks[0], flag
    bullets = " ".join(section.split("### local-only の結果と dispatch 後の規則", 1)[1].splitlines())
    assert "`--phase completed` を呼ばない" in bullets
    assert "`gh issue close`" in bullets and "`gh pr close` / `gh pr comment`" in bullets
    assert "実行せず、候補として報告するだけ" in bullets and "follow-up 起票" in bullets
    assert "従来どおり実行してよい" in bullets
    assert "ローカル cleanup 成功 / Task Context 未記録" in bullets
    for authority in ("cleanup_completed", "parent_issue_close", "superseded_pr_close"):
        assert authority in bullets
    assert "cleanup_exec_argv` は出さない" in bullets and "verbatim" in bullets
    for fld in ("repo", "issue_number", "pr_number", "merge_commit_oid", "worktree_path", "branch_name"):
        assert f"`{fld}`" in bullets, fld
    assert "scripts/agent-ops/cleanup_exec.py" in bullets and "rm -rf" in bullets
    assert "ACTIVE のまま残る" in bullets and "owner Task id" in bullets
    delegation = _section(_skill(), "Delegation / 委譲")
    assert "local-only 経路では `parent_issue_status` による `gh issue close`" in delegation
    assert "verbatim" in delegation and "cleanup_exec" in delegation
    guard = _section(_skill(), "Guardrails")
    assert "local-only 経路では `--phase completed` を呼ばず" in guard


def test_skill_contract_recover_path_is_explicit_and_never_recommends_the_double_task_workaround():
    text = _skill()
    section = _section(text, "復旧経路: `--phase recover`")
    block = re.findall(r"```bash\n(.*?)```", section, flags=re.S)[0]
    for flag in ("--phase recover", "--merge-identity", "--explicit-recovery", "--origin-session-id"):
        assert flag in block, flag
    for token in (
        "EXPLICIT_RECOVERY_REQUIRED",
        "MISSING_REQUIRED_ARGUMENT",
        "ambient env へ fallback しない",
        "推測して attach しない",
        "1 回だけ",
    ):
        assert token in section, token
    assert "Issue と PR の両方で `/task` を実行することを回避策として案内しない" in section
    for line in text.splitlines():  # nowhere else is the double run offered as a recovery procedure
        if "/task pr" in line:
            assert "案内しない" in line or "使わない" in line


def test_skill_contract_frozen_wire_is_distinguished_from_the_changed_orchestration_policy():
    text = _skill()
    paragraph = next(p for p in text.split("\n\n") if "凍結 wire と今回変更する orchestration policy の区別" in p)
    for token in (
        "signal_kind",
        "凍結され",
        "変更しない",
        "cleanup_completed",
        "additive",
        "--phase recover",
        "--phase local-only",
    ):
        assert token in paragraph, token


def test_skill_contract_runtime_prompt_fixture_is_read_only_and_declares_the_ordered_markers():
    fixture = (REPO_ROOT / "tests/task-context/fixtures/post_merge_local_only_runtime_prompt.md").read_text(
        encoding="utf-8"
    )
    positions = [fixture.index(f"POST_MERGE_ROUTE_{x}=") for x in "ABC"]
    assert positions == sorted(positions)
    for marker in (
        "POST_MERGE_ROUTE_A=normal_dispatch",
        "POST_MERGE_ROUTE_B=local_only",
        "POST_MERGE_ROUTE_C=stop_human_review",
    ):
        assert marker in fixture
    for token in (
        ".claude/skills/post-merge-cleanup/SKILL.md",
        "`Explore`",
        "ちょうど 1 件",
        "read-only",
        "origin_ambiguous",
        "deferred/IMPLEMENTATION_NOT_READY",
    ):
        assert token in fixture, token
    assert "post-merge-cleanup-worker` の起動は一切行いません" in fixture


# ===========================================================================
# AC10: docs/dev/task-context.md contract
# ===========================================================================

DOCS_SECTION = "遡及 claim 復旧経路と local-only cleanup 経路（Issue #2817）"


def test_docs_contract_recovery_and_local_only_sections_are_documented():
    text = DOCS.read_text(encoding="utf-8")
    top = _section(text, DOCS_SECTION)
    needed = {
        "復旧経路 `--phase recover` と安全条件": [
            "--explicit-recovery",
            "SNAPSHOT_STALE",
            "300 秒",
            "MERGE_IDENTITY_MISMATCH",
            "BEGIN IMMEDIATE",
            "closingIssuesReferences",
            "暗黙",
            "推測",
        ],
        "claim ownership matrix": [
            "FACT_TASK_IDENTITY_CONFLICT",
            "OUT_OF_ORDER_SIGNAL",
            "released",
            "判定順",
            "partial repair",
            "issue_claim_owner_task_id",
            "pr_claim_owner_task_id",
            "origin_other_issue_number",
            "origin_other_pr_number",
        ],
        "implementation Activity 状態表": [
            "MERGE_FACT_ALREADY_ACCEPTED",
            "IMPLEMENTATION_ACTIVITY_TERMINAL",
            "ACTIVITY_KIND_NOT_RECOVERABLE",
            "ACTIVITY_STATE_INCONSISTENT",
            "decide_activity_action",
            "18 セル",
            "11 組",
            "ux_activities_active_per_task",
        ],
        "merge signal → recovery / local-only 決定表": [
            "deferred/IMPLEMENTATION_NOT_READY",
            "recovery_rejected",
            "deferred/unbound",
            "diagnose-origin",
            "ADAPTER_UNAVAILABLE",
        ],
        "local-only 経路の位置付けと残余状態": [
            "LOCAL_ONLY_NOT_PERMITTED",
            "caller 申告値",
            "security boundary",
            "ACTIVE のまま残る",
            "人間が owner Task を解決",
            "cleanup_exec_argv",
            "10 値",
            "cleanup_completed",
        ],
        "TOCTOU と both-missing の残余リスク": ["TOCTOU", "both-missing", "merge_commit_oid", "受容"],
        "recovery journal（14 key）と allowlist 追加": [
            "recovery:implementation_claims",
            "task-context-v1:retroactive_claim_recovery:{repo}:{pr_number}:{merge_commit_oid}",
            "ALLOWED_EVENT_METADATA_KEYS",
            *sorted(RECOVERY_KEYS),
        ],
        "`/task <issue>` bootstrap 案の不採用理由": ["PR claim", "分裂", "#2856"],
        "#2825 との責務分担": ["#2825", "emit_implementation_pr_observed", "open_pr.py"],
    }
    for heading, tokens in needed.items():
        body = _section(top, heading)
        for token in tokens:
            assert token in body, (heading, token)
    local_only = _section(top, "local-only 経路の位置付けと残余状態")
    for outcome in PERMITTED_10:  # the 10 permitted outcomes are documented verbatim (no 9-value drift)
        assert f"`{outcome}`" in local_only, outcome
    assert "9 値" not in text.split(DOCS_SECTION, 1)[1]


def test_docs_contract_documented_tables_agree_with_the_implementation():
    top = _section(DOCS.read_text(encoding="utf-8"), DOCS_SECTION)
    assert len(_table(_section(top, "implementation Activity 状態表"))) == 5  # decision priority 1..5
    assert [row[0] for row in _table(_section(top, "claim ownership matrix"))] == ["1", "2", "3", "4"]
    journal = _section(top, "recovery journal（14 key）と allowlist 追加")
    for key in workflow_signals.RECOVERY_METADATA_KEYS:
        assert f"`{key}`" in journal, key
    for key in sorted(service.ALLOWED_EVENT_METADATA_KEYS - PRE_EXISTING_ALLOWLIST):
        assert f"`{key}`" in journal, key

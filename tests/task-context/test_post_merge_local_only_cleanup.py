"""Issue #2817 AC7: ``--phase local-only`` of the post-merge-cleanup adapter.

The adapter is run as a REAL subprocess against a real temporary Task Context
DB. The mechanical oracle is the adapter's closed-enum gate; the outcome is a
caller-declared value (a fail-closed local guardrail, not a security
boundary), so these tests pin the gate, the closed result keys and the
"writes nothing" property rather than any claim about outcome truthfulness.
"""

from __future__ import annotations

import json

import pytest

from retroactive_claim_support import (
    ISSUE,
    OID,
    PR,
    REPO,
    SESSION,
    base_args,
    build_origin,
    claim,
    cleanup_report,
    dump_db,
    local_only_args,
    merged_args,
    merged_snapshot,
    read_all,
    run_adapter,
    write_snapshot,
)

WORKTREE = ".claude/worktrees/issue-20-demo"
BRANCH = "worktree-issue-20-demo"

PERMITTED = [
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
REFUSED = [
    "selected",
    "duplicate_noop/activity_terminal",
    "late_noop/CLEANUP_ALREADY_BEGUN",
    "unbound/origin_ambiguous",
    "unbound/resolved",
    "adapter_unavailable",
    "unbound/<reason>",
    "unbound",
    "unbound/",
    "Deferred/IMPLEMENTATION_NOT_READY",
    "deferred/IMPLEMENTATION_NOT_READY ",
    "recovery_rejected,selected",
    "totally-unknown-value",
]
CLOSED_KEYS = {
    "disposition",
    "reason_code",
    "task_context",
    "authority",
    "repo",
    "issue_number",
    "pr_number",
    "merge_commit_oid",
    "worktree_path",
    "branch_name",
}
REFUSAL = {"disposition": "refused", "reason_code": "LOCAL_ONLY_NOT_PERMITTED"}


def _populated_state(conn):
    """A realistic Task Context with a half-recorded implementation, so a
    stray write by the adapter would be visible."""
    origin = build_origin(conn, active_kind="refine")
    claim(conn, origin["task"]["id"], "issue", ISSUE)
    return origin


def _local_only(tmp_path, state_root, outcome, **overrides):
    return run_adapter(
        local_only_args(write_snapshot(tmp_path), outcome=outcome, worktree=WORKTREE, branch=BRANCH, **overrides),
        state_root=state_root,
    )


def test_the_permitted_set_in_this_file_is_exactly_ten_values():
    assert len(PERMITTED) == 10 and len(set(PERMITTED)) == 10
    assert set(PERMITTED).isdisjoint(REFUSED)


@pytest.mark.parametrize("outcome", PERMITTED)
def test_permitted_outcome_returns_exactly_the_closed_result_and_writes_nothing(tmp_path, state_root, conn, outcome):
    _populated_state(conn)
    conn.close()
    before = dump_db()

    result = _local_only(tmp_path, state_root, outcome)

    assert set(result) == CLOSED_KEYS
    assert result == {
        "disposition": "local_only",
        "reason_code": "LOCAL_ONLY_PERMITTED",
        "task_context": "unrecorded",
        "authority": {"cleanup_completed": False, "parent_issue_close": False, "superseded_pr_close": False},
        "repo": REPO,
        "issue_number": ISSUE,
        "pr_number": PR,
        "merge_commit_oid": OID,
        "worktree_path": WORKTREE,
        "branch_name": BRANCH,
    }
    assert "cleanup_exec_argv" not in json.dumps(result)
    assert all(value is False for value in result["authority"].values())
    assert dump_db() == before  # every row of every table is identical
    assert (
        read_all("SELECT 1 FROM events WHERE event_type IN ('workflow:cleanup_started', 'workflow:cleanup_completed')")
        == []
    )


def test_permitted_outcome_never_touches_task_context_at_all(tmp_path):
    never_created = tmp_path / "state-root-that-must-stay-absent"

    result = run_adapter(
        local_only_args(write_snapshot(tmp_path), outcome=PERMITTED[0], worktree=WORKTREE, branch=BRANCH),
        state_root=never_created,
        ambient_session=None,
    )

    assert result["disposition"] == "local_only"
    assert not never_created.exists()  # no DB, no state root, no ctl invocation side effect


@pytest.mark.parametrize("outcome", REFUSED)
def test_refused_outcome_prints_only_the_refusal_and_writes_nothing(tmp_path, state_root, conn, outcome):
    _populated_state(conn)
    conn.close()
    before = dump_db()

    result = _local_only(tmp_path, state_root, outcome)

    assert result == REFUSAL  # no argv, no authority, no fields
    assert dump_db() == before


@pytest.mark.parametrize(
    "build,overrides,expected",
    [
        (
            lambda: merged_snapshot(merged=False),
            {},
            {"disposition": "deferred", "reason_code": "MERGED_SNAPSHOT_INVALID"},
        ),
        (
            lambda: merged_snapshot(errors=[{"message": "partial"}]),
            {},
            {"disposition": "deferred", "reason_code": "RELATION_UNAVAILABLE"},
        ),
        (lambda: merged_snapshot(nodes=[]), {}, {"disposition": "deferred", "reason_code": "RELATION_ISSUE_MISMATCH"}),
        (
            lambda: merged_snapshot(),
            {"merge_identity": "c" * 40},
            {"disposition": "rejected_evidence", "reason_code": "MERGE_IDENTITY_MISMATCH"},
        ),
    ],
    ids=["unmerged", "partial_graphql_errors", "no_closing_reference", "merge_identity_mismatch"],
)
def test_invalid_snapshot_or_identity_never_reaches_a_permit(tmp_path, state_root, conn, build, overrides, expected):
    _populated_state(conn)
    conn.close()
    before = dump_db()
    snapshot = write_snapshot(tmp_path, build())

    for outcome in (PERMITTED[0], REFUSED[0]):  # permitted or not, the snapshot gate comes first
        result = run_adapter(
            local_only_args(snapshot, outcome=outcome, worktree=WORKTREE, branch=BRANCH, **overrides),
            state_root=state_root,
        )
        assert result == expected
        assert "authority" not in result and "cleanup_exec_argv" not in result
    assert dump_db() == before


def test_stale_snapshot_is_deferred_even_for_a_permitted_outcome(tmp_path, state_root, conn):
    _populated_state(conn)
    conn.close()
    before = dump_db()
    snapshot = write_snapshot(tmp_path, age_seconds=301)

    result = run_adapter(
        local_only_args(snapshot, outcome=PERMITTED[0], worktree=WORKTREE, branch=BRANCH), state_root=state_root
    )

    assert result == {"disposition": "deferred", "reason_code": "SNAPSHOT_STALE"}
    assert dump_db() == before


def test_a_snapshot_inside_the_freshness_window_is_accepted(tmp_path, state_root):
    snapshot = write_snapshot(tmp_path, age_seconds=250)

    result = run_adapter(
        local_only_args(snapshot, outcome=PERMITTED[0], worktree=WORKTREE, branch=BRANCH), state_root=state_root
    )

    assert result["disposition"] == "local_only"


@pytest.mark.parametrize(
    "overrides",
    [
        {"outcome": None},
        {"outcome": ""},
        {"merge_identity": None},
        {"merge_identity": ""},
        {"worktree": None},
        {"worktree": ""},
        {"worktree": "   "},
        {"branch": None},
        {"branch": ""},
        {"branch": "  "},
        {"outcome": REFUSED[0], "branch": None},  # a refused outcome does not mask a missing argument
    ],
    ids=[
        "no_outcome",
        "empty_outcome",
        "no_merge_identity",
        "empty_merge_identity",
        "no_worktree",
        "empty_worktree",
        "blank_worktree",
        "no_branch",
        "empty_branch",
        "blank_branch",
        "refused_outcome_and_no_branch",
    ],
)
def test_missing_required_argument_is_rejected_without_output_of_fields(tmp_path, state_root, conn, overrides):
    _populated_state(conn)
    conn.close()
    before = dump_db()
    args = {"outcome": PERMITTED[0], "worktree": WORKTREE, "branch": BRANCH, **overrides}

    result = run_adapter(local_only_args(write_snapshot(tmp_path), **args), state_root=state_root)

    assert result == {"disposition": "rejected_evidence", "reason_code": "MISSING_REQUIRED_ARGUMENT"}
    assert dump_db() == before


def test_local_only_does_not_complete_a_cleanup_lifecycle_and_completion_still_needs_a_final_success_receipt(
    tmp_path, state_root, conn
):
    """Local-only is not a lifecycle: a Task whose cleanup was selected stays
    exactly as it was, and `cleanup_completed` is applied only through the
    unchanged `--phase completed` contract (correct instance + final-success
    receipt), never because a local-only result exists."""
    origin = build_origin(conn, active_kind="implementation")
    claim(conn, origin["task"]["id"], "issue", ISSUE)
    claim(conn, origin["task"]["id"], "pr", PR)
    conn.close()
    snapshot = write_snapshot(tmp_path)
    selected = run_adapter(merged_args(snapshot), state_root=state_root)
    assert (selected["disposition"], selected["reason_code"]) == ("selected", "CLEANUP_STARTED")
    before = dump_db()

    permit = run_adapter(
        local_only_args(snapshot, outcome=PERMITTED[0], worktree=WORKTREE, branch=BRANCH), state_root=state_root
    )
    assert permit["disposition"] == "local_only" and permit["authority"]["cleanup_completed"] is False
    assert dump_db() == before

    completed_args = [*base_args(snapshot, "completed"), "--origin-session-id", SESSION]
    assert run_adapter(completed_args, state_root=state_root) == {
        "disposition": "deferred",
        "reason_code": "CLEANUP_FINAL_SUCCESS_RECEIPT_REQUIRED",
    }
    for index, report in enumerate(
        (cleanup_report(status="partial"), cleanup_report(status="failed"), cleanup_report(human_review_required=True))
    ):
        receipt = tmp_path / f"receipt-{index}.json"
        receipt.write_text(json.dumps(report), encoding="utf-8")
        result = run_adapter([*completed_args, "--cleanup-receipt-file", str(receipt)], state_root=state_root)
        assert result["disposition"] == "deferred", result
    assert dump_db() == before  # still nothing completed

    final = tmp_path / "receipt-final.json"
    final.write_text(json.dumps(cleanup_report()), encoding="utf-8")
    applied = run_adapter([*completed_args, "--cleanup-receipt-file", str(final)], state_root=state_root)
    assert applied["disposition"] == "applied"
    assert len(read_all("SELECT 1 FROM events WHERE event_type = 'workflow:cleanup_completed'")) == 1

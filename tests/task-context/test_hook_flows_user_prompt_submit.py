"""Issue #2625 AC1, AC2, AC5, AC7, AC9 (supersedes Issue #2564 AC4/AC6/AC12's
hard-block contract) -- UserPromptSubmit precedence: observe-only, autobind,
provisional/absorbent Task, different-primary-target ADVISORY (never a
block), terminal-Activity advance/rebind, raw `/task`-looking text as a
non-mutating no-op (state-changing authority moved to `UserPromptExpansion`
-- see test_hook_flows_user_prompt_expansion.py), and order-independent
commit-on-submission semantics."""

from __future__ import annotations

import json

import task_context_hook_flows as hook_flows
import task_context_service as service


def _start_session(tab_id: str, session_id: str, *, source: str = "startup") -> str:
    result = hook_flows.on_session_start(
        conn_holder["conn"], {"source": source, "herdr_tab_id": tab_id, "claude_session_id": session_id}
    )
    return result["binding_id"]


conn_holder = {}


def _submit(session_id: str, tab_id: str, **fields):
    payload = {"herdr_tab_id": tab_id, "claude_session_id": session_id, **fields}
    return hook_flows.on_user_prompt_submit(conn_holder["conn"], payload)


def _expand(session_id: str, tab_id: str, **fields):
    """AC6: `/task` state-changing authority now lives in the
    `UserPromptExpansion` command lifecycle (`command_name == "task"`), not
    `UserPromptSubmit` -- test setup that needs an ad-hoc Task must go
    through this instead of `_submit(..., classification_kind="SLASH_TASK")`."""
    payload = {"herdr_tab_id": tab_id, "claude_session_id": session_id, "command_name": "task", **fields}
    return hook_flows.on_user_prompt_expansion(conn_holder["conn"], payload)


def setup_function(_fn):
    conn_holder.clear()


def test_given_non_herdr_when_prompt_submitted_then_observe_only(conn):
    conn_holder["conn"] = conn
    result = _submit("s1", None, classification_kind="EXPLICIT")
    assert result["decision"] == "pass"
    assert result["reason_code"] == "observe_only_non_herdr"


def test_given_unbound_tab_with_explicit_target_when_prompt_submitted_then_autobind(conn):
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=100,
    )
    assert result["decision"] == "pass"
    assert result["reason_code"] == "autobind"
    task_id = result["task_id"]
    live_claim = service.find_live_claim(conn, "owner/repo", "issue", 100)
    assert live_claim["task_id"] == task_id


def test_given_no_ref_and_no_current_task_when_prompt_submitted_then_pass_no_mutation(conn):
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _submit("s1", "tab-1", classification_kind="NONE")
    assert result["decision"] == "pass"
    assert result["reason_code"] == "reference_only_or_none"


def test_given_ambiguous_classification_when_prompt_submitted_then_pass_with_advisory_not_blocked(conn):
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    result = _submit("s1", "tab-1", classification_kind="AMBIGUOUS")
    assert result["decision"] == "pass"
    assert result["advisory"] is True


def test_given_active_task_with_zero_refs_when_new_primary_target_submitted_then_absorbed_not_blocked(conn):
    """AC4 (Issue #2564, unchanged by #2625): provisional/absorbent ad-hoc
    Task (task_refs == 0) absorbs the first high-confidence primary GitHub
    target instead of being treated as a different-Task rebind."""
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    # No ref yet -- create an ad-hoc provisional Task via the UserPromptExpansion
    # `/task` command lifecycle (AC6) with a label.
    slash_result = _expand("s1", "tab-1", slash_task_ad_hoc_title="ad-hoc work")
    task_id = slash_result["task_id"]
    assert service.count_live_task_ref_claims(conn, task_id) == 0

    absorb_result = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=42,
    )
    assert absorb_result["decision"] == "pass"
    assert absorb_result["reason_code"] == "provisional_absorb"
    assert absorb_result["task_id"] == task_id
    assert service.count_live_task_ref_claims(conn, task_id) == 1


def test_given_active_task_with_a_live_ref_when_different_primary_target_submitted_then_advisory_not_blocked(conn):
    """Issue #2625 AC1/AC2 (supersedes Issue #2564 AC4's hard block): once a
    Task has >=1 live ref, a *different* high-confidence primary target
    while the Activity is still ACTIVE is advisory-only -- Claude prompt
    processing continues (decision: pass), and current Task/Activity/
    Binding are left completely unchanged (no silent rebind, no claim)."""
    conn_holder["conn"] = conn
    binding_id = _start_session("tab-1", "s1")
    first = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    task_a = first["task_id"]

    result = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/other", target_ref_kind="issue",
        target_ref_number=2,
    )
    assert result["decision"] == "pass"
    assert result["reason_code"] == "different_primary_target_active"
    assert result["advisory"] is True

    # AC1: Task A's Binding/Activity/claims are unchanged -- no silent
    # rebind to the mismatched target B, no claim created for it either.
    current_task_id, current_activity_id, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert current_task_id == task_a
    assert current_activity_id == first["activity_id"]
    assert service.find_live_claim(conn, "owner/other", "issue", 2) is None


def test_given_different_primary_target_advisory_when_recorded_then_event_journal_status_is_non_blocking(conn):
    """AC2: the different_primary_target_active advisory is *required* to be
    recorded to EventJournal, but as a non-blocking ``pass`` observation --
    never the hard-block ``status="block"`` state Issue #2564 used."""
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/other", target_ref_kind="issue",
        target_ref_number=2,
    )
    rows = conn.execute(
        "SELECT metadata_json FROM events WHERE event_type = 'hook:UserPromptSubmit' ORDER BY occurred_at"
    ).fetchall()
    metadatas = [json.loads(row["metadata_json"]) for row in rows]
    mismatch_events = [m for m in metadatas if m.get("reason_code") == "different_primary_target_active"]
    assert mismatch_events, "different_primary_target_active advisory が EventJournal に記録されていません"
    assert mismatch_events[-1]["status"] == "pass", (
        f"advisory の EventJournal status は non-blocking 'pass' である想定: "
        f"{mismatch_events[-1]['status']!r}"
    )


def test_given_raw_slash_task_text_on_ordinary_submit_when_processed_then_no_state_mutation(conn):
    """AC6: ordinary UserPromptSubmit no longer treats raw `/task ...`-looking
    prompt text as a state-changing authority signal -- classification_kind
    == SLASH_TASK observed here performs no Task/Activity/Binding mutation
    at all (the sole authority is UserPromptExpansion's command_name ==
    "task" lifecycle -- see test_hook_flows_user_prompt_expansion.py)."""
    conn_holder["conn"] = conn
    binding_id = _start_session("tab-1", "s1")
    first = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    task_a = first["task_id"]

    result = _submit("s1", "tab-1", classification_kind="SLASH_TASK", slash_task_target_repo="owner/other",
                      slash_task_target_ref_kind="issue", slash_task_target_ref_number=99)
    assert result["decision"] == "pass"
    assert result["reason_code"] == "slash_task_raw_text_no_state_authority"

    current_task_id, _, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert current_task_id == task_a
    assert service.find_live_claim(conn, "owner/other", "issue", 99) is None


def test_given_same_target_as_current_task_when_prompt_submitted_then_passed_through(conn):
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    result = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    assert result["decision"] == "pass"
    assert result["reason_code"] == "same_target"


def test_given_terminal_activity_when_different_target_submitted_then_advance_allowed_no_slash_task_needed(conn):
    """AC5: a terminal (non-ACTIVE) current Activity allows a legal
    same-Task advance / different-Task rebind via a normal prompt."""
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    first = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    task_a = first["task_id"]
    # Terminate the current Activity out-of-band (as `transition_activity`
    # to a new kind would in a real completion flow).
    service.transition_activity(conn, task_a, kind="done-marker")

    result = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/other", target_ref_kind="issue",
        target_ref_number=2,
    )
    assert result["decision"] == "pass"
    assert result["reason_code"] == "terminal_advance_or_rebind"
    assert result["task_id"] != task_a


def test_given_reference_only_marker_when_active_task_present_then_never_blocks(conn):
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    result = _submit("s1", "tab-1", classification_kind="REFERENCE_ONLY")
    assert result["decision"] == "pass"


def test_given_no_binding_for_session_when_prompt_submitted_then_fail_open_pass(conn):
    conn_holder["conn"] = conn
    result = _submit("unknown-session", "tab-1", classification_kind="EXPLICIT")
    assert result["decision"] == "pass"
    assert result["reason_code"] == "no_binding_for_session"


def test_given_commit_on_submission_when_sibling_hook_blocks_later_then_no_rollback_carryover(conn):
    """AC13: this adapter's own Task/Activity mutation (autobind here) is
    committed once its own transaction succeeds -- it never depends on, or
    is rolled back by, another hook's later decision for the same
    UserPromptSubmit event. We simulate that by asserting the mutation is
    durably visible via a fresh read immediately after, independent of any
    other in-process state."""
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=7,
    )
    task_id = result["task_id"]
    # A completely independent read (as a différent sibling hook observing
    # "final" state after this hook's own commit would see) must already
    # reflect the committed mutation.
    reread = service.get_task(conn, task_id)
    assert reread["id"] == task_id
    assert service.find_live_claim(conn, "owner/repo", "issue", 7)["task_id"] == task_id

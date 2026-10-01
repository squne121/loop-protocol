"""Issue #2827 (AC5, AC13): workflow connectivity after an ordinary-prompt
auto-rebind. The rebind itself is driven through the real adapter + core; the
workflow signal is the unchanged #2565 service. No Activity is injected by
the fixture -- the ``refine`` Activity that lets ``refinement_approved`` apply
must come from the auto-rebind itself."""

from __future__ import annotations

import pytest

import hook_entry
import task_context_hook_flows as hook_flows
import task_context_service as service
import task_context_workflow_signals as signals
from workflow_signal_test_support import REPO, SHA64

SESSION = "session-1"
ISSUE_A = 10
ISSUE_B = 20


@pytest.fixture(autouse=True)
def _fixed_current_repo(monkeypatch):
    monkeypatch.setattr(hook_entry, "_current_repo", lambda *_a, **_k: REPO)


def _prompt(conn, prompt):
    payload = {"herdr_tab_id": "tab-1", "claude_session_id": SESSION}
    hook_entry._apply_user_prompt_submit_fields(
        payload,
        {"hook_event_name": "UserPromptSubmit", "prompt_id": "prompt-1", "prompt": prompt, "cwd": "/unused"},
    )
    return hook_flows.on_user_prompt_submit(conn, payload)


def _approved(issue_number, sha=SHA64):
    return {
        "signal_kind": "refinement_approved",
        "source": "issue-refinement-loop",
        "source_schema_version": "v1",
        "evidence": {"repo": REPO, "issue_number": issue_number, "approved_body_sha256": sha},
    }


def _rebound_session(conn):
    """Session bound to Issue A by a prompt, then switched to Issue B by an ordinary prompt."""
    binding_id = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": SESSION}
    )["binding_id"]
    first = _prompt(conn, f"Issue #{ISSUE_A} を対象に作業開始")
    assert first["reason_code"] == "autobind"
    second = _prompt(conn, f"Issue #{ISSUE_B} を対象にレビューして")
    assert second["reason_code"] == "user_prompt_primary_target_rebind"
    return binding_id, first["task_id"], second["task_id"]


def _activities(conn, task_id):
    return [
        (r["kind"], r["status"])
        for r in conn.execute("SELECT * FROM activities WHERE task_id = ? ORDER BY rowid", (task_id,))
    ]


def test_first_refinement_approved_applies_after_prompt_rebind_without_injected_activity(conn):
    binding_id, task_a, task_b = _rebound_session(conn)
    assert _activities(conn, task_b) == [("refine", "ACTIVE")], "the auto-rebind alone must start refine"

    result = signals.apply_workflow_signal(conn, _approved(ISSUE_B), origin_session_id=SESSION)

    assert result["disposition"] == "applied"
    assert result["reason_code"] == "APPLIED"
    assert result["task_id"] == task_b
    # B owns the accepted event; refine is done, implementation started, the origin run follows.
    accepted = conn.execute(
        "SELECT * FROM events WHERE event_type LIKE 'workflow:%' AND task_id = ?", (task_b,)
    ).fetchall()
    assert accepted, "an accepted workflow event must be recorded on Task B"
    assert _activities(conn, task_b) == [("refine", "DONE"), ("implementation", "ACTIVE")]
    current_task, current_activity, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert current_task == task_b
    assert service.get_activity(conn, current_activity)["kind"] == "implementation"
    # Task A was never touched by B's approval.
    assert not conn.execute(
        "SELECT 1 FROM events WHERE event_type LIKE 'workflow:%' AND task_id = ?", (task_a,)
    ).fetchone()


def test_stale_origin_a_signal_after_switch_never_applies_to_b(conn):
    binding_id, task_a, task_b = _rebound_session(conn)
    before = {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("activities", "task_ref_claims", "tasks")
    }

    # A late approval for Issue A, sent by the same origin session after the switch to B.
    result = signals.apply_workflow_signal(conn, _approved(ISSUE_A, "c" * 64), origin_session_id=SESSION)

    assert result["disposition"] == "conflict"
    assert result["reason_code"] == "FACT_TASK_IDENTITY_CONFLICT"
    after = {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("activities", "task_ref_claims", "tasks")
    }
    assert after == before, "a stale A signal must not mutate anything on B"
    assert _activities(conn, task_b) == [("refine", "ACTIVE")]
    assert service.find_live_claim(conn, REPO, "issue", ISSUE_A)["task_id"] == task_a

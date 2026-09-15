"""Issue #2625 AC6 -- `/task <target>` state-changing authority lives
exclusively in the `UserPromptExpansion` command lifecycle
(`command_name == "task"`), atomically superseding whatever Task/Activity is
currently ACTIVE, with explicit command-failure semantics for invalid /
unavailable / missing targets. Any other `command_name` is always a
non-mutating no-op pass-through."""

from __future__ import annotations

import json

import pytest

import task_context_hook_flows as hook_flows
import task_context_service as service


conn_holder = {}


def setup_function(_fn):
    conn_holder.clear()


def _start_session(tab_id: str, session_id: str, *, source: str = "startup") -> str:
    result = hook_flows.on_session_start(
        conn_holder["conn"], {"source": source, "herdr_tab_id": tab_id, "claude_session_id": session_id}
    )
    return result["binding_id"]


def _submit(session_id: str, tab_id: str, **fields):
    payload = {"herdr_tab_id": tab_id, "claude_session_id": session_id, **fields}
    return hook_flows.on_user_prompt_submit(conn_holder["conn"], payload)


def _expand(session_id: str | None, tab_id: str | None, command_name: str = "task", **fields):
    payload = {"herdr_tab_id": tab_id, "claude_session_id": session_id, "command_name": command_name, **fields}
    return hook_flows.on_user_prompt_expansion(conn_holder["conn"], payload)


def test_given_other_command_name_when_expanded_then_no_op_pass_through(conn):
    """`command_name != "task"` is not this module's concern at all -- must
    never mutate anything and must never be treated as a `/task` failure."""
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _expand("s1", "tab-1", command_name="some-other-skill", command_args="whatever")
    assert result["decision"] == "pass"
    assert result["reason_code"] == "not_task_command"


def test_given_valid_github_target_when_task_expanded_then_atomic_rebind(conn):
    conn_holder["conn"] = conn
    binding_id = _start_session("tab-1", "s1")
    result = _expand(
        "s1", "tab-1", slash_task_target_repo="owner/repo", slash_task_target_ref_kind="issue",
        slash_task_target_ref_number=123,
    )
    assert result["decision"] == "pass"
    assert result["reason_code"] == "slash_task_rebind"
    task_id = result["task_id"]
    assert service.find_live_claim(conn, "owner/repo", "issue", 123)["task_id"] == task_id
    current_task_id, _, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert current_task_id == task_id


def test_given_ad_hoc_title_when_task_expanded_then_new_ad_hoc_task_created(conn):
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _expand("s1", "tab-1", slash_task_ad_hoc_title="ad-hoc work")
    assert result["decision"] == "pass"
    assert result["reason_code"] == "slash_task_rebind"
    assert service.count_live_task_ref_claims(conn, result["task_id"]) == 0


def test_given_active_task_a_with_live_ref_when_task_b_expanded_then_rebind_supersedes_unconditionally(conn):
    """AC6: `/task B` always supersedes whatever is ACTIVE, even while Task
    A's Activity is ACTIVE and Task A already owns a live ref -- the exact
    scenario that is merely advisory (never mutating) on ordinary
    UserPromptSubmit."""
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    task_a_result = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    task_a = task_a_result["task_id"]

    rebind_result = _expand(
        "s1", "tab-1", slash_task_target_repo="owner/other", slash_task_target_ref_kind="issue",
        slash_task_target_ref_number=99,
    )
    assert rebind_result["decision"] == "pass"
    assert rebind_result["reason_code"] == "slash_task_rebind"
    assert rebind_result["task_id"] != task_a

    binding_id = service.get_binding_by_current_session(conn, "s1")["id"]
    current_task_id, _, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert current_task_id == rebind_result["task_id"]


def test_given_missing_target_when_task_expanded_then_explicit_command_failure_not_silently_ignored(conn):
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _expand("s1", "tab-1")
    assert result["decision"] == "block"
    assert result["reason_code"] == "slash_task_missing_target"


def test_given_non_herdr_session_when_task_expanded_then_not_applicable_pass(conn):
    """A non-Herdr canonical interactive Claude session has no TabBinding to
    rebind -- this is "not applicable", not a target validation failure."""
    conn_holder["conn"] = conn
    result = _expand("s1", None, slash_task_target_repo="owner/repo", slash_task_target_ref_kind="issue",
                      slash_task_target_ref_number=1)
    assert result["decision"] == "pass"
    assert result["reason_code"] == "observe_only_non_herdr"


def test_given_no_binding_for_session_when_task_expanded_then_explicit_command_failure(conn):
    """Unlike ordinary UserPromptSubmit's fail-open default, an explicit
    `/task` command with an unresolvable Binding is surfaced as an explicit
    command failure -- it must never silently pretend to have succeeded."""
    conn_holder["conn"] = conn
    result = _expand(
        "unknown-session", "tab-1", slash_task_target_repo="owner/repo", slash_task_target_ref_kind="issue",
        slash_task_target_ref_number=1,
    )
    assert result["decision"] == "block"
    assert result["reason_code"] == "no_binding_for_session"


# ---------------------------------------------------------------------------
# OWNER PR review P1 fix_delta (PR #2632): `/task` rebind + event recording
# atomicity -- `on_user_prompt_expansion` must record the `slash_task_rebind`
# event exactly once, inside the SAME atomic transaction as the Task/
# Activity switch, and under the correct `event_type` -- not twice (once
# under the wrong default `hook:UserPromptSubmit` inside the transaction,
# once more under a separate, non-atomic `hook:UserPromptExpansion` event
# appended after the transaction already committed).
# ---------------------------------------------------------------------------


def _github_target_kwargs():
    return {
        "slash_task_target_repo": "owner/repo",
        "slash_task_target_ref_kind": "issue",
        "slash_task_target_ref_number": 123,
    }


def _ad_hoc_target_kwargs():
    return {"slash_task_ad_hoc_title": "ad-hoc work"}


@pytest.mark.parametrize("target_kwargs_factory", [_github_target_kwargs, _ad_hoc_target_kwargs])
def test_given_successful_task_rebind_when_recorded_then_exactly_one_event_with_correct_event_type(
    conn, target_kwargs_factory
):
    """Regression for OWNER PR review P1: exactly one `slash_task_rebind`
    event must be recorded (not two), and it must carry `event_type ==
    "hook:UserPromptExpansion"` (not the wrong default
    `"hook:UserPromptSubmit"` that `bind_target_to_binding`/
    `bind_ad_hoc_task_to_binding` silently fell back to before this
    fix_delta)."""
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _expand("s1", "tab-1", **target_kwargs_factory())
    assert result["decision"] == "pass"

    rows = conn.execute("SELECT event_type, metadata_json FROM events ORDER BY occurred_at").fetchall()
    rebind_events = [
        row for row in rows if json.loads(row["metadata_json"]).get("reason_code") == "slash_task_rebind"
    ]
    assert len(rebind_events) == 1, (
        f"slash_task_rebind イベントは1件のみ記録される想定: {len(rebind_events)} 件記録されていた"
    )
    assert rebind_events[0]["event_type"] == "hook:UserPromptExpansion", (
        f"slash_task_rebind イベントの event_type は 'hook:UserPromptExpansion' である想定: "
        f"{rebind_events[0]['event_type']!r}"
    )


@pytest.mark.parametrize(
    "target_kwargs_factory,patched_attr",
    [
        (_github_target_kwargs, "_bump_projection_tx"),
        (_ad_hoc_target_kwargs, "_bump_projection_tx"),
    ],
)
def test_given_write_failure_inside_atomic_transaction_when_task_expanded_then_rolled_back_not_partially_applied(
    conn, monkeypatch, target_kwargs_factory, patched_attr
):
    """Regression for OWNER PR review P1: `bind_target_to_binding` /
    `bind_ad_hoc_task_to_binding` run entirely inside one
    `db.write_transaction` (Task/Activity switch + event append + projection
    bump). If any step inside that same transaction fails, the exception
    must propagate (so the CLI/adapter surfaces a real failure) AND the
    Binding's Task/Activity must be completely unchanged from before the
    `/task` call -- nothing partially committed."""
    conn_holder["conn"] = conn
    binding_id = _start_session("tab-1", "s1")
    before_task_id, before_activity_id, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert before_task_id is None
    assert before_activity_id is None
    events_before = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(service, patched_attr, _boom)

    with pytest.raises(RuntimeError, match="injected write failure"):
        _expand("s1", "tab-1", **target_kwargs_factory())

    after_task_id, after_activity_id, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert after_task_id == before_task_id
    assert after_activity_id == before_activity_id
    events_after = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
    assert events_after == events_before, (
        "失敗したtransactionからイベントが部分的にコミットされてはならない: "
        f"before={events_before} after={events_after}"
    )
    rebind_rows = conn.execute(
        "SELECT metadata_json FROM events WHERE event_type = 'hook:UserPromptExpansion'"
    ).fetchall()
    assert all(
        json.loads(row["metadata_json"]).get("reason_code") != "slash_task_rebind" for row in rebind_rows
    ), "失敗した/taskからslash_task_rebindイベントが記録されてはならない"

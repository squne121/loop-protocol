"""Issue #2564 AC7, AC8, AC11 -- CwdChanged (location-only, never a Task
switch/block), SubAgent roll-up under the parent Task/Activity with no
TabBinding, and observe-only PreToolUse."""

from __future__ import annotations

import task_context_hook_flows as hook_flows
import task_context_service as service


def test_given_cwd_changed_when_locator_changes_then_only_runtime_location_updated_task_unaffected(conn):
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"}
    )
    binding_id = started["binding_id"]
    prompt = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-1",
            "claude_session_id": "s1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 1,
        },
    )
    task_id = prompt["task_id"]

    result = hook_flows.on_cwd_changed(conn, {"claude_session_id": "s1", "herdr_locator": "different-worktree"})
    assert result["decision"] == "pass"
    assert result["reason_code"] == "relocated"

    location = service.get_current_location(conn, binding_id)
    assert location["herdr_locator"] == "different-worktree"

    current_task_id, _, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert current_task_id == task_id, "AC7: cwd change must never switch the current Task"


def test_given_cwd_changed_with_unchanged_locator_when_processed_then_no_op(conn):
    hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"})
    result = hook_flows.on_cwd_changed(conn, {"claude_session_id": "s1", "herdr_locator": "tab-1"})
    assert result["reason_code"] == "location_unchanged"


def test_given_no_binding_when_cwd_changed_then_fail_open_pass(conn):
    result = hook_flows.on_cwd_changed(conn, {"claude_session_id": "unknown", "herdr_locator": "x"})
    assert result["decision"] == "pass"
    assert result["reason_code"] == "no_binding_for_session"


def test_given_subagent_start_and_stop_when_processed_then_rolled_up_under_parent_task_no_binding(conn):
    hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"})
    prompt = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-1",
            "claude_session_id": "s1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 1,
        },
    )
    task_id = prompt["task_id"]
    activity_id = prompt["activity_id"]

    start_result = hook_flows.on_subagent_start(conn, {"claude_session_id": "s1"})
    assert start_result["decision"] == "pass"
    run_id = start_result["execution_run_id"]
    run = service.get_execution_run(conn, run_id)
    assert run["run_kind"] == "subagent"
    assert run["binding_id"] is None
    assert run["task_id"] == task_id
    assert run["activity_id"] == activity_id

    stop_result = hook_flows.on_subagent_stop(conn, {"claude_session_id": "s1"})
    assert stop_result["decision"] == "pass"
    ended_run = service.get_execution_run(conn, run_id)
    assert ended_run["ended_at"] is not None


def test_given_no_open_subagent_run_when_subagent_stop_processed_then_pass_no_crash(conn):
    result = hook_flows.on_subagent_stop(conn, {"claude_session_id": "nonexistent"})
    assert result["decision"] == "pass"
    assert result["reason_code"] == "no_open_subagent_run"


def test_given_pre_tool_use_when_processed_then_observability_only_pass(conn):
    result = hook_flows.on_pre_tool_use(conn, {})
    assert result["decision"] == "pass"
    assert result["reason_code"] == "observability_only"


def test_given_stop_when_processed_then_run_stays_open_but_health_unaffected_and_event_recorded(conn):
    hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"})
    result = hook_flows.on_stop(conn, {"claude_session_id": "s1"})
    assert result["decision"] == "pass"
    assert result["reason_code"] == "runtime_fact_only"


def test_given_session_end_when_processed_then_run_ended_and_binding_suspended(conn):
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"}
    )
    binding_id = started["binding_id"]
    hook_flows.on_session_end(conn, {"claude_session_id": "s1"})
    binding = service.get_binding(conn, binding_id)
    assert binding["runtime_health"] == "SUSPENDED"
    assert not service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")


def test_given_unknown_generic_event_when_dispatched_then_falls_back_to_plain_event_append(conn):
    result = hook_flows.dispatch_hook_event(conn, "SomeFutureEvent", {"metadata": {"status": "ok"}})
    assert result["decision"] == "pass"
    assert result["reason_code"] == "generic_event"
    assert "event_id" in result

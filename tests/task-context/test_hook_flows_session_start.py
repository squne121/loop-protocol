"""Issue #2564 AC1, AC2, AC3, AC11, AC14, AC15 -- SessionStart lifecycle
flows (startup/resume/clear/compact/fork), non-Herdr observe-only, and
stale-run self-heal reconciliation."""

from __future__ import annotations

import task_context_hook_flows as hook_flows
import task_context_service as service


def test_given_no_herdr_tab_id_when_session_start_then_observe_only_no_binding_created(conn):
    result = hook_flows.on_session_start(conn, {"source": "startup", "claude_session_id": "s1"})
    assert result["decision"] == "pass"
    assert result["reason_code"] == "observe_only_non_herdr"
    assert result["binding_id"] is None


def test_given_startup_new_tab_when_session_start_then_binding_and_managed_run_created(conn):
    result = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"}
    )
    assert result["decision"] == "pass"
    binding_id = result["binding_id"]
    assert binding_id is not None
    binding = service.get_binding(conn, binding_id)
    assert binding["current_claude_session_id"] == "s1"
    location = service.get_current_location(conn, binding_id)
    assert location["herdr_locator"] == "tab-1"
    runs = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")
    assert len(runs) == 1


def test_given_resume_same_tab_when_session_start_then_existing_binding_restored_not_duplicated(conn):
    first = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"}
    )
    binding_id = first["binding_id"]

    # Simulate the process restarting with a *new* claude_session_id on the
    # exact same live Herdr Tab (AC3: SessionStart resume/startup recovers
    # via live-location match, not a stored session id).
    second = hook_flows.on_session_start(
        conn, {"source": "resume", "herdr_tab_id": "tab-1", "claude_session_id": "s2"}
    )
    assert second["binding_id"] == binding_id

    runs = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")
    assert len(runs) == 1, "AC1(d): at most one open managed run per binding after resume"
    assert runs[0]["claude_session_id"] == "s2"

    binding = service.get_binding(conn, binding_id)
    assert binding["current_claude_session_id"] == "s2"


def test_given_clear_when_session_start_then_task_activity_binding_preserved(conn):
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-2", "claude_session_id": "s1"}
    )
    binding_id = started["binding_id"]
    prompt_result = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-2",
            "claude_session_id": "s1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 5,
        },
    )
    task_id = prompt_result["task_id"]

    cleared = hook_flows.on_session_start(
        conn, {"source": "clear", "herdr_tab_id": "tab-2", "claude_session_id": "s3"}
    )
    assert cleared["binding_id"] == binding_id
    assert cleared["task_id"] == task_id


def test_given_quit_then_startup_when_session_start_then_suspended_binding_restored_with_continuity(conn):
    """AC3: `/quit` ends the run + suspends the Binding; a later plain
    `claude` launch on the same live Tab restores it via SessionStart alone
    (no extra argv), keeping the last-known Task/Activity."""
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-3", "claude_session_id": "s1"}
    )
    binding_id = started["binding_id"]
    prompt_result = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-3",
            "claude_session_id": "s1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 9,
        },
    )
    task_id = prompt_result["task_id"]

    hook_flows.on_session_end(conn, {"claude_session_id": "s1"})
    binding_after_quit = service.get_binding(conn, binding_id)
    assert binding_after_quit["runtime_health"] == "SUSPENDED"
    assert not service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")

    restored = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-3", "claude_session_id": "s4"}
    )
    assert restored["binding_id"] == binding_id
    assert restored["task_id"] == task_id
    binding_after_restore = service.get_binding(conn, binding_id)
    assert binding_after_restore["runtime_health"] == "ACTIVE"


def test_given_compact_when_session_start_then_no_mutation(conn):
    result = hook_flows.on_session_start(
        conn, {"source": "compact", "herdr_tab_id": "tab-4", "claude_session_id": "s1"}
    )
    assert result["decision"] == "pass"
    assert result["reason_code"] == "compact_no_mutation"
    assert result["binding_id"] is None
    # Nothing should have been created for this Tab.
    assert service.get_binding_by_current_location(conn, "tab-4") is None


def test_given_fork_when_session_start_then_parent_binding_not_stolen(conn):
    parent = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-5", "claude_session_id": "s1"}
    )
    parent_binding_id = parent["binding_id"]

    fork_result = hook_flows.on_session_start(
        conn, {"source": "fork", "herdr_tab_id": "tab-5", "claude_session_id": "s1-fork"}
    )
    assert fork_result["decision"] == "pass"
    assert fork_result["reason_code"] == "fork_no_inherited_binding"
    assert fork_result["binding_id"] is None

    # Parent Binding's location/session must be untouched by the fork.
    parent_binding = service.get_binding(conn, parent_binding_id)
    assert parent_binding["current_claude_session_id"] == "s1"

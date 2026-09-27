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


def test_given_fork_session_when_task_expanded_with_target_then_bootstrap_rebind_succeeds_without_stealing_parent(
    conn,
):
    """Issue #2790 AC5: after a `fork` SessionStart deliberately leaves the
    forked session unbound (Issue #2564 AC15), an explicit `/task <target>`
    on that exact forked `claude_session_id` bootstraps its own independent
    Binding (via `bootstrap_unbound_session`) and binds the target -- the
    parent Binding/session/Task must remain completely untouched."""
    parent = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-6", "claude_session_id": "parent-s1"}
    )
    parent_binding_id = parent["binding_id"]
    parent_prompt = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-6",
            "claude_session_id": "parent-s1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/parent-repo",
            "target_ref_kind": "issue",
            "target_ref_number": 42,
        },
    )
    parent_task_id = parent_prompt["task_id"]

    fork_result = hook_flows.on_session_start(
        conn, {"source": "fork", "herdr_tab_id": "tab-6", "claude_session_id": "fork-s1"}
    )
    assert fork_result["binding_id"] is None

    rebind = hook_flows.on_user_prompt_expansion(
        conn,
        {
            "command_name": "task",
            "herdr_tab_id": "tab-6",
            "claude_session_id": "fork-s1",
            "slash_task_target_repo": "owner/fork-repo",
            "slash_task_target_ref_kind": "issue",
            "slash_task_target_ref_number": 7,
        },
    )
    assert rebind["decision"] == "pass"
    assert rebind["reason_code"] == "slash_task_bootstrap_rebind"
    assert rebind["bootstrapped_binding"] is True

    fork_binding = service.get_binding_by_current_session(conn, "fork-s1")
    assert fork_binding is not None
    assert fork_binding["id"] != parent_binding_id
    fork_task_id, _, _ = service.get_current_task_activity_for_binding(conn, fork_binding["id"])
    assert fork_task_id == rebind["task_id"]
    assert fork_task_id != parent_task_id

    # Parent Binding/Task must be completely unaffected by the fork bootstrap.
    parent_binding = service.get_binding(conn, parent_binding_id)
    assert parent_binding["current_claude_session_id"] == "parent-s1"
    still_parent_task_id, _, _ = service.get_current_task_activity_for_binding(conn, parent_binding_id)
    assert still_parent_task_id == parent_task_id


def test_given_bound_session_when_compact_then_binding_task_activity_execution_run_identity_unchanged(conn):
    """Issue #2790 AC6 (compact_binding_survives_identity_unchanged):
    `compact` must not mutate Task/Activity/Binding/ExecutionRun identity at
    all (Issue #2564 AC15) -- unlike `fork`, `compact` is not a by-design
    unbound candidate (Issue #2790 refinement), so this fixes the exact
    identity tuple in place as a regression guard, distinct from the
    existing `compact_no_mutation`/no-binding-created assertion above."""
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-7", "claude_session_id": "s1"}
    )
    binding_id = started["binding_id"]
    prompt_result = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-7",
            "claude_session_id": "s1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 55,
        },
    )
    task_id = prompt_result["task_id"]
    before_task_id, before_activity_id, before_run_id = service.get_current_task_activity_for_binding(
        conn, binding_id
    )
    assert before_task_id == task_id

    compacted = hook_flows.on_session_start(
        conn, {"source": "compact", "herdr_tab_id": "tab-7", "claude_session_id": "s1"}
    )
    assert compacted["decision"] == "pass"
    assert compacted["reason_code"] == "compact_no_mutation"
    assert compacted["binding_id"] is None

    binding_after_compact = service.get_binding_by_current_session(conn, "s1")
    assert binding_after_compact is not None
    assert binding_after_compact["id"] == binding_id
    after_task_id, after_activity_id, after_run_id = service.get_current_task_activity_for_binding(
        conn, binding_id
    )
    assert after_task_id == before_task_id
    assert after_activity_id == before_activity_id
    assert after_run_id == before_run_id

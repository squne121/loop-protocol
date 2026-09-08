"""AC5: Herdr locator changes never change binding_id/Task/Activity
identity. AC6: ExecutionRun can represent startup (undecided), SubAgent,
runtime-smoke, and Native/Claude-GPT runtime profiles."""

from __future__ import annotations

import task_context_service as service


def test_given_binding_when_relocated_then_task_and_activity_fk_bindings_are_unaffected(conn):
    task = service.create_task(conn)
    activity = service.transition_activity(conn, task["id"], kind="impl")
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        is_managed=True,
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
    )

    service.relocate_binding(conn, binding["id"], "herdr://tab-A")
    service.relocate_binding(conn, binding["id"], "herdr://tab-B")

    reloaded_run = service.get_execution_run(conn, run["id"])
    assert reloaded_run["task_id"] == task["id"]
    assert reloaded_run["activity_id"] == activity["id"]
    assert reloaded_run["binding_id"] == binding["id"]


def test_given_startup_run_when_identities_unknown_then_run_can_be_created_with_all_null(conn):
    run = service.start_execution_run(conn, run_kind="native_operator", is_managed=True)
    assert run["task_id"] is None
    assert run["activity_id"] is None
    assert run["binding_id"] is None
    assert run["ended_at"] is None


def test_given_startup_run_when_identities_resolved_later_then_attach_updates_in_place(conn):
    run = service.start_execution_run(conn, run_kind="native_operator", is_managed=True)
    task = service.create_task(conn)
    activity = service.transition_activity(conn, task["id"], kind="impl")
    binding = service.create_binding(conn)

    updated = service.attach_execution_run(
        conn, run["id"], task_id=task["id"], activity_id=activity["id"], binding_id=binding["id"]
    )
    assert updated["id"] == run["id"]
    assert updated["task_id"] == task["id"]
    assert updated["binding_id"] == binding["id"]


def test_given_subagent_run_kind_when_created_then_stored_and_retrievable(conn):
    run = service.start_execution_run(conn, run_kind="subagent", is_managed=False, runtime_profile="test-runner")
    assert run["run_kind"] == "subagent"
    assert run["runtime_profile"] == "test-runner"


def test_given_runtime_smoke_run_kind_when_created_then_stored_and_retrievable(conn):
    run = service.start_execution_run(conn, run_kind="runtime_smoke", is_managed=False)
    assert run["run_kind"] == "runtime_smoke"


def test_given_claude_gpt_run_kind_when_created_then_stored_with_runtime_and_resume_profile(conn):
    run = service.start_execution_run(
        conn, run_kind="claude_gpt", is_managed=True, runtime_profile="claude-gpt", resume_profile="cold-restart-v1"
    )
    assert run["run_kind"] == "claude_gpt"
    assert run["runtime_profile"] == "claude-gpt"
    assert run["resume_profile"] == "cold-restart-v1"


def test_given_run_when_ended_then_ended_at_set_and_no_longer_counts_as_open(conn):
    binding = service.create_binding(conn)
    run = service.start_execution_run(conn, run_kind="native_operator", is_managed=True, binding_id=binding["id"])
    service.end_execution_run(conn, run["id"])
    # A second managed run on the same binding is now allowed because the
    # first is no longer "open" (AC1d exemption for ended runs).
    run2 = service.start_execution_run(conn, run_kind="native_operator", is_managed=True, binding_id=binding["id"])
    assert run2["id"] != run["id"]


def test_given_binding_health_states_when_transitioned_through_lifecycle_then_all_valid_values_accepted(conn):
    binding = service.create_binding(conn)
    for health in ("SUSPENDED", "RESTORING", "RESTORE_BLOCKED", "DETACHED", "ACTIVE"):
        updated = service.set_binding_health(conn, binding["id"], health)
        assert updated["runtime_health"] == health

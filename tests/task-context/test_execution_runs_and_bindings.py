"""AC5: Herdr locator changes never change binding_id/Task/Activity
identity. AC6: ExecutionRun can represent startup (undecided), SubAgent,
runtime-smoke, and Native/Claude-GPT runtime profiles."""

from __future__ import annotations

import sqlite3

import pytest

import task_context_errors as errors
import task_context_service as service


def test_given_binding_when_relocated_then_task_and_activity_fk_bindings_are_unaffected(conn):
    task = service.create_task(conn)
    activity = service.transition_activity(conn, task["id"], kind="impl")
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
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
    run = service.start_execution_run(conn, run_kind="native_operator")
    assert run["task_id"] is None
    assert run["activity_id"] is None
    assert run["binding_id"] is None
    assert run["ended_at"] is None


def test_given_startup_run_when_identities_resolved_later_then_attach_updates_in_place(conn):
    run = service.start_execution_run(conn, run_kind="native_operator")
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
    run = service.start_execution_run(conn, run_kind="subagent", runtime_profile="test-runner")
    assert run["run_kind"] == "subagent"
    assert run["runtime_profile"] == "test-runner"


def test_given_runtime_smoke_run_kind_when_created_then_stored_and_retrievable(conn):
    run = service.start_execution_run(conn, run_kind="runtime_smoke")
    assert run["run_kind"] == "runtime_smoke"


def test_given_claude_gpt_run_kind_when_created_then_stored_with_runtime_and_resume_profile(conn):
    run = service.start_execution_run(
        conn, run_kind="claude_gpt", runtime_profile="claude-gpt", resume_profile="cold-restart-v1"
    )
    assert run["run_kind"] == "claude_gpt"
    assert run["runtime_profile"] == "claude-gpt"
    assert run["resume_profile"] == "cold-restart-v1"


def test_given_run_when_ended_then_ended_at_set_and_no_longer_counts_as_open(conn):
    binding = service.create_binding(conn)
    run = service.start_execution_run(conn, run_kind="native_operator", binding_id=binding["id"])
    service.end_execution_run(conn, run["id"])
    # A second managed run on the same binding is now allowed because the
    # first is no longer "open" (AC1d exemption for ended runs).
    run2 = service.start_execution_run(conn, run_kind="native_operator", binding_id=binding["id"])
    assert run2["id"] != run["id"]


def test_given_binding_health_states_when_transitioned_through_lifecycle_then_all_valid_values_accepted(conn):
    binding = service.create_binding(conn)
    for health in ("SUSPENDED", "RESTORING", "RESTORE_BLOCKED", "DETACHED", "ACTIVE"):
        updated = service.set_binding_health(conn, binding["id"], health)
        assert updated["runtime_health"] == health


# -- fix_delta finding 3: Claude session identity double-SSOT sync ----------


def test_given_open_managed_run_when_session_set_on_its_binding_then_synced_and_resolvable(conn):
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn, run_kind="native_operator", binding_id=binding["id"], claude_session_id="sess-sync-1"
    )
    updated = service.set_binding_session(conn, binding["id"], "sess-sync-1", execution_run_id=run["id"])
    assert updated["current_claude_session_id"] == "sess-sync-1"

    resolved = service.get_binding_by_current_session(conn, "sess-sync-1")
    assert resolved["id"] == binding["id"]


def test_given_no_execution_run_id_when_setting_non_null_session_then_validation_error(conn):
    binding = service.create_binding(conn)
    with pytest.raises(errors.ValidationError):
        service.set_binding_session(conn, binding["id"], "sess-missing-run")


def test_given_execution_run_id_not_open_managed_on_binding_when_setting_session_then_validation_error(conn):
    binding = service.create_binding(conn)
    other_binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn, run_kind="native_operator", binding_id=other_binding["id"], claude_session_id="sess-wrong-binding"
    )
    with pytest.raises(errors.ValidationError):
        service.set_binding_session(conn, binding["id"], "sess-wrong-binding", execution_run_id=run["id"])


def test_given_execution_run_session_mismatch_when_setting_binding_session_then_validation_error(conn):
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn, run_kind="native_operator", binding_id=binding["id"], claude_session_id="sess-actual"
    )
    with pytest.raises(errors.ValidationError):
        service.set_binding_session(conn, binding["id"], "sess-claimed-instead", execution_run_id=run["id"])


def test_given_non_managed_execution_run_when_setting_binding_session_then_validation_error(conn):
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn, run_kind="subagent", binding_id=binding["id"], claude_session_id="sess-subagent"
    )
    with pytest.raises(errors.ValidationError):
        service.set_binding_session(conn, binding["id"], "sess-subagent", execution_run_id=run["id"])


def test_given_session_already_current_on_another_binding_when_raw_sql_inserted_directly_then_rejected(conn):
    """DB-physical guard (ux_tab_bindings_current_session): a non-null
    session id can be the *current* claim of at most one Binding."""
    binding_a = service.create_binding(conn)
    binding_b = service.create_binding(conn)
    run_a = service.start_execution_run(
        conn, run_kind="native_operator", binding_id=binding_a["id"], claude_session_id="sess-dup"
    )
    service.set_binding_session(conn, binding_a["id"], "sess-dup", execution_run_id=run_a["id"])

    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE tab_bindings SET current_claude_session_id = 'sess-dup' WHERE id = ?",
            (binding_b["id"],),
        )
    conn.execute("ROLLBACK")


def test_given_current_session_cleared_when_new_binding_claims_it_then_allowed(conn):
    binding_a = service.create_binding(conn)
    binding_b = service.create_binding(conn)
    run_a = service.start_execution_run(
        conn, run_kind="native_operator", binding_id=binding_a["id"], claude_session_id="sess-handoff"
    )
    service.set_binding_session(conn, binding_a["id"], "sess-handoff", execution_run_id=run_a["id"])
    service.set_binding_session(conn, binding_a["id"], None)

    service.end_execution_run(conn, run_a["id"])
    run_b = service.start_execution_run(
        conn, run_kind="native_operator", binding_id=binding_b["id"], claude_session_id="sess-handoff"
    )
    updated = service.set_binding_session(conn, binding_b["id"], "sess-handoff", execution_run_id=run_b["id"])
    assert updated["current_claude_session_id"] == "sess-handoff"
    assert service.get_binding_by_current_session(conn, "sess-handoff")["id"] == binding_b["id"]


def test_given_unknown_session_when_resolving_current_binding_then_not_found(conn):
    with pytest.raises(errors.NotFoundError):
        service.get_binding_by_current_session(conn, "sess-does-not-exist")


# -- fix_delta finding 7a: execution_runs.task_id/activity_id consistency ---


def test_given_activity_belongs_to_different_task_when_start_execution_run_then_validation_error(conn):
    task_a = service.create_task(conn, title="A")
    task_b = service.create_task(conn, title="B")
    activity_b = service.transition_activity(conn, task_b["id"], kind="impl")
    with pytest.raises(errors.ValidationError):
        service.start_execution_run(
            conn, run_kind="native_operator", task_id=task_a["id"], activity_id=activity_b["id"]
        )


def test_given_activity_belongs_to_different_task_when_attach_execution_run_then_validation_error(conn):
    task_a = service.create_task(conn, title="A")
    task_b = service.create_task(conn, title="B")
    activity_b = service.transition_activity(conn, task_b["id"], kind="impl")
    run = service.start_execution_run(conn, run_kind="native_operator", task_id=task_a["id"])
    with pytest.raises(errors.ValidationError):
        service.attach_execution_run(conn, run["id"], activity_id=activity_b["id"])


def test_given_consistent_task_and_activity_when_start_execution_run_then_allowed(conn):
    task = service.create_task(conn)
    activity = service.transition_activity(conn, task["id"], kind="impl")
    run = service.start_execution_run(conn, run_kind="native_operator", task_id=task["id"], activity_id=activity["id"])
    assert run["task_id"] == task["id"]
    assert run["activity_id"] == activity["id"]


def test_given_mismatched_task_activity_when_raw_sql_inserted_directly_then_trigger_rejects(conn):
    task_a = service.create_task(conn, title="A")
    task_b = service.create_task(conn, title="B")
    activity_b = service.transition_activity(conn, task_b["id"], kind="impl")
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO execution_runs "
            "(id, task_id, activity_id, binding_id, run_kind, runtime_profile, resume_profile, "
            " claude_session_id, is_managed, started_at, ended_at) "
            "VALUES ('mismatched-run', ?, ?, NULL, 'native_operator', NULL, NULL, NULL, 1, 't', NULL)",
            (task_a["id"], activity_b["id"]),
        )
    conn.execute("ROLLBACK")

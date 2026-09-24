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


# ---------------------------------------------------------------------------
# PR #2731 review fix_delta P1-3 -- locator collision: a stale claim from a
# DIFFERENT Binding must be atomically detached (never that other Binding's
# Task/Activity/Binding semantic identity) when session identity uniquely
# resolves which Binding is relocating to that locator.
# ---------------------------------------------------------------------------


def test_given_stale_locator_claim_from_other_binding_when_relocating_then_only_location_detached(conn):
    """Repro from the review: Binding A (SUSPENDED) holds old locator
    w1:p1; Binding B (ACTIVE) resumes to w1:p1 via session identity. Only
    A's location OBSERVATION at w1:p1 must be released -- A's own Task/
    Activity/Binding semantic identity (runtime_health, open runs, etc.)
    must be completely untouched."""
    task_a = service.create_task(conn, title="Task A")
    activity_a = service.transition_activity(conn, task_a["id"], kind="impl")
    binding_a = service.create_binding(conn)
    service.relocate_binding(conn, binding_a["id"], "w1:p1")
    run_a = service.start_execution_run(
        conn, run_kind="native_operator", task_id=task_a["id"], activity_id=activity_a["id"],
        binding_id=binding_a["id"], claude_session_id="collision-session-a",
    )
    service.set_binding_session(conn, binding_a["id"], "collision-session-a", execution_run_id=run_a["id"])
    service.set_binding_health(conn, binding_a["id"], "SUSPENDED")

    task_b = service.create_task(conn, title="Task B")
    activity_b = service.transition_activity(conn, task_b["id"], kind="impl")
    binding_b = service.create_binding(conn)
    service.relocate_binding(conn, binding_b["id"], "w1:p2")
    run_b = service.start_execution_run(
        conn, run_kind="native_operator", task_id=task_b["id"], activity_id=activity_b["id"],
        binding_id=binding_b["id"], claude_session_id="collision-session-b",
    )
    service.set_binding_session(conn, binding_b["id"], "collision-session-b", execution_run_id=run_b["id"])

    # B (ACTIVE) cold-restart-resumes at w1:p1 (session identity uniquely
    # resolved binding_b -- this call models the destination-locator
    # relocation `on_session_start`'s session-first path performs).
    service.relocate_binding(conn, binding_b["id"], "w1:p1")

    # A's location observation at w1:p1 must be detached (released)...
    assert service.get_current_location(conn, binding_a["id"]) is None
    # ...but A's own Task/Activity/Binding semantic identity is untouched:
    # still SUSPENDED, its Task/Activity ids unchanged, its own managed run
    # never ended by this relocation.
    reloaded_binding_a = service.get_binding(conn, binding_a["id"])
    assert reloaded_binding_a["runtime_health"] == "SUSPENDED"
    reloaded_run_a = service.get_execution_run(conn, run_a["id"])
    assert reloaded_run_a["task_id"] == task_a["id"]
    assert reloaded_run_a["activity_id"] == activity_a["id"]
    assert reloaded_run_a["ended_at"] is None

    # B is now the sole current owner of w1:p1.
    assert service.get_current_location(conn, binding_b["id"])["herdr_locator"] == "w1:p1"
    assert service.get_binding_by_current_location(conn, "w1:p1")["id"] == binding_b["id"]


def test_given_locator_collision_resolved_when_clear_happens_then_correct_binding_retained(conn):
    """Repro from the review: after the w1:p1 collision above is resolved
    (only B's location observation remains at w1:p1), a subsequent
    `/clear` on B (new session id, session-id lookup misses, falls back to
    the locator lookup) must resolve B -- never A -- and B's own Task/
    Activity/Binding identity must persist across the `/clear`."""
    task_a = service.create_task(conn, title="Task A")
    binding_a = service.create_binding(conn)
    service.relocate_binding(conn, binding_a["id"], "w1:p1")
    run_a = service.start_execution_run(
        conn, run_kind="native_operator", task_id=task_a["id"], binding_id=binding_a["id"],
        claude_session_id="collision2-session-a",
    )
    service.set_binding_session(conn, binding_a["id"], "collision2-session-a", execution_run_id=run_a["id"])
    service.set_binding_health(conn, binding_a["id"], "SUSPENDED")

    task_b = service.create_task(conn, title="Task B")
    binding_b = service.create_binding(conn)
    service.relocate_binding(conn, binding_b["id"], "w1:p2")
    run_b = service.start_execution_run(
        conn, run_kind="native_operator", task_id=task_b["id"], binding_id=binding_b["id"],
        claude_session_id="collision2-session-b",
    )
    service.set_binding_session(conn, binding_b["id"], "collision2-session-b", execution_run_id=run_b["id"])

    # B resumes to w1:p1 via session identity -- detaches A's stale claim.
    service.relocate_binding(conn, binding_b["id"], "w1:p1")

    # `/clear` on B: session id changes, so a caller falls back to the
    # locator lookup for w1:p1 -- this must resolve binding_b, never
    # binding_a, and must not be ambiguous.
    resolved = service.get_binding_by_current_location(conn, "w1:p1")
    assert resolved is not None
    assert resolved["id"] == binding_b["id"]
    assert resolved["id"] != binding_a["id"]

    # Same Task/Binding persist for B across the `/clear`-style re-resolve.
    task_id_b, _activity_id_b, execution_run_id_b = service.get_current_task_activity_for_binding(
        conn, binding_b["id"]
    )
    assert task_id_b == task_b["id"]
    assert execution_run_id_b == run_b["id"]


def test_given_no_stale_claim_when_ambiguous_multi_claim_exists_then_lookup_fails_closed(conn):
    """Defensive hardening (review: "get_binding_by_current_location() を
    ... fetchone() で ... 誤って選ぶ ... 曖昧性を検出せず"): if more than
    one Binding somehow still holds an unreleased location observation for
    the same locator (a state the P1-3 detach fix above prevents going
    forward, but which this test constructs directly via the service layer
    to prove the read-side guard independently), the lookup must return
    None (pick none) rather than an arbitrary row."""
    binding_a = service.create_binding(conn)
    binding_b = service.create_binding(conn)
    # Constructed directly (bypassing the now-fixed relocate path) to
    # simulate an ambiguous state and assert the READ side fails closed
    # independently of the WRITE-side fix.
    conn.execute("BEGIN IMMEDIATE")
    for binding, loc_id in ((binding_a, "loc-collision-a"), (binding_b, "loc-collision-b")):
        conn.execute(
            "INSERT INTO runtime_locations "
            "(id, binding_id, herdr_locator, observed_at, released_at, cwd, worktree, branch) "
            "VALUES (?, ?, 'w9:p9', 't', NULL, NULL, NULL, NULL)",
            (loc_id, binding["id"]),
        )
    conn.execute("COMMIT")

    assert service.get_binding_by_current_location(conn, "w9:p9") is None

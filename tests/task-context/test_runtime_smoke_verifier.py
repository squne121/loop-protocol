"""Issue #2568 -- deterministic (non-runtime) tests for the Task Context
runtime-smoke verifier: AC6 (smoke seed scope gate, before-open
non-materialization proof), AC3/AC5 (canonical DB table-scoped delta
contract, execution_runs roll-up), and AC4/E (isolated synthetic fixture
never leaks into the canonical DB).

Real fresh-Claude/Herdr runtime evidence for AC1/AC2/AC7/AC8/AC9 is out of
scope for this file (see PR body / IMPLEMENT_RESULT_V1 for what runtime
evidence was actually captured) -- these tests instead prove the mechanism
this module provides to a real runtime-smoke run is itself correct."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

import task_context_config as config
import task_context_db as db
import task_context_envelope as envelope
import task_context_migration_runner as migration_runner
import task_context_runtime_smoke_verifier as verifier
import task_context_service as service
import task_contextctl as cli

_CLI_PATH = verifier._CLI_PATH


def _run_cli(argv, stdin_obj, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_obj)))
    exit_code = cli.main(argv)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line, got: {captured.out!r}"
    return json.loads(lines[0]), exit_code


# ---------------------------------------------------------------------------
# AC6: smoke seed rejected before _open_db_and_migrate() when scope mismatched
# ---------------------------------------------------------------------------


def test_given_scope_unset_when_smoke_seed_invoked_then_rejected_and_state_root_not_materialized(
    state_root, monkeypatch, capsys
):
    """AC6: with LOOP_TASK_CONTEXT_STATE_ROOT set but LOOP_TASK_CONTEXT_SCOPE
    NOT set to runtime_smoke, smoke seed must be rejected, and the
    state-root directory (and therefore any DB file / migration side
    effect) must never be created."""
    assert not state_root.exists()
    result, exit_code = _run_cli(["smoke", "seed"], envelope.build_request("smoke_seed", {}), monkeypatch, capsys)
    assert exit_code == 2
    assert result["status"] == "error"
    assert result["code"] == "VALIDATION_ERROR"
    assert not state_root.exists(), "AC6: scope-mismatched smoke seed must never materialize the state root"


def test_given_scope_set_to_wrong_value_when_smoke_seed_invoked_then_rejected(state_root, monkeypatch, capsys):
    monkeypatch.setenv(config.SCOPE_ENV_VAR, "not_runtime_smoke")
    result, exit_code = _run_cli(["smoke", "seed"], envelope.build_request("smoke_seed", {}), monkeypatch, capsys)
    assert exit_code == 2
    assert result["code"] == "VALIDATION_ERROR"
    assert not state_root.exists()


def test_given_scope_runtime_smoke_when_smoke_seed_invoked_then_accepted_and_state_root_materialized(
    state_root, runtime_smoke_scope, monkeypatch, capsys
):
    result, exit_code = _run_cli(["smoke", "seed"], envelope.build_request("smoke_seed", {}), monkeypatch, capsys)
    assert exit_code == 0
    assert result["status"] == "ok"
    assert state_root.exists(), "positive path must materialize the isolated state root"


def test_given_scope_unset_when_smoke_seed_invoked_as_real_subprocess_then_rejected_and_no_db_file(
    state_root, monkeypatch
):
    """Real subprocess variant of the AC6 proof (not the in-process
    cli.main() call), mirroring the existing #2563 real-CLI-subprocess test
    convention."""
    request = envelope.build_request("smoke_seed", {})
    env = dict(os.environ)
    env[config.STATE_ROOT_ENV_VAR] = str(state_root)
    env.pop(config.SCOPE_ENV_VAR, None)
    proc = subprocess.run(
        [sys.executable, str(_CLI_PATH), "smoke", "seed"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    result = json.loads(lines[0])
    assert result["code"] == "VALIDATION_ERROR"
    assert not state_root.exists()


# ---------------------------------------------------------------------------
# verifier.invoke_smoke_seed / build_isolated_env / build_isolated_state_root
# ---------------------------------------------------------------------------


def test_given_isolated_env_when_smoke_seed_invoked_via_verifier_then_ok_and_isolated_db_materialized(tmp_path):
    state_root = verifier.build_isolated_state_root(tmp_path, run_id="fixed-run")
    assert not verifier.is_state_root_materialized(state_root)
    env = verifier.build_isolated_env(state_root)
    assert env[config.SCOPE_ENV_VAR] == "runtime_smoke"
    assert env[config.STATE_ROOT_ENV_VAR] == str(state_root)

    result = verifier.invoke_smoke_seed(env, title="verifier smoke")
    assert result["status"] == "ok"
    assert "task_id" in result["data"]
    assert verifier.is_state_root_materialized(state_root)


def test_given_relative_state_root_when_building_isolated_env_then_rejected(tmp_path):
    relative_state_root = tmp_path.relative_to(tmp_path.anchor)
    with pytest.raises(ValueError):
        verifier.build_isolated_env(relative_state_root)


def test_given_missing_scope_env_var_when_invoking_smoke_seed_via_verifier_then_raises(tmp_path):
    state_root = verifier.build_isolated_state_root(tmp_path, run_id="fixed-run-2")
    env = dict(os.environ)
    env[config.STATE_ROOT_ENV_VAR] = str(state_root)
    env.pop(config.SCOPE_ENV_VAR, None)
    with pytest.raises(RuntimeError):
        verifier.invoke_smoke_seed(env)
    assert not verifier.is_state_root_materialized(state_root)


# ---------------------------------------------------------------------------
# AC3/AC5: canonical DB table-scoped delta contract
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_conn(state_root):
    conn = db.connect(config.db_path())
    migration_runner.migrate(conn)
    yield conn
    conn.close()


def test_given_untouched_canonical_db_when_diffed_then_aggregate_passes(canonical_conn):
    before = verifier.snapshot_canonical_tables(canonical_conn)
    after = verifier.snapshot_canonical_tables(canonical_conn)
    result = verifier.canonical_delta_contract(
        before, after, expected_task_id="unused-task-id", expected_activity_id="unused-activity-id"
    )
    assert result.status == "pass"
    assert all(r.status == "pass" for r in result.forbidden_tables.values())
    assert result.execution_runs.status == "pass"


def test_given_single_valid_runtime_smoke_rollup_when_diffed_then_aggregate_passes(canonical_conn):
    task = service.create_task(canonical_conn, title="parent task")
    activity = service.transition_activity(canonical_conn, task["id"], kind="work")
    before = verifier.snapshot_canonical_tables(canonical_conn)

    verifier.roll_up_runtime_smoke_execution_run(canonical_conn, task_id=task["id"], activity_id=activity["id"])

    after = verifier.snapshot_canonical_tables(canonical_conn)
    result = verifier.canonical_delta_contract(
        before, after, expected_task_id=task["id"], expected_activity_id=activity["id"]
    )
    assert result.status == "pass", result.execution_runs.violations
    assert len(result.execution_runs.added) == 1
    added = result.execution_runs.added[0]
    assert added["run_kind"] == "runtime_smoke"
    assert added["binding_id"] is None


def test_given_forbidden_table_row_added_when_diffed_then_fails(canonical_conn):
    before = verifier.snapshot_canonical_tables(canonical_conn)
    service.create_task(canonical_conn, title="unexpected canonical mutation")
    after = verifier.snapshot_canonical_tables(canonical_conn)

    result = verifier.canonical_delta_contract(
        before, after, expected_task_id="unused-task-id", expected_activity_id="unused-activity-id"
    )
    assert result.status == "fail"
    assert result.forbidden_tables["tasks"].status == "fail"
    assert result.forbidden_tables["tasks"].added


def test_given_more_than_one_runtime_smoke_row_added_when_diffed_then_fails(canonical_conn):
    task = service.create_task(canonical_conn, title="parent task")
    activity = service.transition_activity(canonical_conn, task["id"], kind="work")
    before = verifier.snapshot_canonical_tables(canonical_conn)

    verifier.roll_up_runtime_smoke_execution_run(canonical_conn, task_id=task["id"], activity_id=activity["id"])
    verifier.roll_up_runtime_smoke_execution_run(canonical_conn, task_id=task["id"], activity_id=activity["id"])

    after = verifier.snapshot_canonical_tables(canonical_conn)
    result = verifier.assert_execution_runs_delta_contract(
        before, after, expected_task_id=task["id"], expected_activity_id=activity["id"]
    )
    assert result.status == "fail"
    assert any("exceeds max" in v for v in result.violations)


def test_given_execution_run_with_non_null_binding_id_added_when_diffed_then_fails(canonical_conn):
    task = service.create_task(canonical_conn, title="parent task")
    activity = service.transition_activity(canonical_conn, task["id"], kind="work")
    binding = service.create_binding(canonical_conn)
    before = verifier.snapshot_canonical_tables(canonical_conn)

    # A managed operator run (not this module's rollup helper) -- simulates
    # an unrelated real ExecutionRun row showing up during the same window,
    # which the smoke contract must still flag as unexpected (it isn't the
    # runtime_smoke/binding_id-NULL shape).
    service.start_execution_run(canonical_conn, run_kind="native_operator", binding_id=binding["id"])

    after = verifier.snapshot_canonical_tables(canonical_conn)
    result = verifier.assert_execution_runs_delta_contract(
        before, after, expected_task_id=task["id"], expected_activity_id=activity["id"]
    )
    assert result.status == "fail"
    assert any("run_kind" in v for v in result.violations)


def test_given_existing_execution_run_mutated_when_diffed_then_fails(canonical_conn):
    task = service.create_task(canonical_conn, title="parent task")
    activity = service.transition_activity(canonical_conn, task["id"], kind="work")
    run = service.start_execution_run(
        canonical_conn, run_kind="runtime_smoke", task_id=task["id"], activity_id=activity["id"]
    )
    before = verifier.snapshot_canonical_tables(canonical_conn)

    service.end_execution_run(canonical_conn, run["id"])

    after = verifier.snapshot_canonical_tables(canonical_conn)
    result = verifier.assert_execution_runs_delta_contract(
        before, after, expected_task_id=task["id"], expected_activity_id=activity["id"]
    )
    assert result.status == "fail"
    assert any("mutated" in v for v in result.violations)


# ---------------------------------------------------------------------------
# Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 3 (AC5 parent Task/
# Activity attribution must be directly asserted in the DB delta)
# ---------------------------------------------------------------------------


def test_given_task_id_none_when_rolling_up_execution_run_then_raises(canonical_conn):
    activity_task = service.create_task(canonical_conn, title="parent task")
    activity = service.transition_activity(canonical_conn, activity_task["id"], kind="work")
    with pytest.raises(ValueError):
        verifier.roll_up_runtime_smoke_execution_run(canonical_conn, task_id=None, activity_id=activity["id"])


def test_given_activity_id_none_when_rolling_up_execution_run_then_raises(canonical_conn):
    task = service.create_task(canonical_conn, title="parent task")
    with pytest.raises(ValueError):
        verifier.roll_up_runtime_smoke_execution_run(canonical_conn, task_id=task["id"], activity_id=None)


def test_given_added_row_bound_to_different_task_when_diffed_then_fails(canonical_conn):
    task = service.create_task(canonical_conn, title="parent task")
    activity = service.transition_activity(canonical_conn, task["id"], kind="work")
    other_task = service.create_task(canonical_conn, title="a different task")
    other_activity = service.transition_activity(canonical_conn, other_task["id"], kind="work")
    before = verifier.snapshot_canonical_tables(canonical_conn)

    verifier.roll_up_runtime_smoke_execution_run(
        canonical_conn, task_id=other_task["id"], activity_id=other_activity["id"]
    )

    after = verifier.snapshot_canonical_tables(canonical_conn)
    result = verifier.assert_execution_runs_delta_contract(
        before, after, expected_task_id=task["id"], expected_activity_id=activity["id"]
    )
    assert result.status == "fail"
    assert any("task_id" in v for v in result.violations)


def test_given_added_row_bound_to_different_activity_under_same_task_when_diffed_then_fails(canonical_conn):
    task = service.create_task(canonical_conn, title="parent task")
    activity = service.transition_activity(canonical_conn, task["id"], kind="work")
    other_activity = service.transition_activity(canonical_conn, task["id"], kind="review")
    before = verifier.snapshot_canonical_tables(canonical_conn)

    verifier.roll_up_runtime_smoke_execution_run(canonical_conn, task_id=task["id"], activity_id=other_activity["id"])

    after = verifier.snapshot_canonical_tables(canonical_conn)
    result = verifier.assert_execution_runs_delta_contract(
        before, after, expected_task_id=task["id"], expected_activity_id=activity["id"]
    )
    assert result.status == "fail"
    assert any("activity_id" in v for v in result.violations)


def test_given_missing_expected_task_id_when_asserting_delta_contract_then_raises(canonical_conn):
    before = verifier.snapshot_canonical_tables(canonical_conn)
    after = verifier.snapshot_canonical_tables(canonical_conn)
    with pytest.raises(ValueError):
        verifier.assert_execution_runs_delta_contract(before, after, expected_task_id="", expected_activity_id="a1")


def test_given_missing_expected_activity_id_when_asserting_delta_contract_then_raises(canonical_conn):
    before = verifier.snapshot_canonical_tables(canonical_conn)
    after = verifier.snapshot_canonical_tables(canonical_conn)
    with pytest.raises(ValueError):
        verifier.assert_execution_runs_delta_contract(before, after, expected_task_id="t1", expected_activity_id="")


# ---------------------------------------------------------------------------
# AC4/E: isolated synthetic fixture never leaks into the canonical DB
# ---------------------------------------------------------------------------


def test_given_isolated_smoke_fixture_when_checked_against_canonical_then_absent(tmp_path, canonical_conn):
    isolated_state_root = verifier.build_isolated_state_root(tmp_path, run_id="leak-check")
    env = verifier.build_isolated_env(isolated_state_root)
    seeded = verifier.invoke_smoke_seed(env, title="isolated fixture")

    absent = verifier.assert_isolated_fixture_absent_from_canonical(
        canonical_conn, seeded["data"]["task_id"], seeded["data"]["binding_id"]
    )
    assert absent

    canonical_before = verifier.snapshot_canonical_tables(canonical_conn)
    canonical_after = verifier.snapshot_canonical_tables(canonical_conn)
    result = verifier.canonical_delta_contract(
        canonical_before, canonical_after, expected_task_id="unused-task-id", expected_activity_id="unused-activity-id"
    )
    assert result.status == "pass"


# ---------------------------------------------------------------------------
# AC7: wrong-primary-target advisory assertion (pure function, synthetic
# observed values)
# ---------------------------------------------------------------------------


def test_given_advisory_hook_result_and_unchanged_state_when_asserted_then_passes():
    binding = {"id": "b1", "runtime_health": "ACTIVE"}
    activity = {"id": "a1", "status": "ACTIVE"}
    claims = [{"id": "c1", "released_at": None}]
    evidence = verifier.assert_wrong_primary_target_advisory(
        {"decision": "pass", "reason_code": "different_primary_target_active", "advisory": True},
        binding_before=binding,
        binding_after=dict(binding),
        activity_before=activity,
        activity_after=dict(activity),
        claim_before=claims,
        claim_after=[dict(c) for c in claims],
    )
    assert evidence.status == "pass"


def test_given_hard_block_decision_when_asserted_then_fails():
    evidence = verifier.assert_wrong_primary_target_advisory(
        {"decision": "block", "reason_code": "different_primary_target_active", "advisory": True},
        binding_before={"id": "b1"},
        binding_after={"id": "b1"},
        activity_before={"id": "a1"},
        activity_after={"id": "a1"},
        claim_before=[],
        claim_after=[],
    )
    assert evidence.status == "fail"
    assert any("decision" in v for v in evidence.violations)


def test_given_binding_mutated_across_mismatch_turn_when_asserted_then_fails():
    evidence = verifier.assert_wrong_primary_target_advisory(
        {"decision": "pass", "reason_code": "different_primary_target_active", "advisory": True},
        binding_before={"id": "b1", "runtime_health": "ACTIVE"},
        binding_after={"id": "b1", "runtime_health": "SUSPENDED"},
        activity_before={"id": "a1"},
        activity_after={"id": "a1"},
        claim_before=[],
        claim_after=[],
    )
    assert evidence.status == "fail"
    assert any("binding" in v for v in evidence.violations)


# ---------------------------------------------------------------------------
# AC8: /clear causal evidence assertion (pure function, synthetic observed
# values)
# ---------------------------------------------------------------------------


def test_given_distinct_sessions_and_stable_ids_when_asserted_then_passes():
    evidence = verifier.assert_clear_scenario_evidence(
        pre_clear_session_id="s1",
        post_clear_session_id="s2",
        task_id_before="t1",
        task_id_after="t1",
        activity_id_before="a1",
        activity_id_after="a1",
        binding_id_before="b1",
        binding_id_after="b1",
    )
    assert evidence.status == "pass"


def test_given_same_session_id_before_and_after_when_asserted_then_fails():
    evidence = verifier.assert_clear_scenario_evidence(
        pre_clear_session_id="s1",
        post_clear_session_id="s1",
        task_id_before="t1",
        task_id_after="t1",
        activity_id_before="a1",
        activity_id_after="a1",
        binding_id_before="b1",
        binding_id_after="b1",
    )
    assert evidence.status == "fail"
    assert any("did not actually start a new session" in v for v in evidence.violations)


def test_given_task_id_changed_across_clear_when_asserted_then_fails():
    evidence = verifier.assert_clear_scenario_evidence(
        pre_clear_session_id="s1",
        post_clear_session_id="s2",
        task_id_before="t1",
        task_id_after="t2",
        activity_id_before="a1",
        activity_id_after="a1",
        binding_id_before="b1",
        binding_id_after="b1",
    )
    assert evidence.status == "fail"
    assert any("task_id changed" in v for v in evidence.violations)


def test_given_missing_session_ids_when_asserted_then_fails_not_silently_assumed():
    evidence = verifier.assert_clear_scenario_evidence(
        pre_clear_session_id=None,
        post_clear_session_id=None,
        task_id_before="t1",
        task_id_after="t1",
        activity_id_before="a1",
        activity_id_after="a1",
        binding_id_before="b1",
        binding_id_after="b1",
    )
    assert evidence.status == "fail"
    assert any("missing pre_clear_session_id" in v for v in evidence.violations)


# ---------------------------------------------------------------------------
# Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 1 (atomic carrier
# integrity): runtime_smoke scope requires an explicit state-root; extra
# cannot override the two reserved build_isolated_env() keys.
# ---------------------------------------------------------------------------


def test_given_runtime_smoke_scope_and_no_state_root_when_resolving_then_raises_before_canonical_fallback(
    monkeypatch,
):
    monkeypatch.delenv(config.STATE_ROOT_ENV_VAR, raising=False)
    monkeypatch.setenv(config.SCOPE_ENV_VAR, config.RUNTIME_SMOKE_SCOPE_VALUE)
    with pytest.raises(ValueError, match=config.SCOPE_ENV_VAR):
        config.resolve_state_root()


def test_given_runtime_smoke_scope_and_empty_state_root_when_resolving_then_raises(monkeypatch):
    monkeypatch.setenv(config.STATE_ROOT_ENV_VAR, "")
    monkeypatch.setenv(config.SCOPE_ENV_VAR, config.RUNTIME_SMOKE_SCOPE_VALUE)
    with pytest.raises(ValueError):
        config.resolve_state_root()


def test_given_non_runtime_smoke_scope_and_no_state_root_when_resolving_then_unchanged_canonical_behavior(
    monkeypatch,
):
    monkeypatch.delenv(config.STATE_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv(config.SCOPE_ENV_VAR, raising=False)
    # Non-regression: a caller supplying neither carrier var must see
    # exactly the pre-existing canonical resolution, no new exception.
    result = config.resolve_state_root()
    expected = config._default_xdg_state_home() / "loop-protocol" / "task-context" / "v1" / config.repo_instance_key()
    assert result == expected


def test_given_runtime_smoke_scope_with_state_root_but_env_var_stripped_when_smoke_seed_then_rejected(
    state_root, runtime_smoke_scope, monkeypatch, capsys
):
    """AC6 extension: scope alone is not sufficient -- if the STATE_ROOT env
    var itself is unset (even though the `state_root` fixture set it, we
    strip it back off here), smoke seed must be rejected before
    `_open_db_and_migrate()`, never falling back to the canonical DB."""
    monkeypatch.delenv(config.STATE_ROOT_ENV_VAR, raising=False)
    result, exit_code = _run_cli(["smoke", "seed"], envelope.build_request("smoke_seed", {}), monkeypatch, capsys)
    assert exit_code == 2
    assert result["code"] == "VALIDATION_ERROR"
    assert not state_root.exists()


def test_given_extra_attempts_to_override_reserved_keys_when_building_isolated_env_then_reserved_keys_win(tmp_path):
    state_root = verifier.build_isolated_state_root(tmp_path, run_id="extra-override-check")
    env = verifier.build_isolated_env(
        state_root,
        extra={
            config.SCOPE_ENV_VAR: "not_runtime_smoke",
            config.STATE_ROOT_ENV_VAR: "/tmp/attacker-controlled",
            "SOME_OTHER_KEY": "kept",
        },
    )
    assert env[config.SCOPE_ENV_VAR] == config.RUNTIME_SMOKE_SCOPE_VALUE
    assert env[config.STATE_ROOT_ENV_VAR] == str(state_root)
    assert env["SOME_OTHER_KEY"] == "kept"


# ---------------------------------------------------------------------------
# Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 4 (P2): /clear causal
# ordering evidence beyond same-Task/Activity/Binding + distinct sessions.
# ---------------------------------------------------------------------------


def test_given_real_causal_clear_event_in_order_when_asserted_then_passes():
    evidence = verifier.assert_clear_causal_evidence(
        pre_clear_execution_run={"ended_at": "2026-01-01T00:00:00+00:00"},
        clear_event={
            "metadata_json": json.dumps({"reason_code": "clear_restored_binding"}),
            "occurred_at": "2026-01-01T00:00:01+00:00",
        },
        post_clear_execution_run={"started_at": "2026-01-01T00:00:02+00:00"},
    )
    assert evidence.status == "pass"


def test_given_two_unrelated_sessions_with_no_real_clear_event_when_asserted_then_fails():
    """Two unrelated sessions on the same Binding, with no real causal
    clear-event ordering between them, must NOT pass this assertion."""
    evidence = verifier.assert_clear_causal_evidence(
        pre_clear_execution_run={"ended_at": "2026-01-01T00:00:00+00:00"},
        clear_event=None,
        post_clear_execution_run={"started_at": "2026-01-01T00:00:02+00:00"},
    )
    assert evidence.status == "fail"
    assert any("missing clear-associated event evidence" in v for v in evidence.violations)


def test_given_clear_event_with_wrong_reason_code_when_asserted_then_fails():
    evidence = verifier.assert_clear_causal_evidence(
        pre_clear_execution_run={"ended_at": "2026-01-01T00:00:00+00:00"},
        clear_event={
            "metadata": {"reason_code": "startup_new_binding"},
            "occurred_at": "2026-01-01T00:00:01+00:00",
        },
        post_clear_execution_run={"started_at": "2026-01-01T00:00:02+00:00"},
    )
    assert evidence.status == "fail"
    assert any("reason_code" in v for v in evidence.violations)


def test_given_clear_event_out_of_causal_order_when_asserted_then_fails():
    evidence = verifier.assert_clear_causal_evidence(
        pre_clear_execution_run={"ended_at": "2026-01-01T00:00:05+00:00"},
        clear_event={
            "metadata": {"reason_code": "clear_restored_binding"},
            "occurred_at": "2026-01-01T00:00:01+00:00",
        },
        post_clear_execution_run={"started_at": "2026-01-01T00:00:02+00:00"},
    )
    assert evidence.status == "fail"
    assert any("causal ordering violated" in v for v in evidence.violations)


def test_given_missing_pre_clear_execution_run_when_asserted_then_fails():
    evidence = verifier.assert_clear_causal_evidence(
        pre_clear_execution_run=None,
        clear_event={
            "metadata": {"reason_code": "clear_restored_binding"},
            "occurred_at": "2026-01-01T00:00:01+00:00",
        },
        post_clear_execution_run={"started_at": "2026-01-01T00:00:02+00:00"},
    )
    assert evidence.status == "fail"
    assert any("pre_clear_execution_run" in v for v in evidence.violations)

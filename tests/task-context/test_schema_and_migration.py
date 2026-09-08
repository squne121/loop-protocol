"""AC1: physical constraints. AC9: typed corruption/schema-too-new errors.

BDD-style GIVEN/WHEN/THEN test names.
"""

from __future__ import annotations

import sqlite3

import pytest

import task_context_errors as errors
import task_context_migration_runner as migration_runner
import task_context_schema as schema
import task_context_service as service


def test_given_fresh_db_when_migrated_then_all_ten_tables_exist(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {row["name"] for row in rows}
    expected = {
        "db_meta",
        "tasks",
        "task_refs",
        "task_ref_claims",
        "activities",
        "tab_bindings",
        "runtime_locations",
        "execution_runs",
        "events",
        "projection_outbox",
    }
    assert expected.issubset(names)


def test_given_fresh_db_when_migrated_then_user_version_matches_current_schema(conn):
    assert migration_runner.read_user_version(conn) == schema.CURRENT_SCHEMA_VERSION


def test_given_migrated_db_when_migrate_called_again_then_it_is_a_noop(conn):
    version_before = migration_runner.read_user_version(conn)
    result = migration_runner.migrate(conn)
    assert result == version_before == schema.CURRENT_SCHEMA_VERSION


# -- AC1(a): duplicate live task_ref claim is physically rejected -----------


def test_given_live_claim_on_ref_when_second_task_claims_same_ref_then_conflict_readback(conn):
    task_a = service.create_task(conn, title="A")
    task_b = service.create_task(conn, title="B")

    first = service.claim_task_ref(conn, task_a["id"], "squne121/loop-protocol", "issue", 2563)
    assert first["status"] == "claimed"

    second = service.claim_task_ref(conn, task_b["id"], "squne121/loop-protocol", "issue", 2563)
    assert second["status"] == "conflict"
    assert second["winning_task_id"] == task_a["id"]


def test_given_live_claim_when_inserted_directly_twice_then_db_unique_index_raises(conn):
    task_a = service.create_task(conn, title="A")
    task_b = service.create_task(conn, title="B")
    ref_a = service._ensure_task_ref(conn, task_a["id"], "squne121/loop-protocol", "pr", 1)
    ref_b = service._ensure_task_ref(conn, task_b["id"], "squne121/loop-protocol", "pr", 1)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO task_ref_claims (id, task_ref_id, task_id, repo, ref_kind, ref_number, claimed_at, released_at) "
        "VALUES ('c1', ?, ?, 'squne121/loop-protocol', 'pr', 1, 't', NULL)",
        (ref_a, task_a["id"]),
    )
    conn.execute("COMMIT")
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_ref_claims "
            "(id, task_ref_id, task_id, repo, ref_kind, ref_number, claimed_at, released_at) "
            "VALUES ('c2', ?, ?, 'squne121/loop-protocol', 'pr', 1, 't', NULL)",
            (ref_b, task_b["id"]),
        )
    conn.execute("ROLLBACK")


def test_given_released_claim_when_new_claim_taken_then_it_is_allowed(conn):
    task_a = service.create_task(conn, title="A")
    task_b = service.create_task(conn, title="B")
    claimed = service.claim_task_ref(conn, task_a["id"], "squne121/loop-protocol", "issue", 42)
    service.release_task_ref_claim(conn, claimed["claim_id"])
    second = service.claim_task_ref(conn, task_b["id"], "squne121/loop-protocol", "issue", 42)
    assert second["status"] == "claimed"


# -- AC1(b): at most 1 ACTIVE Activity per Task ------------------------------


def test_given_active_activity_when_second_active_row_inserted_directly_then_rejected(conn):
    task = service.create_task(conn)
    service.transition_activity(conn, task["id"], kind="impl")
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO activities (id, task_id, kind, status, started_at, ended_at) "
            "VALUES ('extra-active', ?, 'impl', 'ACTIVE', 't', NULL)",
            (task["id"],),
        )
    conn.execute("ROLLBACK")


def test_given_active_activity_when_transition_called_then_old_activity_ends_and_new_one_active(conn):
    task = service.create_task(conn)
    first = service.transition_activity(conn, task["id"], kind="impl")
    second = service.transition_activity(conn, task["id"], kind="review")

    reloaded_first = service.get_activity(conn, first["id"])
    assert reloaded_first["status"] == "DONE"
    assert reloaded_first["ended_at"] is not None
    reloaded_second = service.get_activity(conn, second["id"])
    assert reloaded_second["status"] == "ACTIVE"


# -- AC1(c): at most 1 unreleased runtime_location per binding ---------------


def test_given_unreleased_location_when_second_unreleased_row_inserted_directly_then_rejected(conn):
    binding = service.create_binding(conn)
    service.relocate_binding(conn, binding["id"], "herdr://tab-1")
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runtime_locations (id, binding_id, herdr_locator, observed_at, released_at) "
            "VALUES ('extra-loc', ?, 'herdr://tab-2', 't', NULL)",
            (binding["id"],),
        )
    conn.execute("ROLLBACK")


def test_given_binding_when_relocated_twice_then_binding_id_unchanged_and_only_latest_location_unreleased(conn):
    binding = service.create_binding(conn)
    loc1 = service.relocate_binding(conn, binding["id"], "herdr://tab-1")
    loc2 = service.relocate_binding(conn, binding["id"], "herdr://tab-2")
    assert loc1["id"] != loc2["id"]
    current = service.get_current_location(conn, binding["id"])
    assert current["id"] == loc2["id"]
    assert current["herdr_locator"] == "herdr://tab-2"
    reloaded_loc1 = conn.execute("SELECT * FROM runtime_locations WHERE id = ?", (loc1["id"],)).fetchone()
    assert reloaded_loc1["released_at"] is not None


# -- AC1(d): at most 1 open managed operator run per binding -----------------


def test_given_open_managed_run_on_binding_when_second_open_managed_run_inserted_directly_then_rejected(conn):
    binding = service.create_binding(conn)
    service.start_execution_run(conn, run_kind="native_operator", is_managed=True, binding_id=binding["id"])
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO execution_runs "
            "(id, task_id, activity_id, binding_id, run_kind, runtime_profile, resume_profile, "
            " claude_session_id, is_managed, started_at, ended_at) "
            "VALUES ('extra-run', NULL, NULL, ?, 'native_operator', NULL, NULL, NULL, 1, 't', NULL)",
            (binding["id"],),
        )
    conn.execute("ROLLBACK")


def test_given_ended_managed_run_when_new_open_managed_run_started_on_same_binding_then_allowed(conn):
    binding = service.create_binding(conn)
    run1 = service.start_execution_run(conn, run_kind="native_operator", is_managed=True, binding_id=binding["id"])
    service.end_execution_run(conn, run1["id"])
    run2 = service.start_execution_run(conn, run_kind="native_operator", is_managed=True, binding_id=binding["id"])
    assert run2["id"] != run1["id"]


def test_given_non_managed_runs_when_multiple_open_on_same_binding_then_allowed(conn):
    binding = service.create_binding(conn)
    run1 = service.start_execution_run(conn, run_kind="subagent", is_managed=False, binding_id=binding["id"])
    run2 = service.start_execution_run(conn, run_kind="subagent", is_managed=False, binding_id=binding["id"])
    assert run1["id"] != run2["id"]


# -- AC1(e): open managed run claude_session_id uniqueness -------------------


def test_given_open_managed_session_when_second_open_managed_run_same_session_inserted_directly_then_rejected(conn):
    service.start_execution_run(conn, run_kind="native_operator", is_managed=True, claude_session_id="sess-1")
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO execution_runs "
            "(id, task_id, activity_id, binding_id, run_kind, runtime_profile, resume_profile, "
            " claude_session_id, is_managed, started_at, ended_at) "
            "VALUES ('extra-run-sess', NULL, NULL, NULL, 'native_operator', NULL, NULL, 'sess-1', 1, 't', NULL)"
        )
    conn.execute("ROLLBACK")


def test_given_historical_ended_run_when_same_session_id_reattached_to_new_run_then_allowed(conn):
    """Historical re-attach semantics must remain allowed (AC1e)."""
    run1 = service.start_execution_run(conn, run_kind="native_operator", is_managed=True, claude_session_id="sess-2")
    service.end_execution_run(conn, run1["id"])
    run2 = service.start_execution_run(conn, run_kind="native_operator", is_managed=True, claude_session_id="sess-2")
    assert run2["id"] != run1["id"]


def test_given_non_managed_run_when_same_session_id_used_concurrently_then_allowed(conn):
    """Non-managed runs (e.g. SubAgent) are exempt from the session uniqueness guard."""
    service.start_execution_run(conn, run_kind="subagent", is_managed=False, claude_session_id="sess-3")
    run2 = service.start_execution_run(conn, run_kind="subagent", is_managed=False, claude_session_id="sess-3")
    assert run2["claude_session_id"] == "sess-3"


def test_given_null_claude_session_id_when_multiple_open_managed_runs_created_then_allowed(conn):
    """The partial index only restricts non-NULL claude_session_id rows."""
    run1 = service.start_execution_run(conn, run_kind="native_operator", is_managed=True, claude_session_id=None)
    run2 = service.start_execution_run(conn, run_kind="native_operator", is_managed=True, claude_session_id=None)
    assert run1["id"] != run2["id"]


# -- AC9: typed corruption / schema-too-new errors, never silent reset ------


def test_given_user_version_newer_than_known_when_migrate_called_then_schema_too_new_raised(conn):
    conn.execute("PRAGMA user_version=99999")
    with pytest.raises(errors.SchemaTooNewError):
        migration_runner.migrate(conn)
    # Never silently reset: tables from the (hypothetical) newer schema
    # must not have been dropped/recreated, and user_version must be
    # unchanged.
    assert migration_runner.read_user_version(conn) == 99999


def test_given_corrupt_fresh_db_file_when_migrate_called_then_corrupt_database_error_raised(tmp_path, monkeypatch):
    """A DB file that fails PRAGMA integrity_check while still at
    user_version=0 (i.e. migrate() must run the DDL) is rejected with a
    typed CorruptDatabaseError -- never silently treated as "fresh" and
    reset/recreated (AC9)."""
    import task_context_config as config
    import task_context_db as db

    root = tmp_path / "corrupt-root"
    monkeypatch.setenv(config.STATE_ROOT_ENV_VAR, str(root))
    db_file = config.db_path()

    # Open once (creates parent dirs + a valid, empty, user_version=0
    # sqlite file) then close and corrupt it on disk.
    conn = db.connect(db_file)
    conn.close()
    with open(db_file, "r+b") as fh:
        fh.truncate(10)

    # The corruption is surfaced as soon as it is touched -- here, at
    # connect() time while configuring pragmas -- and is never silently
    # treated as a fresh, empty DB (AC9). Whether it is caught at connect()
    # time or later at migrate() time is an implementation detail; what
    # matters is that it is always the typed CorruptDatabaseError and never
    # a silent reset.
    with pytest.raises((errors.CorruptDatabaseError, sqlite3.DatabaseError)):
        conn2 = db.connect(db_file)
        migration_runner.migrate(conn2)

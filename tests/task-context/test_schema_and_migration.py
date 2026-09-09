"""AC1: physical constraints. AC9: typed corruption/schema-too-new errors.

BDD-style GIVEN/WHEN/THEN test names.
"""

from __future__ import annotations

import sqlite3

import pytest

import task_context_db as db
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
    service.start_execution_run(conn, run_kind="native_operator", binding_id=binding["id"])
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
    run1 = service.start_execution_run(conn, run_kind="native_operator", binding_id=binding["id"])
    service.end_execution_run(conn, run1["id"])
    run2 = service.start_execution_run(conn, run_kind="native_operator", binding_id=binding["id"])
    assert run2["id"] != run1["id"]


def test_given_non_managed_runs_when_multiple_open_on_same_binding_then_allowed(conn):
    binding = service.create_binding(conn)
    run1 = service.start_execution_run(conn, run_kind="subagent", binding_id=binding["id"])
    run2 = service.start_execution_run(conn, run_kind="subagent", binding_id=binding["id"])
    assert run1["id"] != run2["id"]


# -- AC1(e): open managed run claude_session_id uniqueness -------------------


def test_given_open_managed_session_when_second_open_managed_run_same_session_inserted_directly_then_rejected(conn):
    service.start_execution_run(conn, run_kind="native_operator", claude_session_id="sess-1")
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
    run1 = service.start_execution_run(conn, run_kind="native_operator", claude_session_id="sess-2")
    service.end_execution_run(conn, run1["id"])
    run2 = service.start_execution_run(conn, run_kind="native_operator", claude_session_id="sess-2")
    assert run2["id"] != run1["id"]


def test_given_non_managed_run_when_same_session_id_used_concurrently_then_allowed(conn):
    """Non-managed runs (e.g. SubAgent) are exempt from the session uniqueness guard."""
    service.start_execution_run(conn, run_kind="subagent", claude_session_id="sess-3")
    run2 = service.start_execution_run(conn, run_kind="subagent", claude_session_id="sess-3")
    assert run2["claude_session_id"] == "sess-3"


def test_given_null_claude_session_id_when_multiple_open_managed_runs_created_then_allowed(conn):
    """The partial index only restricts non-NULL claude_session_id rows."""
    run1 = service.start_execution_run(conn, run_kind="native_operator", claude_session_id=None)
    run2 = service.start_execution_run(conn, run_kind="native_operator", claude_session_id=None)
    assert run1["id"] != run2["id"]


# -- fix_delta finding 2: is_managed/run_kind CHECK is a physical invariant --


def test_given_operator_run_kind_when_raw_sql_sets_is_managed_zero_then_check_rejects(conn):
    """A raw SQL caller cannot claim run_kind='native_operator' while
    is_managed=0 -- this would otherwise silently escape the AC1(d)/(e)
    partial unique indexes."""
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO execution_runs "
            "(id, task_id, activity_id, binding_id, run_kind, runtime_profile, resume_profile, "
            " claude_session_id, is_managed, started_at, ended_at) "
            "VALUES ('bypass-managed-off', NULL, NULL, NULL, 'native_operator', NULL, NULL, NULL, 0, 't', NULL)"
        )
    conn.execute("ROLLBACK")


def test_given_subagent_run_kind_when_raw_sql_sets_is_managed_one_then_check_rejects(conn):
    """A raw SQL caller cannot impersonate a managed operator run by naming
    run_kind='subagent' with is_managed=1."""
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO execution_runs "
            "(id, task_id, activity_id, binding_id, run_kind, runtime_profile, resume_profile, "
            " claude_session_id, is_managed, started_at, ended_at) "
            "VALUES ('bypass-managed-on', NULL, NULL, NULL, 'subagent', NULL, NULL, NULL, 1, 't', NULL)"
        )
    conn.execute("ROLLBACK")


def test_given_run_kind_when_start_execution_run_then_is_managed_is_derived_not_caller_supplied(conn):
    managed = service.start_execution_run(conn, run_kind="claude_gpt")
    assert managed["is_managed"] == 1
    non_managed = service.start_execution_run(conn, run_kind="runtime_smoke")
    assert non_managed["is_managed"] == 0


# -- fix_delta finding 7b: task_ref_claims composite FK to task_refs --------


def test_given_mismatched_redundant_columns_when_raw_sql_claim_inserted_then_fk_rejects(conn):
    task_a = service.create_task(conn, title="A")
    task_b = service.create_task(conn, title="B")
    ref_a = service._ensure_task_ref(conn, task_a["id"], "squne121/loop-protocol", "issue", 9001)
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_ref_claims (id, task_ref_id, task_id, repo, ref_kind, ref_number, "
            "claimed_at, released_at) VALUES ('mismatched-claim', ?, ?, "
            "'squne121/loop-protocol', 'issue', 9001, 't', NULL)",
            (ref_a, task_b["id"]),  # task_b does not own ref_a (which belongs to task_a)
        )
    conn.execute("ROLLBACK")


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
    # matters is that it is ALWAYS the typed CorruptDatabaseError -- never a
    # silent reset, and never a raw, untyped sqlite3.DatabaseError leaking
    # through the DB boundary (fix_delta finding 6: this assertion used to
    # accept either CorruptDatabaseError or a bare sqlite3.DatabaseError,
    # which masked exactly the kind of untyped-leak regression finding 6
    # fixes; it now accepts only the typed exception).
    with pytest.raises(errors.CorruptDatabaseError):
        conn2 = db.connect(db_file)
        migration_runner.migrate(conn2)


# -- fix_delta finding 6: DB-boundary translation of post-migration corruption --


def test_given_readonly_execute_when_database_error_raised_then_translated_to_corrupt_database_error():
    """Simulates the exact gap finding 6 closes: a DB that is already at
    CURRENT_SCHEMA_VERSION (so migrate()'s cheap early-return never reaches
    its own PRAGMA integrity_check) hits corruption on a later plain read.
    `db.execute_readonly` must translate this to CorruptDatabaseError rather
    than letting a raw sqlite3.DatabaseError propagate. ``sqlite3.Connection``
    is a builtin type whose ``execute`` slot cannot be monkeypatched on a
    live instance, so a minimal stand-in object is used instead."""

    class _BoomConn:
        def execute(self, sql, params=()):
            raise sqlite3.DatabaseError("database disk image is malformed")

    with pytest.raises(errors.CorruptDatabaseError):
        db.execute_readonly(_BoomConn(), "SELECT 1")


def test_given_service_get_task_when_underlying_read_hits_database_error_then_corrupt_database_error_not_internal(
    conn,
):
    """End-to-end at the typed-service boundary (not just the db.py helper):
    a service-layer read (`get_task`, used both directly and after every
    create_task/mutation) must surface CorruptDatabaseError, never a bare
    sqlite3.DatabaseError that would fall through to the CLI's
    INTERNAL_ERROR catch-all (fix_delta finding 6). A thin proxy wraps the
    real connection (rather than monkeypatching ``conn.execute`` directly,
    which ``sqlite3.Connection``'s builtin slot does not allow) and injects
    the failure only for the specific SELECT under test."""
    task = service.create_task(conn, title="pre-corruption")

    class _FlakyConnProxy:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            if sql.strip().upper().startswith("SELECT * FROM TASKS"):
                raise sqlite3.DatabaseError("database disk image is malformed")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(errors.CorruptDatabaseError):
        service.get_task(_FlakyConnProxy(conn), task["id"])


def test_given_write_transaction_when_database_error_raised_mid_transaction_then_corrupt_database_error_and_rollback(
    conn,
):
    """A genuine (non-locked, non-IntegrityError) sqlite3.DatabaseError
    raised mid-write_transaction must roll back and surface as
    CorruptDatabaseError, not an untyped exception (fix_delta finding 6)."""
    task = service.create_task(conn)
    before = conn.execute("SELECT COUNT(*) AS c FROM activities").fetchone()["c"]

    with pytest.raises(errors.CorruptDatabaseError):
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO activities (id, task_id, kind, status, started_at, ended_at) "
                "VALUES ('corrupt-probe', ?, 'impl', 'ACTIVE', 't', NULL)",
                (task["id"],),
            )
            raise sqlite3.DatabaseError("database disk image is malformed")

    after = conn.execute("SELECT COUNT(*) AS c FROM activities").fetchone()["c"]
    assert after == before  # rolled back -- no partial row survives
    assert conn.execute("SELECT * FROM activities WHERE id = 'corrupt-probe'").fetchone() is None

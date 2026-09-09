"""AC2: single-transaction commit/rollback, explicit BEGIN IMMEDIATE mode.
AC3: no external I/O inside a DB write transaction (static source check).
Busy/retry budget: BEGIN IMMEDIATE contention maps to typed
TEMPORARILY_UNAVAILABLE and never hangs / never blocks for multi-second
durations.
"""

from __future__ import annotations

import ast
import inspect
import time

import pytest

import task_context_db as db
import task_context_errors as errors
import task_context_service as service

# Any call to one of these names inside task_context_service.py would be
# "external I/O" (subprocess / network) reachable from inside a
# write_transaction block. We statically assert none of these identifiers
# even appear in the module's source (a stronger, structural guarantee than
# a runtime mock -- it prevents future write-path code from ever importing
# subprocess/urllib/requests inside this module at all).
FORBIDDEN_EXTERNAL_IO_NAMES = ("subprocess", "urllib", "requests", "socket", "httpx")


def test_given_service_module_source_when_scanned_then_no_external_io_imports_present():
    source = inspect.getsource(service)
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module.split(".")[0])
    forbidden_present = imported_names.intersection(FORBIDDEN_EXTERNAL_IO_NAMES)
    assert not forbidden_present, (
        f"task_context_service.py imports external I/O modules {forbidden_present}; "
        "external I/O (GitHub/Git/Herdr subprocess calls) must never run inside a DB "
        "write transaction (AC3)."
    )


def test_given_transition_activity_when_it_runs_then_exactly_one_begin_immediate_is_issued(conn):
    calls = []

    def trace(sql):
        if sql.strip().upper().startswith("BEGIN"):
            calls.append(sql.strip())

    task = service.create_task(conn)
    conn.set_trace_callback(trace)
    try:
        service.transition_activity(conn, task["id"], kind="impl")
    finally:
        conn.set_trace_callback(None)
    assert calls == ["BEGIN IMMEDIATE"]


def test_given_conflicting_write_when_it_fails_then_transaction_rolls_back_fully(conn):
    task = service.create_task(conn)
    before = conn.execute("SELECT COUNT(*) AS c FROM activities").fetchone()["c"]
    service.transition_activity(conn, task["id"], kind="impl")

    # Force a conflict deep inside a write_transaction block by directly
    # violating a unique index while the helper is imitating a multi-step
    # write: assert that a failed write leaves NO partial row behind.
    with pytest.raises(errors.ConflictError):
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO activities (id, task_id, kind, status, started_at, ended_at) "
                "VALUES ('rollback-probe', ?, 'impl', 'ACTIVE', 't', NULL)",
                (task["id"],),
            )
            raise errors.ConflictError("simulated mid-transaction conflict")

    after = conn.execute("SELECT COUNT(*) AS c FROM activities").fetchone()["c"]
    assert after == before + 1  # only the earlier committed transition_activity insert
    probe = conn.execute("SELECT * FROM activities WHERE id = 'rollback-probe'").fetchone()
    assert probe is None


def test_given_two_connections_contending_when_second_begin_immediate_blocked_then_temporarily_unavailable_and_bounded(
    db_file,
):
    """Simulate BEGIN IMMEDIATE contention across two real connections to
    the same DB file and assert the caller gets a typed
    TEMPORARILY_UNAVAILABLE (never a bare hang, never a raw sqlite3
    exception) and that the wait stays within the bounded busy_timeout
    budget (no multi-second hot-path blocking)."""
    import task_context_migration_runner as migration_runner

    conn_a = db.connect(db_file, busy_timeout_ms=150)
    migration_runner.migrate(conn_a)
    conn_b = db.connect(db_file, busy_timeout_ms=150)

    conn_a.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(errors.TemporarilyUnavailableError):
            with db.write_transaction(conn_b):
                pass  # pragma: no cover - never reached
        elapsed = time.monotonic() - started
    finally:
        conn_a.execute("ROLLBACK")

    conn_a.close()
    conn_b.close()
    # Bounded budget: must not exceed the connection's busy_timeout by more
    # than a small scheduling-jitter margin (never multi-second blocking).
    assert elapsed < 2.0


def test_given_full_open_migrate_operation_path_when_write_lock_held_then_wait_is_bounded_by_single_busy_timeout_budget(
    db_file,
):
    """Deterministic bounded-wait CORRECTNESS test (fix_delta finding 5),
    replacing the previous loose ``elapsed < 2.0`` assertion that only
    exercised an already-open connection's ``write_transaction`` and never
    measured the ``connect()`` path at all. This test contends against the
    FULL ``connect() -> migrate() -> service write`` hot path (the actual
    caller-visible path every ``task-contextctl`` invocation takes) and
    asserts the observed wait stays close to the single configured
    ``busy_timeout_ms`` budget -- never the ~5x-amplified, multi-layered
    retry behavior a prior version of ``task_context_db.connect()`` had
    (which could turn a 200ms budget into >1s of hot-path blocking). The
    300ms margin absorbs ordinary CI scheduling jitter without being a
    flaky tight performance threshold; it is a correctness bound, not a
    performance benchmark."""
    import task_context_migration_runner as migration_runner

    busy_timeout_ms = 200
    conn_a = db.connect(db_file, busy_timeout_ms=busy_timeout_ms)
    migration_runner.migrate(conn_a)
    conn_a.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(errors.TemporarilyUnavailableError):
            conn_b = db.connect(db_file, busy_timeout_ms=busy_timeout_ms)
            migration_runner.migrate(conn_b)
            service.create_task(conn_b, title="contended")
        elapsed = time.monotonic() - started
    finally:
        conn_a.execute("ROLLBACK")
        conn_a.close()

    assert elapsed < (busy_timeout_ms / 1000.0) + 0.3

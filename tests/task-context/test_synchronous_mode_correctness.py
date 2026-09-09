"""AC10(a): deterministic correctness test for synchronous=NORMAL vs
synchronous=FULL -- kept separate from the (non-CI-gated) WSL2 latency
measurement documented in docs/dev/task-context.md. This test asserts
*logical* durability/correctness parity across both pragma settings; it is
NOT a performance/timing assertion (never flaky by construction)."""

from __future__ import annotations

import pytest

import task_context_db as db
import task_context_migration_runner as migration_runner
import task_context_service as service


@pytest.mark.parametrize("synchronous", ["NORMAL", "FULL"])
def test_given_synchronous_mode_when_committed_data_reopened_then_all_rows_present(
    tmp_path, synchronous, monkeypatch
):
    import task_context_config as config

    root = tmp_path / f"sync-{synchronous}"
    monkeypatch.setenv(config.STATE_ROOT_ENV_VAR, str(root))
    db_file = config.db_path()

    conn = db.connect(db_file, synchronous=synchronous)
    migration_runner.migrate(conn)
    task = service.create_task(conn, title=f"sync-{synchronous}")
    service.transition_activity(conn, task["id"], kind="impl")
    conn.close()

    reopened = db.connect(db_file, synchronous=synchronous)
    try:
        row = reopened.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone()
        assert row is not None
        assert row["title"] == f"sync-{synchronous}"
        activity_row = reopened.execute(
            "SELECT * FROM activities WHERE task_id = ? AND status = 'ACTIVE'", (task["id"],)
        ).fetchone()
        assert activity_row is not None
    finally:
        reopened.close()


@pytest.mark.parametrize("synchronous", ["NORMAL", "FULL"])
def test_given_synchronous_mode_when_wal_checkpoint_forced_then_data_survives(tmp_path, synchronous, monkeypatch):
    import task_context_config as config

    root = tmp_path / f"checkpoint-{synchronous}"
    monkeypatch.setenv(config.STATE_ROOT_ENV_VAR, str(root))
    db_file = config.db_path()

    conn = db.connect(db_file, synchronous=synchronous)
    migration_runner.migrate(conn)
    task = service.create_task(conn, title="checkpoint-test")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    reopened = db.connect(db_file, synchronous=synchronous)
    try:
        row = reopened.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone()
        assert row is not None
    finally:
        reopened.close()


def test_given_default_synchronous_when_connecting_then_it_is_normal():
    assert db.DEFAULT_SYNCHRONOUS == "NORMAL"

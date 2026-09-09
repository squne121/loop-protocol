"""AC11: multiple real OS processes opening the same fresh DB file
concurrently and running migrate() must not double-apply, must not leave a
half-migrated state, and must not silently reset a schema-too-new/corrupt
DB to empty."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import task_context_migration_runner as migration_runner
import task_context_schema as schema

_WORKER = pathlib.Path(__file__).resolve().parent / "_migration_worker.py"
_N_PROCESSES = 8


def test_given_fresh_db_when_n_processes_migrate_concurrently_then_no_double_apply_and_final_version_correct(
    db_file,
):
    db_file.parent.mkdir(parents=True, exist_ok=True)

    procs = [
        subprocess.Popen(
            [sys.executable, str(_WORKER), str(db_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(_N_PROCESSES)
    ]
    outputs = [proc.communicate(timeout=30) for proc in procs]

    for index, (proc, (stdout, stderr)) in enumerate(zip(procs, outputs)):
        assert proc.returncode == 0, f"worker {index} failed: rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"
        payload = json.loads(stdout.strip())
        assert payload.get("user_version") == schema.CURRENT_SCHEMA_VERSION, payload

    import task_context_db as db

    final_conn = db.connect(db_file)
    try:
        assert migration_runner.read_user_version(final_conn) == schema.CURRENT_SCHEMA_VERSION
        # No double-apply: each unique index / trigger name must exist
        # exactly once (CREATE INDEX/TRIGGER would itself error on
        # double-apply, but assert explicitly for defense-in-depth).
        index_rows = final_conn.execute(
            "SELECT name, COUNT(*) AS c FROM sqlite_master WHERE type = 'index' GROUP BY name HAVING c > 1"
        ).fetchall()
        assert index_rows == []
        table_rows = final_conn.execute(
            "SELECT name, COUNT(*) AS c FROM sqlite_master WHERE type = 'table' GROUP BY name HAVING c > 1"
        ).fetchall()
        assert table_rows == []
        assert db.integrity_check(final_conn)
    finally:
        final_conn.close()

"""Issue #2822 AC7 -- schema inspection for SendMessage addressability.

The AC1 canary showed a named ordinary SubAgent's ``name`` *is* observable
from the ``Agent`` tool's ``PreToolUse`` / ``PostToolUse`` ``tool_input``, so
exactly one bounded additive nullable column is added
(``execution_runs.addressable_name``, schema v4). The caller/parent session
reuses the existing nullable ``execution_runs.claude_session_id`` and the
agent identity reuses the existing ``execution_runs.agent_id``. This file pins
that DDL delta (additive, nullable, no message body / transcript storage) and
the address-resolution-only metadata allowlist, so any further column must be
an explicit, reviewed additive change.
"""

from __future__ import annotations

import sqlite3

import task_context_db as db
import task_context_migration_runner as migration_runner
import task_context_schema as schema
import task_context_service as service

# execution_runs columns at Issue #2822 baseline (main 070f99fc), in order.
_BASELINE_EXECUTION_RUN_COLUMNS = (
    "id",
    "task_id",
    "activity_id",
    "binding_id",
    "run_kind",
    "runtime_profile",
    "resume_profile",
    "claude_session_id",
    "is_managed",
    "started_at",
    "ended_at",
    "agent_id",
)
_NEW_COLUMN = "addressable_name"
# Identity metadata the addressability feature is allowed to rely on.
_ADDRESS_RESOLUTION_COLUMNS = frozenset({"claude_session_id", "agent_id", _NEW_COLUMN})
_FORBIDDEN_CONTENT_TOKENS = ("message", "body", "transcript", "terminal", "content", "prompt", "output")
_BASELINE_SCHEMA_VERSION = 3


def _columns(conn):
    return [row["name"] for row in conn.execute("PRAGMA table_info(execution_runs)")]


def test_addr_migration_ddl_delta_is_exactly_one_appended_column(conn):
    """Baseline columns are untouched and in order; the only delta is the one
    new trailing column."""
    assert tuple(_columns(conn)) == _BASELINE_EXECUTION_RUN_COLUMNS + (_NEW_COLUMN,)


def test_addr_migration_schema_version_bumped_once_with_single_statement_migration(conn):
    assert schema.CURRENT_SCHEMA_VERSION == _BASELINE_SCHEMA_VERSION + 1
    assert len(schema.MIGRATIONS[schema.CURRENT_SCHEMA_VERSION]) == 1
    statement = " ".join(schema.MIGRATIONS[schema.CURRENT_SCHEMA_VERSION][0].split()).upper()
    assert statement == "ALTER TABLE EXECUTION_RUNS ADD COLUMN ADDRESSABLE_NAME TEXT"
    # Earlier migrations were not rewritten (no drop / rename / retype).
    assert set(schema.MIGRATIONS) == {1, 2, 3, 4}


def test_addr_migration_new_name_column_is_additive_nullable_text_no_default(conn):
    info = {row["name"]: row for row in conn.execute("PRAGMA table_info(execution_runs)")}
    column = info[_NEW_COLUMN]
    assert column["type"] == "TEXT"
    assert column["notnull"] == 0
    assert column["dflt_value"] is None
    assert column["pk"] == 0
    # No stray name-like column anywhere else.
    for table in ("tab_bindings", "tasks", "activities", "events"):
        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
        assert "name" not in columns and "agent_name" not in columns and _NEW_COLUMN not in columns


def test_addr_migration_identity_metadata_is_address_resolution_only(conn):
    assert _ADDRESS_RESOLUTION_COLUMNS.issubset(_columns(conn))
    for column in _columns(conn):
        assert not any(token in column for token in _FORBIDDEN_CONTENT_TOKENS), column


def test_addr_migration_identity_columns_stay_additive_nullable(conn):
    info = {row["name"]: row for row in conn.execute("PRAGMA table_info(execution_runs)")}
    for column in _ADDRESS_RESOLUTION_COLUMNS:
        assert info[column]["notnull"] == 0
        assert info[column]["type"] == "TEXT"


def test_addr_migration_name_writer_is_bounded_and_stores_no_free_text(conn):
    """Only a bounded, printable, trimmed str is ever stored -- never a
    prompt-length / multi-line value."""
    assert service.is_valid_addressable_name("gamma")
    assert not service.is_valid_addressable_name("")
    assert not service.is_valid_addressable_name(" padded ")
    assert not service.is_valid_addressable_name("line1\nline2")
    assert not service.is_valid_addressable_name("x" * 129)
    assert not service.is_valid_addressable_name(None)
    assert not service.is_valid_addressable_name(123)
    run, _ = service.record_subagent_start(conn, claude_session_id="s1", agent_id="agent-bounded")
    assert (
        service.record_subagent_addressable_name(
            conn, claude_session_id="s1", agent_id="agent-bounded", name="x" * 5000
        )
        == "addressable_name_not_recorded_invalid_input"
    )
    assert service.get_execution_run(conn, run["id"])["addressable_name"] is None


def test_addr_migration_existing_open_subagent_unique_index_preserved(conn):
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'ux_execution_runs_open_subagent_agent_id'"
    ).fetchone()["sql"]
    assert "agent_id IS NOT NULL" in sql and "run_kind = 'subagent'" in sql and "ended_at IS NULL" in sql


def test_addr_migration_legacy_row_without_session_still_resolves_after_upgrade(conn):
    """A pre-existing (claude_session_id NULL) open subagent row written
    before session binding existed keeps resolving by agent_id."""
    service.start_execution_run(conn, run_kind="subagent", agent_id="pre-upgrade-agent")
    found = service.find_addressable_subagent_runs(conn, claude_session_id="any-session", agent_id="pre-upgrade-agent")
    assert len(found) == 1 and found[0]["claude_session_id"] is None


def test_addr_migration_v3_database_upgrades_in_place_and_keeps_old_rows(tmp_path):
    """A real v3 DB (schema before this Issue) upgrades additively: old rows
    survive with a NULL name and keep resolving by agent_id only; a name is
    never inferred for them."""
    path = tmp_path / "v3.sqlite3"
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    for version in (1, 2, 3):
        for statement in schema.MIGRATIONS[version]:
            raw.execute(statement)
        raw.execute(f"PRAGMA user_version={version}")
    raw.execute(
        "INSERT INTO execution_runs (id, run_kind, is_managed, started_at, agent_id, claude_session_id) "
        "VALUES ('run_old_bound', 'subagent', 0, '2026-01-01T00:00:00Z', 'agent-old-bound', 'sess-old'), "
        "('run_old_legacy', 'subagent', 0, '2026-01-01T00:00:01Z', 'agent-old-legacy', NULL)"
    )
    raw.commit()
    raw.close()

    upgraded = db.connect(path)
    try:
        assert migration_runner.migrate(upgraded) == schema.CURRENT_SCHEMA_VERSION
        assert migration_runner.read_user_version(upgraded) == schema.CURRENT_SCHEMA_VERSION
        assert _NEW_COLUMN in _columns(upgraded)
        rows = {r["id"]: r for r in upgraded.execute("SELECT * FROM execution_runs")}
        assert set(rows) == {"run_old_bound", "run_old_legacy"}
        assert all(r[_NEW_COLUMN] is None for r in rows.values())
        assert service.find_addressable_subagent_runs(
            upgraded, claude_session_id="sess-old", agent_id="agent-old-bound"
        )
        assert service.find_addressable_subagent_runs(
            upgraded, claude_session_id="sess-old", agent_id="agent-old-legacy"
        )
        # Old rows carry no name, so no name is ever resolvable for them.
        assert service.find_addressable_subagent_runs(upgraded, claude_session_id="sess-old", name="old") == []
        # Idempotent re-run.
        assert migration_runner.migrate(upgraded) == schema.CURRENT_SCHEMA_VERSION
    finally:
        upgraded.close()

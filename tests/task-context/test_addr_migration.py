"""Issue #2822 AC7 -- schema inspection for SendMessage addressability.

The AC1 canary found no observable `name` for an ordinary SubAgent (hooks do
not expose it), so **no DDL is added**: the caller/parent session reuses the
existing nullable ``execution_runs.claude_session_id`` and the agent identity
reuses the existing ``execution_runs.agent_id``. This file pins that empty DDL
delta and the address-resolution-only metadata allowlist, so any future column
must be an explicit, reviewed additive change.
"""

from __future__ import annotations

import task_context_schema as schema

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
# Identity metadata the addressability feature is allowed to rely on.
_ADDRESS_RESOLUTION_COLUMNS = frozenset({"claude_session_id", "agent_id"})
_FORBIDDEN_CONTENT_TOKENS = ("message", "body", "transcript", "terminal", "content", "prompt", "output")
_BASELINE_SCHEMA_VERSION = 3


def _columns(conn):
    return [row["name"] for row in conn.execute("PRAGMA table_info(execution_runs)")]


def test_addr_migration_ddl_delta_is_empty_execution_runs_columns_unchanged(conn):
    assert tuple(_columns(conn)) == _BASELINE_EXECUTION_RUN_COLUMNS


def test_addr_migration_schema_version_not_bumped_no_new_migration(conn):
    assert schema.CURRENT_SCHEMA_VERSION == _BASELINE_SCHEMA_VERSION


def test_addr_migration_no_addressable_name_column_added(conn):
    """No name column: hooks cannot observe an ordinary SubAgent's name."""
    for table in ("execution_runs", "tab_bindings"):
        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
        assert "name" not in columns and "agent_name" not in columns and "addressable_name" not in columns


def test_addr_migration_identity_metadata_is_address_resolution_only(conn):
    assert _ADDRESS_RESOLUTION_COLUMNS.issubset(_columns(conn))
    for column in _columns(conn):
        assert not any(token in column for token in _FORBIDDEN_CONTENT_TOKENS), column


def test_addr_migration_session_and_agent_columns_stay_additive_nullable(conn):
    info = {row["name"]: row for row in conn.execute("PRAGMA table_info(execution_runs)")}
    for column in _ADDRESS_RESOLUTION_COLUMNS:
        assert info[column]["notnull"] == 0
        assert info[column]["type"] == "TEXT"


def test_addr_migration_existing_open_subagent_unique_index_preserved(conn):
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'ux_execution_runs_open_subagent_agent_id'"
    ).fetchone()["sql"]
    assert "agent_id IS NOT NULL" in sql and "run_kind = 'subagent'" in sql and "ended_at IS NULL" in sql


def test_addr_migration_legacy_row_without_session_still_resolves_after_upgrade(conn):
    """A pre-existing (claude_session_id NULL) open subagent row written
    before session binding existed keeps resolving by agent_id."""
    import task_context_service as service

    service.start_execution_run(conn, run_kind="subagent", agent_id="pre-upgrade-agent")
    found = service.find_addressable_subagent_runs(conn, claude_session_id="any-session", agent_id="pre-upgrade-agent")
    assert len(found) == 1 and found[0]["claude_session_id"] is None

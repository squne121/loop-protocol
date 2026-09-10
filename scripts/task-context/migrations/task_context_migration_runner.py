"""Task Context v1 — PRAGMA user_version-based migration runner.

Ordering (see docs/dev/task-context.md ## Migration Ordering and
Concurrency):

1. Caller has already opened the connection via ``task_context_db.connect``
   (WAL/foreign_keys/synchronous/busy_timeout already set, all OUTSIDE any
   transaction).
2. ``migrate(conn)`` reads ``PRAGMA user_version`` OUTSIDE a transaction
   (cheap pre-check, avoids opening a write transaction when nothing needs
   to happen).
3. If the on-disk version is already current: no-op, return.
4. If the on-disk version is NEWER than this code's
   ``CURRENT_SCHEMA_VERSION``: typed ``SchemaTooNewError`` -- never reset
   (AC9).
5. Otherwise: run ``PRAGMA integrity_check`` OUTSIDE a transaction (detect
   corruption before attempting to migrate); typed
   ``CorruptDatabaseError`` if it fails (AC9).
6. Open ``BEGIN IMMEDIATE`` (serializes concurrent migrators -- AC11), and
   inside ONE transaction: re-read ``user_version`` (in case a concurrent
   process already migrated while we were blocked on the write lock), run
   the applicable DDL statements (one ``execute()`` call per statement --
   never ``executescript``, which would break transaction atomicity), bump
   ``user_version`` for each version applied, and COMMIT.
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_THIS_DIR)
for _dir in (_THIS_DIR, _PARENT_DIR):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import sqlite3  # noqa: E402

import task_context_errors as errors  # noqa: E402
import task_context_schema as schema  # noqa: E402


def read_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Idempotently migrate ``conn`` up to
    ``task_context_schema.CURRENT_SCHEMA_VERSION``. Returns the resulting
    ``user_version``. Safe to call from multiple concurrent processes
    against the same fresh (or partially migrated) DB file (AC11)."""
    current = read_user_version(conn)

    if current > schema.CURRENT_SCHEMA_VERSION:
        raise errors.SchemaTooNewError(
            f"on-disk user_version={current} is newer than this tool's "
            f"CURRENT_SCHEMA_VERSION={schema.CURRENT_SCHEMA_VERSION}; refusing to "
            "touch the database (never silently reset to empty).",
            on_disk_version=current,
            known_version=schema.CURRENT_SCHEMA_VERSION,
        )

    if current == schema.CURRENT_SCHEMA_VERSION:
        return current

    integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
    if not integrity_row or integrity_row[0] != "ok":
        raise errors.CorruptDatabaseError(
            "PRAGMA integrity_check failed before migration; refusing to "
            "silently reset to an empty database.",
            integrity_check_result=(integrity_row[0] if integrity_row else None),
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise errors.TemporarilyUnavailableError(
                "could not acquire write lock to run migration within the "
                "busy_timeout budget"
            ) from exc
        raise

    try:
        # Re-check inside the transaction: a concurrent process may have
        # already migrated (fully or partially, up to some intermediate
        # version) while we were blocked acquiring the write lock.
        current = read_user_version(conn)
        if current > schema.CURRENT_SCHEMA_VERSION:
            raise errors.SchemaTooNewError(
                f"on-disk user_version={current} advanced past "
                f"CURRENT_SCHEMA_VERSION={schema.CURRENT_SCHEMA_VERSION} while "
                "waiting for the write lock.",
                on_disk_version=current,
                known_version=schema.CURRENT_SCHEMA_VERSION,
            )
        for version in range(current + 1, schema.CURRENT_SCHEMA_VERSION + 1):
            for statement in schema.MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={version}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return schema.CURRENT_SCHEMA_VERSION

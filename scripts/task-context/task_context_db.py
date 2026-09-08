"""Task Context v1 — connection factory and transaction helpers.

Ordering invariant (documented in docs/dev/task-context.md
## Transaction Boundaries and WAL Initialization Ordering):

1. ``sqlite3.connect()`` (fresh connection, autocommit / no pending
   transaction).
2. ``PRAGMA busy_timeout=...`` -- single owner of the waiting budget.
3. ``PRAGMA foreign_keys=ON`` -- per-connection, before any transaction.
4. ``PRAGMA journal_mode=WAL`` -- must run outside any transaction (SQLite
   silently refuses to change journal_mode inside an active transaction).
5. ``PRAGMA synchronous=...``.

Only *after* all of the above does any caller open a ``BEGIN`` /
``BEGIN IMMEDIATE`` transaction (e.g. migrations, or a service-layer write).
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import time
from contextlib import contextmanager
from typing import Iterator

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import task_context_errors as errors  # noqa: E402

# Connection-level busy_timeout in milliseconds. This is the SINGLE owner of
# the "waiting budget" for BEGIN IMMEDIATE contention (SQLite `PRAGMA
# busy_timeout` docs: the connection retries internally, sleeping in
# increasing intervals, until this many total milliseconds have elapsed).
# Application-level retry loops must NOT stack additional sleeping on top of
# this value -- see docs/dev/task-context.md ## Busy/Retry Budget for the
# 100-250ms measurement/justification behind picking 200ms as the hot-path
# default.
DEFAULT_BUSY_TIMEOUT_MS = 200

DEFAULT_SYNCHRONOUS = "NORMAL"


def connect(
    db_file: pathlib.Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    synchronous: str = DEFAULT_SYNCHRONOUS,
) -> sqlite3.Connection:
    """Open (creating parent dirs if needed) a connection with the canonical
    pragma ordering. Does NOT run migrations -- call
    ``task_context_migration_runner.migrate(conn)`` explicitly."""
    db_file = pathlib.Path(db_file)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None => autocommit mode: the sqlite3 module never
    # opens an implicit transaction on our behalf, so every `BEGIN` /
    # `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` we issue is authoritative
    # and unambiguous (required for AC2's "transaction mode を明示する").
    try:
        conn = sqlite3.connect(
            str(db_file),
            timeout=busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise errors.CorruptDatabaseError(
            f"failed to open Task Context DB at {db_file}: {exc}"
        ) from exc

    conn.row_factory = sqlite3.Row
    # Each pragma below goes through a small manual lock-retry wrapper: the
    # `journal_mode=WAL` mode-transition on a brand-new file can itself
    # raise a retryable "database is locked"/"database is busy"
    # OperationalError under heavy concurrent first-open contention (e.g.
    # many processes racing to open the same fresh DB file, AC11), which is
    # NOT reliably absorbed by the connection-level `PRAGMA busy_timeout`
    # alone since it fires before that pragma has necessarily taken effect
    # for this specific mode-change operation. Genuine corruption
    # ("file is not a database") is a *non-retryable* `sqlite3.DatabaseError`
    # and is translated to the typed `CorruptDatabaseError` (AC9) --
    # `sqlite3.OperationalError` is a subtype of `sqlite3.DatabaseError`, so
    # it is always checked first.
    _execute_with_lock_retry(conn, f"PRAGMA busy_timeout={int(busy_timeout_ms)}", busy_timeout_ms)
    _execute_with_lock_retry(conn, "PRAGMA foreign_keys=ON", busy_timeout_ms)
    _execute_with_lock_retry(conn, "PRAGMA journal_mode=WAL", busy_timeout_ms)
    _execute_with_lock_retry(conn, f"PRAGMA synchronous={synchronous}", busy_timeout_ms)
    return conn


def _execute_with_lock_retry(conn: sqlite3.Connection, sql: str, busy_timeout_ms: int) -> None:
    deadline = time.monotonic() + (max(busy_timeout_ms, 200) / 1000.0) * 5
    while True:
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if ("locked" in str(exc).lower() or "busy" in str(exc).lower()) and time.monotonic() < deadline:
                time.sleep(0.01)
                continue
            raise errors.TemporarilyUnavailableError(
                f"could not run {sql!r} within the busy_timeout budget while connecting"
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise errors.CorruptDatabaseError(
                f"failed to configure Task Context DB connection ({sql!r}): {exc}"
            ) from exc


def integrity_check(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and row[0] == "ok"


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Open a ``BEGIN IMMEDIATE`` write transaction, translating SQLite
    busy/locked failures into the typed ``TemporarilyUnavailableError``
    (AC2, AC/'BEGIN IMMEDIATE 競合は TEMPORARILY_UNAVAILABLE 相当へ
    mapping')."""
    started = time.monotonic()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise errors.TemporarilyUnavailableError(
                "could not acquire write lock within the busy_timeout budget",
                waited_seconds=time.monotonic() - started,
            ) from exc
        raise
    try:
        yield conn
    except errors.TaskContextError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise errors.ConflictError(f"constraint violation: {exc}") from exc
    except sqlite3.OperationalError as exc:
        conn.execute("ROLLBACK")
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise errors.TemporarilyUnavailableError(
                "write blocked mid-transaction within the busy_timeout budget",
                waited_seconds=time.monotonic() - started,
            ) from exc
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

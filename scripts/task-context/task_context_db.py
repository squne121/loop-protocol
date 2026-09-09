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

Waiting-budget ownership (fix_delta finding 5): ``sqlite3.connect(...,
timeout=busy_timeout_ms / 1000.0)`` registers SQLite's own native busy
handler (equivalent to ``PRAGMA busy_timeout``) for this connection *before*
any of the pragmas below run, so every one of them -- including the
``journal_mode=WAL`` mode-transition, which itself needs a momentary
exclusive lock and can raise a retryable "database is locked"
``OperationalError`` under heavy concurrent first-open contention (AC11) --
is already covered by that single SQLite-native waiting budget. Connection
setup therefore does NOT run its own Python-level sleep/retry loop on top of
it (a prior version did, stacking a second ~5x-amplified budget on top of
the connection-level one and turning a ~200ms hot-path bound into
multi-second worst-case blocking). ``_configure_pragma`` below only
translates the *outcome* of that single wait into a typed exception; it
never adds additional waiting of its own.
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
    # Single waiting-budget owner (fix_delta finding 5): `timeout=` above
    # already registered SQLite's native busy handler for this connection,
    # so each pragma below is executed exactly once -- no additional
    # Python-level sleep/retry loop is stacked on top of it. `_configure_pragma`
    # only translates the outcome (success / still-locked-after-budget /
    # genuine corruption) into the appropriate typed exception.
    _configure_pragma(conn, f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    _configure_pragma(conn, "PRAGMA foreign_keys=ON")
    _configure_pragma(conn, "PRAGMA journal_mode=WAL")
    _configure_pragma(conn, f"PRAGMA synchronous={synchronous}")
    return conn


def _configure_pragma(conn: sqlite3.Connection, sql: str) -> None:
    """Run a one-off connection-setup pragma, translating its *outcome* into
    a typed exception. Does not retry/sleep on its own -- the connection's
    own ``timeout=``/``PRAGMA busy_timeout`` (set once, above) is the single
    owner of the waiting budget for lock contention (fix_delta finding 5).
    Genuine corruption (``sqlite3.DatabaseError`` that is not a
    locked/busy ``OperationalError``, e.g. "file is not a database") is
    never confused with transient contention and is translated immediately
    to the typed ``CorruptDatabaseError`` (AC9). ``sqlite3.OperationalError``
    is a subtype of ``sqlite3.DatabaseError``, so it is always checked
    first.
    """
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise errors.TemporarilyUnavailableError(
                f"could not run {sql!r} within the busy_timeout budget while connecting"
            ) from exc
        raise errors.CorruptDatabaseError(
            f"unexpected SQLite operational error while configuring connection ({sql!r}): {exc}"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise errors.CorruptDatabaseError(
            f"failed to configure Task Context DB connection ({sql!r}): {exc}"
        ) from exc


def integrity_check(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and row[0] == "ok"


def execute_readonly(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """DB-boundary wrapper for read-only queries executed *outside* an
    explicit ``write_transaction`` block (e.g. the ``get_*``/``read_*``
    lookups in ``task_context_service``).

    fix_delta finding 6: previously, a ``sqlite3.DatabaseError`` raised by a
    plain ``conn.execute(...)`` read call (e.g. "database disk image is
    malformed" surfacing on a DB that is already at
    ``CURRENT_SCHEMA_VERSION`` -- so ``migrate()``'s cheap version-match
    early-return never reached its own ``PRAGMA integrity_check``) had no
    typed-translation boundary to pass through and leaked as a raw
    ``sqlite3.DatabaseError`` up to the CLI's generic exception handler,
    which reports it as ``INTERNAL_ERROR`` instead of the more actionable
    ``CORRUPT_DATABASE`` (AC9). Routing every read through this helper
    closes that gap without requiring an eager ``PRAGMA integrity_check`` on
    every hot-path open.
    """
    try:
        return conn.execute(sql, params)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise errors.TemporarilyUnavailableError(f"read blocked within the busy_timeout budget: {exc}") from exc
        raise errors.CorruptDatabaseError(f"unexpected SQLite operational error on read: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        raise errors.CorruptDatabaseError(f"SQLite database error on read (possible corruption): {exc}") from exc


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
    except sqlite3.DatabaseError as exc:
        # fix_delta finding 6: a genuine corruption-class error
        # (`sqlite3.DatabaseError` that is neither the `IntegrityError`
        # constraint-violation case above nor a locked/busy
        # `OperationalError`) surfacing mid-transaction must also be typed
        # as `CorruptDatabaseError` rather than leaking as a raw sqlite3
        # exception up to the CLI's generic catch-all (AC9).
        conn.execute("ROLLBACK")
        raise errors.CorruptDatabaseError(
            f"SQLite database error mid-transaction (possible corruption): {exc}"
        ) from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

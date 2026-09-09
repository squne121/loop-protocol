"""Task Context v1 — typed error taxonomy.

Every business-level failure path in ``task_context_service`` raises one of
these typed exceptions instead of leaking a raw ``sqlite3`` exception or
silently resetting/recreating the database. The CLI (``task_contextctl.py``)
catches these and maps them to (a) a business ``code`` inside the single
JSON result object on stdout, and (b) a process exit code -- kept
deliberately separate (AC8).
"""

from __future__ import annotations


class TaskContextError(Exception):
    """Base class for all typed Task Context errors."""

    code = "INTERNAL_ERROR"
    exit_code = 1

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ValidationError(TaskContextError):
    """Request payload / argument failed validation before touching the DB."""

    code = "VALIDATION_ERROR"
    exit_code = 2


class TemporarilyUnavailableError(TaskContextError):
    """BEGIN IMMEDIATE could not acquire the write lock within the bounded
    busy-timeout budget. Callers should retry the whole operation later --
    this is never raised as a bare ``sqlite3.OperationalError``."""

    code = "TEMPORARILY_UNAVAILABLE"
    exit_code = 3


class ConflictError(TaskContextError):
    """A DB physical constraint (unique index / CHECK) rejected the write --
    e.g. duplicate live ref claim, duplicate ACTIVE Activity, duplicate
    unreleased binding location, duplicate open managed session."""

    code = "CONFLICT"
    exit_code = 4


class NotFoundError(TaskContextError):
    """Referenced entity (Task/Activity/Binding/Run/...) does not exist."""

    code = "NOT_FOUND"
    exit_code = 5


class CorruptDatabaseError(TaskContextError):
    """``PRAGMA integrity_check`` failed, or sqlite3 raised a
    corruption-class ``sqlite3.DatabaseError`` while opening/configuring the
    connection, or while executing a read or write against it (see
    ``task_context_db.connect``/``execute_readonly``/``write_transaction``
    -- fix_delta finding 6). Never silently reset to an empty DB (AC9)."""

    code = "CORRUPT_DATABASE"
    exit_code = 6


class SchemaTooNewError(TaskContextError):
    """``PRAGMA user_version`` on disk is higher than
    ``task_context_schema.CURRENT_SCHEMA_VERSION`` known to this code --
    i.e. a newer version of this tool already migrated the DB further than
    this process understands. Never silently reset to an empty DB (AC9)."""

    code = "SCHEMA_TOO_NEW"
    exit_code = 7

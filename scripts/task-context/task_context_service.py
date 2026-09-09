"""Task Context v1 — core typed service layer.

Every function that mutates the DB opens exactly one
``task_context_db.write_transaction`` (BEGIN IMMEDIATE ... COMMIT/ROLLBACK)
covering the full read-modify-write it needs (AC2). None of these functions
perform any external I/O (GitHub/Git/Herdr subprocess calls, etc.) --
callers are responsible for doing that *outside* of any call into this
module (AC3). Two-phase flows (e.g. ref claim "loser readback", projection
flush/ack) are modeled as separate calls precisely so external I/O can sit
in between them without ever being inside a write transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import task_context_db as db  # noqa: E402
import task_context_errors as errors  # noqa: E402

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


def create_task(conn: sqlite3.Connection, *, title: str | None = None) -> dict[str, Any]:
    task_id = new_id("task")
    ts = now_iso()
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at) "
            "VALUES (?, ?, 'OPEN', ?, ?)",
            (task_id, title, ts, ts),
        )
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = db.execute_readonly(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise errors.NotFoundError(f"task {task_id} not found")
    return _row_to_dict(row)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# task_refs / task_ref_claims (AC1a, AC4)
# ---------------------------------------------------------------------------


def _ensure_task_ref(conn: sqlite3.Connection, task_id: str, repo: str, ref_kind: str, ref_number: int) -> str:
    row = conn.execute(
        "SELECT id FROM task_refs WHERE task_id = ? AND repo = ? AND ref_kind = ? AND ref_number = ?",
        (task_id, repo, ref_kind, ref_number),
    ).fetchone()
    if row is not None:
        return row["id"]
    ref_id = new_id("ref")
    conn.execute(
        "INSERT INTO task_refs (id, task_id, repo, ref_kind, ref_number, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ref_id, task_id, repo, ref_kind, ref_number, now_iso()),
    )
    return ref_id


def claim_task_ref(
    conn: sqlite3.Connection, task_id: str, repo: str, ref_kind: str, ref_number: int
) -> dict[str, Any]:
    """Attempt to take a live claim on (repo, ref_kind, ref_number) for
    task_id. On success returns {"status": "claimed", "claim_id": ...}. On
    conflict (some other live claim already owns this ref) returns
    {"status": "conflict", "winning_task_id": ...} -- the "loser readback"
    flow described in the Issue #2563 Outcome, backed by the
    ``ux_task_ref_claims_live`` DB physical constraint."""
    if ref_kind not in ("issue", "pr"):
        raise errors.ValidationError(f"ref_kind must be 'issue' or 'pr', got {ref_kind!r}")

    claim_id = new_id("claim")
    try:
        with db.write_transaction(conn):
            ref_id = _ensure_task_ref(conn, task_id, repo, ref_kind, ref_number)
            conn.execute(
                "INSERT INTO task_ref_claims "
                "(id, task_ref_id, task_id, repo, ref_kind, ref_number, claimed_at, released_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (claim_id, ref_id, task_id, repo, ref_kind, ref_number, now_iso()),
            )
    except errors.ConflictError:
        winner = db.execute_readonly(
            conn,
            "SELECT task_id FROM task_ref_claims "
            "WHERE repo = ? AND ref_kind = ? AND ref_number = ? AND released_at IS NULL",
            (repo, ref_kind, ref_number),
        ).fetchone()
        return {"status": "conflict", "winning_task_id": winner["task_id"] if winner else None}
    return {"status": "claimed", "claim_id": claim_id}


def release_task_ref_claim(conn: sqlite3.Connection, claim_id: str) -> None:
    with db.write_transaction(conn):
        cur = conn.execute(
            "UPDATE task_ref_claims SET released_at = ? WHERE id = ? AND released_at IS NULL",
            (now_iso(), claim_id),
        )
        if cur.rowcount == 0:
            raise errors.NotFoundError(f"live task_ref_claim {claim_id} not found")


# ---------------------------------------------------------------------------
# activities (AC1b)
# ---------------------------------------------------------------------------


def transition_activity(conn: sqlite3.Connection, task_id: str, kind: str) -> dict[str, Any]:
    """End the Task's current ACTIVE Activity (if any) and start a new one,
    inside a single transaction (AC2)."""
    new_activity_id = new_id("activity")
    ts = now_iso()
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE activities SET status = 'DONE', ended_at = ? "
            "WHERE task_id = ? AND status = 'ACTIVE'",
            (ts, task_id),
        )
        conn.execute(
            "INSERT INTO activities (id, task_id, kind, status, started_at, ended_at) "
            "VALUES (?, ?, ?, 'ACTIVE', ?, NULL)",
            (new_activity_id, task_id, kind, ts),
        )
    return get_activity(conn, new_activity_id)


def get_activity(conn: sqlite3.Connection, activity_id: str) -> dict[str, Any]:
    row = db.execute_readonly(conn, "SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if row is None:
        raise errors.NotFoundError(f"activity {activity_id} not found")
    return _row_to_dict(row)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# tab_bindings / runtime_locations (AC1c, AC5)
# ---------------------------------------------------------------------------


def create_binding(conn: sqlite3.Connection) -> dict[str, Any]:
    binding_id = new_id("binding")
    ts = now_iso()
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO tab_bindings (id, current_claude_session_id, runtime_health, created_at, updated_at) "
            "VALUES (?, NULL, 'ACTIVE', ?, ?)",
            (binding_id, ts, ts),
        )
    return get_binding(conn, binding_id)


def get_binding(conn: sqlite3.Connection, binding_id: str) -> dict[str, Any]:
    row = db.execute_readonly(conn, "SELECT * FROM tab_bindings WHERE id = ?", (binding_id,)).fetchone()
    if row is None:
        raise errors.NotFoundError(f"binding {binding_id} not found")
    return _row_to_dict(row)  # type: ignore[return-value]


def get_binding_by_current_session(conn: sqlite3.Connection, claude_session_id: str) -> dict[str, Any]:
    """Resolve ``session_id S -> exactly one current Binding`` (fix_delta
    finding 3). Relies on the ``ux_tab_bindings_current_session`` DB
    partial-unique index to guarantee at most one row can match."""
    row = db.execute_readonly(
        conn, "SELECT * FROM tab_bindings WHERE current_claude_session_id = ?", (claude_session_id,)
    ).fetchone()
    if row is None:
        raise errors.NotFoundError(f"no binding currently claims session {claude_session_id!r}")
    return _row_to_dict(row)  # type: ignore[return-value]


def relocate_binding(conn: sqlite3.Connection, binding_id: str, herdr_locator: str) -> dict[str, Any]:
    """Release the binding's current (unreleased) location observation (if
    any) and record a new one. ``binding_id`` never changes -- only the
    mutable ``runtime_locations`` observation does (AC5)."""
    new_location_id = new_id("loc")
    ts = now_iso()
    with db.write_transaction(conn):
        get_binding(conn, binding_id)  # raises NotFoundError if missing
        conn.execute(
            "UPDATE runtime_locations SET released_at = ? WHERE binding_id = ? AND released_at IS NULL",
            (ts, binding_id),
        )
        conn.execute(
            "INSERT INTO runtime_locations (id, binding_id, herdr_locator, observed_at, released_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (new_location_id, binding_id, herdr_locator, ts),
        )
        conn.execute("UPDATE tab_bindings SET updated_at = ? WHERE id = ?", (ts, binding_id))
    return get_current_location(conn, binding_id)  # type: ignore[return-value]


def get_current_location(conn: sqlite3.Connection, binding_id: str) -> dict[str, Any] | None:
    row = db.execute_readonly(
        conn,
        "SELECT * FROM runtime_locations WHERE binding_id = ? AND released_at IS NULL",
        (binding_id,),
    ).fetchone()
    return _row_to_dict(row)


def set_binding_session(
    conn: sqlite3.Connection,
    binding_id: str,
    claude_session_id: str | None,
    *,
    execution_run_id: str | None = None,
) -> dict[str, Any]:
    """Set (or clear) the Binding's current Claude session claim.

    fix_delta finding 3 ("Claude session identity の二重SSOT"): setting a
    non-null ``claude_session_id`` requires ``execution_run_id`` to name an
    *open* (``ended_at IS NULL``), *managed* (``run_kind IN
    ('native_operator', 'claude_gpt')``) ExecutionRun already attached to
    this exact ``binding_id`` and already carrying the identical
    ``claude_session_id`` -- verified inside the same transaction as the
    write. This keeps the Binding-level "current session" copy synchronized
    with the ExecutionRun-level SSOT (the AC1e partial unique index) instead
    of letting the two drift independently. The
    ``ux_tab_bindings_current_session`` DB partial-unique index additionally
    guarantees at most one Binding can claim a given non-null session as
    "current" at a time, so ``session_id S -> exactly one current managed
    Binding/run`` is resolvable via ``get_binding_by_current_session``.
    Clearing (``claude_session_id=None``) never requires
    ``execution_run_id``.
    """
    with db.write_transaction(conn):
        get_binding(conn, binding_id)
        if claude_session_id is not None:
            if not execution_run_id:
                raise errors.ValidationError(
                    "setting a non-null claude_session_id requires execution_run_id of the "
                    "open managed ExecutionRun it is being synchronized with"
                )
            run = conn.execute(
                "SELECT claude_session_id FROM execution_runs "
                "WHERE id = ? AND binding_id = ? AND ended_at IS NULL "
                "AND run_kind IN ('native_operator', 'claude_gpt')",
                (execution_run_id, binding_id),
            ).fetchone()
            if run is None:
                raise errors.ValidationError(
                    f"execution_run_id {execution_run_id!r} is not an open managed "
                    f"ExecutionRun attached to binding {binding_id!r}"
                )
            if run["claude_session_id"] != claude_session_id:
                raise errors.ValidationError(
                    "execution_run_id's claude_session_id does not match the session "
                    "being set on the binding -- the two SSOTs must agree"
                )
        conn.execute(
            "UPDATE tab_bindings SET current_claude_session_id = ?, updated_at = ? WHERE id = ?",
            (claude_session_id, now_iso(), binding_id),
        )
    return get_binding(conn, binding_id)


def set_binding_health(conn: sqlite3.Connection, binding_id: str, runtime_health: str) -> dict[str, Any]:
    valid = {"ACTIVE", "SUSPENDED", "RESTORING", "RESTORE_BLOCKED", "DETACHED"}
    if runtime_health not in valid:
        raise errors.ValidationError(f"runtime_health must be one of {sorted(valid)}, got {runtime_health!r}")
    with db.write_transaction(conn):
        get_binding(conn, binding_id)
        conn.execute(
            "UPDATE tab_bindings SET runtime_health = ?, updated_at = ? WHERE id = ?",
            (runtime_health, now_iso(), binding_id),
        )
    return get_binding(conn, binding_id)


# ---------------------------------------------------------------------------
# execution_runs (AC1d, AC1e, AC6)
# ---------------------------------------------------------------------------

VALID_RUN_KINDS = frozenset({"native_operator", "subagent", "runtime_smoke", "claude_gpt"})

# fix_delta finding 2: is_managed is derived from run_kind, never an
# independent caller-supplied flag. This mirrors the DB-physical CHECK
# constraint in task_context_schema.py -- the two must always agree.
MANAGED_RUN_KINDS = frozenset({"native_operator", "claude_gpt"})


def _is_managed_for_run_kind(run_kind: str) -> bool:
    return run_kind in MANAGED_RUN_KINDS


def _validate_task_activity_consistency(
    conn: sqlite3.Connection, task_id: str | None, activity_id: str | None
) -> None:
    """fix_delta finding 7a: an ExecutionRun's task_id and activity_id must
    never point at different Tasks. Application-level guard; the
    ``trg_execution_runs_task_activity_consistency_*`` DB triggers are the
    physical backstop against a raw SQL write bypassing this check."""
    if task_id is None or activity_id is None:
        return
    activity_row = db.execute_readonly(
        conn, "SELECT task_id FROM activities WHERE id = ?", (activity_id,)
    ).fetchone()
    if activity_row is None:
        raise errors.NotFoundError(f"activity {activity_id} not found")
    if activity_row["task_id"] != task_id:
        raise errors.ValidationError(
            f"activity {activity_id!r} belongs to task {activity_row['task_id']!r}, "
            f"not {task_id!r} -- execution_runs.task_id/activity_id must reference the same Task"
        )


def start_execution_run(
    conn: sqlite3.Connection,
    *,
    run_kind: str,
    task_id: str | None = None,
    activity_id: str | None = None,
    binding_id: str | None = None,
    runtime_profile: str | None = None,
    resume_profile: str | None = None,
    claude_session_id: str | None = None,
) -> dict[str, Any]:
    if run_kind not in VALID_RUN_KINDS:
        raise errors.ValidationError(f"run_kind must be one of {sorted(VALID_RUN_KINDS)}, got {run_kind!r}")
    is_managed = _is_managed_for_run_kind(run_kind)
    run_id = new_id("run")
    ts = now_iso()
    with db.write_transaction(conn):
        _validate_task_activity_consistency(conn, task_id, activity_id)
        conn.execute(
            "INSERT INTO execution_runs "
            "(id, task_id, activity_id, binding_id, run_kind, runtime_profile, resume_profile, "
            " claude_session_id, is_managed, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                run_id,
                task_id,
                activity_id,
                binding_id,
                run_kind,
                runtime_profile,
                resume_profile,
                claude_session_id,
                1 if is_managed else 0,
                ts,
            ),
        )
    return get_execution_run(conn, run_id)


def get_execution_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = db.execute_readonly(conn, "SELECT * FROM execution_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise errors.NotFoundError(f"execution_run {run_id} not found")
    return _row_to_dict(row)  # type: ignore[return-value]


def attach_execution_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    task_id: str | None = None,
    activity_id: str | None = None,
    binding_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a startup-state run's Task/Activity/Binding once they become
    known (AC6). No external I/O -- caller resolves identities beforehand."""
    with db.write_transaction(conn):
        current = get_execution_run(conn, run_id)
        final_task_id = task_id if task_id is not None else current["task_id"]
        final_activity_id = activity_id if activity_id is not None else current["activity_id"]
        _validate_task_activity_consistency(conn, final_task_id, final_activity_id)
        conn.execute(
            "UPDATE execution_runs SET task_id = ?, activity_id = ?, binding_id = ? WHERE id = ?",
            (
                final_task_id,
                final_activity_id,
                binding_id if binding_id is not None else current["binding_id"],
                run_id,
            ),
        )
    return get_execution_run(conn, run_id)


def end_execution_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    with db.write_transaction(conn):
        get_execution_run(conn, run_id)
        conn.execute("UPDATE execution_runs SET ended_at = ? WHERE id = ?", (now_iso(), run_id))
    return get_execution_run(conn, run_id)


# ---------------------------------------------------------------------------
# events (AC7) -- append-only, allowlisted structured metadata only.
# ---------------------------------------------------------------------------

# Deliberately an ALLOWLIST (not a blocklist) of small structured metadata
# keys. Anything not in this set is rejected -- this is the typed-API-layer
# enforcement that raw prompt/transcript/full command/message bodies can
# never reach the `events` table (AC7), complementing the DB-layer
# append-only triggers in task_context_schema.py.
ALLOWED_EVENT_METADATA_KEYS = frozenset(
    {
        "reason_code",
        "status",
        "count",
        "ac_id",
        "operation",
        "ref_kind",
        "ref_number",
        "repo",
        "binding_id",
        "task_id",
        "activity_id",
        "execution_run_id",
        "runtime_health",
        "duration_ms",
        "exit_code",
        "run_kind",
    }
)
_MAX_EVENT_METADATA_STRING_LEN = 200


def _validate_event_metadata(metadata: dict[str, Any]) -> None:
    for key, value in metadata.items():
        if key not in ALLOWED_EVENT_METADATA_KEYS:
            raise errors.ValidationError(
                f"event metadata key {key!r} is not in the allowlist; raw prompt/transcript/"
                "full command/message body content must not be stored in events (AC7)",
                key=key,
            )
        if value is None or isinstance(value, (bool, int, float)):
            continue
        if isinstance(value, str):
            if len(value) > _MAX_EVENT_METADATA_STRING_LEN:
                raise errors.ValidationError(
                    f"event metadata value for {key!r} exceeds {_MAX_EVENT_METADATA_STRING_LEN} chars; "
                    "events must not carry raw prompt/transcript/message body content (AC7)",
                    key=key,
                )
            continue
        raise errors.ValidationError(
            f"event metadata value for {key!r} must be a small scalar (str/int/float/bool/None), "
            f"got {type(value).__name__}",
            key=key,
        )


def append_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    task_id: str | None = None,
    activity_id: str | None = None,
    binding_id: str | None = None,
    execution_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = metadata or {}
    _validate_event_metadata(metadata)
    event_id = new_id("event")
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO events "
            "(id, task_id, activity_id, binding_id, execution_run_id, event_type, metadata_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                task_id,
                activity_id,
                binding_id,
                execution_run_id,
                event_type,
                json.dumps(metadata, sort_keys=True),
                now_iso(),
            ),
        )
    row = db.execute_readonly(conn, "SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# projection_outbox (AC12) -- coalescing, revision-aware conditional ack.
#
# fix_delta finding 4: this table (and these functions) hold ONLY the
# (projection_key, desired_revision) marker. There is no payload column and
# no payload parameter -- projection_outbox is not a second SSOT for
# projection content. The actual projection consumer re-derives the content
# to project by reading canonical DB state (Task/Activity/Binding/...) at
# `desired_revision` flush time, outside of any DB write transaction.
# ---------------------------------------------------------------------------


def enqueue_projection(conn: sqlite3.Connection, projection_key: str, revision: int) -> dict[str, Any]:
    ts = now_iso()
    with db.write_transaction(conn):
        row = conn.execute(
            "SELECT desired_revision FROM projection_outbox WHERE projection_key = ?", (projection_key,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO projection_outbox (projection_key, desired_revision, enqueued_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (projection_key, revision, ts, ts),
            )
        elif revision > row["desired_revision"]:
            conn.execute(
                "UPDATE projection_outbox SET desired_revision = ?, updated_at = ? WHERE projection_key = ?",
                (revision, ts, projection_key),
            )
    return read_projection(conn, projection_key)  # type: ignore[return-value]


def read_projection(conn: sqlite3.Connection, projection_key: str) -> dict[str, Any] | None:
    row = db.execute_readonly(
        conn, "SELECT * FROM projection_outbox WHERE projection_key = ?", (projection_key,)
    ).fetchone()
    return _row_to_dict(row)


def flush_projection(conn: sqlite3.Connection, projection_key: str) -> dict[str, Any] | None:
    """Read-only: returns the current ``{projection_key, desired_revision,
    ...}`` marker snapshot for the caller to (re-derive from canonical DB
    state and) project *outside* of any DB transaction. Does NOT
    delete/ack -- call ``ack_projection`` afterwards with the
    ``desired_revision`` this call returned."""
    return read_projection(conn, projection_key)


def ack_projection(conn: sqlite3.Connection, projection_key: str, read_revision: int) -> dict[str, Any]:
    """Conditional delete: only removes the outbox row if its
    desired_revision still equals ``read_revision`` (i.e. nothing enqueued a
    newer revision between the caller's flush-read and this ack). AC12."""
    with db.write_transaction(conn):
        cur = conn.execute(
            "DELETE FROM projection_outbox WHERE projection_key = ? AND desired_revision = ?",
            (projection_key, read_revision),
        )
        acked = cur.rowcount == 1
    return {"acked": acked}

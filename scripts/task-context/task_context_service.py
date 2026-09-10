"""Task Context v1 — core typed service layer.

Every *public* function that mutates the DB opens exactly one
``task_context_db.write_transaction`` (BEGIN IMMEDIATE ... COMMIT/ROLLBACK)
covering the full read-modify-write it needs (AC2). None of these functions
perform any external I/O (GitHub/Git/Herdr subprocess calls, etc.) --
callers are responsible for doing that *outside* of any call into this
module (AC3). Two-phase flows (e.g. ref claim "loser readback", projection
flush/ack) are modeled as separate calls precisely so external I/O can sit
in between them without ever being inside a write transaction.

Transaction-internal helpers (Issue #2564 / PR #2615 fix_delta 3)
----------------------------------------------------------------
Each mutating public function is a thin ``with db.write_transaction(conn):``
wrapper around a private ``_*_tx`` helper that assumes a write transaction
is already open. That factoring exists so that the *coarse-grained* binding
operations at the bottom of this module (``bind_target_to_binding`` /
``bind_ad_hoc_task_to_binding`` / ``absorb_ref_into_task``) can perform the
whole "resolve-or-create Task -> claim ref -> ensure ACTIVE Activity ->
attach ExecutionRun -> append event -> bump projection outbox" sequence
inside **one** ``BEGIN IMMEDIATE``, instead of a chain of independently
committing primitives that could leave a half-applied rebind (e.g. an orphan
OPEN Task whose ref claim lost the race) behind. No two-phase commit and no
new coordinator is introduced -- the single ``BEGIN IMMEDIATE`` write lock is
the whole mechanism.
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


def _create_task_tx(conn: sqlite3.Connection, title: str | None) -> str:
    """Transaction-internal: INSERT a new OPEN Task, return its id."""
    task_id = new_id("task")
    ts = now_iso()
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES (?, ?, 'OPEN', ?, ?)",
        (task_id, title, ts, ts),
    )
    return task_id


def create_task(conn: sqlite3.Connection, *, title: str | None = None) -> dict[str, Any]:
    with db.write_transaction(conn):
        task_id = _create_task_tx(conn, title)
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


def _claim_task_ref_tx(
    conn: sqlite3.Connection, task_id: str, repo: str, ref_kind: str, ref_number: int
) -> str:
    """Transaction-internal: take a live claim on the ref, returning the new
    claim id. Raises (via ``ux_task_ref_claims_live``) if a live claim
    already exists -- callers holding the ``BEGIN IMMEDIATE`` write lock can
    rule that out with a preceding ``_find_live_claim_tx`` read."""
    claim_id = new_id("claim")
    ref_id = _ensure_task_ref(conn, task_id, repo, ref_kind, ref_number)
    conn.execute(
        "INSERT INTO task_ref_claims "
        "(id, task_ref_id, task_id, repo, ref_kind, ref_number, claimed_at, released_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (claim_id, ref_id, task_id, repo, ref_kind, ref_number, now_iso()),
    )
    return claim_id


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

    try:
        with db.write_transaction(conn):
            claim_id = _claim_task_ref_tx(conn, task_id, repo, ref_kind, ref_number)
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


def _transition_activity_tx(conn: sqlite3.Connection, task_id: str, kind: str) -> str:
    """Transaction-internal: end the Task's ACTIVE Activity (if any) and
    start a new one, returning the new activity id."""
    new_activity_id = new_id("activity")
    ts = now_iso()
    conn.execute(
        "UPDATE activities SET status = 'DONE', ended_at = ? WHERE task_id = ? AND status = 'ACTIVE'",
        (ts, task_id),
    )
    conn.execute(
        "INSERT INTO activities (id, task_id, kind, status, started_at, ended_at) "
        "VALUES (?, ?, ?, 'ACTIVE', ?, NULL)",
        (new_activity_id, task_id, kind, ts),
    )
    return new_activity_id


def transition_activity(conn: sqlite3.Connection, task_id: str, kind: str) -> dict[str, Any]:
    """End the Task's current ACTIVE Activity (if any) and start a new one,
    inside a single transaction (AC2)."""
    with db.write_transaction(conn):
        new_activity_id = _transition_activity_tx(conn, task_id, kind)
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


def _relocate_binding_tx(
    conn: sqlite3.Connection,
    binding_id: str,
    herdr_locator: str,
    *,
    cwd: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
) -> str:
    new_location_id = new_id("loc")
    ts = now_iso()
    get_binding(conn, binding_id)  # raises NotFoundError if missing
    conn.execute(
        "UPDATE runtime_locations SET released_at = ? WHERE binding_id = ? AND released_at IS NULL",
        (ts, binding_id),
    )
    conn.execute(
        "INSERT INTO runtime_locations "
        "(id, binding_id, herdr_locator, observed_at, released_at, cwd, worktree, branch) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
        (new_location_id, binding_id, herdr_locator, ts, cwd, worktree, branch),
    )
    conn.execute("UPDATE tab_bindings SET updated_at = ? WHERE id = ?", (ts, binding_id))
    return new_location_id


def relocate_binding(
    conn: sqlite3.Connection,
    binding_id: str,
    herdr_locator: str,
    *,
    cwd: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Release the binding's current (unreleased) location observation (if
    any) and record a new one. ``binding_id`` never changes -- only the
    mutable ``runtime_locations`` observation does (AC5).

    ``cwd`` / ``worktree`` / ``branch`` (Issue #2564 fix_delta 7) are
    display-only RuntimeLocation observations rendered by the statusLine.
    They are explicitly NOT part of Task/Binding identity and never
    participate in rebind/block decisions (AC7)."""
    with db.write_transaction(conn):
        _relocate_binding_tx(
            conn, binding_id, herdr_locator, cwd=cwd, worktree=worktree, branch=branch
        )
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
        _set_binding_session_tx(conn, binding_id, claude_session_id, execution_run_id=execution_run_id)
    return get_binding(conn, binding_id)


def _set_binding_session_tx(
    conn: sqlite3.Connection,
    binding_id: str,
    claude_session_id: str | None,
    *,
    execution_run_id: str | None = None,
) -> None:
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


def _start_execution_run_tx(
    conn: sqlite3.Connection,
    *,
    run_kind: str,
    task_id: str | None = None,
    activity_id: str | None = None,
    binding_id: str | None = None,
    runtime_profile: str | None = None,
    resume_profile: str | None = None,
    claude_session_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    if run_kind not in VALID_RUN_KINDS:
        raise errors.ValidationError(f"run_kind must be one of {sorted(VALID_RUN_KINDS)}, got {run_kind!r}")
    is_managed = _is_managed_for_run_kind(run_kind)
    run_id = new_id("run")
    ts = now_iso()
    _validate_task_activity_consistency(conn, task_id, activity_id)
    conn.execute(
        "INSERT INTO execution_runs "
        "(id, task_id, activity_id, binding_id, run_kind, runtime_profile, resume_profile, "
        " claude_session_id, is_managed, started_at, ended_at, agent_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
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
            agent_id,
        ),
    )
    return run_id


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
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Start a new ExecutionRun.

    ``agent_id`` (Issue #2564 fix_delta 5) is the SubAgent instance UUID
    Claude Code supplies on ``SubagentStart``/``SubagentStop``. Persisting it
    is what lets ``SubagentStop`` end the *exact* run that started, instead
    of guessing "the first open subagent run" and ending a concurrently
    running sibling SubAgent's run by mistake."""
    with db.write_transaction(conn):
        run_id = _start_execution_run_tx(
            conn,
            run_kind=run_kind,
            task_id=task_id,
            activity_id=activity_id,
            binding_id=binding_id,
            runtime_profile=runtime_profile,
            resume_profile=resume_profile,
            claude_session_id=claude_session_id,
            agent_id=agent_id,
        )
    return get_execution_run(conn, run_id)


def set_execution_run_session(conn: sqlite3.Connection, run_id: str, claude_session_id: str) -> dict[str, Any]:
    """Attach ``claude_session_id`` to an already-started ExecutionRun after
    the fact (Issue #2564 SessionStart recovery/new-binding flows only learn
    the actual Claude session id once the hook payload arrives, after the
    run row already exists). Still fully covered by the existing AC1(e)
    partial unique index -- SQLite re-checks it on this UPDATE the same as
    any INSERT."""
    with db.write_transaction(conn):
        get_execution_run(conn, run_id)
        conn.execute(
            "UPDATE execution_runs SET claude_session_id = ? WHERE id = ?",
            (claude_session_id, run_id),
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
        _attach_execution_run_tx(
            conn, run_id, task_id=task_id, activity_id=activity_id, binding_id=binding_id
        )
    return get_execution_run(conn, run_id)


def _attach_execution_run_tx(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    task_id: str | None = None,
    activity_id: str | None = None,
    binding_id: str | None = None,
) -> None:
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
    with db.write_transaction(conn):
        event_id = _append_event_tx(
            conn,
            event_type=event_type,
            task_id=task_id,
            activity_id=activity_id,
            binding_id=binding_id,
            execution_run_id=execution_run_id,
            metadata=metadata,
        )
    row = db.execute_readonly(conn, "SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def _append_event_tx(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    task_id: str | None = None,
    activity_id: str | None = None,
    binding_id: str | None = None,
    execution_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    metadata = metadata or {}
    _validate_event_metadata(metadata)
    event_id = new_id("event")
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
    return event_id


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


def _enqueue_projection_tx(conn: sqlite3.Connection, projection_key: str, revision: int) -> None:
    ts = now_iso()
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


def _bump_projection_tx(conn: sqlite3.Connection, binding_id: str | None) -> tuple[str | None, int | None]:
    """Transaction-internal: advance this Binding's desired projection
    revision by one, in the *same* transaction as the mutation it describes.

    Returns ``(projection_key, desired_revision)`` so the caller can hand the
    exact revision to the out-of-band projection worker it kicks off *after*
    the transaction commits (fix_delta 2's causal ``commit -> project``
    ordering)."""
    if not binding_id:
        return None, None
    projection_key = f"tab_binding:{binding_id}"
    row = conn.execute(
        "SELECT desired_revision FROM projection_outbox WHERE projection_key = ?", (projection_key,)
    ).fetchone()
    next_revision = (row["desired_revision"] + 1) if row is not None else 1
    _enqueue_projection_tx(conn, projection_key, next_revision)
    return projection_key, next_revision


def enqueue_projection(conn: sqlite3.Connection, projection_key: str, revision: int) -> dict[str, Any]:
    with db.write_transaction(conn):
        _enqueue_projection_tx(conn, projection_key, revision)
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


# ---------------------------------------------------------------------------
# Issue #2564 additive read helpers.
#
# These are pure read-only lookups (no new invariants, no external I/O) that
# the Native Claude operator hook adapter / statusLine projection needs on
# top of the #2563 core surface. They deliberately reuse the existing
# tables/columns only (no schema change) -- "current task/activity for a
# binding" is derived by joining through the binding's open *managed*
# ExecutionRun (native_operator/claude_gpt, ended_at IS NULL), since
# `tab_bindings` itself intentionally carries no task_id column (see
# docs/dev/task-context.md "runtime_locations を tab_bindings から分離した
# 理由").
# ---------------------------------------------------------------------------


def find_open_execution_runs(
    conn: sqlite3.Connection,
    *,
    task_id: str | None = None,
    activity_id: str | None = None,
    binding_id: str | None = None,
    run_kind: str | None = None,
    claude_session_id: str | None = None,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read-only lookup of currently-open (``ended_at IS NULL``)
    ExecutionRuns matching the given (optional) filters, most-recently
    started first."""
    conditions = ["ended_at IS NULL"]
    params: list[Any] = []
    if agent_id is not None:
        conditions.append("agent_id = ?")
        params.append(agent_id)
    if task_id is not None:
        conditions.append("task_id = ?")
        params.append(task_id)
    if activity_id is not None:
        conditions.append("activity_id = ?")
        params.append(activity_id)
    if binding_id is not None:
        conditions.append("binding_id = ?")
        params.append(binding_id)
    if run_kind is not None:
        conditions.append("run_kind = ?")
        params.append(run_kind)
    if claude_session_id is not None:
        conditions.append("claude_session_id = ?")
        params.append(claude_session_id)
    sql = "SELECT * FROM execution_runs WHERE " + " AND ".join(conditions) + " ORDER BY started_at DESC"
    rows = db.execute_readonly(conn, sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def get_current_task_activity_for_binding(
    conn: sqlite3.Connection, binding_id: str
) -> tuple[str | None, str | None, str | None]:
    """Resolve ``binding_id -> (task_id, activity_id, execution_run_id)`` via
    the binding's currently open *managed* ExecutionRun (native_operator /
    claude_gpt). Returns ``(None, None, None)`` if the binding has no open
    managed run yet (e.g. a freshly created, still-unbound Tab)."""
    for run_kind in MANAGED_RUN_KINDS:
        runs = find_open_execution_runs(conn, binding_id=binding_id, run_kind=run_kind)
        if runs:
            run = runs[0]
            return run["task_id"], run["activity_id"], run["id"]
    return None, None, None


def get_most_recent_execution_run_for_binding(
    conn: sqlite3.Connection, binding_id: str, *, run_kind: str | None = None
) -> dict[str, Any] | None:
    """Most recently started ExecutionRun for ``binding_id`` regardless of
    whether it has ended -- used to recover the last-known Task/Activity
    identity for a Binding whose managed run already ended cleanly (e.g.
    `/quit` -> SessionEnd already called ``end_execution_run``) so that a
    later `SessionStart` restore keeps the same Task/Activity (AC3)."""
    conditions = ["binding_id = ?"]
    params: list[Any] = [binding_id]
    if run_kind is not None:
        conditions.append("run_kind = ?")
        params.append(run_kind)
    sql = "SELECT * FROM execution_runs WHERE " + " AND ".join(conditions) + " ORDER BY started_at DESC LIMIT 1"
    row = db.execute_readonly(conn, sql, tuple(params)).fetchone()
    return _row_to_dict(row)


def get_active_activity_for_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = db.execute_readonly(
        conn, "SELECT * FROM activities WHERE task_id = ? AND status = 'ACTIVE'", (task_id,)
    ).fetchone()
    return _row_to_dict(row)


def get_binding_by_current_location(conn: sqlite3.Connection, herdr_locator: str) -> dict[str, Any] | None:
    """Resolve the Binding (if any) whose *current* (unreleased)
    RuntimeLocation observation matches ``herdr_locator`` -- used by
    `SessionStart` `startup`/`resume` to deterministically recover a
    suspended Binding for the same live Herdr Tab (AC3)."""
    row = db.execute_readonly(
        conn,
        "SELECT tb.* FROM tab_bindings tb "
        "JOIN runtime_locations rl ON rl.binding_id = tb.id "
        "WHERE rl.herdr_locator = ? AND rl.released_at IS NULL",
        (herdr_locator,),
    ).fetchone()
    return _row_to_dict(row)


def find_live_claim(conn: sqlite3.Connection, repo: str, ref_kind: str, ref_number: int) -> dict[str, Any] | None:
    """Read-only lookup of the live (unreleased) claim, if any, on
    ``(repo, ref_kind, ref_number)``."""
    row = db.execute_readonly(
        conn,
        "SELECT * FROM task_ref_claims WHERE repo = ? AND ref_kind = ? AND ref_number = ? AND released_at IS NULL",
        (repo, ref_kind, ref_number),
    ).fetchone()
    return _row_to_dict(row)


def count_live_task_ref_claims(conn: sqlite3.Connection, task_id: str) -> int:
    """Number of live (unreleased) ref claims currently owned by ``task_id``
    -- used to detect a "provisional/absorbent" ad-hoc Task (0 live refs)."""
    row = db.execute_readonly(
        conn, "SELECT COUNT(*) AS c FROM task_ref_claims WHERE task_id = ? AND released_at IS NULL", (task_id,)
    ).fetchone()
    return int(row["c"])


def list_live_task_refs(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = db.execute_readonly(
        conn,
        "SELECT repo, ref_kind, ref_number FROM task_ref_claims WHERE task_id = ? AND released_at IS NULL "
        "ORDER BY claimed_at ASC",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_current_projection_for_session(conn: sqlite3.Connection, claude_session_id: str) -> dict[str, Any]:
    """Read-only ``session_id -> Binding -> Task / Activity / RuntimeLocation
    / runtime health`` join for the statusLine `query current` projection
    (AC9). Raises ``errors.NotFoundError`` if no Binding currently claims
    ``claude_session_id`` -- callers (the read-only CLI path) turn that into
    a degraded/empty projection rather than propagating an error to a
    statusLine renderer.

    ``attention`` is an intentional forward-compatible placeholder (``None``)
    -- the actual Attention signal is produced by the workflow-trusted
    completion-signal producer, which is explicitly Out of Scope for this
    Issue (#2565 child)."""
    binding = get_binding_by_current_session(conn, claude_session_id)
    task_id, activity_id, execution_run_id = get_current_task_activity_for_binding(conn, binding["id"])
    task = get_task(conn, task_id) if task_id else None
    activity = get_activity(conn, activity_id) if activity_id else None
    location = get_current_location(conn, binding["id"])
    task_refs = list_live_task_refs(conn, task_id) if task_id else []
    return {
        "binding": binding,
        "task": task,
        "activity": activity,
        "runtime_location": location,
        "task_refs": task_refs,
        "execution_run_id": execution_run_id,
        "attention": None,
    }


# ---------------------------------------------------------------------------
# Coarse-grained atomic binding operations (Issue #2564 / PR #2615 fix_delta 3)
#
# The Native operator hook flows used to call create_task -> claim_task_ref ->
# transition_activity -> attach_execution_run -> append_event ->
# enqueue_projection as a chain of *independently committing* primitives. That
# chain is not atomic: two operators racing to claim the same Issue could both
# commit `create_task()` before either claimed the ref, leaving the loser's
# freshly created OPEN Task behind as an orphan; and a crash between any two
# links left a half-applied rebind.
#
# The operations below collapse the whole sequence into ONE `BEGIN IMMEDIATE`
# transaction. Because `BEGIN IMMEDIATE` takes the database write lock up
# front, the "is this ref already live-claimed?" read inside the transaction is
# authoritative: a concurrent operator either has not started yet (and will
# block until we commit, then see our claim) or has already committed (and we
# see its claim). No task is ever created for a ref that another Task wins.
#
# This is deliberately NOT two-phase commit and introduces no new coordinator:
# the single SQLite write lock is the entire mechanism.
# ---------------------------------------------------------------------------


def _find_live_claim_tx(
    conn: sqlite3.Connection, repo: str, ref_kind: str, ref_number: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM task_ref_claims WHERE repo = ? AND ref_kind = ? AND ref_number = ? AND released_at IS NULL",
        (repo, ref_kind, ref_number),
    ).fetchone()
    return _row_to_dict(row)


def _resolve_or_create_task_for_target_tx(
    conn: sqlite3.Connection, repo: str, ref_kind: str, ref_number: int
) -> str:
    """Transaction-internal resolve-or-create. Safe against the concurrent
    "both create, one loses the claim" orphan because the enclosing
    ``BEGIN IMMEDIATE`` already holds the write lock when the live-claim read
    below runs."""
    live = _find_live_claim_tx(conn, repo, ref_kind, ref_number)
    if live is not None:
        return live["task_id"]
    task_id = _create_task_tx(conn, f"{repo}#{ref_number}")
    _claim_task_ref_tx(conn, task_id, repo, ref_kind, ref_number)
    return task_id


def _ensure_active_activity_tx(conn: sqlite3.Connection, task_id: str, kind: str) -> str:
    row = conn.execute(
        "SELECT id FROM activities WHERE task_id = ? AND status = 'ACTIVE'", (task_id,)
    ).fetchone()
    if row is not None:
        return row["id"]
    return _transition_activity_tx(conn, task_id, kind)


def _attach_or_start_binding_run_tx(
    conn: sqlite3.Connection,
    binding_id: str,
    task_id: str,
    activity_id: str,
    execution_run_id: str | None,
) -> str:
    if execution_run_id is not None:
        _attach_execution_run_tx(conn, execution_run_id, task_id=task_id, activity_id=activity_id)
        return execution_run_id
    # No open managed run on this binding yet (SessionStart normally starts
    # one) -- degrade gracefully by starting one rather than raising.
    run_id = _start_execution_run_tx(
        conn, run_kind="native_operator", task_id=task_id, activity_id=activity_id, binding_id=binding_id
    )
    run_session = conn.execute(
        "SELECT claude_session_id FROM execution_runs WHERE id = ?", (run_id,)
    ).fetchone()
    _set_binding_session_tx(
        conn, binding_id, run_session["claude_session_id"] if run_session else None, execution_run_id=run_id
    )
    return run_id


def _finish_binding_mutation_tx(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
    task_id: str,
    activity_id: str,
    execution_run_id: str | None,
    event_type: str,
    reason_code: str,
) -> dict[str, Any]:
    run_id = _attach_or_start_binding_run_tx(conn, binding_id, task_id, activity_id, execution_run_id)
    _append_event_tx(
        conn,
        event_type=event_type,
        task_id=task_id,
        activity_id=activity_id,
        binding_id=binding_id,
        execution_run_id=run_id,
        metadata={"reason_code": reason_code, "status": "pass"},
    )
    projection_key, projection_revision = _bump_projection_tx(conn, binding_id)
    return {
        "task_id": task_id,
        "activity_id": activity_id,
        "execution_run_id": run_id,
        "projection_key": projection_key,
        "projection_revision": projection_revision,
    }


def bind_target_to_binding(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
    execution_run_id: str | None,
    repo: str,
    ref_kind: str,
    ref_number: int,
    reason_code: str,
    event_type: str = "hook:UserPromptSubmit",
    activity_kind: str = "native_operator",
) -> dict[str, Any]:
    """Atomically point ``binding_id`` at the Task that owns
    ``(repo, ref_kind, ref_number)`` -- creating the Task and claiming the
    ref if nobody owns it yet -- ensure it has an ACTIVE Activity, attach the
    binding's ExecutionRun, append the lifecycle event, and bump the
    projection outbox. Used for autobind, terminal-activity advance/rebind
    and explicit ``/task <github-ref>`` rebind."""
    with db.write_transaction(conn):
        task_id = _resolve_or_create_task_for_target_tx(conn, repo, ref_kind, ref_number)
        activity_id = _ensure_active_activity_tx(conn, task_id, activity_kind)
        return _finish_binding_mutation_tx(
            conn,
            binding_id=binding_id,
            task_id=task_id,
            activity_id=activity_id,
            execution_run_id=execution_run_id,
            event_type=event_type,
            reason_code=reason_code,
        )


def bind_ad_hoc_task_to_binding(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
    execution_run_id: str | None,
    title: str,
    reason_code: str,
    event_type: str = "hook:UserPromptSubmit",
    activity_kind: str = "native_operator",
) -> dict[str, Any]:
    """Atomically create an ad-hoc (0 GitHub refs, provisional/absorbent)
    Task and point ``binding_id`` at it -- explicit ``/task <free text>``."""
    with db.write_transaction(conn):
        task_id = _create_task_tx(conn, title)
        activity_id = _ensure_active_activity_tx(conn, task_id, activity_kind)
        return _finish_binding_mutation_tx(
            conn,
            binding_id=binding_id,
            task_id=task_id,
            activity_id=activity_id,
            execution_run_id=execution_run_id,
            event_type=event_type,
            reason_code=reason_code,
        )


def absorb_ref_into_task(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
    execution_run_id: str | None,
    task_id: str,
    repo: str,
    ref_kind: str,
    ref_number: int,
    reason_code: str,
    event_type: str = "hook:UserPromptSubmit",
    activity_kind: str = "native_operator",
) -> dict[str, Any]:
    """AC4 provisional/absorbent claim: atomically let the current ACTIVE
    Task (which holds 0 live refs) claim its first high-confidence primary
    GitHub ref, instead of treating it as a different-Task rebind.

    If another Task won the same ref in between (only possible before this
    transaction acquired the write lock), the winner is honoured and the
    binding follows it -- never a partially applied claim."""
    with db.write_transaction(conn):
        live = _find_live_claim_tx(conn, repo, ref_kind, ref_number)
        if live is None:
            _claim_task_ref_tx(conn, task_id, repo, ref_kind, ref_number)
            resolved_task_id = task_id
        else:
            resolved_task_id = live["task_id"]
        activity_id = _ensure_active_activity_tx(conn, resolved_task_id, activity_kind)
        return _finish_binding_mutation_tx(
            conn,
            binding_id=binding_id,
            task_id=resolved_task_id,
            activity_id=activity_id,
            execution_run_id=execution_run_id,
            event_type=event_type,
            reason_code=reason_code,
        )

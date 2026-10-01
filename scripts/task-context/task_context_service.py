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

import task_context_config  # noqa: E402
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


def _create_binding_tx(conn: sqlite3.Connection) -> str:
    """Transaction-internal: INSERT a new Binding, return its id."""
    binding_id = new_id("binding")
    ts = now_iso()
    conn.execute(
        "INSERT INTO tab_bindings (id, current_claude_session_id, runtime_health, created_at, updated_at) "
        "VALUES (?, NULL, 'ACTIVE', ?, ?)",
        (binding_id, ts, ts),
    )
    return binding_id


def create_binding(conn: sqlite3.Connection) -> dict[str, Any]:
    with db.write_transaction(conn):
        binding_id = _create_binding_tx(conn)
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
    evict_foreign_holder: bool = True,
) -> str:
    new_location_id = new_id("loc")
    ts = now_iso()
    get_binding(conn, binding_id)  # raises NotFoundError if missing
    conn.execute(
        "UPDATE runtime_locations SET released_at = ? WHERE binding_id = ? AND released_at IS NULL",
        (ts, binding_id),
    )
    # PR #2731 review fix_delta P1-3 ("locatorの再割り当て時、同じpaneを
    # 2つのBindingが所有したままになる"): the UPDATE above only ever
    # releases THIS binding's own prior location. If the destination
    # ``herdr_locator`` is still held (unreleased) by a DIFFERENT binding
    # (e.g. a stale claim left behind by a Binding that cold-restarted into
    # a different locator, or a SUSPENDED binding whose old pane got
    # reassigned), that stale claim was never released -- the DB unique
    # constraint is per-binding, not per-locator, so a double-claim on the
    # same locator was silently accepted. Detach ONLY that other binding's
    # location OBSERVATION row here -- this must never terminate/release
    # the other binding's Task/Activity/Binding semantic identity
    # (runtime_health, execution_runs, etc. are untouched).
    #
    # PR #2795 review fix_delta P1-B ("同じPaneのforkをbootstrapすると、
    # 親のRuntimeLocationを解除してしまう"): this eviction is only correct
    # for a genuine *reassignment* of an existing Binding's own locator
    # (SessionStart cold-restart recovery, CwdChanged) -- callers that are
    # instead bootstrapping a brand-new, independent Binding onto a locator
    # that may still be legitimately held by a live parent/sibling Binding
    # (e.g. `fork`'s by-design non-inheritance bootstrap) must pass
    # ``evict_foreign_holder=False`` so creating the new Binding never
    # mutates the other Binding's location observation. Physical Pane
    # ownership transfer and independent-Binding creation are deliberately
    # kept separate operations -- this flag is how a caller opts into the
    # (destructive to the *other* Binding's location) former.
    if evict_foreign_holder:
        conn.execute(
            "UPDATE runtime_locations SET released_at = ? "
            "WHERE herdr_locator = ? AND binding_id != ? AND released_at IS NULL",
            (ts, herdr_locator, binding_id),
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
    evict_foreign_holder: bool = True,
) -> dict[str, Any]:
    """Release the binding's current (unreleased) location observation (if
    any) and record a new one. ``binding_id`` never changes -- only the
    mutable ``runtime_locations`` observation does (AC5).

    ``cwd`` / ``worktree`` / ``branch`` (Issue #2564 fix_delta 7) are
    display-only RuntimeLocation observations rendered by the statusLine.
    They are explicitly NOT part of Task/Binding identity and never
    participate in rebind/block decisions (AC7).

    ``evict_foreign_holder`` (PR #2795 review fix_delta P1-B, default
    ``True`` to preserve the existing SessionStart/CwdChanged reassignment
    contract): set ``False`` when relocating a *newly-bootstrapped*,
    independent Binding so this call never releases a different Binding's
    still-live location observation on the same locator."""
    with db.write_transaction(conn):
        _relocate_binding_tx(
            conn,
            binding_id,
            herdr_locator,
            cwd=cwd,
            worktree=worktree,
            branch=branch,
            evict_foreign_holder=evict_foreign_holder,
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


VALID_RUNTIME_HEALTH = frozenset({"ACTIVE", "SUSPENDED", "RESTORING", "RESTORE_BLOCKED", "DETACHED"})


def _set_binding_health_tx(conn: sqlite3.Connection, binding_id: str, runtime_health: str) -> None:
    """Transaction-internal: assumes a write transaction (``BEGIN
    IMMEDIATE``) is already open. Factored out (PR #2731 review fix_delta
    P2-1) so callers that need to combine a *read* (e.g.
    ``classify_for_resume``) with this write inside a SINGLE atomic
    transaction -- rather than the read committing separately before this
    write opens its own -- can call this helper directly instead of nesting
    the public ``set_binding_health`` wrapper's own ``BEGIN IMMEDIATE``
    (SQLite does not support nested transactions on one connection)."""
    if runtime_health not in VALID_RUNTIME_HEALTH:
        raise errors.ValidationError(
            f"runtime_health must be one of {sorted(VALID_RUNTIME_HEALTH)}, got {runtime_health!r}"
        )
    get_binding(conn, binding_id)
    conn.execute(
        "UPDATE tab_bindings SET runtime_health = ?, updated_at = ? WHERE id = ?",
        (runtime_health, now_iso(), binding_id),
    )


def set_binding_health(conn: sqlite3.Connection, binding_id: str, runtime_health: str) -> dict[str, Any]:
    with db.write_transaction(conn):
        _set_binding_health_tx(conn, binding_id, runtime_health)
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


def record_subagent_start(
    conn: sqlite3.Connection,
    *,
    task_id: str | None = None,
    activity_id: str | None = None,
    claude_session_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Idempotent, session-scoped ``SubagentStart`` bookkeeping (Issue #2822
    AC4 refire contract + OWNER Finding 4). Returns ``(run, created)``.

    Claude Code fires ``SubagentStart`` not only when an Agent is launched but
    also on subagent resume and whenever an in-process teammate processes a
    new message, so the same ``(session, agent_id)`` can be reported again
    while its run is still open:

    - ``agent_id`` given and an *open* ``run_kind='subagent'`` row already
      carries it **for the same** ``claude_session_id`` -> no-op, return the
      existing row (``created=False``). No ``IntegrityError`` from
      ``ux_execution_runs_open_subagent_agent_id``.
    - ``agent_id`` given and only *ended* rows carry it -> ``ended_at`` is
      never touched (no re-open); a new run row is inserted.
    - ``agent_id`` given, a *bound* caller session, and an open row with that
      agent_id belongs to a **different** session (or is an unbound legacy
      row) -> fail-safe non-fatal no-op: ``(None, False)``. The other row is
      neither rewritten nor taken over, and no row is inserted (the
      pre-existing partial unique index would reject it anyway). Claude Code
      documents ``agent_id`` only as a "unique identifier" and does not
      promise cross-session uniqueness, so the repository contract scopes
      identity to ``(claude_session_id, agent_id)``.
    - ``claude_session_id`` NULL (legacy / unbound caller) -> the previous
      agent_id-only open-row match is kept for compatibility.
    - no ``agent_id`` -> always a new row (unchanged legacy behavior; a
      SubAgent that supplies no identity cannot be deduplicated).

    ``claude_session_id`` is stored on the row as the *parent/caller* session
    (hook common ``session_id``), never as an operator session -- see
    ``docs/dev/task-context.md`` and the reader-side protection in
    ``task_context_workflow_signals._classify_origin_candidates``.

    Check + insert share one ``BEGIN IMMEDIATE`` transaction, so two
    concurrent refires cannot both pass the open-row check."""
    try:
        with db.write_transaction(conn):
            if agent_id is not None:
                existing = conn.execute(
                    "SELECT id, claude_session_id FROM execution_runs "
                    "WHERE run_kind = 'subagent' AND agent_id = ? AND ended_at IS NULL "
                    "ORDER BY started_at DESC LIMIT 1",
                    (agent_id,),
                ).fetchone()
                if existing is not None:
                    if claude_session_id is None or existing["claude_session_id"] == claude_session_id:
                        return get_execution_run(conn, existing["id"]), False
                    return None, False
            run_id = _start_execution_run_tx(
                conn,
                run_kind="subagent",
                task_id=task_id,
                activity_id=activity_id,
                claude_session_id=claude_session_id,
                agent_id=agent_id,
            )
    except errors.ConflictError:
        # Belt and braces: a concurrent writer claimed the same open agent_id
        # between the check and the insert. Never leak out of the hook.
        return None, False
    return get_execution_run(conn, run_id), True


_ADDRESSABLE_NAME_MAX_LEN = 128


def is_valid_addressable_name(name: object) -> bool:
    """Bounded, printable, non-empty ``str`` only (address metadata, never
    free text): at most 128 characters and no control characters."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= _ADDRESSABLE_NAME_MAX_LEN
        and name.isprintable()
        and name == name.strip()
    )


def record_subagent_addressable_name(
    conn: sqlite3.Connection,
    *,
    claude_session_id: str | None,
    agent_id: str | None,
    name: object,
    task_id: str | None = None,
    activity_id: str | None = None,
) -> str:
    """Bind the ``Agent`` tool ``name`` to the SubAgent ExecutionRun(s) of
    exactly ``(claude_session_id, agent_id)`` (Issue #2822, written by the
    ``PostToolUse:Agent`` adapter). Returns a diagnostic reason code.

    Claude Code does not guarantee the relative order of ``SubagentStart``
    and ``PostToolUse:Agent`` (they are separate hook processes fired at
    almost the same instant), so the binding must not depend on it:

    - A ``run_kind='subagent'`` row of ``(claude_session_id, agent_id)``
      already exists (``SubagentStart`` won the race): its ``addressable_name``
      is updated (ended or not; a refire history of one agent_id is one
      identity).
    - No such row yet (``PostToolUse`` won the race): exactly one **ended**
      row carrying the name is inserted (``binding_id`` NULL, parent
      Task/Activity as resolved by the caller, ``ended_at`` = insert time),
      inside the same ``BEGIN IMMEDIATE`` transaction as the existence check.
      Being ended it can never leak as an open run nor collide with
      ``ux_execution_runs_open_subagent_agent_id``. The ``SubagentStart``
      that arrives afterwards finds no open row and follows its normal
      dedupe contract (a new open row; ``ended_at`` of the ended row is never
      touched), so the two hooks converge on the same addressable identity.
    - An *open* row with this ``agent_id`` belongs to a different session (or
      is an unbound legacy row): nothing is written and nothing is taken
      over -- identity is scoped to ``(claude_session_id, agent_id)`` and the
      destination stays ASK (fail-safe).

    Other sessions' rows are never touched. Invalid input is a no-op."""
    if not claude_session_id or not agent_id or not is_valid_addressable_name(name):
        return "addressable_name_not_recorded_invalid_input"
    with db.write_transaction(conn):
        cursor = conn.execute(
            "UPDATE execution_runs SET addressable_name = ? "
            "WHERE run_kind = 'subagent' AND claude_session_id = ? AND agent_id = ?",
            (name, claude_session_id, agent_id),
        )
        if cursor.rowcount:
            return "addressable_name_recorded"
        foreign_open = conn.execute(
            "SELECT 1 FROM execution_runs WHERE run_kind = 'subagent' AND agent_id = ? "
            "AND ended_at IS NULL LIMIT 1",
            (agent_id,),
        ).fetchone()
        if foreign_open is not None:
            return "addressable_name_agent_id_open_in_other_session"
        run_id = _start_execution_run_tx(
            conn,
            run_kind="subagent",
            task_id=task_id,
            activity_id=activity_id,
            claude_session_id=claude_session_id,
            agent_id=agent_id,
        )
        conn.execute(
            "UPDATE execution_runs SET addressable_name = ?, ended_at = ? WHERE id = ?",
            (name, now_iso(), run_id),
        )
    return "addressable_name_recorded_ended_row"


def _set_execution_run_session_tx(conn: sqlite3.Connection, run_id: str, claude_session_id: str) -> None:
    """Transaction-internal counterpart of ``set_execution_run_session`` (PR
    #2731 review fix_delta Finding 3 follow-up factoring -- see
    ``_set_binding_health_tx``)."""
    get_execution_run(conn, run_id)
    conn.execute(
        "UPDATE execution_runs SET claude_session_id = ? WHERE id = ?",
        (claude_session_id, run_id),
    )


def set_execution_run_session(conn: sqlite3.Connection, run_id: str, claude_session_id: str) -> dict[str, Any]:
    """Attach ``claude_session_id`` to an already-started ExecutionRun after
    the fact (Issue #2564 SessionStart recovery/new-binding flows only learn
    the actual Claude session id once the hook payload arrives, after the
    run row already exists). Still fully covered by the existing AC1(e)
    partial unique index -- SQLite re-checks it on this UPDATE the same as
    any INSERT."""
    with db.write_transaction(conn):
        _set_execution_run_session_tx(conn, run_id, claude_session_id)
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


def _end_execution_run_tx(conn: sqlite3.Connection, run_id: str) -> None:
    """Transaction-internal counterpart of ``end_execution_run`` (PR #2731
    review fix_delta P2-1 factoring -- see ``_set_binding_health_tx``)."""
    get_execution_run(conn, run_id)
    conn.execute("UPDATE execution_runs SET ended_at = ? WHERE id = ?", (now_iso(), run_id))


def end_execution_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    with db.write_transaction(conn):
        _end_execution_run_tx(conn, run_id)
    return get_execution_run(conn, run_id)


# ---------------------------------------------------------------------------
# SessionStart restore-ACK-success bundling (PR #2731 review fix_delta
# Finding 3 follow-up -- see module docstring "Transaction-internal
# helpers")
# ---------------------------------------------------------------------------
#
# ``task_context_hook_flows.on_session_start()``'s restore branch used to
# call ``end_execution_run`` / ``start_execution_run`` / ``relocate_binding``
# / ``set_binding_health`` (and, when a ``claude_session_id`` is already
# known, ``set_execution_run_session`` / ``set_binding_session``) as up to
# six separate public functions, each opening and committing its own
# ``BEGIN IMMEDIATE``. A process death between any two of those commits left
# the restore half-applied (e.g. the stale run closed but no new run
# started, or the new run started but the Binding never returned to
# ACTIVE). ``_complete_restore_tx`` bundles the whole sequence into the
# SINGLE already-open write transaction its public wrapper
# ``complete_session_start_restore`` holds, reusing the existing
# transaction-internal ``_*_tx`` helpers exactly as ``prepare_managed_resume``
# already does for its own classify+transition bundling (P2-1) -- no new
# schema, lock file, or lease table is introduced.


def _complete_restore_tx(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
    stale_run_ids: list[str],
    run_kind: str,
    task_id: str | None,
    activity_id: str | None,
    runtime_profile: str | None,
    resume_profile: str | None,
    herdr_locator: str,
    cwd: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
    claude_session_id: str | None = None,
) -> str:
    """Transaction-internal: assumes a write transaction (``BEGIN
    IMMEDIATE``) is already open. Bundles old-run close -> new-run start ->
    locator detach/re-home -> Binding ACTIVE -> (optional) session attach
    into one all-or-nothing unit; an exception raised partway through rolls
    back every step already applied within this same transaction (see
    ``task_context_db.write_transaction``'s bare ``except Exception:
    ROLLBACK``)."""
    for stale_run_id in stale_run_ids:
        _end_execution_run_tx(conn, stale_run_id)
    run_id = _start_execution_run_tx(
        conn,
        run_kind=run_kind,
        task_id=task_id,
        activity_id=activity_id,
        binding_id=binding_id,
        runtime_profile=runtime_profile,
        resume_profile=resume_profile,
    )
    _relocate_binding_tx(conn, binding_id, herdr_locator, cwd=cwd, worktree=worktree, branch=branch)
    _set_binding_health_tx(conn, binding_id, "ACTIVE")
    if claude_session_id:
        _set_execution_run_session_tx(conn, run_id, claude_session_id)
        _set_binding_session_tx(conn, binding_id, claude_session_id, execution_run_id=run_id)
    return run_id


def complete_session_start_restore(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
    stale_run_ids: list[str],
    run_kind: str,
    task_id: str | None = None,
    activity_id: str | None = None,
    runtime_profile: str | None = None,
    resume_profile: str | None = None,
    herdr_locator: str,
    cwd: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
    claude_session_id: str | None = None,
) -> dict[str, Any]:
    """Public wrapper: opens exactly ONE ``BEGIN IMMEDIATE`` write
    transaction covering the entire SessionStart restore-ACK-success
    sequence (``task_context_hook_flows.on_session_start()``'s restore
    branch). See ``_complete_restore_tx`` for the bundled step sequence and
    crash-window rationale."""
    with db.write_transaction(conn):
        run_id = _complete_restore_tx(
            conn,
            binding_id=binding_id,
            stale_run_ids=stale_run_ids,
            run_kind=run_kind,
            task_id=task_id,
            activity_id=activity_id,
            runtime_profile=runtime_profile,
            resume_profile=resume_profile,
            herdr_locator=herdr_locator,
            cwd=cwd,
            worktree=worktree,
            branch=branch,
            claude_session_id=claude_session_id,
        )
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
        # Trusted workflow-signal facts (Issue #2565). Values remain small
        # scalar evidence; raw workflow output is never journaled.
        "signal_kind",
        "source",
        "source_schema_version",
        "issue_number",
        "pr_number",
        "approved_body_sha256",
        "merge_commit_oid",
        "merge_identity",
        # Task-aware PreToolUse guard bounded metadata (Issue #2566 In
        # Scope: "EventJournal には ... bounded metadata のみを記録し、
        # message body・terminal output・full Bash command は保存しない").
        # `task_id` (above) already carries the *source* Task; `source_task_id`
        # is kept as an explicit alias in the allowlist so a guard event can
        # name it directly when it is not the row's own `task_id` (never a
        # second SSOT -- both are small scalar Task ids).
        "transport",
        "target_kind",
        "source_task_id",
        "destination_task_id",
        "decision",
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
    dedupe_key: str | None = None,
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
            dedupe_key=dedupe_key,
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
    dedupe_key: str | None = None,
) -> str:
    metadata = metadata or {}
    _validate_event_metadata(metadata)
    event_id = new_id("event")
    conn.execute(
        "INSERT INTO events "
        "(id, task_id, activity_id, binding_id, execution_run_id, event_type, metadata_json, dedupe_key, occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            task_id,
            activity_id,
            binding_id,
            execution_run_id,
            event_type,
            json.dumps(metadata, sort_keys=True),
            dedupe_key,
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


def find_addressable_subagent_runs(
    conn: sqlite3.Connection,
    *,
    claude_session_id: str | None,
    agent_id: str | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """The single addressability lookup for ``SendMessage`` (Issue #2822).

    Returns ``run_kind='subagent'`` rows the *caller* session may address,
    most-recently started first. Exactly one of ``agent_id`` / ``name``:

    - ``agent_id``: rows bound to the caller's own ``claude_session_id``,
      **ended or not** -- Claude Code resumes a completed / stopped SubAgent
      when it is sent a message, so an ended ExecutionRun does not make an
      agent unaddressable; plus legacy rows with ``claude_session_id IS NULL``
      (written before session binding existed) only while still **open** --
      exactly the previous ``find_open_execution_runs(agent_id=...)``
      behavior.
    - ``name``: only rows bound to the caller's own ``claude_session_id``
      whose ``addressable_name`` (recorded from the ``Agent`` tool ``name``
      by ``PostToolUse:Agent``) equals ``name``, ended or not. Legacy /
      unbound rows never grant name addressability.

    A row bound to a *different* session is never returned. When the caller
    session is unknown (``None`` / empty) only the legacy open agent_id rows
    can match. Several rows for one ``agent_id`` (refire history) are one
    addressable identity, not a collision -- callers dedupe by ``agent_id``
    (a collision is *distinct* agent_ids sharing a name)."""
    if bool(agent_id) == bool(name):
        return []
    if name:
        if not claude_session_id:
            return []
        sql = (
            "SELECT * FROM execution_runs WHERE run_kind = 'subagent' AND agent_id IS NOT NULL "
            "AND claude_session_id = ? AND addressable_name = ? ORDER BY started_at DESC"
        )
        return [dict(r) for r in db.execute_readonly(conn, sql, (claude_session_id, name)).fetchall()]
    legacy = "(claude_session_id IS NULL AND ended_at IS NULL)"
    if claude_session_id:
        where = f"(claude_session_id = ? OR {legacy})"
        params: tuple[Any, ...] = (agent_id, claude_session_id)
    else:
        where = legacy
        params = (agent_id,)
    sql = (
        "SELECT * FROM execution_runs WHERE run_kind = 'subagent' AND agent_id = ? AND "
        + where
        + " ORDER BY started_at DESC"
    )
    return [dict(r) for r in db.execute_readonly(conn, sql, params).fetchall()]


def find_open_subagent_runs_for_stop(
    conn: sqlite3.Connection,
    *,
    claude_session_id: str | None,
    agent_id: str,
) -> list[dict[str, Any]]:
    """Open ``run_kind='subagent'`` rows a ``SubagentStop(agent_id)`` from
    ``claude_session_id`` may end (Issue #2822 OWNER Finding 4): the same
    session's own open rows; only when there are none, legacy unbound
    (``claude_session_id IS NULL``) open rows for back-compat. Another
    session's open row with the same agent_id is never returned. An unbound
    caller keeps the previous agent_id-only match."""
    if claude_session_id is None:
        return find_open_execution_runs(conn, run_kind="subagent", agent_id=agent_id)
    own = find_open_execution_runs(
        conn, run_kind="subagent", agent_id=agent_id, claude_session_id=claude_session_id
    )
    if own:
        return own
    rows = db.execute_readonly(
        conn,
        "SELECT * FROM execution_runs WHERE run_kind = 'subagent' AND agent_id = ? "
        "AND ended_at IS NULL AND claude_session_id IS NULL ORDER BY started_at DESC",
        (agent_id,),
    ).fetchall()
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
    suspended Binding for the same live Herdr Tab (AC3).

    PR #2731 review fix_delta P1-3: previously this used a bare
    ``fetchone()``, which silently returned an ARBITRARY row whenever more
    than one Binding held an unreleased location observation for the same
    ``herdr_locator`` (a stale-claim double-ownership state that
    ``_relocate_binding_tx``'s detach fix above now actively prevents going
    forward, but which existing/pre-fix data, or any other future bug,
    could still produce). That ambiguity must fail closed -- pick none --
    rather than risk silently steering a caller (e.g. a post-cold-restart
    ``/clear`` locator fallback lookup) at the WRONG Binding/Task."""
    rows = db.execute_readonly(
        conn,
        "SELECT tb.* FROM tab_bindings tb "
        "JOIN runtime_locations rl ON rl.binding_id = tb.id "
        "WHERE rl.herdr_locator = ? AND rl.released_at IS NULL",
        (herdr_locator,),
    ).fetchall()
    if len(rows) != 1:
        return None
    return _row_to_dict(rows[0])


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
    # Workflow facts derive attention; presentation stays owned by the
    # existing renderer and consumes this unchanged projection field.
    attention = None
    if task_id:
        import task_context_workflow_signals as workflow_signals
        if workflow_signals.cleanup_pending_for_task(conn, task_id):
            attention = "CLEANUP_PENDING"
    return {
        "binding": binding,
        "task": task,
        "activity": activity,
        "runtime_location": location,
        "task_refs": task_refs,
        "execution_run_id": execution_run_id,
        "attention": attention,
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


def _resolve_or_create_task_with_created_tx(
    conn: sqlite3.Connection, repo: str, ref_kind: str, ref_number: int
) -> tuple[str, bool]:
    """Transaction-internal resolve-or-create that also reports whether this
    call created the Task (Issue #2827: only a Task created in the SAME
    transaction gets the ``refine`` initial Activity of an ordinary ACTIVE
    auto-rebind). Safe against the concurrent "both create, one loses the
    claim" orphan because the enclosing ``BEGIN IMMEDIATE`` already holds the
    write lock when the live-claim read below runs."""
    live = _find_live_claim_tx(conn, repo, ref_kind, ref_number)
    if live is not None:
        return live["task_id"], False
    task_id = _create_task_tx(conn, f"{repo}#{ref_number}")
    _claim_task_ref_tx(conn, task_id, repo, ref_kind, ref_number)
    return task_id, True


def _resolve_or_create_task_for_target_tx(
    conn: sqlite3.Connection, repo: str, ref_kind: str, ref_number: int
) -> str:
    """Transaction-internal resolve-or-create (task id only)."""
    return _resolve_or_create_task_with_created_tx(conn, repo, ref_kind, ref_number)[0]


def _other_binding_open_managed_runs_tx(
    conn: sqlite3.Connection, task_id: str, binding_id: str
) -> list[dict[str, Any]]:
    """Transaction-internal lookup of OPEN managed ExecutionRuns
    (``run_kind`` in ``MANAGED_RUN_KINDS``) that belong to ``task_id`` but NOT
    to ``binding_id`` (Issue #2827: another live managed session already
    holds this Task). A managed run with a NULL ``binding_id`` cannot be
    attributed to the caller either, so it is treated as held (fail toward
    no-mutation). Same predicate as ``find_open_execution_runs`` (run per
    managed ``run_kind``) but evaluated on the write connection so it sits in
    the binder's own ``BEGIN IMMEDIATE`` transaction."""
    kinds = sorted(MANAGED_RUN_KINDS)
    placeholders = ",".join("?" for _ in kinds)
    rows = conn.execute(
        "SELECT id, binding_id, run_kind FROM execution_runs "
        f"WHERE task_id = ? AND ended_at IS NULL AND run_kind IN ({placeholders}) "
        "AND binding_id IS NOT ? ORDER BY started_at DESC",
        (task_id, *kinds, binding_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _current_task_id_for_binding_tx(conn: sqlite3.Connection, binding_id: str) -> str | None:
    """Transaction-internal counterpart of
    ``get_current_task_activity_for_binding`` returning only the Task id of
    the binding's open managed run (the pre-rebind Task identity)."""
    kinds = sorted(MANAGED_RUN_KINDS)
    placeholders = ",".join("?" for _ in kinds)
    row = conn.execute(
        "SELECT task_id FROM execution_runs "
        f"WHERE binding_id = ? AND ended_at IS NULL AND run_kind IN ({placeholders}) "
        "ORDER BY started_at DESC LIMIT 1",
        (binding_id, *kinds),
    ).fetchone()
    return row["task_id"] if row is not None else None


def _ensure_active_activity_tx(conn: sqlite3.Connection, task_id: str, kind: str) -> str:
    row = conn.execute(
        "SELECT id FROM activities WHERE task_id = ? AND status = 'ACTIVE'", (task_id,)
    ).fetchone()
    if row is not None:
        return row["id"]
    return _transition_activity_tx(conn, task_id, kind)


def _cleanup_already_begun_for_merge_tx(conn: sqlite3.Connection, task_id: str, merged_row: sqlite3.Row) -> bool:
    """True when a ``workflow:cleanup_started`` event already exists for the
    same ``(repo, pr_number)`` the merge-gap fallback below would otherwise
    resume the historical implementation Activity for.

    Once cleanup has begun (whether it is still ACTIVE or has since reached
    DONE), the merge-gap fallback must not fire again: cleanup already owns
    -- or has already finished -- the post-merge transition, so a fresh
    Binding must fall through to the ordinary activity-transition path
    instead of re-attaching to a completed implementation Activity.
    """
    try:
        merged_metadata = json.loads(merged_row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        return False
    repo, pr_number = merged_metadata.get("repo"), merged_metadata.get("pr_number")
    if repo is None or pr_number is None:
        return False
    rows = conn.execute(
        "SELECT metadata_json FROM events WHERE task_id = ? AND event_type = 'workflow:cleanup_started'",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            cleanup_metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if cleanup_metadata.get("repo") == repo and cleanup_metadata.get("pr_number") == pr_number:
            return True
    return False


def _select_activity_for_binding_tx(conn: sqlite3.Connection, task_id: str, kind: str) -> str:
    """Select an ACTIVE Activity, except resume a merge-accepted Task at its
    historical implementation Activity until cleanup owns the next transition.

    A fresh Binding may resolve the same Task after ``pr_merged_observed`` was
    committed but before ``cleanup begin`` ran.  Starting a generic native
    Activity in that narrow gap would make the canonical cleanup transition
    out-of-order.  Reattach to the completed implementation Activity instead;
    ``begin_cleanup_lifecycle`` owns creating and binding cleanup atomically.

    This fallback is scoped strictly to that narrow gap: it only applies
    while no cleanup instance has begun yet for the accepted merge fact
    (``_cleanup_already_begun_for_merge_tx`` is False). Once cleanup has
    begun -- ACTIVE or already DONE -- the ``pr_merged_observed`` event
    remaining in history must not keep re-selecting the DONE implementation
    Activity; fall through to the ordinary ``_transition_activity_tx`` path
    so the session is free to start/advance to the next normal Activity.
    """
    active = conn.execute(
        "SELECT id FROM activities WHERE task_id = ? AND status = 'ACTIVE'", (task_id,)
    ).fetchone()
    if active is not None:
        return active["id"]
    merged = conn.execute(
        "SELECT activity_id, metadata_json FROM events "
        "WHERE task_id = ? AND event_type = 'workflow:pr_merged_observed' "
        "AND activity_id IS NOT NULL ORDER BY occurred_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if merged is not None and not _cleanup_already_begun_for_merge_tx(conn, task_id, merged):
        implementation = conn.execute(
            "SELECT id FROM activities WHERE id = ? AND task_id = ? AND kind = 'implementation'",
            (merged["activity_id"], task_id),
        ).fetchone()
        if implementation is not None:
            return implementation["id"]
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
    # one) -- degrade gracefully by starting one rather than raising. Issue
    # #2567 AC4: use the current process's runtime-variant-derived run_kind
    # (claude_gpt vs native_operator) instead of hardcoding native_operator,
    # so this degrade path never mis-tags a Claude-GPT operator run.
    degrade_run_kind, degrade_runtime_profile, degrade_resume_profile = (
        task_context_config.normalize_operator_profiles_for_new_run(
            *task_context_config.operator_run_kind_and_profiles()
        )
    )
    run_id = _start_execution_run_tx(
        conn,
        run_kind=degrade_run_kind,
        task_id=task_id,
        activity_id=activity_id,
        binding_id=binding_id,
        runtime_profile=degrade_runtime_profile,
        resume_profile=degrade_resume_profile,
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
    extra_event_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = _attach_or_start_binding_run_tx(conn, binding_id, task_id, activity_id, execution_run_id)
    _append_event_tx(
        conn,
        event_type=event_type,
        task_id=task_id,
        activity_id=activity_id,
        binding_id=binding_id,
        execution_run_id=run_id,
        metadata={"reason_code": reason_code, "status": "pass", **(extra_event_metadata or {})},
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
    activity_kind_for_new_task: str | None = None,
    refuse_when_other_binding_holds_task: bool = False,
    record_identity_transition: bool = False,
) -> dict[str, Any]:
    """Atomically point ``binding_id`` at the Task that owns
    ``(repo, ref_kind, ref_number)`` -- creating the Task and claiming the
    ref if nobody owns it yet -- ensure it has an ACTIVE Activity, attach the
    binding's ExecutionRun, append the lifecycle event, and bump the
    projection outbox. Used for autobind, terminal-activity advance/rebind
    and explicit ``/task <github-ref>`` rebind.

    Issue #2827: three additive opt-in arguments used ONLY by the ordinary
    ACTIVE ``A -> prompt B`` auto-rebind (every other caller leaves them at
    their defaults and behaves exactly as before):

    - ``activity_kind_for_new_task``: when set, a Task created by THIS
      transaction starts with this Activity kind (``refine``). An existing
      Task keeps the normal selector (an ACTIVE Activity is preserved; with no
      ACTIVE one the ``activity_kind`` default applies, so an existing Task is
      never restarted as ``refine`` just for the rebind).
    - ``refuse_when_other_binding_holds_task``: evaluated inside this same
      ``BEGIN IMMEDIATE``; if a DIFFERENT Binding has an open managed
      ExecutionRun on the resolved Task, nothing is written and
      ``{"blocked_by_other_live_binding": True, ...}`` is returned. Because
      the check and the run attach share one write transaction, two Bindings
      racing for the same Task cannot both succeed.
    - ``record_identity_transition``: adds bounded pre/post Task identity
      (``source_task_id`` / ``destination_task_id`` plus the target ref) to the
      lifecycle event's metadata in the same transaction. No raw prompt."""
    with db.write_transaction(conn):
        task_id, created = _resolve_or_create_task_with_created_tx(conn, repo, ref_kind, ref_number)
        if refuse_when_other_binding_holds_task and not created:
            holders = _other_binding_open_managed_runs_tx(conn, task_id, binding_id)
            if holders:
                return {
                    "blocked_by_other_live_binding": True,
                    "task_id": task_id,
                    "blocking_binding_ids": sorted({h["binding_id"] for h in holders if h["binding_id"]}),
                }
        source_task_id = _current_task_id_for_binding_tx(conn, binding_id) if record_identity_transition else None
        kind = activity_kind_for_new_task if (created and activity_kind_for_new_task) else activity_kind
        activity_id = _select_activity_for_binding_tx(conn, task_id, kind)
        extra_metadata = None
        if record_identity_transition:
            extra_metadata = {
                "source_task_id": source_task_id,
                "destination_task_id": task_id,
                "repo": repo,
                "ref_kind": ref_kind,
                "ref_number": ref_number,
                "operation": "active_prompt_rebind",
            }
        result = _finish_binding_mutation_tx(
            conn,
            binding_id=binding_id,
            task_id=task_id,
            activity_id=activity_id,
            execution_run_id=execution_run_id,
            event_type=event_type,
            reason_code=reason_code,
            extra_event_metadata=extra_metadata,
        )
        if record_identity_transition:
            result["task_created"] = created
            result["source_task_id"] = source_task_id
        return result


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


# ---------------------------------------------------------------------------
# unbound-session bootstrap (Issue #2790 AC2/AC5; PR #2795 review fix_delta
# P1-A/P1-B, comment
# https://github.com/squne121/loop-protocol/pull/2795#issuecomment-5852749710)
# ---------------------------------------------------------------------------


def bootstrap_binding_with_run(
    conn: sqlite3.Connection,
    *,
    herdr_locator: str,
    location_fields: dict[str, Any],
    claude_session_id: str | None,
    run_kind: str,
    runtime_profile: str | None,
    resume_profile: str | None,
    event_type: str,
    reason_code: str,
    evict_foreign_holder: bool = False,
) -> dict[str, Any]:
    """Coarse-grained, single-transaction "no existing Binding -- create one
    from scratch" operation: create Binding -> relocate -> start
    ExecutionRun -> (if a Claude session id is known) attach it -> append the
    lifecycle event, all inside ONE ``BEGIN IMMEDIATE``.

    fix_delta P1-A: this replaces a chain of independently-committing public
    calls (``create_binding`` / ``relocate_binding`` / ``start_execution_run``
    / ``set_execution_run_session`` / ``set_binding_session`` /
    ``append_event``) that could previously leave an orphan half-created
    Binding/RuntimeLocation/ExecutionRun behind if a later step in the chain
    raised. A failure anywhere in this function now rolls back everything
    (the enclosing ``BEGIN IMMEDIATE``), so callers never observe a
    partially-bootstrapped Binding.

    ``evict_foreign_holder`` (fix_delta P1-B) defaults to ``False`` here --
    bootstrapping a brand-new, independent Binding must never release a
    different (e.g. parent/sibling) Binding's still-live location claim on
    the same locator merely because this new Binding also observes it.
    Callers that know this locator genuinely belongs to no other live
    Binding (e.g. a truly new Herdr Tab) may pass ``True``."""
    with db.write_transaction(conn):
        binding_id = _create_binding_tx(conn)
        _relocate_binding_tx(
            conn,
            binding_id,
            herdr_locator,
            evict_foreign_holder=evict_foreign_holder,
            **location_fields,
        )
        run_id = _start_execution_run_tx(
            conn,
            run_kind=run_kind,
            binding_id=binding_id,
            runtime_profile=runtime_profile,
            resume_profile=resume_profile,
        )
        if claude_session_id:
            _set_execution_run_session_tx(conn, run_id, claude_session_id)
            _set_binding_session_tx(conn, binding_id, claude_session_id, execution_run_id=run_id)
        _append_event_tx(
            conn,
            event_type=event_type,
            binding_id=binding_id,
            execution_run_id=run_id,
            metadata={"reason_code": reason_code, "status": "ok"},
        )
    return {"binding_id": binding_id, "execution_run_id": run_id}


def bootstrap_binding_and_bind_target(
    conn: sqlite3.Connection,
    *,
    herdr_locator: str,
    location_fields: dict[str, Any],
    claude_session_id: str,
    run_kind: str,
    runtime_profile: str | None,
    resume_profile: str | None,
    bootstrap_event_type: str,
    bootstrap_reason_code: str,
    bind_event_type: str,
    bind_reason_code: str,
    target_repo: str | None = None,
    target_ref_kind: str | None = None,
    target_ref_number: int | None = None,
    ad_hoc_title: str | None = None,
    activity_kind: str = "native_operator",
) -> dict[str, Any]:
    """Explicit ``/task <target>`` bootstrap of a genuinely unbound Herdr
    session (Issue #2790 AC2/AC5): create an independent Binding + managed
    ExecutionRun AND immediately point it at the already-parsed explicit
    target, all inside one ``BEGIN IMMEDIATE``.

    fix_delta P1-A (PR #2795 review comment 5852749710, finding 1): the
    previous implementation ran the bootstrap
    (``bootstrap_unbound_session``) and the target bind
    (``bind_target_to_binding`` / ``bind_ad_hoc_task_to_binding``) as two
    separately-committing top-level calls. A failure in the bind step left
    a real, committed Binding/RuntimeLocation/ExecutionRun/session-claim/
    event behind with no Task attached -- exactly the
    ``origin_task_unattached``-shaped half-applied state the review comment
    identifies. This function makes the whole
    "create -> relocate -> start run -> attach session -> bootstrap event ->
    resolve-or-create Task -> ensure ACTIVE Activity -> attach run -> rebind
    event -> bump projection" sequence a single atomic unit: any failure
    (including the target-bind half) rolls back the bootstrap half too, so
    `/task` either fully succeeds or leaves no trace.

    Requires exactly one target shape: a GitHub ref
    (``target_repo``/``target_ref_kind``/``target_ref_number``) or an
    ad-hoc title (``ad_hoc_title``) -- the caller (``on_user_prompt_expansion``)
    has already validated ``has_explicit_target`` before calling this, so
    this raises ``ValidationError`` rather than silently no-op'ing if
    neither is given."""
    has_ref_target = bool(target_repo and target_ref_kind and target_ref_number is not None)
    if not has_ref_target and not ad_hoc_title:
        raise errors.ValidationError(
            "bootstrap_binding_and_bind_target requires either a GitHub ref target "
            "(target_repo/target_ref_kind/target_ref_number) or ad_hoc_title"
        )
    with db.write_transaction(conn):
        binding_id = _create_binding_tx(conn)
        # fix_delta P1-B: never evict a foreign Binding's live location claim
        # just because this brand-new bootstrap Binding also observes the
        # same locator (e.g. a `fork`'d session sharing its parent's Pane).
        _relocate_binding_tx(
            conn,
            binding_id,
            herdr_locator,
            evict_foreign_holder=False,
            **location_fields,
        )
        run_id = _start_execution_run_tx(
            conn,
            run_kind=run_kind,
            binding_id=binding_id,
            runtime_profile=runtime_profile,
            resume_profile=resume_profile,
        )
        _set_execution_run_session_tx(conn, run_id, claude_session_id)
        _set_binding_session_tx(conn, binding_id, claude_session_id, execution_run_id=run_id)
        _append_event_tx(
            conn,
            event_type=bootstrap_event_type,
            binding_id=binding_id,
            execution_run_id=run_id,
            metadata={"reason_code": bootstrap_reason_code, "status": "ok"},
        )
        if has_ref_target:
            task_id = _resolve_or_create_task_for_target_tx(
                conn, target_repo, target_ref_kind, target_ref_number
            )
            activity_id = _select_activity_for_binding_tx(conn, task_id, activity_kind)
        else:
            task_id = _create_task_tx(conn, ad_hoc_title)
            activity_id = _ensure_active_activity_tx(conn, task_id, activity_kind)
        bind_result = _finish_binding_mutation_tx(
            conn,
            binding_id=binding_id,
            task_id=task_id,
            activity_id=activity_id,
            execution_run_id=run_id,
            event_type=bind_event_type,
            reason_code=bind_reason_code,
        )
    return {"binding_id": binding_id, "bootstrap_execution_run_id": run_id, **bind_result}

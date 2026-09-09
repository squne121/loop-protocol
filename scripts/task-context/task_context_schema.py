"""Task Context v1 — canonical SQLite schema (DDL statements).

This module is the single source of truth for the current-state schema
described in ``docs/dev/task-context.md``. It intentionally exposes the DDL
as a **list of individual statements** (not one big SQL script) so that the
migration runner (``migrations/runner.py``) can execute each statement with
``sqlite3.Connection.execute`` inside a single manually-managed
``BEGIN IMMEDIATE`` ... ``COMMIT`` transaction.

Why not ``Connection.executescript``: per the Python ``sqlite3`` docs,
``executescript`` implicitly commits any pending transaction before running,
which would break the "single write transaction for DDL + data migration +
user_version bump" invariant required by Issue #2563 AC2/AC11. Using
``execute`` per-statement keeps everything inside our own transaction.

Table inventory (fixed by the Issue #2563 contract, do not add tables without
a v1 consumer — see "Scope Growth Guard" in the Issue body):

- ``db_meta``            — small generic key/value introspection store.
- ``tasks``               — Task lifecycle root.
- ``task_refs``           — Issue/PR reference candidates associated with a Task.
- ``task_ref_claims``     — live ownership claim of a ref (Issue/PR) by a Task.
- ``activities``          — unit-of-work under a Task (at most 1 ACTIVE at a time).
- ``tab_bindings``        — durable operator-binding identity (``binding_id``).
- ``runtime_locations``   — mutable Herdr locator *observation* history for a binding.
- ``execution_runs``      — Native/SubAgent/runtime-smoke/Claude-GPT run records.
- ``events``              — append-only retrospective history (no raw content).
- ``projection_outbox``   — desired-revision-only projection queue (coalescing).
"""

from __future__ import annotations

CURRENT_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# v1 DDL (PRAGMA user_version target = 1)
# ---------------------------------------------------------------------------
# NOTE: statements are executed strictly in this order (FK-dependency order).
DDL_V1: list[str] = [
    # -- db_meta --------------------------------------------------------
    """
    CREATE TABLE db_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # -- tasks ------------------------------------------------------------
    """
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        title TEXT,
        status TEXT NOT NULL DEFAULT 'OPEN'
            CHECK (status IN ('OPEN', 'DONE', 'ABANDONED')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # -- task_refs ----------------------------------------------------------
    """
    CREATE TABLE task_refs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        repo TEXT NOT NULL,
        ref_kind TEXT NOT NULL CHECK (ref_kind IN ('issue', 'pr')),
        ref_number INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (task_id, repo, ref_kind, ref_number)
    )
    """,
    """
    CREATE INDEX ix_task_refs_task_id ON task_refs(task_id)
    """,
    # Composite covering unique index used purely as the *parent* side of the
    # `task_ref_claims` composite FOREIGN KEY below (fix_delta finding 7b).
    # SQLite requires an explicit unique index over the exact parent column
    # tuple referenced by a composite FK. This lets task_ref_claims keep its
    # own (repo, ref_kind, ref_number, task_id) columns -- required because
    # SQLite partial UNIQUE INDEXes cannot span a join -- while making it
    # physically impossible for those columns to disagree with the
    # task_refs row they claim.
    """
    CREATE UNIQUE INDEX ux_task_refs_id_task_id_repo_kind_number
        ON task_refs(id, task_id, repo, ref_kind, ref_number)
    """,
    # -- task_ref_claims ------------------------------------------------------
    # AC1(a)/AC4: a live (released_at IS NULL) claim on the same
    # (repo, ref_kind, ref_number) tuple can never be held by more than one
    # claim row (regardless of task_id) -- this is the DB-physical guard
    # against Issue/PR split-brain across candidate Tasks.
    #
    # task_id/repo/ref_kind/ref_number duplicate fields already present on
    # the referenced task_refs row (via task_ref_id). They cannot be
    # dropped outright because the ux_task_ref_claims_live partial unique
    # index below must be a single-table index (SQLite partial indexes
    # cannot span a join to task_refs). Instead, the composite FOREIGN KEY
    # below makes it a DB-physical impossibility for these redundant
    # columns to disagree with the task_refs row identified by
    # task_ref_id (fix_delta finding 7b) -- a raw SQL row that names one
    # task_ref_id but a different task_id/repo/ref_kind/ref_number is
    # rejected by SQLite itself, not merely by application code.
    """
    CREATE TABLE task_ref_claims (
        id TEXT PRIMARY KEY,
        task_ref_id TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        repo TEXT NOT NULL,
        ref_kind TEXT NOT NULL CHECK (ref_kind IN ('issue', 'pr')),
        ref_number INTEGER NOT NULL,
        claimed_at TEXT NOT NULL,
        released_at TEXT,
        FOREIGN KEY (task_ref_id, task_id, repo, ref_kind, ref_number)
            REFERENCES task_refs(id, task_id, repo, ref_kind, ref_number)
    )
    """,
    """
    CREATE UNIQUE INDEX ux_task_ref_claims_live
        ON task_ref_claims(repo, ref_kind, ref_number)
        WHERE released_at IS NULL
    """,
    """
    CREATE INDEX ix_task_ref_claims_task_id ON task_ref_claims(task_id)
    """,
    # -- activities -----------------------------------------------------------
    # AC1(b): at most 1 ACTIVE Activity per Task.
    """
    CREATE TABLE activities (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK (status IN ('ACTIVE', 'DONE', 'ABANDONED')),
        started_at TEXT NOT NULL,
        ended_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_activities_active_per_task
        ON activities(task_id)
        WHERE status = 'ACTIVE'
    """,
    # -- tab_bindings -------------------------------------------------------
    # Durable operator-binding identity. Deliberately holds NO Herdr locator
    # field -- locator observations live in `runtime_locations` so that
    # relocating the Herdr tab/pane never mutates this row's identity
    # (AC5: "Herdr locator変更だけでbinding_id/Task/Activity identityが
    # 変わらない").
    """
    CREATE TABLE tab_bindings (
        id TEXT PRIMARY KEY,
        current_claude_session_id TEXT,
        runtime_health TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK (runtime_health IN
                ('ACTIVE', 'SUSPENDED', 'RESTORING', 'RESTORE_BLOCKED', 'DETACHED')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # fix_delta finding 3: `current_claude_session_id` is a durable
    # recovery-facing field required by #2569 cold-restore contract, so it
    # is intentionally NOT removed. Without this index, `set_binding_session`
    # could previously write the *same* non-null session id onto more than
    # one Binding, creating a second, un-synchronized SSOT alongside the
    # execution_runs.claude_session_id partial-unique guard (AC1e). This
    # partial unique index makes "session_id S -> at most one Binding
    # currently claims S" a DB-physical invariant, so
    # `task_context_service.get_binding_by_current_session` can resolve
    # "session_id S -> exactly one current Binding" deterministically.
    """
    CREATE UNIQUE INDEX ux_tab_bindings_current_session
        ON tab_bindings(current_claude_session_id)
        WHERE current_claude_session_id IS NOT NULL
    """,
    # -- runtime_locations ---------------------------------------------------
    # AC1(c): at most 1 *unreleased* (released_at IS NULL) location
    # observation per binding at a time. Relocating = release the old
    # observation row + insert a new one, inside one transaction.
    """
    CREATE TABLE runtime_locations (
        id TEXT PRIMARY KEY,
        binding_id TEXT NOT NULL REFERENCES tab_bindings(id),
        herdr_locator TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        released_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_runtime_locations_unreleased_per_binding
        ON runtime_locations(binding_id)
        WHERE released_at IS NULL
    """,
    """
    CREATE INDEX ix_runtime_locations_binding_id ON runtime_locations(binding_id)
    """,
    # -- execution_runs -------------------------------------------------------
    # task_id / activity_id / binding_id are all nullable to represent the
    # startup state before Task/Activity/Binding are decided (AC6).
    # is_managed distinguishes a "managed operator run/session" (Native
    # Claude Code / Claude-GPT operator loop) from other run kinds
    # (SubAgent, runtime-smoke) for the purposes of AC1(d)/(e).
    #
    # fix_delta finding 2: is_managed is NOT an independent caller-supplied
    # flag -- it is fully *derived* from run_kind
    # (native_operator/claude_gpt => managed; subagent/runtime_smoke =>
    # non-managed). The CHECK below makes any other combination a DB
    # physical impossibility, closing the two bypasses of the AC1(d)/(e)
    # operator invariants that were previously possible by naming an
    # arbitrary is_managed value from a raw SQL caller:
    #   (a) run_kind='native_operator'/'claude_gpt' with is_managed=0 would
    #       have silently escaped the AC1(d)/(e) partial unique indexes;
    #   (b) run_kind='subagent'/'runtime_smoke' with is_managed=1 would have
    #       falsely impersonated a managed operator run and consumed the
    #       AC1(d)/(e) uniqueness slot on behalf of a non-operator run.
    # `task_context_service.start_execution_run` also derives is_managed
    # from run_kind in application code (belt-and-suspenders), but this
    # CHECK is the authoritative physical guard.
    """
    CREATE TABLE execution_runs (
        id TEXT PRIMARY KEY,
        task_id TEXT REFERENCES tasks(id),
        activity_id TEXT REFERENCES activities(id),
        binding_id TEXT REFERENCES tab_bindings(id),
        run_kind TEXT NOT NULL
            CHECK (run_kind IN ('native_operator', 'subagent', 'runtime_smoke', 'claude_gpt')),
        runtime_profile TEXT,
        resume_profile TEXT,
        claude_session_id TEXT,
        is_managed INTEGER NOT NULL DEFAULT 0 CHECK (is_managed IN (0, 1)),
        started_at TEXT NOT NULL,
        ended_at TEXT,
        CHECK (
            (run_kind IN ('native_operator', 'claude_gpt') AND is_managed = 1)
            OR
            (run_kind IN ('subagent', 'runtime_smoke') AND is_managed = 0)
        )
    )
    """,
    # AC1(d): at most 1 open (ended_at IS NULL) managed operator run per
    # binding. The index authority is `run_kind IN (...)` rather than
    # `is_managed = 1` (fix_delta finding 2) -- these are guaranteed
    # equivalent by the table CHECK above, but keying the index directly off
    # run_kind means the *index itself* never silently trusts a
    # caller-supplied is_managed value.
    """
    CREATE UNIQUE INDEX ux_execution_runs_open_managed_per_binding
        ON execution_runs(binding_id)
        WHERE run_kind IN ('native_operator', 'claude_gpt') AND ended_at IS NULL AND binding_id IS NOT NULL
    """,
    # AC1(e): partial unique index restricted to rows where
    # claude_session_id IS NOT NULL AND run_kind IN ('native_operator',
    # 'claude_gpt') AND ended_at IS NULL -- i.e. only *currently open*
    # managed/operator runs enforce claude_session_id uniqueness. Historical
    # (ended) runs and non-managed runs are exempt, so re-attaching the same
    # claude_session_id to a new ExecutionRun after the prior one ended
    # remains allowed.
    """
    CREATE UNIQUE INDEX ux_execution_runs_open_managed_session
        ON execution_runs(claude_session_id)
        WHERE claude_session_id IS NOT NULL
            AND run_kind IN ('native_operator', 'claude_gpt')
            AND ended_at IS NULL
    """,
    """
    CREATE INDEX ix_execution_runs_task_id ON execution_runs(task_id)
    """,
    """
    CREATE INDEX ix_execution_runs_binding_id ON execution_runs(binding_id)
    """,
    # fix_delta finding 7a: execution_runs.task_id and .activity_id must
    # never point at different Tasks. Application code
    # (`task_context_service._validate_task_activity_consistency`) already
    # checks this before every INSERT/UPDATE, but these triggers are the
    # DB-physical backstop against a raw SQL write bypassing the typed API.
    """
    CREATE TRIGGER trg_execution_runs_task_activity_consistency_insert
        BEFORE INSERT ON execution_runs
        WHEN NEW.task_id IS NOT NULL AND NEW.activity_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'execution_runs.activity_id must belong to execution_runs.task_id')
            WHERE (SELECT task_id FROM activities WHERE id = NEW.activity_id) IS NOT NEW.task_id;
        END
    """,
    """
    CREATE TRIGGER trg_execution_runs_task_activity_consistency_update
        BEFORE UPDATE ON execution_runs
        WHEN NEW.task_id IS NOT NULL AND NEW.activity_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'execution_runs.activity_id must belong to execution_runs.task_id')
            WHERE (SELECT task_id FROM activities WHERE id = NEW.activity_id) IS NOT NEW.task_id;
        END
    """,
    # -- events ---------------------------------------------------------------
    # Append-only retrospective history. Enforced append-only at the DB
    # layer via triggers (belt) AND at the typed API layer via an allowlist
    # of permitted metadata keys/value shapes that structurally excludes raw
    # prompt/transcript/command/message bodies (braces, AC7).
    """
    CREATE TABLE events (
        id TEXT PRIMARY KEY,
        task_id TEXT REFERENCES tasks(id),
        activity_id TEXT REFERENCES activities(id),
        binding_id TEXT REFERENCES tab_bindings(id),
        execution_run_id TEXT REFERENCES execution_runs(id),
        event_type TEXT NOT NULL,
        metadata_json TEXT,
        occurred_at TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER trg_events_no_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events is append-only: UPDATE is not allowed');
        END
    """,
    """
    CREATE TRIGGER trg_events_no_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events is append-only: DELETE is not allowed');
        END
    """,
    """
    CREATE INDEX ix_events_task_id ON events(task_id)
    """,
    # -- projection_outbox ------------------------------------------------------
    # AC12: coalesces to the latest desired revision only (one row per
    # projection_key). flush() reads desired_revision; ack() performs a
    # conditional DELETE keyed on the *read* revision so a concurrent
    # enqueue() that has advanced desired_revision beyond the read revision
    # is never lost.
    #
    # fix_delta finding 4: projection_outbox holds ONLY the
    # (projection_key, desired_revision) marker -- it is deliberately not a
    # second SSOT for projection payload content. The projector/flush
    # consumer re-derives the actual projection content by reading canonical
    # DB state (Task/Activity/Binding/... tables) for `desired_revision` at
    # flush time; it does not read a stored payload snapshot out of this
    # table.
    """
    CREATE TABLE projection_outbox (
        projection_key TEXT PRIMARY KEY,
        desired_revision INTEGER NOT NULL,
        enqueued_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
]

MIGRATIONS: dict[int, list[str]] = {
    1: DDL_V1,
}

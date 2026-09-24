#!/usr/bin/env python3
"""Task Context v1 -- Herdr cold-restart profile-aware resume dispatcher
(Issue #2569 "Profile-aware resume dispatcher" / "Two-phase restore state
machine" sections).

Architecture (post-hoc orchestration model, Issue #2569 AC12; NOT a
`claude --resume` shim/interception layer):

1. The LOOP_PROTOCOL project-scoped named Herdr session is started with
   ``HERDR_CONFIG_PATH`` pointing at a config with
   ``resume_agents_on_restore = false`` -- this disables Herdr's OWN native
   resume for every pane in that session, architecturally (not by racing
   against it), so there is no "shim wins the race" concern at all.
2. Herdr's own ``[[startup]]`` plugin hook -- which the Issue's causal-probe
   evidence confirmed fires exactly once, automatically, after Herdr's own
   session restore + API socket become ready -- is the SOLE automatic
   trigger for ``task_context_cold_restart_startup.py``'s orchestrator
   entrypoint (PR #2731 review fix_delta P1-1 -- that sibling module, in
   THIS same ``scripts/task-context/`` directory, is the actual
   ``[[startup]]``-hook-invoked multi-pane orchestrator; see its own
   docstring and ``docs/dev/task-context.md`` "Cold-restart resume
   dispatcher").
3. That orchestrator enumerates the named session's CURRENTLY LIVE panes
   (``herdr pane list``) and cross-references them against Task Context's
   OWN durable state (``tab_bindings``/``runtime_locations`` -- never
   Herdr's own per-pane ``agent_session`` field, which is never populated
   for Claude-GPT panes at all -- fix_delta P1-1) to resolve which
   (session_id, pane_id) pairs are dispatch candidates, then calls THIS
   module's ``prepare_managed_resume()`` / ``execute_resume_decision()`` /
   ``await_all_acks()`` for each one. This module itself stays a single-
   session-id classify+launch+ACK building block -- it never becomes/
   replaces its own process image (no ``os.execvp``), since a single
   ``[[startup]]``-triggered orchestrator process must be able to dispatch
   N panes, not just one, by calling into this module N times.

No new daemon/lease table/lock file/distributed coordinator is introduced
(Issue Outcome/Stop Conditions) -- both this module and
``task_context_cold_restart_startup.py`` are plain one-shot Python
entrypoints that exit once every resolvable pane has been dispatched (or
classified as not-auto-resumable) and ACKs have been collected within a
bounded deadline. Existing SQLite constraints (``BEGIN IMMEDIATE``, the
``ux_tab_bindings_current_session``/``ux_execution_runs_open_managed_*``
unique indexes) remain the sole concurrency mechanism -- neither module adds
any locking of its own.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MIGRATIONS_DIR = os.path.join(_THIS_DIR, "migrations")
for _dir in (_THIS_DIR, _MIGRATIONS_DIR):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import task_context_config as config  # noqa: E402
import task_context_db as db  # noqa: E402
import task_context_errors as errors  # noqa: E402
import task_context_migration_runner as migration_runner  # noqa: E402
import task_context_service as service  # noqa: E402

# ---------------------------------------------------------------------------
# Fixed dispatcher repository identity anchor (AC15)
# ---------------------------------------------------------------------------
#
# `resolve_dispatcher_state_root()` below ALWAYS resolves the Task Context
# state root from THIS MODULE's own on-disk location -- never from any
# pane/caller-supplied cwd. Herdr always launches the dispatcher with *some*
# process cwd (typically whatever directory the resumed pane last reported),
# and that cwd is explicitly NOT an input to state-root resolution, per
# AC15 / Stop Condition "dispatcherが復元paneのcwdをproject identity /
# state-root解決に使わざるを得ないと判明した場合".
_DISPATCHER_REPO_ANCHOR_CWD = _THIS_DIR

# Repository root (two levels up from scripts/task-context/) -- used only to
# locate the fixed, repository-owned Claude-GPT launcher script path below.
# Also derived from this module's own location, never from a pane cwd.
_REPO_ROOT = Path(_THIS_DIR).resolve().parents[1]
_DEFAULT_CLAUDE_GPT_LAUNCH_SCRIPT = _REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"


def resolve_dispatcher_state_root() -> Path:
    """AC15: resolve the Task Context state root using the dispatcher's own
    fixed repository identity, never a pane-supplied cwd."""
    return config.resolve_state_root(cwd=_DISPATCHER_REPO_ANCHOR_CWD)


def open_dispatcher_db() -> Any:
    """Open (creating/migrating if needed) the Task Context DB at the
    dispatcher's fixed-identity state root (AC15)."""
    db_file = resolve_dispatcher_state_root() / config.DB_FILE_NAME
    conn = db.connect(db_file)
    migration_runner.migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# 5-way decision table (Issue #2569 "Profile-aware resume dispatcher")
# ---------------------------------------------------------------------------

ACTION_LAUNCH_NATIVE = "launch_native"
ACTION_LAUNCH_CLAUDE_GPT = "launch_claude_gpt"
ACTION_RESTORE_BLOCKED = "restore_blocked"
ACTION_NOOP_SUSPENDED = "noop_suspended_no_auto_resume"
ACTION_NOOP_UNMANAGED = "noop_unmanaged_session"
ACTION_NOOP_ALREADY_RESTORING = "noop_restore_already_in_progress"

_LAUNCHABLE_ACTIONS = frozenset({ACTION_LAUNCH_NATIVE, ACTION_LAUNCH_CLAUDE_GPT})

# Binding runtime_health values that must NEVER be auto-resumed (AC5 --
# `/quit`-suspended -- and DETACHED, which is likewise not a live managed
# session). Only ACTIVE is a candidate for classification into a launch
# action; every other value is either a defined no-auto-resume state or
# (RESTORE_BLOCKED/RESTORING) already terminal/in-flight for this dispatch.
_NO_AUTO_RESUME_HEALTH = frozenset({"SUSPENDED", "DETACHED"})


@dataclasses.dataclass(frozen=True)
class ResumeDecision:
    """Pure classification result -- never performs I/O itself. See
    ``execute_resume_decision()`` for the only function in this module that
    launches a process."""

    action: str
    reason_code: str
    session_id: str
    binding_id: str | None = None
    task_id: str | None = None
    activity_id: str | None = None
    execution_run_id: str | None = None
    effective_profile: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def classify_for_resume(conn, session_id: str) -> ResumeDecision:
    """Implements the Issue #2569 5-way dispatcher decision table for a
    saved Herdr ``agent_session`` observation (``session_id``) recovered
    from a (possibly cold-restarted) named Herdr session's pane snapshot.
    Read-only -- never mutates the DB."""
    if not session_id:
        return ResumeDecision(action=ACTION_NOOP_UNMANAGED, reason_code="empty_session_id", session_id="")

    try:
        binding = service.get_binding_by_current_session(conn, session_id)
    except errors.NotFoundError:
        # unknown/never-managed (AC8): Task Context never claims ownership
        # of a session it has no record of -- existing Herdr native
        # semantics for this pane are left untouched by this dispatcher.
        return ResumeDecision(
            action=ACTION_NOOP_UNMANAGED,
            reason_code="unknown_never_managed_session",
            session_id=session_id,
        )

    binding_id = binding["id"]
    runtime_health = binding["runtime_health"]

    if runtime_health in _NO_AUTO_RESUME_HEALTH:
        # SUSPENDED (AC5: `/quit`-ed) / DETACHED (historical, not a live
        # managed session) -- never auto-resumed, regardless of profile.
        return ResumeDecision(
            action=ACTION_NOOP_SUSPENDED,
            reason_code=f"binding_{runtime_health.lower()}_no_auto_resume",
            session_id=session_id,
            binding_id=binding_id,
        )
    if runtime_health == "RESTORING":
        # A prior dispatch for this exact Binding is already mid-flight
        # (ACTIVE -> RESTORING, ACK not yet observed) -- never launch a
        # second concurrent resume for the same Binding.
        return ResumeDecision(
            action=ACTION_NOOP_ALREADY_RESTORING,
            reason_code="binding_restore_already_in_progress",
            session_id=session_id,
            binding_id=binding_id,
        )
    if runtime_health == "RESTORE_BLOCKED":
        # Already blocked (a prior dispatch attempt failed pre-ACK, or an
        # invalid_managed_profile was previously observed) -- stays
        # RESTORE_BLOCKED; idempotent, never silently retried into a
        # Native fallback.
        return ResumeDecision(
            action=ACTION_RESTORE_BLOCKED,
            reason_code="binding_already_restore_blocked",
            session_id=session_id,
            binding_id=binding_id,
        )
    if runtime_health != "ACTIVE":
        # Defensive: the schema CHECK constraint already restricts
        # runtime_health's value set, so this branch is unreachable with
        # the current contract -- fail-closed rather than silently
        # dispatching an unrecognized state.
        return ResumeDecision(  # pragma: no cover - defensive
            action=ACTION_RESTORE_BLOCKED,
            reason_code=f"unrecognized_runtime_health_{runtime_health}",
            session_id=session_id,
            binding_id=binding_id,
        )

    task_id, activity_id, execution_run_id = service.get_current_task_activity_for_binding(conn, binding_id)
    if execution_run_id is None:
        # ACTIVE Binding with no open managed ExecutionRun is an
        # inconsistent state for cold-restore purposes (Issue: "duplicate
        # open session claim / DB corrupt / ... profile不明 ... は managed
        # session について fail-closed にする") -- never guess a runtime
        # flavor.
        return ResumeDecision(
            action=ACTION_RESTORE_BLOCKED,
            reason_code="active_binding_missing_open_managed_run",
            session_id=session_id,
            binding_id=binding_id,
            task_id=task_id,
            activity_id=activity_id,
        )

    run = service.get_execution_run(conn, execution_run_id)
    effective_profile = config.resolve_effective_runtime_profile(
        run["run_kind"], run["runtime_profile"], run["resume_profile"]
    )

    if effective_profile == config.INVALID_MANAGED_PROFILE:
        return ResumeDecision(
            action=ACTION_RESTORE_BLOCKED,
            reason_code="invalid_managed_profile",
            session_id=session_id,
            binding_id=binding_id,
            task_id=task_id,
            activity_id=activity_id,
            execution_run_id=execution_run_id,
            effective_profile=effective_profile,
        )
    if effective_profile == config.NATIVE_CLAUDE_RUNTIME_PROFILE:
        action = ACTION_LAUNCH_NATIVE
    elif effective_profile == config.CLAUDE_GPT_RUNTIME_PROFILE:
        action = ACTION_LAUNCH_CLAUDE_GPT
    else:  # pragma: no cover - defensive, config contract keeps this closed
        return ResumeDecision(
            action=ACTION_RESTORE_BLOCKED,
            reason_code=f"unrecognized_effective_profile_{effective_profile}",
            session_id=session_id,
            binding_id=binding_id,
            task_id=task_id,
            activity_id=activity_id,
            execution_run_id=execution_run_id,
        )
    return ResumeDecision(
        action=action,
        reason_code="active_managed_binding_dispatchable",
        session_id=session_id,
        binding_id=binding_id,
        task_id=task_id,
        activity_id=activity_id,
        execution_run_id=execution_run_id,
        effective_profile=effective_profile,
    )


# ---------------------------------------------------------------------------
# Two-phase restore state machine -- pre-ACK half (Issue #2569 "Two-phase
# restore state machine" section)
# ---------------------------------------------------------------------------
#
# ACTIVE -> RESTORING happens HERE, before any launch is attempted (AC17).
# The post-ACK half (SessionStart(source=resume, session_id=S) ->
# old-run-technical-close -> new-run-start -> locator re-home -> ACTIVE) is
# already implemented generically by `task_context_hook_flows.on_session_start`
# (Issue #2569's session-id-anchor-first fix makes it resolve correctly
# across a cold restart, where the Herdr locator itself is not stable -- see
# that module) -- this dispatcher deliberately does NOT duplicate that half;
# it only ever transitions the Binding OUT of ACTIVE before a launch attempt
# and INTO RESTORE_BLOCKED if the attempt fails before ACK.


def prepare_managed_resume(session_id: str) -> ResumeDecision:
    """Typed service operation (Issue #2569 naming): classify
    ``session_id``'s Binding and, if it is dispatchable, transition
    ACTIVE -> RESTORING inside the dispatcher's own fixed-identity DB
    (AC15) before returning. Never launches a process itself --
    ``execute_resume_decision()`` is the only I/O-performing function.

    PR #2731 review fix_delta P2-1 ("classify+transition が atomic では
    ない -- TOCTOU race"): the classification read and the ACTIVE ->
    RESTORING (or -> RESTORE_BLOCKED) write below now run inside ONE
    ``db.write_transaction`` (``BEGIN IMMEDIATE``), instead of
    ``classify_for_resume`` reading outside any transaction and a separate
    ``service.set_binding_health`` call opening its own transaction
    afterwards. ``BEGIN IMMEDIATE`` acquires SQLite's write lock up front
    (before the read below runs), so two concurrent callers for the same
    session_id fully serialize here: whichever call's transaction commits
    first is the only one that can observe ACTIVE and transition it to
    RESTORING; the second call's ``BEGIN IMMEDIATE`` blocks (within the
    connection's busy_timeout budget) until the first commits, then
    re-reads the NOW-RESTORING state itself and classifies to
    ``ACTION_NOOP_ALREADY_RESTORING`` -- never a second concurrent
    ``launch_*`` result for the same Binding."""
    conn = open_dispatcher_db()
    try:
        with db.write_transaction(conn):
            decision = classify_for_resume(conn, session_id)
            if decision.action in _LAUNCHABLE_ACTIONS and decision.binding_id:
                service._set_binding_health_tx(conn, decision.binding_id, "RESTORING")
            elif decision.action == ACTION_RESTORE_BLOCKED and decision.binding_id:
                service._set_binding_health_tx(conn, decision.binding_id, "RESTORE_BLOCKED")
        return decision
    finally:
        conn.close()


def classify_for_resume_readonly(session_id: str) -> ResumeDecision:
    """PR #2731 review fix_delta P2-2 ("--dry-run が実際の復元を妨げる状態
    変更を行う"): the read-only counterpart of ``prepare_managed_resume``
    for ``--dry-run`` callers. Uses ``task_context_db.connect_readonly``
    (never ``open_dispatcher_db``/``db.connect``) -- no DB file creation, no
    migration, no ``ACTIVE -> RESTORING`` write, no filesystem state-root
    materialization side effect of any kind. If the state root/DB has not
    been materialized yet at all, this returns a NOOP_UNMANAGED-shaped
    decision rather than creating it."""
    db_file = resolve_dispatcher_state_root() / config.DB_FILE_NAME
    conn = db.connect_readonly(db_file)
    if conn is None:
        return ResumeDecision(
            action=ACTION_NOOP_UNMANAGED,
            reason_code="state_root_not_yet_materialized",
            session_id=session_id,
        )
    try:
        return classify_for_resume(conn, session_id)
    finally:
        conn.close()


def mark_restore_blocked_if_pending(binding_id: str, execution_run_id: str | None) -> bool:
    """AC17 + PR #2731 review fix_delta P1-2 ("mark_restore_blocked() は
    現在の状態を確認せず更新するため ... 正常復元済みの Binding を
    RESTORE_BLOCKED へ戻せる"): only transitions ``binding_id`` to
    RESTORE_BLOCKED if it is STILL the exact same pending RESTORING
    transaction this caller is reacting to -- i.e. ``runtime_health`` is
    still ``RESTORING`` AND (when ``execution_run_id``, the PRE-restore/old
    managed ExecutionRun id, is given) that old run has not already been
    technically closed by a successful ACK. Both the read and the
    conditional write happen inside ONE ``BEGIN IMMEDIATE`` transaction, so
    a late/stale dispatch-failure signal arriving after
    ``SessionStart(source=resume, ...)`` already advanced the Binding to
    ACTIVE (and technically closed the old run) is correctly recognized as
    stale and is a no-op -- it never downgrades an already-ACTIVE Binding.
    Returns ``True`` if the RESTORE_BLOCKED transition was actually
    applied, ``False`` if this was a no-op (already ACK'd / already
    blocked / binding health changed out from under this pending attempt).

    Opens its own fresh connection at the dispatcher's fixed-identity state
    root (AC15) -- callers must not reuse a connection that might already
    be closed/stale by the time a launch failure/timeout is observed."""
    conn = open_dispatcher_db()
    try:
        with db.write_transaction(conn):
            binding = service.get_binding(conn, binding_id)
            if binding["runtime_health"] != "RESTORING":
                return False
            if execution_run_id:
                run = service.get_execution_run(conn, execution_run_id)
                if run["ended_at"] is not None:
                    # The old run was already technically closed -- an ACK
                    # already completed the restore (it is ACTIVE again, or
                    # mid-flight to becoming so); this failure signal is
                    # stale and must not downgrade a successful restore.
                    return False
            service._set_binding_health_tx(conn, binding_id, "RESTORE_BLOCKED")
            return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bounded ACK collection (PR #2731 review fix_delta P1-2)
# ---------------------------------------------------------------------------
#
# Herdr's `pane run`/`agent start` returning exit 0 only means the launch
# COMMAND was successfully injected/sent into the pane -- it is NOT proof
# that the resumed Claude process actually started or that
# `SessionStart(source=resume, session_id=S)` for the target session was
# ever observed. `await_ack` below polls the dispatcher's own DB (the same
# state `task_context_hook_flows.on_session_start` mutates once the ACK
# hook payload actually arrives, out-of-process, from the resumed Claude
# session) for a bounded deadline. Success (`"restored"`) requires the
# Binding to have reached ACTIVE again; on deadline expiry, this calls
# ``mark_restore_blocked_if_pending`` -- which is itself guarded, so an ACK
# that lands concurrently with (just after) the deadline is never
# downgraded from ACTIVE back to RESTORE_BLOCKED.

_DEFAULT_ACK_TIMEOUT_SECONDS = 30.0
_DEFAULT_ACK_POLL_INTERVAL_SECONDS = 0.2


def await_ack(
    binding_id: str,
    execution_run_id: str | None,
    *,
    timeout_seconds: float = _DEFAULT_ACK_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _DEFAULT_ACK_POLL_INTERVAL_SECONDS,
    clock=time.monotonic,
    sleep=time.sleep,
) -> str:
    """Poll for the ACK (Binding back at ACTIVE) within ``timeout_seconds``
    of wall-clock budget starting NOW. Returns one of:

    - ``"restored"`` -- the Binding reached ACTIVE within the deadline
      (the post-ACK half, ``task_context_hook_flows.on_session_start``,
      observed the matching ``SessionStart(source=resume, session_id=S)``
      and completed old-run-close/new-run-start/locator-re-home/ACTIVE).
    - ``"restore_blocked"`` -- either some other observer already
      transitioned the Binding to RESTORE_BLOCKED, or the deadline expired
      first and this call itself applied that transition (guarded --
      ``mark_restore_blocked_if_pending`` never downgrades an ACK that
      raced in concurrently).
    - ``"noop_no_longer_pending"`` -- the Binding is neither RESTORING nor
      ACTIVE nor RESTORE_BLOCKED by the time this polled/timed out (e.g.
      externally SUSPENDED/DETACHED mid-flight) -- reported distinctly so
      callers do not conflate it with an ordinary timeout.
    """
    deadline = clock() + timeout_seconds
    while True:
        conn = open_dispatcher_db()
        try:
            binding = service.get_binding(conn, binding_id)
        finally:
            conn.close()
        health = binding["runtime_health"]
        if health == "ACTIVE":
            return "restored"
        if health == "RESTORE_BLOCKED":
            return "restore_blocked"
        if health != "RESTORING":
            return "noop_no_longer_pending"
        now = clock()
        if now >= deadline:
            mark_restore_blocked_if_pending(binding_id, execution_run_id)
            return "restore_blocked"
        sleep(min(poll_interval_seconds, deadline - now))


def await_all_acks(
    pending: list[tuple[str, str | None]],
    *,
    timeout_seconds: float = _DEFAULT_ACK_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _DEFAULT_ACK_POLL_INTERVAL_SECONDS,
    clock=time.monotonic,
    sleep=time.sleep,
) -> dict[str, str]:
    """Multi-pane counterpart of ``await_ack`` (fix_delta P1-2: "全pane
    へのdispatch後に共通の期限で結果を回収すれば、1つの失敗paneが他の
    復元を長時間止める必要もありません"). ``pending`` is a list of
    ``(binding_id, execution_run_id)`` pairs -- ONE shared deadline starting
    NOW covers every one of them; each round polls every still-pending
    binding once (never sequentially blocking on one binding's own full
    timeout before moving to the next). Returns ``{binding_id: status}``
    with the same status vocabulary as ``await_ack``."""
    deadline = clock() + timeout_seconds
    results: dict[str, str] = {}
    remaining = list(pending)
    while remaining:
        still_pending: list[tuple[str, str | None]] = []
        for binding_id, execution_run_id in remaining:
            conn = open_dispatcher_db()
            try:
                binding = service.get_binding(conn, binding_id)
            finally:
                conn.close()
            health = binding["runtime_health"]
            if health == "ACTIVE":
                results[binding_id] = "restored"
            elif health == "RESTORE_BLOCKED":
                results[binding_id] = "restore_blocked"
            elif health != "RESTORING":
                results[binding_id] = "noop_no_longer_pending"
            else:
                still_pending.append((binding_id, execution_run_id))
        remaining = still_pending
        if not remaining:
            break
        now = clock()
        if now >= deadline:
            for binding_id, execution_run_id in remaining:
                mark_restore_blocked_if_pending(binding_id, execution_run_id)
                results[binding_id] = "restore_blocked"
            break
        sleep(min(poll_interval_seconds, deadline - now))
    return results


# ---------------------------------------------------------------------------
# Launch argv construction -- repository-owned finite runtime profiles only
# ---------------------------------------------------------------------------
#
# Issue Outcome: "raw argv/environment/credential replay方式にしない。
# repository-defined finite runtime profileからcanonical launcherを再適用
# する。" build_launch_argv() below only ever re-applies ONE of exactly two
# fixed, repository-owned launch shapes -- it never replays arbitrary
# captured argv/env from a prior process.
#
# The exact Herdr surface differs per profile, matching the causal-probe
# evidence recorded in the Issue (Herdr's `agent` API validates/tracks the
# recognized `claude` TUI signature; the Claude-GPT launcher is a
# repository-owned wrapper SCRIPT, not the `claude` executable itself, so it
# cannot go through `--kind claude`'s PATH-resolved executable and instead
# runs as a plain pane command):
#
# - Native: ``herdr [--session S] agent start <name> --kind claude
#   --pane <pane_id> -- --resume <session_id>`` -- this is the literal
#   invocation shape the round-2 automatic-trigger sub-probe observed the
#   `[[startup]]` hook itself execute
#   (https://github.com/squne121/loop-protocol/issues/2569#issuecomment-5772732319).
# - Claude-GPT: ``herdr [--session S] pane run <pane_id>
#   scripts/claude-gpt/launch.sh -- --resume <session_id>`` -- the
#   repository-owned launcher wrapper, run as a plain pane command (never
#   through `agent start --kind claude`, which would bypass the wrapper's
#   env swap and resolve/launch plain Native `claude` instead -- the exact
#   "Claude-GPT restore silently downgrades to Native" failure mode AC6/the
#   Issue Outcome forbid).
#
# Known residual caveat (round-3 causal probe): a nested Claude Code
# environment can inherit `CLAUDE_CODE_CHILD_SESSION`, which implicitly
# disables transcript persistence in the resumed process. Neither
# `herdr agent start` nor `herdr pane run` expose an env-injection flag
# this module could use to set `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`
# on the resumed process itself, so this is documented here (and in
# docs/dev/task-context.md) as a known limitation rather than silently
# left unaddressed or falsely claimed fixed.
#
# PR #2731 fix_delta corrective iteration (2026-09-22): direct reproduction
# of the exact production argv shape this module builds for Claude-GPT
# (`scripts/claude-gpt/launch.sh -- --resume <session_id> ...`), run from a
# nested Claude Code environment with `CLAUDE_CODE_CHILD_SESSION=1` ambient
# (matching the conditions under which the independent test-runner canary
# reported "No conversation found"), did NOT reproduce that failure across
# three attempts (same-cwd create->resume, cross-cwd resume, fresh
# disposable create->resume) -- `--resume` correctly recalled a canary
# token each time.
#
# PR #2731 Real Herdr canary (2026-09-24, root orchestrator, disposable
# named Herdr session with `resume_agents_on_restore=false` + `[[startup]]`
# plugin hook; cleanup confirmed): this module's actual `launch_native` and
# `launch_claude_gpt` decisions were both dispatched end to end against a
# real Herdr server abrupt `kill -9` cold restart (not a direct-script
# reproduction). The Native and Claude-GPT resumed processes each correctly
# live-recalled a pre-planted canary token via a fresh prompt, and OS-level
# `/proc/<pid>/status` PPid-chain tracing confirmed both resumed processes
# trace back to the disposable session's own herdr server process. This
# closes the open question left by the two entries above for the exact
# argv shapes this module builds: exact restore for both profiles is now
# confirmed working end to end in a real Herdr cold restart, not only via
# direct script reproduction. See docs/dev/task-context.md ("Real Herdr
# canary（PR #2731、2026-09-24）完全実施結果") for the full account.


def _native_agent_name(decision: ResumeDecision) -> str:
    """Deterministic, `[a-z][a-z0-9_-]{0,31}`-compatible unique agent name
    for `herdr agent start` (Herdr's own naming constraint) -- derived from
    the Binding id so re-dispatching the same Binding always uses the same
    name (never a fresh random name that could collide with a stale
    still-registered agent name from a prior failed attempt)."""
    raw = (decision.binding_id or decision.session_id or "resume").split("_")[-1]
    return f"r-{raw}"[:32]


def build_launch_argv(
    decision: ResumeDecision,
    *,
    claude_gpt_launch_script: Path | str | None = None,
    agent_name: str | None = None,
    pane_id: str = "",
) -> list[str]:
    """Pure argv-construction helper -- no I/O, fully deterministic given
    ``decision``. Returns the FULL ``herdr ...`` argv (herdr subcommand
    included) since Native and Claude-GPT use different Herdr subcommand
    surfaces (see module-level comment above). Raises ``ValueError`` if
    ``decision.action`` is not one of the two launchable actions or if
    ``pane_id`` is empty."""
    if not pane_id:
        raise ValueError("pane_id is required to build a launch argv")
    if decision.action == ACTION_LAUNCH_NATIVE:
        name = agent_name or _native_agent_name(decision)
        return ["agent", "start", name, "--kind", "claude", "--pane", pane_id, "--", "--resume", decision.session_id]
    if decision.action == ACTION_LAUNCH_CLAUDE_GPT:
        script = str(claude_gpt_launch_script or _DEFAULT_CLAUDE_GPT_LAUNCH_SCRIPT)
        return ["pane", "run", pane_id, script, "--", "--resume", decision.session_id]
    raise ValueError(f"decision.action={decision.action!r} is not a launchable action")


# ---------------------------------------------------------------------------
# Execution -- the only functions in this module that perform process I/O
# ---------------------------------------------------------------------------


def execute_resume_decision(
    decision: ResumeDecision,
    *,
    pane_id: str,
    herdr_bin: str = "herdr",
    herdr_session: str | None = None,
    claude_gpt_launch_script: Path | str | None = None,
    agent_name: str | None = None,
    run_fn=None,
) -> subprocess.CompletedProcess:
    """Dispatch ``decision`` (must be one of the two launchable actions)
    into the exact Herdr pane ``pane_id``. This is the process-boundary
    Issue AC10 requires evidence of: this dispatcher process (a child of
    Herdr's ``[[startup]]`` hook invocation) spawns the ``herdr`` CLI (a
    child of THIS process), which talks to Herdr's own server, which then
    executes the resolved launch command as the target pane's foreground
    process (a descendant of the Herdr server process). ``run_fn`` is
    injectable purely so tests never spawn a real herdr/claude process --
    left unset, it resolves ``subprocess.run`` at CALL time (a dynamic
    module-attribute lookup, not a def-time-bound default) so
    ``unittest.mock``/``monkeypatch`` patching ``subprocess.run`` works
    uniformly whether a caller passes ``run_fn`` explicitly or reaches this
    function indirectly (e.g. via the CLI's ``main()``, which never exposes
    its own ``run_fn`` knob)."""
    if run_fn is None:
        run_fn = subprocess.run
    subcommand_argv = build_launch_argv(
        decision, claude_gpt_launch_script=claude_gpt_launch_script, agent_name=agent_name, pane_id=pane_id
    )
    herdr_argv = [herdr_bin]
    if herdr_session:
        herdr_argv += ["--session", herdr_session]
    herdr_argv += subcommand_argv
    try:
        result = run_fn(herdr_argv, check=False, capture_output=True, text=True)
    except OSError:
        if decision.binding_id:
            mark_restore_blocked_if_pending(decision.binding_id, decision.execution_run_id)
        raise
    if result.returncode != 0 and decision.binding_id:
        # Pre-ACK failure (AC17): the launch command itself could not be
        # dispatched into the pane -- fail closed rather than leaving the
        # Binding stuck at RESTORING with nothing actually launched. Guarded
        # (fix_delta P1-2) so a late/stale failure that arrives after an ACK
        # already succeeded never downgrades an already-ACTIVE Binding.
        mark_restore_blocked_if_pending(decision.binding_id, decision.execution_run_id)
    return result


# ---------------------------------------------------------------------------
# CLI entrypoint -- single-session-id dispatch (the `[[startup]]`-hook
# orchestrator drives this once per discovered pane/agent_session; see
# docs/dev/task-context.md "Cold-restart resume dispatcher" for the
# multi-pane enumeration this CLI is composed with).
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True, help="Saved Herdr agent_session/Claude session id")
    parser.add_argument("--pane-id", required=True, help="Herdr pane id to dispatch the resume command into")
    parser.add_argument("--herdr-bin", default="herdr")
    parser.add_argument("--herdr-session", default=None, help="Named Herdr session (omit for the default session)")
    parser.add_argument("--claude-gpt-launch-script", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Classify only (read-only DB connection, no write of any kind -- "
            "fix_delta P2-2); never actually launch."
        ),
    )
    parser.add_argument(
        "--ack-timeout-seconds",
        type=float,
        default=_DEFAULT_ACK_TIMEOUT_SECONDS,
        help="Bounded wait, after a successful Herdr dispatch, for the SessionStart ACK (fix_delta P1-2)",
    )
    parser.add_argument(
        "--no-await-ack",
        action="store_true",
        help="Dispatch only; report dispatched_waiting_ack immediately instead of polling for the ACK",
    )
    args = parser.parse_args(argv)

    # fix_delta P2-2: --dry-run uses the read-only classify path -- never
    # `prepare_managed_resume` (which writes ACTIVE -> RESTORING) -- so a
    # dry-run invocation can NEVER block a subsequent real run.
    decision = classify_for_resume_readonly(args.session_id) if args.dry_run else prepare_managed_resume(
        args.session_id
    )
    result: dict[str, Any] = {"decision": decision.to_public_dict(), "dispatch_status": "not_dispatched"}

    if args.dry_run or decision.action not in _LAUNCHABLE_ACTIONS:
        print(json.dumps(result))
        return 0

    # fix_delta P1-2: the CLI's exit code/result JSON must distinguish
    # "the launch command was sent to Herdr" from "the resume actually
    # succeeded" -- and must be nonzero when the Herdr dispatch ITSELF
    # fails (previously `main()` always returned 0 even then).
    try:
        proc = execute_resume_decision(
            decision,
            pane_id=args.pane_id,
            herdr_bin=args.herdr_bin,
            herdr_session=args.herdr_session,
            claude_gpt_launch_script=args.claude_gpt_launch_script,
        )
    except OSError as exc:
        result["dispatch_status"] = "dispatch_failed"
        result["error"] = str(exc)
        print(json.dumps(result))
        return 1

    result["herdr_pane_run_returncode"] = proc.returncode
    if proc.returncode != 0:
        result["dispatch_status"] = "dispatch_failed"
        print(json.dumps(result))
        return 1

    if args.no_await_ack:
        result["dispatch_status"] = "dispatched_waiting_ack"
        print(json.dumps(result))
        return 0

    ack_status = await_ack(
        decision.binding_id, decision.execution_run_id, timeout_seconds=args.ack_timeout_seconds
    )
    result["dispatch_status"] = ack_status
    print(json.dumps(result))
    return 0 if ack_status == "restored" else 1


if __name__ == "__main__":
    raise SystemExit(main())

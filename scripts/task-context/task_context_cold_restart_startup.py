#!/usr/bin/env python3
"""Task Context v1 -- Herdr `[[startup]]`-hook-invoked multi-pane cold-restart
resume orchestrator (Issue #2569 "Profile-aware resume dispatcher"; PR #2731
review fix_delta P1-1 "自動復元に必要なstartup配線が、成果物として同梱
されていない").

This is the ONE committed, repository-owned artifact the Herdr
``[[startup]]`` plugin hook actually invokes -- not a canary-only scratch
script. It closes the chain the fix_delta required:

    専用named sessionの復元
      -> [[startup]] hook fires this module's `main()`
      -> discover Native/Claude-GPT restore candidates
         (Task Context DB is the identity authority -- NEVER Herdr's own
         per-pane `agent_session` field, which is never populated for
         Claude-GPT panes at all)
      -> dispatch each candidate (herdr socket context is ambient -- see
         "Session scoping" below)
      -> collect ACK results within one shared bounded deadline
         (`task_context_resume_dispatcher.await_all_acks()`)
      -> exit (nonzero if any candidate failed to reach `restored`)

No new daemon/lease table/lock file/distributed coordinator is introduced --
this is a plain one-shot process the `[[startup]]` hook invocation runs to
completion and exits (Issue Outcome / Stop Conditions).

Session scoping (fix_delta P1-1 correction over an earlier draft of this
module, and over an earlier draft of docs/dev/task-context.md): Herdr's own
plugin docs (https://herdr.dev "Plugins" -> "Install and link") state
plainly that "Installed and linked plugins ... are global to the current
user and available in EVERY Herdr session" -- there is no Herdr-level
mechanism to scope a linked plugin's `[[startup]]` hook to only ONE named
session. Concretely this means the SAME linked plugin's `[[startup]]` hook
also fires on every restart of the human/default Herdr session (or any
other named session on the machine) -- not only the dedicated LOOP_PROTOCOL
project-scoped session `HERDR_CONFIG_PATH` sets
`resume_agents_on_restore = false` for. Left unguarded, that risks the
double-interference the Issue's Real Herdr canary explicitly rules out
("human/default Herdr sessionへ影響しないこと"). This module therefore:

1. Refuses to do ANYTHING (no herdr subprocess call, no DB read) unless
   ``LOOP_TASK_CONTEXT_COLD_RESTART_SCOPE`` (see
   ``task_context_config.COLD_RESTART_SCOPE_ENV_VAR``) is set to the exact
   sentinel value in the process environment -- an immediate, side-effect-
   free no-op otherwise. Only the dedicated session's OWN launch wrapper
   sets this env var (alongside ``HERDR_CONFIG_PATH``); ordinary OS process
   environment inheritance (a plugin startup-hook command is a child
   process of the Herdr server that launched it) carries it down without
   requiring any Herdr-specific per-session plugin scoping support.
2. Never passes an explicit ``--session <name>`` flag to any ``herdr``
   invocation -- doing so would let this process address (and, if the name
   did not already exist, silently CREATE) an arbitrary OTHER named
   session. Instead every ``herdr`` call below is unqualified, so it
   implicitly targets whichever session's socket this plugin process is
   already running inside (Herdr injects ``HERDR_SOCKET_PATH``/
   ``HERDR_BIN_PATH`` into every plugin runtime command, scoped to that
   invocation's own session -- see the Herdr "Plugins" doc, "Commands and
   environment").

Installation (see docs/dev/task-context.md "Cold-restart resume dispatcher"
for the full operational account, and
``scripts/task-context/examples/herdr-plugin.toml`` /
``scripts/task-context/examples/herdr_config_dedicated_session.toml`` for
the actual example manifest/config this module has been exercised against
in the Real Herdr canary).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MIGRATIONS_DIR = os.path.join(_THIS_DIR, "migrations")
for _dir in (_THIS_DIR, _MIGRATIONS_DIR):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import task_context_config as config  # noqa: E402
import task_context_db as db  # noqa: E402
import task_context_resume_dispatcher as dispatcher  # noqa: E402
import task_context_service as service  # noqa: E402

# ---------------------------------------------------------------------------
# Discovery result vocabulary (Issue #2742 -- follow-up to #2570's runtime
# acceptance fact-check; aligned with the #2570 result vocabulary
# `restored` / `expected_skip_suspended` / `restore_blocked` /
# `unresolved_target`).
# ---------------------------------------------------------------------------

# A managed (ACTIVE) Binding's recorded `herdr_locator` no longer names any
# currently-live pane in this session -- there is nowhere to dispatch a
# resume command to. Previously silently dropped from the candidate set
# (indistinguishable from "no candidates at all" -- a false green).
REASON_LOCATOR_NOT_LIVE = "locator_not_live"

# Two or more ACTIVE Bindings claim the exact same live `herdr_locator`.
# `runtime_locations` only enforces `UNIQUE(binding_id) WHERE released_at IS
# NULL` -- it does NOT enforce locator-level uniqueness across different
# Bindings -- so this is an application-level detection, not a DB
# constraint violation. Arbitrarily picking one claimant to dispatch into
# would silently starve the others; instead every claimant for the pane is
# reported and none are dispatched.
REASON_DUPLICATE_LOCATOR_CLAIM = "duplicate_locator_claim"

# A RESTORING Binding whose pre-restore ExecutionRun the guarded
# `mark_restore_blocked_if_pending()` primitive could NOT confirm as stale
# (the primitive itself is authoritative here -- see its own docstring for
# exactly which conditions make it decline the transition). DB state is
# left untouched; this is reported so it never silently vanishes from an
# operator's view the way a permanently-stuck RESTORING Binding previously
# did.
REASON_RESTORING_NOT_PROVABLY_STALE = "restoring_not_provably_stale"

# A RESTORING Binding whose pre-restore ExecutionRun the guarded
# `mark_restore_blocked_if_pending()` primitive DID confirm as stale (still
# RESTORING, pre-restore run not yet ended) -- converged to RESTORE_BLOCKED.
# This is the "orphan RESTORING" defect's resolution: a prior dispatch
# attempt that never reached SessionStart(source=resume) ACK before this
# process itself was interrupted (e.g. host crash/reboot mid-restore) is no
# longer left stuck at RESTORING forever across subsequent cold restarts.
REASON_STALE_RESTORING_CONVERGED = "stale_restoring_converged"


class HerdrDiscoveryError(RuntimeError):
    """`herdr pane list` itself failed or returned unparseable output --
    treated as a hard failure of this orchestrator run (fail-closed: never
    silently proceed with an empty/guessed candidate set)."""


def _default_herdr_bin() -> str:
    """Prefer ``HERDR_BIN_PATH`` (the exact binary Herdr itself injects for
    plugin runtime commands -- portable across Unix socket / Windows named
    pipe transports per the Herdr "Plugins" doc), falling back to a bare
    ``herdr`` lookup on ``PATH`` only for manual/local invocation outside a
    real plugin context (e.g. this repository's own deterministic tests)."""
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def discover_resume_candidates(
    conn: Any,
    *,
    herdr_bin: str,
    run_fn=None,
) -> dict[str, Any]:
    """Enumerate the AMBIENT Herdr session's CURRENTLY LIVE panes
    (unqualified ``herdr pane list`` -- see module docstring "Session
    scoping" for why no ``--session`` flag is ever passed) and cross-
    reference them against Task Context's OWN durable state
    (``tab_bindings``/``runtime_locations``, via ``task_context_db``'s
    typed read helper -- never raw ad hoc SQL) to resolve dispatch
    candidates.

    fix_delta P1-1: this deliberately does NOT use Herdr's own per-pane
    ``agent_session`` field as the identity authority -- that field is only
    ever populated for panes started via ``herdr agent start --kind
    claude`` (Native), never for Claude-GPT panes (started via plain
    ``herdr pane run <script>``), so a discovery path keyed off it would
    silently skip every Claude-GPT candidate (docs/dev/task-context.md
    "Cold-restart resume dispatcher" 運用上の知見). Instead, Task Context's
    own ACTIVE-with-a-current-session managed Bindings are the candidate
    set; the live pane list is used ONLY to confirm the Binding's last-
    known ``herdr_locator`` still names a currently-live pane in THIS
    session (Herdr's own session/pane persistence typically preserves the
    exact locator across a cold restart -- only the agent process auto-
    launch into it is suppressed by ``resume_agents_on_restore=false``) --
    i.e. the locator is used only to find WHERE to dispatch, never as the
    identity itself.

    Issue #2742 (follow-up to #2570's runtime acceptance fact-check): this
    now also (a) reports -- rather than silently drops -- an ACTIVE
    Binding whose recorded locator no longer names a live pane, or whose
    locator is claimed by more than one ACTIVE Binding at once, and (b)
    includes RESTORING Bindings in discovery so a prior dispatch attempt
    that never reached ACK before THIS process itself was interrupted
    (host crash/reboot mid-restore) is re-detected on a subsequent cold
    restart instead of being permanently orphaned at RESTORING.

    Returns a dict with three keys:

    - ``"candidates"``: a list of ``(session_id, pane_id)`` pairs --
      exactly the previous return shape -- for ACTIVE Bindings whose
      locator uniquely names a currently-live pane. Dispatchable.
    - ``"unresolved_targets"``: a list of
      ``{"binding_id", "session_id", "reason"}`` dicts for Bindings this
      run could not (or must not) resolve a dispatch target for. No
      resume command is ever sent for these. ``reason`` is one of
      ``REASON_LOCATOR_NOT_LIVE`` / ``REASON_DUPLICATE_LOCATOR_CLAIM`` /
      ``REASON_RESTORING_NOT_PROVABLY_STALE``.
    - ``"restore_blocked"``: a list of
      ``{"binding_id", "session_id", "reason": REASON_STALE_RESTORING_CONVERGED}``
      dicts for RESTORING Bindings the guarded
      ``dispatcher.mark_restore_blocked_if_pending()`` primitive confirmed
      as stale and converged to RESTORE_BLOCKED (idempotent -- a Binding
      already RESTORE_BLOCKED is no longer selected by the query below at
      all, so re-running this against the same DB state never re-emits it
      here nor re-applies any transition).
    """
    if run_fn is None:
        run_fn = subprocess.run
    proc = run_fn([herdr_bin, "pane", "list"], check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HerdrDiscoveryError(
            f"`herdr pane list` failed (exit {proc.returncode}): {(proc.stderr or '').strip()}"
        )
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HerdrDiscoveryError(f"`herdr pane list` returned unparseable JSON: {exc}") from exc
    panes = payload.get("result", {}).get("panes", [])
    live_pane_ids = {pane["pane_id"] for pane in panes if "pane_id" in pane}

    rows = db.execute_readonly(
        conn,
        "SELECT tb.id AS binding_id, tb.current_claude_session_id AS session_id, "
        "tb.runtime_health AS runtime_health, "
        "rl.herdr_locator AS herdr_locator, tb.updated_at AS updated_at "
        "FROM tab_bindings tb "
        "JOIN runtime_locations rl ON rl.binding_id = tb.id AND rl.released_at IS NULL "
        "WHERE tb.runtime_health IN ('ACTIVE', 'RESTORING') AND tb.current_claude_session_id IS NOT NULL "
        "ORDER BY tb.updated_at DESC",
    ).fetchall()

    candidates: list[tuple[str, str]] = []
    unresolved_targets: list[dict[str, Any]] = []
    restore_blocked: list[dict[str, Any]] = []

    # AC4: group ACTIVE rows by locator FIRST so a duplicate claim on one
    # live pane is detected and reported for ALL claimants -- never
    # resolved by arbitrarily picking whichever row `ORDER BY
    # tb.updated_at DESC` happened to sort first.
    active_rows_by_locator: dict[str, list[Any]] = {}
    for row in rows:
        if row["runtime_health"] != "ACTIVE":
            continue
        locator = row["herdr_locator"]
        session_id = row["session_id"]
        if not locator or not session_id:
            continue
        active_rows_by_locator.setdefault(locator, []).append(row)

    for locator, claimant_rows in active_rows_by_locator.items():
        if locator not in live_pane_ids:
            # AC1: the recorded locator no longer names any currently-live
            # pane -- nothing to dispatch to. Explicitly reported (never
            # silently dropped) so this is distinguishable from "no
            # candidates at all".
            for row in claimant_rows:
                unresolved_targets.append(
                    {
                        "binding_id": row["binding_id"],
                        "session_id": row["session_id"],
                        "reason": REASON_LOCATOR_NOT_LIVE,
                    }
                )
            continue
        if len(claimant_rows) > 1:
            # AC4: same live pane claimed by >1 ACTIVE Binding -- report
            # every claimant, dispatch none.
            for row in claimant_rows:
                unresolved_targets.append(
                    {
                        "binding_id": row["binding_id"],
                        "session_id": row["session_id"],
                        "reason": REASON_DUPLICATE_LOCATOR_CLAIM,
                    }
                )
            continue
        row = claimant_rows[0]
        candidates.append((row["session_id"], locator))

    # AC2: RESTORING Bindings -- re-detect a prior dispatch attempt that
    # never reached ACK before a previous orchestrator run was itself
    # interrupted. Delegates staleness confirmation entirely to the
    # existing guarded primitive (never re-implements its guard
    # conditions -- Stop Condition boundary with
    # `task_context_resume_dispatcher.py`).
    for row in rows:
        if row["runtime_health"] != "RESTORING":
            continue
        binding_id = row["binding_id"]
        session_id = row["session_id"]
        _task_id, _activity_id, execution_run_id = service.get_current_task_activity_for_binding(
            conn, binding_id
        )
        guard_applied = dispatcher.mark_restore_blocked_if_pending(binding_id, execution_run_id)
        if guard_applied:
            restore_blocked.append(
                {
                    "binding_id": binding_id,
                    "session_id": session_id,
                    "reason": REASON_STALE_RESTORING_CONVERGED,
                }
            )
        else:
            unresolved_targets.append(
                {
                    "binding_id": binding_id,
                    "session_id": session_id,
                    "reason": REASON_RESTORING_NOT_PROVABLY_STALE,
                }
            )

    return {
        "candidates": candidates,
        "unresolved_targets": unresolved_targets,
        "restore_blocked": restore_blocked,
    }


def run_startup_orchestrator(
    *,
    herdr_bin: str,
    claude_gpt_launch_script: str | None = None,
    ack_timeout_seconds: float = dispatcher._DEFAULT_ACK_TIMEOUT_SECONDS,
    discovery_run_fn=None,
) -> dict[str, Any]:
    """The full one-shot orchestration cycle: discover -> dispatch every
    candidate -> collect ACKs against one shared bounded deadline -> return
    a JSON-serializable summary. Never launches a process itself outside of
    ``task_context_resume_dispatcher.execute_resume_decision`` (dispatch)
    and ``herdr pane list`` (read-only discovery). Never passes
    ``herdr_session`` through to the dispatcher (see module docstring
    "Session scoping") -- every herdr call this whole cycle makes stays
    unqualified/ambient."""
    conn = dispatcher.open_dispatcher_db()
    try:
        discovery = discover_resume_candidates(conn, herdr_bin=herdr_bin, run_fn=discovery_run_fn)
    finally:
        conn.close()

    candidates = discovery["candidates"]
    unresolved_targets = discovery["unresolved_targets"]
    stale_restore_blocked = discovery["restore_blocked"]

    results: list[dict[str, Any]] = []
    pending_acks: list[tuple[str, str | None]] = []
    for session_id, pane_id in candidates:
        decision = dispatcher.prepare_managed_resume(session_id)
        entry: dict[str, Any] = {
            "session_id": session_id,
            "pane_id": pane_id,
            "decision": decision.to_public_dict(),
            "dispatch_status": "not_dispatched",
        }
        if decision.action in dispatcher._LAUNCHABLE_ACTIONS:
            try:
                proc = dispatcher.execute_resume_decision(
                    decision,
                    pane_id=pane_id,
                    herdr_bin=herdr_bin,
                    claude_gpt_launch_script=claude_gpt_launch_script,
                )
            except OSError as exc:
                entry["dispatch_status"] = "dispatch_failed"
                entry["error"] = str(exc)
                results.append(entry)
                continue
            entry["herdr_pane_run_returncode"] = proc.returncode
            if proc.returncode == 0:
                entry["dispatch_status"] = "dispatched_waiting_ack"
                pending_acks.append((decision.binding_id, decision.execution_run_id))
            else:
                entry["dispatch_status"] = "dispatch_failed"
        results.append(entry)

    # AC1/AC2/AC4: discovery-level classifications that were never
    # dispatchable at all -- reported as first-class result entries
    # (never silently dropped from the summary) so `any_failed` below can
    # see them.
    for entry_data in unresolved_targets:
        results.append(
            {
                "session_id": entry_data["session_id"],
                "pane_id": None,
                "binding_id": entry_data["binding_id"],
                "dispatch_status": "unresolved_target",
                "reason": entry_data["reason"],
            }
        )
    for entry_data in stale_restore_blocked:
        results.append(
            {
                "session_id": entry_data["session_id"],
                "pane_id": None,
                "binding_id": entry_data["binding_id"],
                "dispatch_status": "restore_blocked",
                "reason": entry_data["reason"],
            }
        )

    if pending_acks:
        ack_statuses = dispatcher.await_all_acks(pending_acks, timeout_seconds=ack_timeout_seconds)
        for entry in results:
            binding_id = entry.get("decision", {}).get("binding_id") if "decision" in entry else entry.get(
                "binding_id"
            )
            if entry["dispatch_status"] == "dispatched_waiting_ack" and binding_id in ack_statuses:
                entry["dispatch_status"] = ack_statuses[binding_id]

    # Issue #2742: previously only `dispatch_status not in ("restored",
    # "not_dispatched")` counted as a failure -- `not_dispatched` (e.g. an
    # ACTIVE candidate that `classify_for_resume` itself immediately
    # classified as RESTORE_BLOCKED, such as an invalid managed profile)
    # was a false-green path, and the new `unresolved_target` /
    # `restore_blocked` discovery-level classifications were not
    # represented at all. Every non-"restored" outcome is now a failure.
    any_failed = any(entry["dispatch_status"] != "restored" for entry in results)
    return {
        "candidates_discovered": len(candidates),
        "results": results,
        "any_failed": any_failed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--herdr-bin",
        default=None,
        help="Defaults to $HERDR_BIN_PATH (the path Herdr itself injects for plugin runtime commands), else 'herdr'",
    )
    parser.add_argument("--claude-gpt-launch-script", default=None)
    parser.add_argument(
        "--ack-timeout-seconds",
        type=float,
        default=dispatcher._DEFAULT_ACK_TIMEOUT_SECONDS,
        help=(
            "Shared bounded deadline (fix_delta P1-2) for ALL dispatched candidates' "
            "ACKs, starting after every dispatch has been sent"
        ),
    )
    parser.add_argument(
        "--force-run-outside-scope-gate",
        action="store_true",
        help=(
            "TESTING/MANUAL ONLY: bypass the LOOP_TASK_CONTEXT_COLD_RESTART_SCOPE gate. "
            "Never set this in a real [[startup]] hook manifest."
        ),
    )
    args = parser.parse_args(argv)

    if not args.force_run_outside_scope_gate and not config.is_cold_restart_dedicated_session():
        # fix_delta P1-1 safety guard: Herdr plugins are user-global, so this
        # exact command also fires on every OTHER Herdr session's restart
        # (including the human/default session) unless this dedicated-
        # session-only env var is present. No herdr subprocess call, no DB
        # read -- an immediate, side-effect-free no-op.
        print(json.dumps({"status": "noop_outside_dedicated_scope", "candidates_discovered": 0, "any_failed": False}))
        return 0

    herdr_bin = args.herdr_bin or _default_herdr_bin()

    try:
        summary = run_startup_orchestrator(
            herdr_bin=herdr_bin,
            claude_gpt_launch_script=args.claude_gpt_launch_script,
            ack_timeout_seconds=args.ack_timeout_seconds,
        )
    except HerdrDiscoveryError as exc:
        print(json.dumps({"status": "discovery_failed", "error": str(exc)}))
        return 1

    print(json.dumps(summary))
    return 1 if summary["any_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

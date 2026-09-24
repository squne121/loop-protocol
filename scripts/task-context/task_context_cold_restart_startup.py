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
) -> list[tuple[str, str]]:
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

    Returns a list of ``(session_id, pane_id)`` pairs, most-recently-updated
    Binding first.
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
        "rl.herdr_locator AS herdr_locator, tb.updated_at AS updated_at "
        "FROM tab_bindings tb "
        "JOIN runtime_locations rl ON rl.binding_id = tb.id AND rl.released_at IS NULL "
        "WHERE tb.runtime_health = 'ACTIVE' AND tb.current_claude_session_id IS NOT NULL "
        "ORDER BY tb.updated_at DESC",
    ).fetchall()

    candidates: list[tuple[str, str]] = []
    for row in rows:
        locator = row["herdr_locator"]
        session_id = row["session_id"]
        if locator and session_id and locator in live_pane_ids:
            candidates.append((session_id, locator))
    return candidates


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
        candidates = discover_resume_candidates(conn, herdr_bin=herdr_bin, run_fn=discovery_run_fn)
    finally:
        conn.close()

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

    if pending_acks:
        ack_statuses = dispatcher.await_all_acks(pending_acks, timeout_seconds=ack_timeout_seconds)
        for entry in results:
            binding_id = entry["decision"].get("binding_id")
            if entry["dispatch_status"] == "dispatched_waiting_ack" and binding_id in ack_statuses:
                entry["dispatch_status"] = ack_statuses[binding_id]

    any_failed = any(
        entry["dispatch_status"] not in ("restored", "not_dispatched") for entry in results
    )
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

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

# A RESTORING Binding discovery re-detected on a subsequent cold restart.
# Contract Reconciliation (2026-09-25, Issue #2742 AC2): this is reported
# UNCONDITIONALLY for every RESTORING Binding -- discovery never calls
# `mark_restore_blocked_if_pending()` and never mutates DB state, because
# that guard's success condition (runtime_health == "RESTORING" AND
# execution_run.ended_at IS NULL) only confirms a pending restore has not
# yet been ACKed, NOT that the underlying process/pane is actually dead
# (a live Herdr handoff can leave a Binding in this exact same DB state).
# DB state is left untouched; this is reported so it never silently
# vanishes from an operator's view the way a permanently-stuck RESTORING
# Binding previously did.
REASON_RESTORING_NOT_PROVABLY_STALE = "restoring_not_provably_stale"

# Reserved (Contract Reconciliation 2026-09-25, Issue #2742 AC2): NEVER
# emitted by `discover_resume_candidates()` today -- kept only so the
# `"restore_blocked"` result key and downstream summary/exit-code wiring
# remain backward compatible for a future independent liveness mechanism
# (e.g. a generation counter or pid liveness check) that could actually
# prove a RESTORING Binding is stale and converge it to RESTORE_BLOCKED.
# See the module Notes for Reviewer for the narrow follow-up this defers
# to.
REASON_STALE_RESTORING_CONVERGED = "stale_restoring_converged"

# Issue #2752 AC4/UNKNOWN: an ACTIVE Binding's locator names a currently-live
# pane (`live_pane_ids`), but `herdr pane process-info` itself failed, was
# empty, was unparseable, is not exposed by this platform at all, or
# returned an entry this module cannot positively attribute to the pane's
# own shell process (identity ambiguous) -- liveness could not be proven
# EITHER way. Never dispatched (duplicate-launch risk); never silently
# treated as either PROVEN_ALIVE or PROVEN_ABSENT.
REASON_LIVENESS_UNRESOLVED = "liveness_unresolved"

# Issue #2752 AC5: an ACTIVE/RESTORING Binding has no CURRENT (unreleased)
# `runtime_locations` row at all -- either it never had one, or every prior
# one has since been released. Previously silently dropped by the discovery
# SQL's (implicit) `INNER JOIN` (indistinguishable from "no managed Bindings
# at all" -- a false green, same class of defect as `REASON_LOCATOR_NOT_LIVE`
# before Issue #2742 fixed it for the "row exists but locator mismatches"
# case).
REASON_MISSING_CURRENT_RUNTIME_LOCATION = "missing_current_runtime_location"


# ---------------------------------------------------------------------------
# Tri-state pane-process liveness classification (Issue #2752)
# ---------------------------------------------------------------------------
#
# `herdr pane list`'s `live_pane_ids` only proves a PANE still exists in this
# session -- it says nothing about whether the RUNTIME PROCESS that pane was
# last known to host is still alive (a Herdr live handoff can preserve a
# pane's PTY/process across a `[[startup]]` hook re-fire; a real cold
# restart, by contrast, loses the process but Herdr can still re-generate a
# pane at the SAME locator). `herdr pane process-info <pane_id>` is the one
# liveness primitive the Issue's Herdr 0.9.1 fact-check confirms actually
# reports foreground-process data (platform-dependent, never guaranteed) --
# see the Issue body's "Herdr 0.9.1 一次資料 fact-check" section. The three
# outcomes below are a closed, fixed vocabulary (Stop Condition: changing
# this 3-value set requires human sign-off).
LIVENESS_PROVEN_ALIVE = "PROVEN_ALIVE"
LIVENESS_PROVEN_ABSENT = "PROVEN_ABSENT"
LIVENESS_UNKNOWN = "UNKNOWN"


def _classify_pane_process_liveness(pane_id: str, *, herdr_bin: str, run_fn) -> tuple[str, dict[str, Any]]:
    """Call ``herdr pane process-info --pane <pane_id>`` (never with
    ``--session``, matching every other discovery-phase herdr call -- see
    module docstring "Session scoping") and classify the pane's runtime-
    process liveness into exactly one of ``LIVENESS_PROVEN_ALIVE`` /
    ``LIVENESS_PROVEN_ABSENT`` / ``LIVENESS_UNKNOWN``. Returns
    ``(classification, evidence)`` where ``evidence`` is a JSON-serializable
    dict recording exactly what this call observed (never a bare boolean/
    opaque flag) -- this is what ``live_runtime_preserved``'s
    ``liveness_evidence`` field and ``unresolved_target``'s
    ``liveness_unresolved`` diagnostics are built from.

    Response shape (fact-checked live against an installed Herdr 0.9.1
    server, Issue #2752 implementation): ``herdr pane process-info --pane
    <id>`` prints ``{"id": ..., "result": {"process_info": {"pane_id": ...,
    "shell_pid": <int>, "foreground_process_group_id": <int | omitted>,
    "foreground_processes": [{"pid": <int>, "name": <str>, "cwd": <str>,
    "argv": [...] (platform-dependent, may be omitted)}, ...] (omitted on
    platforms that do not expose per-process foreground data)}, "type":
    "pane_process_info"}}`` -- i.e. the payload is nested one level under
    ``result.process_info``, NOT directly under ``result``.

    AC8: the ONLY signal this function ever inspects is structured
    foreground-process PID data (compared against the pane's own
    ``shell_pid``) -- never ``cwd``, terminal title, process ordering, or a
    process ``name`` alone. A response this module cannot positively map to
    "is/is not the pane's own shell" is always ``LIVENESS_UNKNOWN``, never
    guessed.

    Never raises -- a transport-level failure (``OSError``, e.g. the herdr
    binary itself is missing) is itself a form of "could not prove
    liveness", so it is folded into ``LIVENESS_UNKNOWN`` rather than
    propagated as ``HerdrDiscoveryError`` (unlike `herdr pane list` failing,
    which aborts the whole discovery run -- a single pane's liveness probe
    failing must never abort discovery for every OTHER Binding)."""
    try:
        proc = run_fn(
            [herdr_bin, "pane", "process-info", "--pane", pane_id],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return LIVENESS_UNKNOWN, {"reason": "process_info_call_raised", "detail": str(exc)}

    if proc.returncode != 0:
        return LIVENESS_UNKNOWN, {
            "reason": "process_info_call_failed",
            "returncode": proc.returncode,
            "stderr": (proc.stderr or "").strip(),
        }

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return LIVENESS_UNKNOWN, {"reason": "process_info_empty_response"}

    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return LIVENESS_UNKNOWN, {"reason": "process_info_unparseable_json"}

    outer_result = payload.get("result") if isinstance(payload, dict) else None
    result = outer_result.get("process_info") if isinstance(outer_result, dict) else None
    if not isinstance(result, dict):
        return LIVENESS_UNKNOWN, {"reason": "process_info_missing_result"}

    shell_pid = result.get("shell_pid")
    foreground_processes = result.get("foreground_processes")
    foreground_process_group_id = result.get("foreground_process_group_id")

    if isinstance(foreground_processes, list):
        non_shell: list[Any] = []
        for entry in foreground_processes:
            if not isinstance(entry, dict) or "pid" not in entry:
                # Identity ambiguous (Issue body AC4 wording): an entry we
                # cannot positively attribute to the pane's own shell or
                # not -- never guess either way.
                return LIVENESS_UNKNOWN, {
                    "reason": "process_info_ambiguous_foreground_process_identity",
                    "foreground_processes": foreground_processes,
                }
            if entry["pid"] != shell_pid:
                non_shell.append(entry)
        if non_shell:
            return LIVENESS_PROVEN_ALIVE, {"foreground_processes": non_shell, "shell_pid": shell_pid}
        # bare-shell-only: parseable, successful response, every reported
        # foreground process IS the pane's own shell (or none at all) --
        # AC3 case 2 (real cold restart re-generating a pane at the same
        # locator).
        return LIVENESS_PROVEN_ABSENT, {"foreground_processes": foreground_processes, "shell_pid": shell_pid}

    if foreground_process_group_id is not None:
        # Some platforms only expose a single foreground-process(-group) id,
        # not a full process list (Herdr socket-api doc: "foreground process
        # group id" when full per-process data is unavailable).
        if shell_pid is not None and foreground_process_group_id == shell_pid:
            return LIVENESS_PROVEN_ABSENT, {
                "foreground_process_group_id": foreground_process_group_id,
                "shell_pid": shell_pid,
            }
        return LIVENESS_PROVEN_ALIVE, {
            "foreground_process_group_id": foreground_process_group_id,
            "shell_pid": shell_pid,
        }

    if shell_pid is not None:
        # This platform published only the pane's own shell pid and nothing
        # about what (if anything) is foreground in it -- "platform 非対応"
        # per the Issue body. Never treat shell_pid alone as proof of either
        # liveness or absence.
        return LIVENESS_UNKNOWN, {"reason": "process_info_platform_no_foreground_data", "shell_pid": shell_pid}

    return LIVENESS_UNKNOWN, {"reason": "process_info_empty_result"}


def _resolve_runtime_profile_for_binding(conn: Any, binding_id: str) -> str | None:
    """Best-effort ``effective_profile`` lookup for a ``live_runtime_preserved``
    entry's observability fields ONLY -- mirrors the read-only half of
    ``task_context_resume_dispatcher.classify_for_resume``'s profile
    resolution, but never writes anything and never gates the
    PROVEN_ALIVE/no-dispatch decision itself (that decision is made purely
    from the liveness classification, before this is even called). Returns
    ``None`` if the Binding has no currently-open managed ExecutionRun to
    resolve a profile from."""
    _task_id, _activity_id, execution_run_id = service.get_current_task_activity_for_binding(conn, binding_id)
    if execution_run_id is None:
        return None
    run = service.get_execution_run(conn, execution_run_id)
    return config.resolve_effective_runtime_profile(run["run_kind"], run["runtime_profile"], run["resume_profile"])


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

    Issue #2752 (follow-up to #2742's "本 Issue の Out of Scope" ACTIVE +
    live-handoff carve-out): a locator naming a currently-live pane does
    NOT by itself prove the pane's runtime process is dead -- Herdr's
    ``[[startup]]`` hook re-fires on a live handoff too (a successful
    handoff best-effort preserves the pane PTY/process), not only on a
    true cold restart. For every ACTIVE Binding whose locator uniquely
    names a currently-live pane, this now calls ``herdr pane process-info``
    (see ``_classify_pane_process_liveness()``) BEFORE deciding whether it
    is dispatchable, classifying it into one of three outcomes:

    - ``LIVENESS_PROVEN_ALIVE``: the pane's runtime process is still
      running (parseable foreground-process evidence beyond the pane's own
      shell). Reported via ``"live_runtime_preserved"`` -- NOT a candidate,
      NEVER dispatched, Binding identity untouched.
    - ``LIVENESS_PROVEN_ABSENT``: ``process-info`` succeeded and clearly
      showed bare-shell-only (no foreground process besides the pane's own
      shell) -- a real cold restart re-generated a pane at the same
      locator (AC3 case 2). Added to ``"candidates"`` exactly like before.
    - ``LIVENESS_UNKNOWN``: the call itself failed/was empty/unparseable/
      not exposed by this platform/identity-ambiguous. Reported via
      ``"unresolved_targets"`` (``REASON_LIVENESS_UNRESOLVED``) -- never
      dispatched, never silently treated as either proven state (AC4/AC8
      fail-closed).

    This probe (a single ``subprocess`` call per unique, non-duplicate-
    claimed ACTIVE locator) runs entirely within this read-only discovery
    phase -- strictly BEFORE and OUTSIDE
    ``task_context_resume_dispatcher.prepare_managed_resume()``'s
    ``write_transaction`` (``BEGIN IMMEDIATE``), which this function never
    calls. It never mutates DB state.

    Returns a dict with four keys:

    - ``"candidates"``: a list of ``(session_id, pane_id)`` pairs --
      exactly the previous return shape -- for ACTIVE Bindings whose
      locator uniquely names a currently-live pane AND whose pane process
      liveness classified as ``LIVENESS_PROVEN_ABSENT``. Dispatchable.
    - ``"unresolved_targets"``: a list of
      ``{"binding_id", "session_id", "reason"}`` dicts for Bindings this
      run could not (or must not) resolve a dispatch target for. No
      resume command is ever sent for these. ``reason`` is one of
      ``REASON_LOCATOR_NOT_LIVE`` / ``REASON_DUPLICATE_LOCATOR_CLAIM`` /
      ``REASON_RESTORING_NOT_PROVABLY_STALE`` / ``REASON_LIVENESS_UNRESOLVED``
      / ``REASON_MISSING_CURRENT_RUNTIME_LOCATION``.
    - ``"restore_blocked"``: ALWAYS empty (Contract Reconciliation
      2026-09-25, Issue #2742 AC2). Reserved for a future independent
      liveness mechanism (e.g. a generation counter or pid liveness
      check) that could confirm a RESTORING Binding is truly stale and
      converge it to RESTORE_BLOCKED -- ``discover_resume_candidates()``
      never calls ``dispatcher.mark_restore_blocked_if_pending()`` and
      never mutates DB state for a RESTORING Binding; every RESTORING
      Binding is reported only via ``"unresolved_targets"`` (see AC2
      Contract Reconciliation note in the module Notes for Reviewer).
    - ``"live_runtime_preserved"`` (Issue #2752): a list of dicts (one per
      ACTIVE Binding classified ``LIVENESS_PROVEN_ALIVE``) with keys
      ``binding_id`` / ``session_id`` / ``pane_id`` / ``runtime_profile``
      (best-effort, may be ``None``) / ``liveness_evidence`` /
      ``launch_commands_dispatched`` (always ``0``) / ``mutations_applied``
      (always ``0``) -- an explicit expected-success outcome, never
      silently indistinguishable from "no candidates at all".
    """
    if run_fn is None:
        run_fn = subprocess.run
    proc = run_fn([herdr_bin, "pane", "list"], check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HerdrDiscoveryError(f"`herdr pane list` failed (exit {proc.returncode}): {(proc.stderr or '').strip()}")
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HerdrDiscoveryError(f"`herdr pane list` returned unparseable JSON: {exc}") from exc
    panes = payload.get("result", {}).get("panes", [])
    live_pane_ids = {pane["pane_id"] for pane in panes if "pane_id" in pane}

    # Issue #2752 AC5: `LEFT JOIN` (never the previous implicit `INNER
    # JOIN`) so an ACTIVE/RESTORING Binding with NO current (unreleased)
    # `runtime_locations` row at all still appears in `rows` (with
    # `herdr_locator IS NULL`) instead of vanishing from discovery
    # entirely.
    rows = db.execute_readonly(
        conn,
        "SELECT tb.id AS binding_id, tb.current_claude_session_id AS session_id, "
        "tb.runtime_health AS runtime_health, "
        "rl.herdr_locator AS herdr_locator, tb.updated_at AS updated_at "
        "FROM tab_bindings tb "
        "LEFT JOIN runtime_locations rl ON rl.binding_id = tb.id AND rl.released_at IS NULL "
        "WHERE tb.runtime_health IN ('ACTIVE', 'RESTORING') AND tb.current_claude_session_id IS NOT NULL "
        "ORDER BY tb.updated_at DESC",
    ).fetchall()

    candidates: list[tuple[str, str]] = []
    unresolved_targets: list[dict[str, Any]] = []
    restore_blocked: list[dict[str, Any]] = []
    live_runtime_preserved: list[dict[str, Any]] = []

    # AC5: split off Bindings with no current runtime_location row FIRST --
    # regardless of ACTIVE/RESTORING -- before any of the locator-based
    # classification below (which is meaningless without a locator).
    rows_with_location: list[Any] = []
    for row in rows:
        if row["herdr_locator"] is None:
            unresolved_targets.append(
                {
                    "binding_id": row["binding_id"],
                    "session_id": row["session_id"],
                    "reason": REASON_MISSING_CURRENT_RUNTIME_LOCATION,
                }
            )
            continue
        rows_with_location.append(row)

    # AC4: group ACTIVE rows by locator FIRST so a duplicate claim on one
    # live pane is detected and reported for ALL claimants -- never
    # resolved by arbitrarily picking whichever row `ORDER BY
    # tb.updated_at DESC` happened to sort first.
    active_rows_by_locator: dict[str, list[Any]] = {}
    for row in rows_with_location:
        if row["runtime_health"] != "ACTIVE":
            continue
        locator = row["herdr_locator"]
        session_id = row["session_id"]
        if not locator or not session_id:
            continue
        active_rows_by_locator.setdefault(locator, []).append(row)

    for locator, claimant_rows in active_rows_by_locator.items():
        if locator not in live_pane_ids:
            # AC3 case 1 / PROVEN_ABSENT (locator itself not live): the
            # recorded locator no longer names any currently-live pane --
            # nothing to dispatch to. Explicitly reported (never silently
            # dropped) so this is distinguishable from "no candidates at
            # all". This is the ONLY reason the pane-process liveness probe
            # below is never reached for this claimant.
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
            # every claimant, dispatch none. (Liveness is not even probed
            # here -- there is no single unambiguous claimant to dispatch
            # into regardless of what the pane's process turns out to be.)
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

        # Issue #2752: the locator is live AND uniquely claimed -- but that
        # alone does not distinguish a live-handoff-preserved runtime from
        # a cold-restart-regenerated empty pane. Probe the pane's own
        # process liveness before deciding candidacy.
        classification, evidence = _classify_pane_process_liveness(locator, herdr_bin=herdr_bin, run_fn=run_fn)
        if classification == LIVENESS_PROVEN_ALIVE:
            live_runtime_preserved.append(
                {
                    "binding_id": row["binding_id"],
                    "session_id": row["session_id"],
                    "pane_id": locator,
                    "runtime_profile": _resolve_runtime_profile_for_binding(conn, row["binding_id"]),
                    "liveness_evidence": evidence,
                    "launch_commands_dispatched": 0,
                    "mutations_applied": 0,
                }
            )
        elif classification == LIVENESS_UNKNOWN:
            unresolved_targets.append(
                {
                    "binding_id": row["binding_id"],
                    "session_id": row["session_id"],
                    "reason": REASON_LIVENESS_UNRESOLVED,
                }
            )
        else:
            # LIVENESS_PROVEN_ABSENT (AC3 case 2: bare-shell-only, real cold
            # restart re-generated a pane at the same locator) -- dispatch
            # candidate, exactly the pre-#2752 behaviour.
            candidates.append((row["session_id"], locator))

    # AC2 (Contract Reconciliation 2026-09-25): RESTORING Bindings are
    # always reported as unresolved_target WITHOUT any DB mutation.
    # mark_restore_blocked_if_pending()'s guard condition
    # (runtime_health == "RESTORING" AND execution_run.ended_at IS NULL)
    # confirms only that a pending restore has not yet been ACKed -- it
    # does NOT prove the underlying process/pane is dead. A live Herdr
    # handoff can leave a Binding in this exact same DB state while the
    # pane is still genuinely alive, so treating guard success as a
    # staleness proof would risk RESTORE_BLOCKED-ing a Binding that is
    # mid live-handoff. True stale-RESTORING recovery requires an
    # independent liveness mechanism (e.g. a generation counter or pid
    # liveness check) that does not yet exist -- see Issue #2742 Notes
    # for Reviewer "Contract Reconciliation" and the resulting narrow
    # follow-up issue. This function therefore never mutates DB state
    # for RESTORING bindings; it only reports them. (Liveness is not
    # probed for RESTORING Bindings -- this Issue's tri-state pane-process
    # primitive is deliberately scoped to ACTIVE Bindings only; see Out of
    # Scope.)
    for row in rows_with_location:
        if row["runtime_health"] != "RESTORING":
            continue
        unresolved_targets.append(
            {
                "binding_id": row["binding_id"],
                "session_id": row["session_id"],
                "reason": REASON_RESTORING_NOT_PROVABLY_STALE,
            }
        )

    return {
        "candidates": candidates,
        "unresolved_targets": unresolved_targets,
        "restore_blocked": restore_blocked,
        "live_runtime_preserved": live_runtime_preserved,
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
    live_runtime_preserved = discovery["live_runtime_preserved"]

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
    # Issue #2752 AC1/AC2: an ACTIVE Binding whose runtime process was
    # PROVEN alive -- an explicit expected-success outcome, reported as a
    # first-class result entry (never silently indistinguishable from "no
    # candidates at all", and never counted as a failure below).
    for entry_data in live_runtime_preserved:
        results.append(
            {
                "session_id": entry_data["session_id"],
                "pane_id": entry_data["pane_id"],
                "binding_id": entry_data["binding_id"],
                "dispatch_status": "live_runtime_preserved",
                "runtime_profile": entry_data["runtime_profile"],
                "liveness_evidence": entry_data["liveness_evidence"],
                "launch_commands_dispatched": entry_data["launch_commands_dispatched"],
                "mutations_applied": entry_data["mutations_applied"],
            }
        )

    if pending_acks:
        ack_statuses = dispatcher.await_all_acks(pending_acks, timeout_seconds=ack_timeout_seconds)
        for entry in results:
            binding_id = entry.get("decision", {}).get("binding_id") if "decision" in entry else entry.get("binding_id")
            if entry["dispatch_status"] == "dispatched_waiting_ack" and binding_id in ack_statuses:
                entry["dispatch_status"] = ack_statuses[binding_id]

    # Issue #2742: previously only `dispatch_status not in ("restored",
    # "not_dispatched")` counted as a failure -- `not_dispatched` (e.g. an
    # ACTIVE candidate that `classify_for_resume` itself immediately
    # classified as RESTORE_BLOCKED, such as an invalid managed profile)
    # was a false-green path, and the new `unresolved_target` /
    # `restore_blocked` discovery-level classifications were not
    # represented at all. Every non-"restored" outcome is now a failure --
    # EXCEPT Issue #2752's `live_runtime_preserved`, which is an explicit
    # expected-success outcome (a duplicate-launch was correctly AVOIDED),
    # never a failure.
    any_failed = any(entry["dispatch_status"] not in ("restored", "live_runtime_preserved") for entry in results)
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

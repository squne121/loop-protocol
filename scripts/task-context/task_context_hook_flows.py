"""Task Context v1 — Native Claude operator hook lifecycle flows (Issue #2564).

This module is the "thin Claude-native hook adapter -> core typed API" glue
the Issue's In Scope section requires: it contains the multi-step
orchestration for each Claude Code hook lifecycle event (SessionStart /
UserPromptSubmit / CwdChanged / SubagentStart / SubagentStop / PreToolUse /
Stop / StopFailure / SessionEnd), built *entirely* out of the existing
``task_context_service`` primitives (create_task/claim_task_ref/
transition_activity/start_execution_run/attach_execution_run/
end_execution_run/relocate_binding/set_binding_session/set_binding_health/
enqueue_projection/append_event/...). It performs no external I/O and no raw
SQL of its own -- Task semantics/SQL stay in ``task_context_service``
(Scope Growth Guard: "Task semantics/SQL を `.claude/hooks` へ重複実装
しない").

The Claude-native hook adapter scripts under ``.claude/hooks/task_context/``
are responsible for:
  - reading the actual Claude Code hook JSON off stdin / relevant env vars,
  - (for UserPromptSubmit) running the deterministic primary-target
    classifier over the *raw* prompt text and reducing it to small
    structured fields (never sending raw prompt/transcript content into this
    module or into ``events`` -- AC7),
  - calling ``task-contextctl hook <event>`` with a payload built from those
    structured fields,
  - translating the JSON result envelope's ``data.decision`` into the actual
    Claude Code hook exit code / stdout contract.

This module only ever receives already-reduced, already-typed payload
fields -- never a raw prompt string.
"""

from __future__ import annotations

import os
import sys
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import task_context_errors as errors  # noqa: E402
import task_context_service as service  # noqa: E402

# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------


def _event_metadata(reason_code: str, status: str) -> dict[str, Any]:
    return {"reason_code": reason_code, "status": status}


def _record(
    conn,
    *,
    event_type: str,
    task_id: str | None,
    activity_id: str | None,
    binding_id: str | None,
    execution_run_id: str | None,
    reason_code: str,
    status: str,
) -> None:
    service.append_event(
        conn,
        event_type=event_type,
        task_id=task_id,
        activity_id=activity_id,
        binding_id=binding_id,
        execution_run_id=execution_run_id,
        metadata=_event_metadata(reason_code, status),
    )


def _bump_projection(conn, binding_id: str | None) -> None:
    """Enqueue a fresh desired-revision marker for this Binding's Herdr
    projection (coalescing -- `service.enqueue_projection` only advances,
    never regresses). The actual Herdr I/O happens later, out-of-band, in
    the async ``herdr_projection`` adapter consumer -- never synchronously
    inside a hook's hot path (Outcome: "projection を UserPromptSubmit hot
    path へ同期的に抱え込まない")."""
    if not binding_id:
        return
    projection_key = f"tab_binding:{binding_id}"
    current = service.read_projection(conn, projection_key)
    next_revision = (current["desired_revision"] + 1) if current else 1
    service.enqueue_projection(conn, projection_key, next_revision)


def _ensure_active_activity(conn, task_id: str, *, kind: str = "native_operator") -> str:
    existing = service.get_active_activity_for_task(conn, task_id)
    if existing is not None:
        return existing["id"]
    activity = service.transition_activity(conn, task_id, kind=kind)
    return activity["id"]


def _resolve_or_create_task_for_target(conn, repo: str, ref_kind: str, ref_number: int) -> str:
    live = service.find_live_claim(conn, repo, ref_kind, ref_number)
    if live is not None:
        return live["task_id"]
    task = service.create_task(conn, title=f"{repo}#{ref_number}")
    result = service.claim_task_ref(conn, task["id"], repo, ref_kind, ref_number)
    if result["status"] == "conflict":
        # Race: another Task claimed this ref between the read above and our
        # attempt. The loser readback tells us the actual winner.
        return result["winning_task_id"]
    return task["id"]


def _attach_binding_run(conn, binding_id: str, task_id: str, activity_id: str, execution_run_id: str | None) -> None:
    if execution_run_id is not None:
        service.attach_execution_run(conn, execution_run_id, task_id=task_id, activity_id=activity_id)
        return
    # No open managed run yet on this binding (should not normally happen --
    # SessionStart always starts one -- but degrade gracefully instead of
    # raising if it does).
    run = service.start_execution_run(
        conn, run_kind="native_operator", task_id=task_id, activity_id=activity_id, binding_id=binding_id
    )
    service.set_binding_session(conn, binding_id, run["claude_session_id"], execution_run_id=run["id"])


# ---------------------------------------------------------------------------
# SessionStart (AC1, AC2, AC3, AC14, AC15)
# ---------------------------------------------------------------------------

_RECOVERABLE_SOURCES = frozenset({"startup", "resume", "clear"})


def on_session_start(conn, payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("source") or "startup"
    herdr_tab_id = payload.get("herdr_tab_id")
    herdr_locator = payload.get("herdr_locator") or herdr_tab_id
    claude_session_id = payload.get("claude_session_id")

    if not herdr_tab_id:
        # AC11: non-Herdr canonical interactive Claude is observe-only --
        # never autobind/rebind/create a TabBinding for it.
        return {"decision": "pass", "reason_code": "observe_only_non_herdr", "binding_id": None}

    if source == "compact":
        # AC15: compact must not mutate Task/Activity/Binding/ExecutionRun
        # identity at all -- context refresh/reinjection only, which is the
        # adapter's responsibility (re-reading current projection), not a
        # DB mutation.
        return {"decision": "pass", "reason_code": "compact_no_mutation", "binding_id": None}

    if source == "fork":
        # AC15: v1 does not inherit the parent operator's TabBinding. Do not
        # even attempt the locator-reuse lookup below (that would silently
        # steal the parent's live Binding for the same Tab).
        return {"decision": "pass", "reason_code": "fork_no_inherited_binding", "binding_id": None}

    existing_binding = None
    if source in _RECOVERABLE_SOURCES:
        existing_binding = service.get_binding_by_current_location(conn, herdr_locator)

    if existing_binding is None:
        binding = service.create_binding(conn)
        binding_id = binding["id"]
        service.relocate_binding(conn, binding_id, herdr_locator)
        run = service.start_execution_run(conn, run_kind="native_operator", binding_id=binding_id)
        if claude_session_id:
            _set_session_on_run(conn, binding_id, run["id"], claude_session_id)
        service.append_event(
            conn,
            event_type="hook:SessionStart",
            binding_id=binding_id,
            execution_run_id=run["id"],
            metadata=_event_metadata("startup_new_binding", "ok"),
        )
        _bump_projection(conn, binding_id)
        return {"decision": "pass", "reason_code": f"{source}_new_binding", "binding_id": binding_id}

    binding_id = existing_binding["id"]

    # Self-heal: reconcile any stale open managed run left over from a prior
    # abnormal termination (SessionEnd never guaranteed to fire cleanly)
    # before starting/attaching this session's run, so we never create a
    # duplicate open managed run for the same binding (AC1(d)). Capture the
    # stale run's Task/Activity identity *before* ending it -- once ended it
    # no longer shows up in the open-run lookup used to recover it.
    stale_runs = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")
    if stale_runs:
        task_id = stale_runs[0]["task_id"]
        activity_id = stale_runs[0]["activity_id"]
    else:
        # No currently-open run (e.g. a prior clean `/quit` already ended
        # it) -- recover the last-known Task/Activity from history so the
        # restore doesn't lose continuity (AC3).
        last_run = service.get_most_recent_execution_run_for_binding(conn, binding_id, run_kind="native_operator")
        task_id = last_run["task_id"] if last_run else None
        activity_id = last_run["activity_id"] if last_run else None
    for stale in stale_runs:
        service.end_execution_run(conn, stale["id"])
    run = service.start_execution_run(
        conn, run_kind="native_operator", task_id=task_id, activity_id=activity_id, binding_id=binding_id
    )
    service.relocate_binding(conn, binding_id, herdr_locator)
    service.set_binding_health(conn, binding_id, "ACTIVE")
    if claude_session_id:
        _set_session_on_run(conn, binding_id, run["id"], claude_session_id)
    service.append_event(
        conn,
        event_type="hook:SessionStart",
        task_id=task_id,
        activity_id=activity_id,
        binding_id=binding_id,
        execution_run_id=run["id"],
        metadata=_event_metadata(f"{source}_restored_binding", "ok"),
    )
    _bump_projection(conn, binding_id)
    return {
        "decision": "pass",
        "reason_code": f"{source}_restored_binding",
        "binding_id": binding_id,
        "task_id": task_id,
    }


def _set_session_on_run(conn, binding_id: str, execution_run_id: str, claude_session_id: str) -> None:
    """Attach ``claude_session_id`` to an already-started ExecutionRun and
    sync the Binding's ``current_claude_session_id`` copy.

    ``execution_runs.claude_session_id`` is normally set at
    ``start_execution_run`` time, but the SessionStart recovery/new-binding
    flows above only learn the actual session id *after* starting the run,
    so ``service.set_execution_run_session`` performs the (typed,
    service-layer) UPDATE instead of this module issuing raw SQL."""
    service.set_execution_run_session(conn, execution_run_id, claude_session_id)
    service.set_binding_session(conn, binding_id, claude_session_id, execution_run_id=execution_run_id)


# ---------------------------------------------------------------------------
# UserPromptSubmit (AC4, AC5, AC6, AC12, AC13)
# ---------------------------------------------------------------------------


def on_user_prompt_submit(conn, payload: dict[str, Any]) -> dict[str, Any]:
    herdr_tab_id = payload.get("herdr_tab_id")
    claude_session_id = payload.get("claude_session_id")
    kind = payload.get("classification_kind") or "NONE"

    if not herdr_tab_id:
        return {"decision": "pass", "reason_code": "observe_only_non_herdr"}
    if not claude_session_id:
        return {"decision": "pass", "reason_code": "missing_session_id"}

    try:
        binding = service.get_binding_by_current_session(conn, claude_session_id)
    except errors.NotFoundError:
        # Fail-open: never block a prompt because the Binding could not be
        # resolved (DB-degraded / SessionStart mutation gap policy).
        return {"decision": "pass", "reason_code": "no_binding_for_session"}

    binding_id = binding["id"]
    current_task_id, current_activity_id, current_run_id = service.get_current_task_activity_for_binding(
        conn, binding_id
    )

    if kind == "SLASH_TASK":
        return _apply_slash_task_rebind(conn, payload, binding_id, current_run_id, current_task_id, current_activity_id)

    if kind in ("NONE", "REFERENCE_ONLY", "AMBIGUOUS"):
        reason = "ambiguous_no_silent_rebind" if kind == "AMBIGUOUS" else "reference_only_or_none"
        _record(
            conn,
            event_type="hook:UserPromptSubmit",
            task_id=current_task_id,
            activity_id=current_activity_id,
            binding_id=binding_id,
            execution_run_id=current_run_id,
            reason_code=reason,
            status="pass",
        )
        return {"decision": "pass", "reason_code": reason, "advisory": kind == "AMBIGUOUS"}

    target_repo = payload.get("target_repo")
    target_ref_kind = payload.get("target_ref_kind")
    target_ref_number = payload.get("target_ref_number")
    if not (target_repo and target_ref_kind and target_ref_number is not None):
        return {"decision": "pass", "reason_code": "malformed_target_payload"}

    if current_task_id is None:
        task_id = _resolve_or_create_task_for_target(conn, target_repo, target_ref_kind, target_ref_number)
        activity_id = _ensure_active_activity(conn, task_id)
        _attach_binding_run(conn, binding_id, task_id, activity_id, current_run_id)
        _record(
            conn,
            event_type="hook:UserPromptSubmit",
            task_id=task_id,
            activity_id=activity_id,
            binding_id=binding_id,
            execution_run_id=current_run_id,
            reason_code="autobind",
            status="pass",
        )
        _bump_projection(conn, binding_id)
        return {"decision": "pass", "reason_code": "autobind", "task_id": task_id, "activity_id": activity_id}

    live_claim = service.find_live_claim(conn, target_repo, target_ref_kind, target_ref_number)
    if live_claim is not None and live_claim["task_id"] == current_task_id:
        return {"decision": "pass", "reason_code": "same_target"}

    refs_count = service.count_live_task_ref_claims(conn, current_task_id)
    current_activity = service.get_activity(conn, current_activity_id) if current_activity_id else None
    activity_is_terminal_or_missing = current_activity is None or current_activity["status"] != "ACTIVE"

    if refs_count == 0 and live_claim is None:
        # AC4: ACTIVE Task with 0 live GitHub refs is provisional/absorbent
        # -- the first high-confidence primary GitHub target claims it
        # rather than being treated as a different-Task rebind.
        service.claim_task_ref(conn, current_task_id, target_repo, target_ref_kind, target_ref_number)
        _record(
            conn,
            event_type="hook:UserPromptSubmit",
            task_id=current_task_id,
            activity_id=current_activity_id,
            binding_id=binding_id,
            execution_run_id=current_run_id,
            reason_code="provisional_absorb",
            status="pass",
        )
        _bump_projection(conn, binding_id)
        return {"decision": "pass", "reason_code": "provisional_absorb", "task_id": current_task_id}

    if activity_is_terminal_or_missing:
        # AC5: terminal Activity -> legal same-Task advance / different-Task
        # rebind via a normal prompt (no /task needed).
        task_id = (
            live_claim["task_id"]
            if live_claim is not None
            else _resolve_or_create_task_for_target(conn, target_repo, target_ref_kind, target_ref_number)
        )
        activity_id = _ensure_active_activity(conn, task_id)
        _attach_binding_run(conn, binding_id, task_id, activity_id, current_run_id)
        _record(
            conn,
            event_type="hook:UserPromptSubmit",
            task_id=task_id,
            activity_id=activity_id,
            binding_id=binding_id,
            execution_run_id=current_run_id,
            reason_code="terminal_advance_or_rebind",
            status="pass",
        )
        _bump_projection(conn, binding_id)
        return {"decision": "pass", "reason_code": "terminal_advance_or_rebind", "task_id": task_id}

    # AC4: ACTIVE current Activity + different high-confidence primary
    # target -> block before Claude processes the prompt.
    _record(
        conn,
        event_type="hook:UserPromptSubmit",
        task_id=current_task_id,
        activity_id=current_activity_id,
        binding_id=binding_id,
        execution_run_id=current_run_id,
        reason_code="different_primary_target_active",
        status="block",
    )
    return {"decision": "block", "reason_code": "different_primary_target_active"}


def _apply_slash_task_rebind(
    conn,
    payload: dict[str, Any],
    binding_id: str,
    current_run_id: str | None,
    current_task_id: str | None,
    current_activity_id: str | None,
) -> dict[str, Any]:
    """AC6/AC12: `/task <target>` is the sole explicit human escape hatch --
    it always supersedes the normal primary-target guard, atomically, in a
    single UserPromptSubmit adapter invocation, regardless of hook
    registration/execution order relative to other handlers (AC13:
    commit-on-submission, no rollback on a sibling hook's later block)."""
    target_repo = payload.get("slash_task_target_repo")
    target_ref_kind = payload.get("slash_task_target_ref_kind")
    target_ref_number = payload.get("slash_task_target_ref_number")
    ad_hoc_title = payload.get("slash_task_ad_hoc_title")

    if target_repo and target_ref_kind and target_ref_number is not None:
        task_id = _resolve_or_create_task_for_target(conn, target_repo, target_ref_kind, target_ref_number)
    elif ad_hoc_title:
        task = service.create_task(conn, title=ad_hoc_title)
        task_id = task["id"]
    else:
        return {"decision": "block", "reason_code": "slash_task_missing_target"}

    activity_id = _ensure_active_activity(conn, task_id)
    _attach_binding_run(conn, binding_id, task_id, activity_id, current_run_id)
    _record(
        conn,
        event_type="hook:UserPromptSubmit",
        task_id=task_id,
        activity_id=activity_id,
        binding_id=binding_id,
        execution_run_id=current_run_id,
        reason_code="slash_task_rebind",
        status="pass",
    )
    _bump_projection(conn, binding_id)
    return {"decision": "pass", "reason_code": "slash_task_rebind", "task_id": task_id, "activity_id": activity_id}


# ---------------------------------------------------------------------------
# CwdChanged (AC7)
# ---------------------------------------------------------------------------


def on_cwd_changed(conn, payload: dict[str, Any]) -> dict[str, Any]:
    claude_session_id = payload.get("claude_session_id")
    herdr_locator = payload.get("herdr_locator")
    if not claude_session_id or not herdr_locator:
        return {"decision": "pass", "reason_code": "missing_fields"}
    try:
        binding = service.get_binding_by_current_session(conn, claude_session_id)
    except errors.NotFoundError:
        return {"decision": "pass", "reason_code": "no_binding_for_session"}
    binding_id = binding["id"]
    current_location = service.get_current_location(conn, binding_id)
    if current_location is not None and current_location["herdr_locator"] == herdr_locator:
        return {"decision": "pass", "reason_code": "location_unchanged"}
    # AC7: cwd/worktree change updates RuntimeLocation only -- never a
    # Task/Activity switch or block condition.
    service.relocate_binding(conn, binding_id, herdr_locator)
    _bump_projection(conn, binding_id)
    return {"decision": "pass", "reason_code": "relocated", "binding_id": binding_id}


# ---------------------------------------------------------------------------
# SubagentStart / SubagentStop (AC8)
# ---------------------------------------------------------------------------


def on_subagent_start(conn, payload: dict[str, Any]) -> dict[str, Any]:
    claude_session_id = payload.get("claude_session_id")
    task_id = None
    activity_id = None
    if claude_session_id:
        try:
            binding = service.get_binding_by_current_session(conn, claude_session_id)
            task_id, activity_id, _ = service.get_current_task_activity_for_binding(conn, binding["id"])
        except errors.NotFoundError:
            pass
    # AC8: SubAgent runs roll up under the parent Task/Activity as a
    # non-managed ExecutionRun -- never given its own TabBinding.
    run = service.start_execution_run(conn, run_kind="subagent", task_id=task_id, activity_id=activity_id)
    _record(
        conn,
        event_type="hook:SubagentStart",
        task_id=task_id,
        activity_id=activity_id,
        binding_id=None,
        execution_run_id=run["id"],
        reason_code="subagent_started",
        status="pass",
    )
    return {"decision": "pass", "reason_code": "subagent_started", "execution_run_id": run["id"]}


def on_subagent_stop(conn, payload: dict[str, Any]) -> dict[str, Any]:
    claude_session_id = payload.get("claude_session_id")
    task_id = None
    activity_id = None
    if claude_session_id:
        try:
            binding = service.get_binding_by_current_session(conn, claude_session_id)
            task_id, activity_id, _ = service.get_current_task_activity_for_binding(conn, binding["id"])
        except errors.NotFoundError:
            pass
    open_subagent_runs = service.find_open_execution_runs(
        conn, task_id=task_id, activity_id=activity_id, run_kind="subagent"
    )
    if not open_subagent_runs:
        return {"decision": "pass", "reason_code": "no_open_subagent_run"}
    run = service.end_execution_run(conn, open_subagent_runs[0]["id"])
    _record(
        conn,
        event_type="hook:SubagentStop",
        task_id=task_id,
        activity_id=activity_id,
        binding_id=None,
        execution_run_id=run["id"],
        reason_code="subagent_ended",
        status="pass",
    )
    return {"decision": "pass", "reason_code": "subagent_ended", "execution_run_id": run["id"]}


# ---------------------------------------------------------------------------
# PreToolUse -- observability only in v1 (no Task Context gating semantics
# are specified for it in this Issue's In Scope; existing PreToolUse guards
# already cover tool-level policy).
# ---------------------------------------------------------------------------


def on_pre_tool_use(conn, payload: dict[str, Any]) -> dict[str, Any]:
    return {"decision": "pass", "reason_code": "observability_only"}


# ---------------------------------------------------------------------------
# Stop / StopFailure / SessionEnd (runtime facts only -- no NL inference)
# ---------------------------------------------------------------------------


def _end_current_run(conn, payload: dict[str, Any], event_type: str, health: str) -> dict[str, Any]:
    claude_session_id = payload.get("claude_session_id")
    if not claude_session_id:
        return {"decision": "pass", "reason_code": "missing_session_id"}
    try:
        binding = service.get_binding_by_current_session(conn, claude_session_id)
    except errors.NotFoundError:
        return {"decision": "pass", "reason_code": "no_binding_for_session"}
    binding_id = binding["id"]
    task_id, activity_id, run_id = service.get_current_task_activity_for_binding(conn, binding_id)
    if event_type == "hook:SessionEnd" and run_id is not None:
        service.end_execution_run(conn, run_id)
    if health is not None:
        service.set_binding_health(conn, binding_id, health)
        _bump_projection(conn, binding_id)
    _record(
        conn,
        event_type=event_type,
        task_id=task_id,
        activity_id=activity_id,
        binding_id=binding_id,
        execution_run_id=run_id,
        reason_code="runtime_fact_only",
        status="ok",
    )
    return {"decision": "pass", "reason_code": "runtime_fact_only", "binding_id": binding_id}


def on_stop(conn, payload: dict[str, Any]) -> dict[str, Any]:
    return _end_current_run(conn, payload, "hook:Stop", health=None)


def on_stop_failure(conn, payload: dict[str, Any]) -> dict[str, Any]:
    return _end_current_run(conn, payload, "hook:StopFailure", health=None)


def on_session_end(conn, payload: dict[str, Any]) -> dict[str, Any]:
    # AC3: `/quit` ends the operator run and suspends the Binding; recovery
    # relies on the *next* SessionStart self-healing, not a guaranteed clean
    # SessionEnd (see docstring on `on_session_start`'s stale-run reconcile).
    return _end_current_run(conn, payload, "hook:SessionEnd", health="SUSPENDED")


EVENT_HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt_submit,
    "CwdChanged": on_cwd_changed,
    "SubagentStart": on_subagent_start,
    "SubagentStop": on_subagent_stop,
    "PreToolUse": on_pre_tool_use,
    "Stop": on_stop,
    "StopFailure": on_stop_failure,
    "SessionEnd": on_session_end,
}


def dispatch_hook_event(conn, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a Claude-native hook lifecycle event to its typed flow. Any
    ``event`` name not in ``EVENT_HANDLERS`` (e.g. a generic/legacy
    diagnostic event name) falls back to a plain ``append_event`` record for
    backward compatibility with the pre-#2564 minimal `hook` operation --
    never a hard failure."""
    handler = EVENT_HANDLERS.get(event)
    if handler is not None:
        return handler(conn, payload)
    result = service.append_event(
        conn,
        event_type=f"hook:{event}",
        task_id=payload.get("task_id"),
        activity_id=payload.get("activity_id"),
        binding_id=payload.get("binding_id"),
        execution_run_id=payload.get("execution_run_id"),
        metadata=payload.get("metadata") or {},
    )
    return {
        "decision": "pass",
        "reason_code": "generic_event",
        "event_id": result["id"],
        "event_type": result["event_type"],
    }

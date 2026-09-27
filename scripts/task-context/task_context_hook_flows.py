"""Task Context v1 — Native Claude operator hook lifecycle flows (Issue #2564,
advisory-only ACTIVE different-primary guard + `/task` authority move to
`UserPromptExpansion`: Issue #2625).

This module is the "thin Claude-native hook adapter -> core typed API" glue
the Issue's In Scope section requires: it contains the multi-step
orchestration for each Claude Code hook lifecycle event (SessionStart /
UserPromptSubmit / UserPromptExpansion / CwdChanged / SubagentStart /
SubagentStop / PreToolUse / Stop / StopFailure / SessionEnd), built *entirely*
out of the existing
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

import hashlib
import json
import os
import subprocess
import sys
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import task_context_config as config  # noqa: E402
import task_context_errors as errors  # noqa: E402
import task_context_service as service  # noqa: E402
import task_context_session_registry as session_registry  # noqa: E402
import task_context_target_kind as target_kind  # noqa: E402

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


def _bump_projection(conn, binding_id: str | None) -> dict[str, Any]:
    """Enqueue a fresh desired-revision marker for this Binding's Herdr
    projection (coalescing -- ``enqueue_projection`` only advances, never
    regresses), and report the resulting key/revision so the caller can pass
    them back to the adapter.

    The actual Herdr I/O happens out-of-band, in a projection worker the
    adapter starts **after** this mutation has committed (fix_delta 2's
    causal ``commit -> project`` ordering) -- never synchronously inside a
    hook's hot path (Outcome: "projection を UserPromptSubmit hot path へ
    同期的に抱え込まない")."""
    if not binding_id:
        return {}
    projection_key = f"tab_binding:{binding_id}"
    current = service.read_projection(conn, projection_key)
    next_revision = (current["desired_revision"] + 1) if current else 1
    service.enqueue_projection(conn, projection_key, next_revision)
    return {"projection_key": projection_key, "projection_revision": next_revision}


def _open_managed_runs_for_binding(conn, binding_id: str) -> list[dict[str, Any]]:
    """Issue #2567 AC4: an open managed run for ``binding_id`` may now be
    either ``run_kind`` in ``service.MANAGED_RUN_KINDS`` (native_operator or
    claude_gpt) -- the DB's ``ux_execution_runs_open_managed_per_binding``
    unique index already enforces at most one such row *regardless of
    run_kind*, so this never needs to reconcile more than one candidate; it
    only needs to look under whichever kind the stale run actually used,
    instead of assuming native_operator."""
    runs: list[dict[str, Any]] = []
    for kind in service.MANAGED_RUN_KINDS:
        runs.extend(service.find_open_execution_runs(conn, binding_id=binding_id, run_kind=kind))
    return runs


def _most_recent_managed_run_for_binding(conn, binding_id: str) -> dict[str, Any] | None:
    """Issue #2567 AC4 counterpart of ``_open_managed_runs_for_binding`` for
    the "no open run -- recover the last-known Task/Activity from history"
    branch (AC3): considers the most recent run across every managed
    run_kind, not just native_operator, so a binding whose last managed run
    happened to be a claude_gpt run still restores correctly."""
    candidates = [
        run
        for kind in service.MANAGED_RUN_KINDS
        if (run := service.get_most_recent_execution_run_for_binding(conn, binding_id, run_kind=kind)) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda run: run["started_at"])


_LOCATION_PAYLOAD_KEYS = ("cwd", "worktree", "branch")


def _location_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the display-only RuntimeLocation observation fields
    (fix_delta 7). These are recorded on ``runtime_locations`` for the
    statusLine only -- they never take part in Task identity, rebind or
    block decisions (AC7)."""
    return {key: payload.get(key) or None for key in _LOCATION_PAYLOAD_KEYS}


def _projection_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the projection key/revision a coarse service operation
    reports, in the shape the adapter expects on the hook result."""
    key = result.get("projection_key")
    if not key:
        return {}
    return {"projection_key": key, "projection_revision": result.get("projection_revision")}


# ---------------------------------------------------------------------------
# SessionStart debug trace (Issue #2790 AC1/AC11): opt-in, session-scoped,
# fingerprint-only 1-line diagnostic trace of exactly the fields the Issue's
# Runtime Verification Applicability artifact_requirements calls for --
# `source`, Claude Code version, a *fingerprint* (never the raw value) of
# the session identity / effective Herdr locator, `has_tab_id`/`has_pane_id`,
# and the lookup-by-current-session / lookup-by-current-location / recovery
# outcomes. Gated by `TASK_CONTEXT_DEBUG_TRACE` (unset/empty == off, the
# permanent default) so it never runs -- and never costs a `claude
# --version` subprocess spawn -- on any production hot path. Never a raw
# env dump / raw secret (Issue #2790 Stop Condition): `claude_session_id`
# and the Herdr locator are only ever fingerprinted via `_fingerprint`
# below, one-way and non-reversible.
# ---------------------------------------------------------------------------

_DEBUG_TRACE_ENV_VAR = "TASK_CONTEXT_DEBUG_TRACE"


def _debug_trace_enabled() -> bool:
    return bool(os.environ.get(_DEBUG_TRACE_ENV_VAR))


def _fingerprint(value: str | None) -> str | None:
    """One-way, truncated SHA-256 fingerprint of ``value`` -- safe to print
    to a debug trace line. Never the raw session id / Herdr locator itself."""
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _claude_code_version() -> str | None:
    """Best-effort ``claude --version`` probe. Only ever invoked when the
    opt-in debug trace is enabled (never on the default-off hot path).
    Never raises -- any failure (binary not on PATH, timeout, non-zero
    exit) degrades to ``None`` rather than breaking the hook."""
    try:
        result = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=2.0
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _emit_session_start_debug_trace(
    *,
    source: str,
    claude_session_id: str | None,
    herdr_tab_id: str | None,
    has_pane_id: bool,
    herdr_locator: str | None,
    lookup_by_session_outcome: str,
    lookup_by_locator_outcome: str,
    recovery_decision: str,
) -> None:
    if not _debug_trace_enabled():
        return
    trace = {
        "task_context_debug_trace": "SessionStart",
        "source": source,
        "claude_code_version": _claude_code_version(),
        "claude_session_id_fingerprint": _fingerprint(claude_session_id),
        "has_tab_id": bool(herdr_tab_id),
        "has_pane_id": bool(has_pane_id),
        "herdr_locator_fingerprint": _fingerprint(herdr_locator),
        "lookup_by_session_outcome": lookup_by_session_outcome,
        "lookup_by_locator_outcome": lookup_by_locator_outcome,
        "recovery_decision": recovery_decision,
    }
    print(json.dumps(trace, sort_keys=True), file=sys.stderr)


# ---------------------------------------------------------------------------
# SessionStart (AC1, AC2, AC3, AC14, AC15; Issue #2567 AC1/AC4 runtime-variant
# awareness)
# ---------------------------------------------------------------------------

_RECOVERABLE_SOURCES = frozenset({"startup", "resume", "clear"})


def _create_new_binding_and_run(
    conn,
    *,
    herdr_locator: str,
    location_fields: dict[str, Any],
    claude_session_id: str | None,
    event_type: str,
    reason_code: str,
    evict_foreign_holder: bool,
) -> dict[str, Any]:
    """"No existing Binding -- create one from scratch" sequence for a
    brand-new SessionStart Tab (the ``existing_binding is None`` branch of
    ``on_session_start`` below). Creates one independent Binding + managed
    ExecutionRun, relocates it to ``herdr_locator``, and (if a Claude
    session id is known) attaches it -- this never inherits/steals any
    other Binding's identity.

    (The explicit `/task <target>` unbound-session self-heal, Issue #2790
    AC2/AC5, used to share this helper but now calls
    ``service.bootstrap_binding_and_bind_target`` directly -- PR #2795
    review fix_delta P1-A -- so the bootstrap and the immediately-following
    target bind run inside one transaction instead of two.)

    ``evict_foreign_holder`` (PR #2795 review fix_delta P1-B, comment
    5852749710): a genuine brand-new Herdr Tab's own locator can never
    legitimately still be claimed by a *live* other Binding, so reclaiming
    a stale claim on it (``True``, the existing PR #2731 P1-3 reassignment
    contract) is safe here."""
    run_kind, runtime_profile, resume_profile = config.normalize_operator_profiles_for_new_run(
        *config.operator_run_kind_and_profiles()
    )
    # PR #2795 review fix_delta P1-A (comment 5852749710, finding 1): this
    # used to be a chain of independently-committing `service.*` calls
    # (create_binding -> relocate_binding -> start_execution_run ->
    # set_execution_run_session -> set_binding_session -> append_event),
    # each opening its own `BEGIN IMMEDIATE`. A failure partway through
    # could leave a real, committed orphan Binding/RuntimeLocation/
    # ExecutionRun behind. `service.bootstrap_binding_with_run` runs the
    # whole sequence inside one transaction instead.
    result = service.bootstrap_binding_with_run(
        conn,
        herdr_locator=herdr_locator,
        location_fields=location_fields,
        claude_session_id=claude_session_id,
        run_kind=run_kind,
        runtime_profile=runtime_profile,
        resume_profile=resume_profile,
        event_type=event_type,
        reason_code=reason_code,
        evict_foreign_holder=evict_foreign_holder,
    )
    return {"binding_id": result["binding_id"], "execution_run_id": result["execution_run_id"]}


def on_session_start(conn, payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("source") or "startup"
    herdr_tab_id = payload.get("herdr_tab_id")
    has_pane_id = bool(payload.get("has_pane_id"))
    herdr_locator = payload.get("herdr_locator") or herdr_tab_id
    claude_session_id = payload.get("claude_session_id")

    def _trace(*, lookup_by_session_outcome: str, lookup_by_locator_outcome: str, recovery_decision: str) -> None:
        _emit_session_start_debug_trace(
            source=source,
            claude_session_id=claude_session_id,
            herdr_tab_id=herdr_tab_id,
            has_pane_id=has_pane_id,
            herdr_locator=herdr_locator,
            lookup_by_session_outcome=lookup_by_session_outcome,
            lookup_by_locator_outcome=lookup_by_locator_outcome,
            recovery_decision=recovery_decision,
        )

    if not herdr_tab_id:
        # AC11: non-Herdr canonical interactive Claude is observe-only --
        # never autobind/rebind/create a TabBinding for it.
        _trace(
            lookup_by_session_outcome="not_attempted",
            lookup_by_locator_outcome="not_attempted",
            recovery_decision="observe_only_non_herdr",
        )
        return {"decision": "pass", "reason_code": "observe_only_non_herdr", "binding_id": None}

    if source == "compact":
        # AC15: compact must not mutate Task/Activity/Binding/ExecutionRun
        # identity at all -- context refresh/reinjection only, which is the
        # adapter's responsibility (re-reading current projection), not a
        # DB mutation.
        _trace(
            lookup_by_session_outcome="not_attempted",
            lookup_by_locator_outcome="not_attempted",
            recovery_decision="compact_no_mutation",
        )
        return {"decision": "pass", "reason_code": "compact_no_mutation", "binding_id": None}

    if source == "fork":
        # AC15: v1 does not inherit the parent operator's TabBinding. Do not
        # even attempt the locator-reuse lookup below (that would silently
        # steal the parent's live Binding for the same Tab).
        _trace(
            lookup_by_session_outcome="not_attempted",
            lookup_by_locator_outcome="not_attempted",
            recovery_decision="fork_no_inherited_binding",
        )
        return {"decision": "pass", "reason_code": "fork_no_inherited_binding", "binding_id": None}

    # Issue #2569 AC3/AC4 (durable recovery / strong anchor): the current
    # Claude native session id is a STRONGER recovery anchor than the Herdr
    # locator -- a Herdr cold restart typically reassigns
    # tab/workspace/pane ids (AC3), but `claude --resume S`/the Claude-GPT
    # launcher's `--resume S` preserve the exact same Claude session id S
    # across the restart, and `current_claude_session_id` on the Binding is
    # never cleared by `/quit`/SessionEnd (see `_end_current_run` below --
    # only `runtime_health` changes). So try the session-id anchor FIRST;
    # only fall back to the (locator may have changed across a cold
    # restart, but is still the right anchor for the existing same-tab
    # resume/`/clear` case the locator lookup already covered) live-location
    # match when no Binding currently claims this exact session id.
    existing_binding = None
    lookup_by_session_outcome = "not_attempted"
    if claude_session_id:
        try:
            existing_binding = service.get_binding_by_current_session(conn, claude_session_id)
            lookup_by_session_outcome = "found"
        except errors.NotFoundError:
            existing_binding = None
            lookup_by_session_outcome = "not_found"
    lookup_by_locator_outcome = "not_attempted"
    if existing_binding is None and source in _RECOVERABLE_SOURCES:
        existing_binding = service.get_binding_by_current_location(conn, herdr_locator)
        lookup_by_locator_outcome = "found" if existing_binding is not None else "not_found"

    location_fields = _location_fields(payload)
    run_kind, runtime_profile, resume_profile = config.normalize_operator_profiles_for_new_run(
        *config.operator_run_kind_and_profiles()
    )

    if existing_binding is None:
        created = _create_new_binding_and_run(
            conn,
            herdr_locator=herdr_locator,
            location_fields=location_fields,
            claude_session_id=claude_session_id,
            event_type="hook:SessionStart",
            reason_code=f"{source}_new_binding",
            # A genuine new Herdr Tab's own locator preserves the existing
            # PR #2731 P1-3 reassignment contract: reclaim a stale claim
            # left behind by a Binding that cold-restarted elsewhere.
            evict_foreign_holder=True,
        )
        binding_id = created["binding_id"]
        projection = _bump_projection(conn, binding_id)
        _trace(
            lookup_by_session_outcome=lookup_by_session_outcome,
            lookup_by_locator_outcome=lookup_by_locator_outcome,
            recovery_decision=f"{source}_new_binding",
        )
        return {
            "decision": "pass",
            "reason_code": f"{source}_new_binding",
            "binding_id": binding_id,
            **projection,
        }

    binding_id = existing_binding["id"]

    # Self-heal: reconcile any stale open managed run left over from a prior
    # abnormal termination (SessionEnd never guaranteed to fire cleanly)
    # before starting/attaching this session's run, so we never create a
    # duplicate open managed run for the same binding (AC1(d)). Capture the
    # stale run's Task/Activity identity *before* ending it -- once ended it
    # no longer shows up in the open-run lookup used to recover it.
    #
    # Issue #2567 AC1/AC4: this binding's stale/latest managed run may be
    # either run_kind (native_operator or claude_gpt) depending on which
    # runtime flavor last held it -- looked up across BOTH kinds so
    # switching runtime flavor across restarts never loses/duplicates
    # Task/Activity identity.
    stale_runs = _open_managed_runs_for_binding(conn, binding_id)
    if stale_runs:
        task_id = stale_runs[0]["task_id"]
        activity_id = stale_runs[0]["activity_id"]
    else:
        # No currently-open run (e.g. a prior clean `/quit` already ended
        # it) -- recover the last-known Task/Activity from history so the
        # restore doesn't lose continuity (AC3).
        last_run = _most_recent_managed_run_for_binding(conn, binding_id)
        task_id = last_run["task_id"] if last_run else None
        activity_id = last_run["activity_id"] if last_run else None
    # PR #2731 review fix_delta Finding 3 follow-up: the old-run close ->
    # new-run start -> locator detach/re-home -> Binding ACTIVE -> (optional)
    # session attach sequence below used to be 4-6 independently committing
    # ``service`` calls, each opening its own ``BEGIN IMMEDIATE``. Bundled
    # into a single atomic service-layer operation so a crash partway
    # through can never leave the Binding in a half-restored state (see
    # ``service._complete_restore_tx`` for the transaction-internal step
    # sequence and rollback rationale).
    run = service.complete_session_start_restore(
        conn,
        binding_id=binding_id,
        stale_run_ids=[stale["id"] for stale in stale_runs],
        run_kind=run_kind,
        task_id=task_id,
        activity_id=activity_id,
        runtime_profile=runtime_profile,
        resume_profile=resume_profile,
        herdr_locator=herdr_locator,
        claude_session_id=claude_session_id,
        **location_fields,
    )
    service.append_event(
        conn,
        event_type="hook:SessionStart",
        task_id=task_id,
        activity_id=activity_id,
        binding_id=binding_id,
        execution_run_id=run["id"],
        metadata=_event_metadata(f"{source}_restored_binding", "ok"),
    )
    projection = _bump_projection(conn, binding_id)
    _trace(
        lookup_by_session_outcome=lookup_by_session_outcome,
        lookup_by_locator_outcome=lookup_by_locator_outcome,
        recovery_decision=f"{source}_restored_binding",
    )
    return {
        "decision": "pass",
        "reason_code": f"{source}_restored_binding",
        "binding_id": binding_id,
        "task_id": task_id,
        **projection,
    }


# ---------------------------------------------------------------------------
# UserPromptSubmit (AC1, AC2, AC5, AC7, AC9 -- Issue #2625: the ACTIVE
# different-primary-target branch is advisory-only, never an admission
# gate. `/task` state-changing authority lives exclusively in
# `on_user_prompt_expansion` below (AC6) -- a raw `/task ...`-looking prompt
# observed here performs no mutation whatsoever.)
# ---------------------------------------------------------------------------

# Issue #2625 AC6: classification kinds that never mutate Task/Activity/
# Binding state on ordinary UserPromptSubmit -- SLASH_TASK is included here
# (not dispatched to a rebind) because raw prompt text is no longer a
# state-changing authority signal on this event; only the explicit
# `UserPromptExpansion` `command_name == "task"` command lifecycle can rebind.
_NO_MUTATION_REASON_CODES = {
    "AMBIGUOUS": "ambiguous_no_silent_rebind",
    "SLASH_TASK": "slash_task_raw_text_no_state_authority",
}


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

    if kind in ("NONE", "REFERENCE_ONLY", "AMBIGUOUS", "SLASH_TASK"):
        reason = _NO_MUTATION_REASON_CODES.get(kind, "reference_only_or_none")
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

    # An explicit PR target has no equivalence authority until a local claim
    # exists. This check intentionally precedes autobind: an otherwise-unbound
    # Binding must not turn an unclaimed PR prompt into a new Task.
    live_claim = service.find_live_claim(conn, target_repo, target_ref_kind, target_ref_number)
    if target_ref_kind == "pr" and live_claim is None:
        return {"decision": "pass", "reason_code": "unclaimed_pr_local_only"}

    if current_task_id is None:
        bound = service.bind_target_to_binding(
            conn,
            binding_id=binding_id,
            execution_run_id=current_run_id,
            repo=target_repo,
            ref_kind=target_ref_kind,
            ref_number=target_ref_number,
            reason_code="autobind",
            activity_kind="refine",
        )
        return {
            "decision": "pass",
            "reason_code": "autobind",
            "task_id": bound["task_id"],
            "activity_id": bound["activity_id"],
            **_projection_fields(bound),
        }

    if live_claim is not None and live_claim["task_id"] == current_task_id:
        return {"decision": "pass", "reason_code": "same_target"}

    refs_count = service.count_live_task_ref_claims(conn, current_task_id)
    current_activity = service.get_activity(conn, current_activity_id) if current_activity_id else None
    activity_is_terminal_or_missing = current_activity is None or current_activity["status"] != "ACTIVE"

    if refs_count == 0 and live_claim is None:
        # AC4: ACTIVE Task with 0 live GitHub refs is provisional/absorbent
        # -- the first high-confidence primary GitHub target claims it
        # rather than being treated as a different-Task rebind. Atomic
        # (fix_delta 3): claim + Activity + run attach + event + outbox all
        # commit together, or not at all.
        absorbed = service.absorb_ref_into_task(
            conn,
            binding_id=binding_id,
            execution_run_id=current_run_id,
            task_id=current_task_id,
            repo=target_repo,
            ref_kind=target_ref_kind,
            ref_number=target_ref_number,
            reason_code="provisional_absorb",
        )
        return {
            "decision": "pass",
            "reason_code": "provisional_absorb",
            "task_id": absorbed["task_id"],
            **_projection_fields(absorbed),
        }

    if activity_is_terminal_or_missing:
        # AC5: terminal Activity -> legal same-Task advance / different-Task
        # rebind via a normal prompt (no /task needed).
        advanced = service.bind_target_to_binding(
            conn,
            binding_id=binding_id,
            execution_run_id=current_run_id,
            repo=target_repo,
            ref_kind=target_ref_kind,
            ref_number=target_ref_number,
            reason_code="terminal_advance_or_rebind",
        )
        return {
            "decision": "pass",
            "reason_code": "terminal_advance_or_rebind",
            "task_id": advanced["task_id"],
            **_projection_fields(advanced),
        }

    # Issue #2625 AC1/AC2/AC9 (supersedes Issue #2564 AC4's hard block):
    # ACTIVE current Activity + different high-confidence primary target is
    # advisory-only. Claude prompt processing always continues (decision:
    # pass); current Task/Activity/Binding are left completely untouched
    # (no mutation above this point in this branch, no silent rebind, no
    # target-ref claim created); the mismatch is recorded to EventJournal as
    # a *required*, non-blocking observation (status="pass", never
    # status="block" -- this is an advisory record, not a hard-block
    # state). Producer workflows are never rolled back because of this
    # advisory (AC9) -- there is nothing here that could roll anything back.
    _record(
        conn,
        event_type="hook:UserPromptSubmit",
        task_id=current_task_id,
        activity_id=current_activity_id,
        binding_id=binding_id,
        execution_run_id=current_run_id,
        reason_code="different_primary_target_active",
        status="pass",
    )
    return {"decision": "pass", "reason_code": "different_primary_target_active", "advisory": True}


# ---------------------------------------------------------------------------
# UserPromptExpansion (AC6 -- Issue #2625): the sole explicit human
# state-changing authority for `/task <target>`. Reached only for Claude
# Code's own user-typed slash/Skill command expansion lifecycle -- never for
# ordinary natural-language `UserPromptSubmit` prompts. `command_name`
# values other than ``"task"`` are not this module's concern (some other
# Skill/command being expanded) and are always a silent, non-mutating
# pass-through.
#
# Issue #2790 AC2/AC5: the `binding is None` branch of
# `on_user_prompt_expansion` below is the primary design candidate this
# Issue validated for a Herdr-tracked session whose current Binding is
# absent (`fork`'s by-design non-inheritance of the parent operator's
# TabBinding, Issue #2564 AC15, is the case this was written for, but the
# precondition below never actually inspects `SessionStart`'s `source` --
# `UserPromptExpansion` does not even carry it -- so it applies uniformly to
# *any* confirmed-absent Binding for a Herdr-tracked session with a valid
# Claude session identity, which also self-heals a hypothetical
# `startup`/`resume`/`clear` binding-restoration gap the same way). It is
# built entirely out of the existing typed `task_context_service` primitives
# already used elsewhere in this module (no new daemon/lease/lock/table) and
# only ever runs once `on_user_prompt_expansion` has itself confirmed every
# precondition (Herdr-tracked, valid session id, Binding absent, explicit
# `/task` invocation carrying an already-parsed target) -- never
# speculatively.
#
# PR #2795 review fix_delta P1-A (comment 5852749710, finding 1): the
# bootstrap (previously a separate `bootstrap_unbound_session` call) and the
# immediately-following target bind (previously a separate
# `service.bind_target_to_binding` / `service.bind_ad_hoc_task_to_binding`
# call) used to be two independently-committing top-level operations. A
# failure in the bind half left a real, committed Binding/RuntimeLocation/
# ExecutionRun/session-claim/event with no Task attached -- exactly the
# `origin_task_unattached`-shaped half-applied state the review identifies.
# `service.bootstrap_binding_and_bind_target` now runs both halves inside a
# single `BEGIN IMMEDIATE`, so `/task`'s self-heal either fully succeeds or
# leaves no trace.
# ---------------------------------------------------------------------------


def on_user_prompt_expansion(conn, payload: dict[str, Any]) -> dict[str, Any]:
    """AC6: `/task <target>` always supersedes whatever Task/Activity is
    currently ACTIVE for this Binding, atomically, in a single
    UserPromptExpansion adapter invocation -- the sole explicit human escape
    hatch, moved off the raw-text `UserPromptSubmit` special-case (Issue
    #2564 PR #2615) onto this dedicated command lifecycle event.

    "Atomic" here is literal (carried over from PR #2615 fix_delta 3): the
    whole resolve-or-create-Task -> claim ref -> ensure ACTIVE Activity ->
    attach ExecutionRun -> append event -> bump projection outbox sequence
    runs inside a single ``BEGIN IMMEDIATE`` in the service layer, so a
    `/task` rebind can never be half-applied. Target validation failure,
    persistence failure, and (at the adapter layer) transport failure are
    all surfaced as an explicit `/task` *command* failure (``decision:
    block``) -- never silently swallowed as if the rebind had succeeded.

    Issue #2790 AC2/AC5: when the current Binding cannot be resolved for
    this exact `claude_session_id` (the historical `no_binding_for_session`
    dead-end -- most notably for a `fork`'d session, Issue #2564 AC15's
    by-design non-inheritance of the parent operator's TabBinding, though
    this precondition never actually inspects `source`), an explicit
    `/task <target>` that already carries a resolvable target (a GitHub ref
    or an ad-hoc title) now self-heals via a single atomic
    `service.bootstrap_binding_and_bind_target` call instead of
    dead-ending -- only when a target is genuinely absent too does this
    remain the `no_binding_for_session` command failure it always was (see
    the hook adapter's `_task_command_failure_message` for why that message
    is now always accurate for this reason_code)."""
    command_name = payload.get("command_name")
    if command_name != "task":
        return {"decision": "pass", "reason_code": "not_task_command"}

    herdr_tab_id = payload.get("herdr_tab_id")
    claude_session_id = payload.get("claude_session_id")

    if not herdr_tab_id:
        # AC11-equivalent (carried over from SessionStart): a non-Herdr
        # canonical interactive Claude session is observe-only -- there is
        # no TabBinding to rebind, so `/task` is not-applicable here (not a
        # validation failure of the target itself).
        return {"decision": "pass", "reason_code": "observe_only_non_herdr"}
    if not claude_session_id:
        return {"decision": "block", "reason_code": "missing_session_id"}

    target_repo = payload.get("slash_task_target_repo")
    target_ref_kind = payload.get("slash_task_target_ref_kind")
    target_ref_number = payload.get("slash_task_target_ref_number")
    ad_hoc_title = payload.get("slash_task_ad_hoc_title")
    has_explicit_target = bool(target_repo and target_ref_kind and target_ref_number is not None) or bool(
        ad_hoc_title
    )

    try:
        binding = service.get_binding_by_current_session(conn, claude_session_id)
    except errors.NotFoundError:
        binding = None

    bootstrapped = False
    if binding is None:
        # AC2/AC5: never bootstrap speculatively -- only once the user's own
        # `/task` invocation already carries an explicit, already-parsed
        # target. With no target at all, this is exactly the same command
        # failure it always was (see the module docstring above).
        if not has_explicit_target:
            return {"decision": "block", "reason_code": "no_binding_for_session"}
        herdr_locator = payload.get("herdr_locator") or herdr_tab_id
        run_kind, runtime_profile, resume_profile = config.normalize_operator_profiles_for_new_run(
            *config.operator_run_kind_and_profiles()
        )
        # PR #2795 review fix_delta P1-A/P1-B (comment 5852749710): the
        # bootstrap (create Binding, relocate WITHOUT evicting a foreign
        # holder, start run, attach session) and the target bind now run
        # inside a single `BEGIN IMMEDIATE` via
        # `bootstrap_binding_and_bind_target` -- a failure anywhere in
        # either half leaves no half-applied Binding/RuntimeLocation/
        # ExecutionRun/Task behind.
        rebound = service.bootstrap_binding_and_bind_target(
            conn,
            herdr_locator=herdr_locator,
            location_fields=_location_fields(payload),
            claude_session_id=claude_session_id,
            run_kind=run_kind,
            runtime_profile=runtime_profile,
            resume_profile=resume_profile,
            bootstrap_event_type="hook:UserPromptExpansion",
            bootstrap_reason_code="slash_task_bootstrap_unbound_session",
            bind_event_type="hook:UserPromptExpansion",
            bind_reason_code="slash_task_rebind",
            target_repo=target_repo,
            target_ref_kind=target_ref_kind,
            target_ref_number=target_ref_number,
            ad_hoc_title=ad_hoc_title,
        )
        bootstrapped = True
    else:
        binding_id = binding["id"]
        _, _, current_run_id = service.get_current_task_activity_for_binding(conn, binding_id)
        if target_repo and target_ref_kind and target_ref_number is not None:
            rebound = service.bind_target_to_binding(
                conn,
                binding_id=binding_id,
                execution_run_id=current_run_id,
                repo=target_repo,
                ref_kind=target_ref_kind,
                ref_number=target_ref_number,
                reason_code="slash_task_rebind",
                event_type="hook:UserPromptExpansion",
            )
        elif ad_hoc_title:
            rebound = service.bind_ad_hoc_task_to_binding(
                conn,
                binding_id=binding_id,
                execution_run_id=current_run_id,
                title=ad_hoc_title,
                reason_code="slash_task_rebind",
                event_type="hook:UserPromptExpansion",
            )
        else:
            return {"decision": "block", "reason_code": "slash_task_missing_target"}

    return {
        "decision": "pass",
        "reason_code": "slash_task_bootstrap_rebind" if bootstrapped else "slash_task_rebind",
        "task_id": rebound["task_id"],
        "activity_id": rebound["activity_id"],
        "bootstrapped_binding": bootstrapped,
        **_projection_fields(rebound),
    }


# ---------------------------------------------------------------------------
# CwdChanged (AC7)
# ---------------------------------------------------------------------------


def on_cwd_changed(conn, payload: dict[str, Any]) -> dict[str, Any]:
    """AC7 + fix_delta 7: record cwd / worktree / branch as a mutable
    ``RuntimeLocation`` **observation** alongside the Herdr locator, so the
    statusLine can show which worktree/branch this operator is in.

    This is display-only. `CwdChanged` never switches Task/Activity, never
    rebinds and never blocks -- promoting worktree/branch to Task identity is
    an explicit Issue #2564 Stop Condition."""
    claude_session_id = payload.get("claude_session_id")
    herdr_locator = payload.get("herdr_locator")
    if not claude_session_id or not herdr_locator:
        return {"decision": "pass", "reason_code": "missing_fields"}
    try:
        binding = service.get_binding_by_current_session(conn, claude_session_id)
    except errors.NotFoundError:
        return {"decision": "pass", "reason_code": "no_binding_for_session"}
    binding_id = binding["id"]
    location_fields = _location_fields(payload)
    current_location = service.get_current_location(conn, binding_id)
    if current_location is not None and current_location["herdr_locator"] == herdr_locator:
        unchanged = all(
            current_location.get(key) == location_fields[key] for key in _LOCATION_PAYLOAD_KEYS
        )
        if unchanged:
            return {"decision": "pass", "reason_code": "location_unchanged"}
    service.relocate_binding(conn, binding_id, herdr_locator, **location_fields)
    projection = _bump_projection(conn, binding_id)
    return {"decision": "pass", "reason_code": "relocated", "binding_id": binding_id, **projection}


# ---------------------------------------------------------------------------
# SubagentStart / SubagentStop (AC8)
# ---------------------------------------------------------------------------


def _parent_task_activity_for_session(conn, claude_session_id: str | None):
    if not claude_session_id:
        return None, None
    try:
        binding = service.get_binding_by_current_session(conn, claude_session_id)
    except errors.NotFoundError:
        return None, None
    task_id, activity_id, _ = service.get_current_task_activity_for_binding(conn, binding["id"])
    return task_id, activity_id


def on_subagent_start(conn, payload: dict[str, Any]) -> dict[str, Any]:
    """AC8 (+ fix_delta 5): SubAgent runs roll up under the parent
    Task/Activity as a non-managed ExecutionRun -- never given their own
    TabBinding -- and record the Claude Code ``agent_id`` so the matching
    ``SubagentStop`` can end this exact run even when sibling SubAgents are
    running concurrently."""
    claude_session_id = payload.get("claude_session_id")
    agent_id = payload.get("agent_id") or None
    task_id, activity_id = _parent_task_activity_for_session(conn, claude_session_id)
    run = service.start_execution_run(
        conn, run_kind="subagent", task_id=task_id, activity_id=activity_id, agent_id=agent_id
    )
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
    return {
        "decision": "pass",
        "reason_code": "subagent_started",
        "execution_run_id": run["id"],
        "agent_id": agent_id,
    }


def on_subagent_stop(conn, payload: dict[str, Any]) -> dict[str, Any]:
    """fix_delta 5: end the ExecutionRun belonging to the *exact*
    ``agent_id`` that stopped.

    Previously this ended ``open_subagent_runs[0]``, so with two concurrent
    SubAgents (A started, B started, A stops) the stop for A could end B's
    run. When Claude Code does supply ``agent_id`` we now resolve the run
    exactly; when it does not, we only close an unambiguous single open run
    and otherwise close nothing rather than guessing."""
    claude_session_id = payload.get("claude_session_id")
    agent_id = payload.get("agent_id") or None
    task_id, activity_id = _parent_task_activity_for_session(conn, claude_session_id)

    if agent_id is not None:
        matching = service.find_open_execution_runs(conn, run_kind="subagent", agent_id=agent_id)
        if not matching:
            return {"decision": "pass", "reason_code": "no_open_subagent_run_for_agent_id", "agent_id": agent_id}
        target_run_id = matching[0]["id"]
    else:
        open_subagent_runs = service.find_open_execution_runs(
            conn, task_id=task_id, activity_id=activity_id, run_kind="subagent"
        )
        if not open_subagent_runs:
            return {"decision": "pass", "reason_code": "no_open_subagent_run"}
        if len(open_subagent_runs) > 1:
            # Ambiguous without an agent_id -- never guess which concurrent
            # SubAgent stopped; leave every run open rather than ending the
            # wrong one. The next stop carrying an agent_id resolves exactly.
            return {
                "decision": "pass",
                "reason_code": "subagent_stop_ambiguous_without_agent_id",
                "open_subagent_run_count": len(open_subagent_runs),
            }
        target_run_id = open_subagent_runs[0]["id"]

    run = service.end_execution_run(conn, target_run_id)
    _record(
        conn,
        event_type="hook:SubagentStop",
        task_id=run["task_id"],
        activity_id=run["activity_id"],
        binding_id=None,
        execution_run_id=run["id"],
        reason_code="subagent_ended",
        status="pass",
    )
    return {
        "decision": "pass",
        "reason_code": "subagent_ended",
        "execution_run_id": run["id"],
        "agent_id": agent_id,
    }


# ---------------------------------------------------------------------------
# PreToolUse -- Task-aware cross-session messaging / cross-Task Herdr
# control guard (Issue #2566). Every other tool (Read/Write/Edit/ListAgents/
# ...) stays observability-only, unchanged from Issue #2564 -- existing
# PreToolUse guards already cover tool-level policy for those, and Native
# Claude `ListAgents` is explicitly never blocked on Task Context grounds
# (Issue #2566 In Scope).
# ---------------------------------------------------------------------------


def _resolve_caller_task_id(conn, claude_session_id: str | None) -> str | None:
    if not claude_session_id:
        return None
    try:
        binding = service.get_binding_by_current_session(conn, claude_session_id)
    except errors.NotFoundError:
        return None
    task_id, _, _ = service.get_current_task_activity_for_binding(conn, binding["id"])
    return task_id


def _record_pre_tool_use_guard_event(
    conn,
    *,
    transport: str,
    operation: str,
    task_id: str | None,
    target_kind_value: str,
    destination_task_id: str | None,
    decision: str,
    reason_code: str,
) -> None:
    """AC7 bounded EventJournal write for the guard's own decision --
    `transport`/`operation`/`target_kind`/`source_task_id`/
    `destination_task_id`/`decision`/`reason_code` only. Never the peer
    message body, terminal output, or full Bash command line (those never
    reach this function in the first place -- see
    `pre_tool_use_classifier.py` / the SendMessage field extraction in
    `hook_entry.py`)."""
    service.append_event(
        conn,
        event_type="hook:PreToolUse",
        task_id=task_id,
        metadata={
            "transport": transport,
            "operation": operation,
            "target_kind": target_kind_value,
            "source_task_id": task_id,
            "destination_task_id": destination_task_id,
            "decision": decision,
            "reason_code": reason_code,
        },
    )


def _on_pre_tool_use_send_message(conn, payload: dict[str, Any]) -> dict[str, Any]:
    """Issue #2566 fix_delta P1-B iteration 2 (operator finding, OWNER PR
    #2691 review, 2026-09-21): independent investigation of Claude Code's
    own ``https://code.claude.com/docs/en/cross-session-messaging`` docs,
    plus a real on-machine sample of its own on-disk session registry
    (``~/.claude/sessions/<pid>.json``), found that a real ``SendMessage``/
    ``notify_when_idle`` ``to`` value for a genuinely independent (non-child)
    Claude Code session addresses that session by its ``name`` field -- not
    by a raw ``claude_session_id``, and not only by the ``agentId``-shaped
    identifier this module already resolves via the open-SubAgent-
    ExecutionRun lookup below. Claude Code itself already owns and
    maintains that on-disk registry (one JSON record per session, carrying
    both ``name`` and ``sessionId``); this module never builds a new peer
    registry/router of its own -- ``task_context_session_registry`` only
    performs a **read-only** scan of that existing, Claude-Code-owned
    registry to translate a ``name`` into the ``sessionId`` Task Context's
    own ``tab_bindings.current_claude_session_id`` already tracks (see
    ``task_context_session_registry.resolve_session_name_to_claude_session_id``
    for the read-only lookup and its fail-closed/collision behavior).

    Known limitation (intentionally out of scope for this fix_delta): when
    more than one on-disk registry record shares the same ``name`` (a short-
    identifier collision, per Claude Code's own docs), that name resolves
    to ``None`` (unresolved) rather than being guessed at -- Claude Code's
    exact short-identifier derivation algorithm for that case was not
    confirmed by this investigation, and guessing wrong would silently
    misroute a cross-Task guard decision to the wrong peer, which is worse
    than the existing ``unknown_independent_session`` -> ASK fail-safe a
    ``None`` resolution falls through to below.

    The direct ``get_binding_by_current_session(conn, to)`` fallback
    (``to`` equals ``claude_session_id`` verbatim) is preserved, unchanged,
    for backward compatibility -- it is tried only when the name-based
    resolver above returns no match, so a real ``claude_session_id``-shaped
    ``to`` (should one ever occur) would still resolve correctly."""
    to = payload.get("to")
    caller_task_id = _resolve_caller_task_id(conn, payload.get("claude_session_id"))

    is_in_session_subagent = False
    peer_session_found = False
    peer_task_id: str | None = None
    if to:
        # `to` matching an *open* SubAgent ExecutionRun's own `agent_id`
        # (already tracked via SubagentStart, Issue #2564) covers both an
        # in-session SubAgent and an Agent Teams teammate represented the
        # same way -- never a new peer registry, just the existing
        # ExecutionRun bookkeeping.
        if service.find_open_execution_runs(conn, run_kind="subagent", agent_id=to):
            is_in_session_subagent = True
        else:
            peer_binding = None
            # Name-based resolution against Claude Code's own on-disk
            # session registry (read-only; see docstring above and
            # `task_context_session_registry` module docstring). Only when
            # this resolves to exactly one `sessionId` do we look that
            # `sessionId` up as a Binding -- an unresolved name (no match,
            # or a collision) falls through to the legacy direct fallback
            # below rather than being guessed at.
            resolved_session_id = session_registry.resolve_session_name_to_claude_session_id(to)
            if resolved_session_id:
                try:
                    peer_binding = service.get_binding_by_current_session(conn, resolved_session_id)
                except errors.NotFoundError:
                    peer_binding = None
            else:
                # Legacy defensive fallback: `to` equals `claude_session_id`
                # verbatim. Tried only when the name-based resolver above
                # found no match, preserving prior back-compat behavior.
                try:
                    peer_binding = service.get_binding_by_current_session(conn, to)
                except errors.NotFoundError:
                    peer_binding = None
            if peer_binding is not None:
                peer_session_found = True
                peer_task_id, _, _ = service.get_current_task_activity_for_binding(conn, peer_binding["id"])

    resolved_kind = target_kind.classify_send_message_target(
        to=to,
        is_in_session_subagent=is_in_session_subagent,
        peer_session_found=peer_session_found,
        peer_task_id=peer_task_id,
        caller_task_id=caller_task_id,
    )
    decision, reason_code = target_kind.decision_for_target_kind(resolved_kind)
    operation = "notify_when_idle" if payload.get("notify_when_idle") else "send_message"
    _record_pre_tool_use_guard_event(
        conn,
        transport="send_message",
        operation=operation,
        task_id=caller_task_id,
        target_kind_value=resolved_kind,
        destination_task_id=peer_task_id,
        decision=decision,
        reason_code=reason_code,
    )
    return {"decision": decision, "reason_code": reason_code, "target_kind": resolved_kind}


def _on_pre_tool_use_herdr(conn, payload: dict[str, Any]) -> dict[str, Any]:
    category = payload.get("herdr_category")
    operation = payload.get("herdr_operation") or "unknown"

    if category == "discovery":
        # Metadata-only discovery: ALLOW/no-decision, never journaled as a
        # guard decision (Issue #2566 In Scope).
        return {"decision": "pass", "reason_code": "herdr_discovery_no_decision"}

    caller_task_id = _resolve_caller_task_id(conn, payload.get("claude_session_id"))
    locator = payload.get("herdr_target_locator")
    machine_scoped = bool(payload.get("herdr_machine_scoped"))

    peer_task_id: str | None = None
    locator_resolved = False
    if locator and not machine_scoped:
        peer_binding = service.get_binding_by_current_location(conn, locator)
        if peer_binding is not None:
            locator_resolved = True
            peer_task_id, _, _ = service.get_current_task_activity_for_binding(conn, peer_binding["id"])

    resolved_kind = target_kind.classify_herdr_target(
        machine_scoped=machine_scoped,
        locator_resolved=locator_resolved,
        peer_task_id=peer_task_id,
        caller_task_id=caller_task_id,
    )
    decision, reason_code = target_kind.decision_for_target_kind(resolved_kind)
    _record_pre_tool_use_guard_event(
        conn,
        transport="herdr",
        operation=operation,
        task_id=caller_task_id,
        target_kind_value=resolved_kind,
        destination_task_id=peer_task_id,
        decision=decision,
        reason_code=reason_code,
    )
    return {"decision": decision, "reason_code": reason_code, "target_kind": resolved_kind}


def on_pre_tool_use(conn, payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = payload.get("tool_name")
    if tool_name in ("SendMessage", "notify_when_idle"):
        return _on_pre_tool_use_send_message(conn, payload)
    if tool_name == "Bash" and payload.get("herdr_category"):
        return _on_pre_tool_use_herdr(conn, payload)
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
    "UserPromptExpansion": on_user_prompt_expansion,
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

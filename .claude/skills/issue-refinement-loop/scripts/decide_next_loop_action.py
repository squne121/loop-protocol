#!/usr/bin/env python3
"""
decide_next_loop_action.py

Deterministic routing script for the issue-refinement-loop orchestrator.
Reads LOOP_STATE_V1 (from file or stdin JSON) and a compact review result,
then emits the next action to stdout.

This script is READ-ONLY with respect to loop state — it never mutates the
state file. All mutation is the orchestrator's responsibility.

#1873 (bounded review loops): this router was reduced to a bounded
verdict/iteration/max_iterations decision. The Replay Arbitration (Step 2a),
ISSUE_REFINEMENT_PHASE_STATE_V1 phase-gate, and ISSUE_EXECUTION_DECISION_V1
self-validation that previously wrapped this router were removed — the
orchestrator now trusts the reviewer VERDICT directly (see
`compact_review_result.py` / `issue-reviewer` SubAgent) instead of
independently re-deriving it via `reviewer_claim_replay.py`.

Input:
  --loop-state-file <path>      Path to LOOP_STATE_V1 JSON file
  --loop-state-json <json>      Inline LOOP_STATE_V1 JSON (alternative to file)
  --review-result-verdict <v>   One of: approve | needs-fix | null  (optional;
                                when omitted, loop_state.last_verdict is used)
  --max-iterations <N>          Override max_iterations from state (default: 3)

Output (stdout, budget < 2000 bytes):
  STATUS: pass | warn | human_escalation | inconsistent_state
  NEXT_ACTION: continue_to_step_4 | proceed_to_step_4_5 | human_escalation |
               terminate | proceed_with_contract_update
  BLOCKERS: (optional) blocker codes

Exit codes:
  0  pass      — NEXT_ACTION is actionable
  1  warn      — NEXT_ACTION is actionable but has notes
  2  human_escalation — stop and report to human
  3  inconsistent_state — state is corrupt or contradictory

Priority: inconsistent_state (3) > human_escalation (2) > warn (1) > pass (0).

Verdict resolution:
  When --review-result-verdict is omitted (or passed as "null"/""),
  the router uses loop_state.last_verdict as the single source of truth.
  When both --review-result-verdict and loop_state.last_verdict are non-null
  and differ, the router exits with inconsistent_state (exit 3).

Core decision (verdict / iteration / max_iterations only):
  VERDICT == approve                                -> terminate(approve)
  VERDICT == needs-fix and iteration < max_iterations -> continue(rewrite)
  otherwise (blocked, or iteration >= max_iterations) -> terminate(human_review_required)

scope_signal_guard hard-stop and the #1090/#1323 scope_delta_authority
non-destructive branch (owned by the ANCHOR_SCOPE_REFRAME_V1 lane, #1869)
remain intact — they are evaluated with higher priority than the verdict
routing above and are NOT part of the #1873 simplification scope.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "loop_state/v1"

# Verdict constants
VERDICT_APPROVE = "approve"
VERDICT_NEEDS_FIX = "needs-fix"
VERDICT_NULL = None

# Next action constants
ACTION_CONTINUE_TO_STEP_4 = "continue_to_step_4"
ACTION_PROCEED_TO_STEP_4_5 = "proceed_to_step_4_5"
ACTION_HUMAN_ESCALATION = "human_escalation"
ACTION_TERMINATE = "terminate"
ACTION_PROCEED_WITH_CONTRACT_UPDATE = "proceed_with_contract_update"

# Status constants
STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_HUMAN_ESCALATION = "human_escalation"
STATUS_INCONSISTENT_STATE = "inconsistent_state"

# Exit codes
EXIT_PASS = 0
EXIT_WARN = 1
EXIT_HUMAN_ESCALATION = 2
EXIT_INCONSISTENT_STATE = 3

DEFAULT_MAX_ITERATIONS = 3


# ---------------------------------------------------------------------------
# Minimal structural validation
# ---------------------------------------------------------------------------


def validate_loop_state(data: Any) -> tuple[bool, str]:
    """
    Validate the minimal shape this router depends on.

    #1873: the router no longer validates the full LOOP_STATE_V1 JSON Schema
    (schemas/loop_state.schema.json was removed along with
    build_loop_state.py). It only checks the fields it actually reads:
    `iteration` and `max_iterations` (both optional, must be int if present).

    Returns (valid, error_message). Never raises for ordinary failures.
    """
    if not isinstance(data, dict):
        return False, "loop state must be a JSON object"

    iteration = data.get("iteration", 0)
    if not isinstance(iteration, int) or isinstance(iteration, bool):
        return False, f"iteration must be an int, got {type(iteration).__name__}"

    max_iterations = data.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
        return False, f"max_iterations must be an int, got {type(max_iterations).__name__}"

    scope_signal = data.get("scope_signal_guard", {})
    if scope_signal is not None and not isinstance(scope_signal, dict):
        return False, "scope_signal_guard must be an object when present"

    return True, ""


# ---------------------------------------------------------------------------
# Core routing logic (pure function)
# ---------------------------------------------------------------------------


def decide_next_action(
    loop_state: dict[str, Any],
    review_verdict: Optional[str],
    max_iterations_override: Optional[int] = None,
    scope_signal_guard_decision_v2: Optional[dict[str, Any]] = None,
) -> tuple[str, str, list[str], list[str], Optional[str]]:
    """
    Determine the next action for the refinement loop.

    Args:
        loop_state: Validated LOOP_STATE_V1-shaped dict (read-only).
        review_verdict: "approve" | "needs-fix" | None
        max_iterations_override: If provided, overrides loop_state["max_iterations"].
        scope_signal_guard_decision_v2: Optional SCOPE_SIGNAL_GUARD_DECISION_V2
            sidecar (#1090/#1323). NOT part of LOOP_STATE_V1 — passed as a
            separate argument, same pattern as before. When
            scope_signal_guard_decision_v2.scope_delta_authority.route.action
            == "contract_update_required" (the SCOPE_DELTA_AUTHORITY_V1 nested
            route emitted by classify_scope_delta_authority() in
            scope_signal_delta.py), this router returns
            NEXT_ACTION: proceed_with_contract_update without touching
            termination_reason (loop stays open, Issue contract update
            happens out-of-band, then refinement re-runs).

    Returns:
        (status, next_action, commands, blockers, termination_cause_hint)

    Priority order:
        1. inconsistent_state — corrupt/contradictory state fields
        2. proceed_with_contract_update —
           scope_signal_guard_decision_v2.scope_delta_authority.route.action
           == contract_update_required (#1323, non-destructive branch)
        3. scope_signal_guard hard stop
        4. max_iterations exceeded
        5. routing on verdict (bounded #1873 decision: approve / needs-fix / other)
    """
    iteration: int = loop_state.get("iteration", 0)
    max_iterations: int = (
        max_iterations_override
        if max_iterations_override is not None
        else loop_state.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    )
    termination_reason = loop_state.get("termination_reason")
    scope_signal = loop_state.get("scope_signal_guard", {}) or {}
    blockers: list[str] = []
    _commands: list[str] = []

    # --- Priority 1: inconsistent_state detection ---
    if iteration < 0:
        return (
            STATUS_INCONSISTENT_STATE,
            ACTION_HUMAN_ESCALATION,
            [],
            ["iteration_negative"],
            None,
        )
    if max_iterations < 1:
        return (
            STATUS_INCONSISTENT_STATE,
            ACTION_HUMAN_ESCALATION,
            [],
            ["max_iterations_below_1"],
            None,
        )

    # --- Already terminated ---
    if termination_reason is not None:
        return (
            STATUS_PASS,
            ACTION_TERMINATE,
            [],
            [],
            None,
        )

    # --- Priority 2 (#1323): explicit human-review contract-update directive.
    # Non-destructive: termination_reason is left untouched (loop keeps
    # running); this only redirects the immediate next step toward a
    # contract update + refinement re-run instead of human_escalation.
    _scope_delta_authority = (
        scope_signal_guard_decision_v2.get("scope_delta_authority")
        if isinstance(scope_signal_guard_decision_v2, dict)
        else None
    )
    _authority_route_action = (
        _scope_delta_authority.get("route", {}).get("action")
        if isinstance(_scope_delta_authority, dict)
        else None
    )
    if _authority_route_action == "contract_update_required":
        return (
            STATUS_PASS,
            ACTION_PROCEED_WITH_CONTRACT_UPDATE,
            [],
            [],
            None,
        )

    # --- Priority 3: scope signal guard hard stop ---
    if scope_signal.get("triggered") and not scope_signal.get(
        "excluded_by_anchor_reframe", False
    ):
        reason_code = scope_signal.get("reason_code")
        scope_blockers = ["scope_signal_guard_triggered"]
        if reason_code:
            scope_blockers.append(f"scope_signal_guard_reason_code:{reason_code}")
        return (
            STATUS_HUMAN_ESCALATION,
            ACTION_HUMAN_ESCALATION,
            [],
            scope_blockers,
            "human_judgment_required",
        )

    # --- Priority 4: max_iterations exceeded ---
    if review_verdict == VERDICT_NEEDS_FIX and iteration + 1 >= max_iterations:
        blockers = ["max_iterations_exceeded"]
        return (
            STATUS_HUMAN_ESCALATION,
            ACTION_HUMAN_ESCALATION,
            [],
            blockers,
            "max_iterations_exceeded",
        )

    # --- Priority 5: bounded verdict routing (#1873) ---
    if review_verdict == VERDICT_APPROVE:
        return (
            STATUS_PASS,
            ACTION_PROCEED_TO_STEP_4_5,
            [],
            [],
            None,
        )

    if review_verdict == VERDICT_NEEDS_FIX:
        # iteration + 1 < max_iterations is guaranteed here (else escalated above)
        return (
            STATUS_PASS,
            ACTION_CONTINUE_TO_STEP_4,
            [],
            [],
            None,
        )

    # verdict is null/blocked/unknown — terminate to human review
    blockers = [f"unknown_verdict:{review_verdict}"]
    return (
        STATUS_WARN,
        ACTION_HUMAN_ESCALATION,
        [],
        blockers,
        None,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_output(
    status: str,
    next_action: str,
    commands: list[str],
    blockers: list[str],
    termination_cause_hint: Optional[str] = None,
) -> str:
    """Format the stdout output (budget < 2000 bytes)."""
    lines = [
        f"STATUS: {status}",
        f"NEXT_ACTION: {next_action}",
    ]
    if termination_cause_hint is not None:
        lines.append(f"TERMINATION_CAUSE: {termination_cause_hint}")
    if commands:
        for cmd in commands:
            lines.append(f"COMMANDS: {cmd}")
    if blockers:
        for b in blockers:
            lines.append(f"BLOCKERS: {b}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decide next action for the issue-refinement-loop."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--loop-state-file",
        metavar="PATH",
        help="Path to LOOP_STATE_V1 JSON file.",
    )
    group.add_argument(
        "--loop-state-json",
        metavar="JSON",
        help="Inline LOOP_STATE_V1 JSON string.",
    )
    parser.add_argument(
        "--review-result-verdict",
        metavar="VERDICT",
        default=None,
        help=(
            "Review result verdict: approve | needs-fix | null. "
            "When omitted, loop_state.last_verdict is used as the single source of truth. "
            "When both are provided and non-null, they must agree (else inconsistent_state)."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        metavar="N",
        help="Override max_iterations from state (default: 3).",
    )
    sig_group = parser.add_mutually_exclusive_group()
    sig_group.add_argument(
        "--scope-signal-guard-decision-v2-file",
        metavar="PATH",
        default=None,
        help=(
            "Path to a SCOPE_SIGNAL_GUARD_DECISION_V2 JSON sidecar (#1090/#1323). "
            "NOT part of LOOP_STATE_V1; when route == contract_update_required "
            "this router returns NEXT_ACTION: proceed_with_contract_update "
            "without touching termination_reason."
        ),
    )
    sig_group.add_argument(
        "--scope-signal-guard-decision-v2-json",
        metavar="JSON",
        default=None,
        help="Inline SCOPE_SIGNAL_GUARD_DECISION_V2 JSON string (alternative to the file form).",
    )
    return parser.parse_args(argv)


def _load_scope_signal_guard_decision_v2(
    args: argparse.Namespace,
) -> tuple[Optional[dict[str, Any]], str]:
    """Load the optional SCOPE_SIGNAL_GUARD_DECISION_V2 sidecar (#1090/#1323).

    Returns (data, error_msg). error_msg is '' on success (including the
    "not provided" case, where data is None). Malformed input is a soft
    failure (warning only, sidecar treated as absent) -- this sidecar is
    additive/optional and must never fail-closed the whole router.
    """
    if getattr(args, "scope_signal_guard_decision_v2_file", None):
        path = Path(args.scope_signal_guard_decision_v2_file)
        if not path.exists():
            return None, f"scope_signal_guard_decision_v2 file not found: {path}"
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return None, f"Invalid scope_signal_guard_decision_v2 file: {e}"
        if not isinstance(data, dict):
            return None, "scope_signal_guard_decision_v2 must be a JSON object"
        return data, ""

    if getattr(args, "scope_signal_guard_decision_v2_json", None):
        try:
            data = json.loads(args.scope_signal_guard_decision_v2_json)
        except json.JSONDecodeError as e:
            return None, f"Invalid inline scope_signal_guard_decision_v2 JSON: {e}"
        if not isinstance(data, dict):
            return None, "scope_signal_guard_decision_v2 must be a JSON object"
        return data, ""

    return None, ""


def _load_loop_state(args: argparse.Namespace) -> tuple[Optional[dict[str, Any]], str]:
    """Load loop state from file or inline JSON. Returns (data, error_msg)."""
    if args.loop_state_file:
        path = Path(args.loop_state_file)
        if not path.exists():
            return None, f"Loop state file not found: {path}"
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return None, f"Invalid JSON in loop state file: {e}"
        except OSError as e:
            return None, f"Cannot read loop state file: {e}"
        return data, ""

    if args.loop_state_json:
        try:
            data = json.loads(args.loop_state_json)
        except json.JSONDecodeError as e:
            return None, f"Invalid inline JSON: {e}"
        return data, ""

    # No source — try stdin
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return None, "No loop state provided (file, JSON, or stdin required)"
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON from stdin: {e}"
    return data, ""


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # Load loop state
    loop_state, load_error = _load_loop_state(args)
    if load_error or loop_state is None:
        msg = load_error or "No loop state data"
        print(f"STATUS: {STATUS_INCONSISTENT_STATE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(f"BLOCKERS: {msg}")
        sys.exit(EXIT_INCONSISTENT_STATE)

    # Validate minimal shape (#1873: no full JSON Schema dependency)
    valid, error_msg = validate_loop_state(loop_state)
    if not valid:
        print(f"STATUS: {STATUS_INCONSISTENT_STATE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(f"BLOCKERS: {error_msg}")
        sys.exit(EXIT_INCONSISTENT_STATE)

    # Parse CLI verdict (may be absent / "null" / "")
    raw_verdict = args.review_result_verdict
    if raw_verdict in (None, "null", ""):
        cli_verdict: Optional[str] = None
    else:
        cli_verdict = raw_verdict

    # Resolve single source of truth for verdict:
    # - If CLI verdict is absent, use loop_state.last_verdict.
    # - If CLI verdict is present and loop_state.last_verdict is also present
    #   and they differ → inconsistent_state.
    state_last_verdict = loop_state.get("last_verdict")

    if cli_verdict is None:
        # Use loop_state.last_verdict as the authoritative source.
        verdict: Optional[str] = state_last_verdict
    elif state_last_verdict is not None and cli_verdict != state_last_verdict:
        # Both non-null and conflicting.
        print(f"STATUS: {STATUS_INCONSISTENT_STATE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(
            f"BLOCKERS: last_verdict_conflict:"
            f" state={state_last_verdict!r} cli={cli_verdict!r}"
        )
        sys.exit(EXIT_INCONSISTENT_STATE)
    else:
        verdict = cli_verdict

    # Load optional SCOPE_SIGNAL_GUARD_DECISION_V2 sidecar (#1090/#1323).
    # Soft-fail: a malformed/missing sidecar never blocks the router; it is
    # simply treated as absent (BLOCKERS records the parse warning).
    scope_signal_guard_decision_v2, sidecar_load_error = _load_scope_signal_guard_decision_v2(args)
    sidecar_warning: list[str] = [sidecar_load_error] if sidecar_load_error else []

    # Compute next action
    status, next_action, commands, blockers, termination_cause_hint = decide_next_action(
        loop_state=loop_state,
        review_verdict=verdict,
        max_iterations_override=args.max_iterations,
        scope_signal_guard_decision_v2=scope_signal_guard_decision_v2,
    )
    blockers = list(blockers) + sidecar_warning

    # Emit output
    print(_format_output(status, next_action, commands, blockers, termination_cause_hint))

    # Exit with appropriate code
    exit_map = {
        STATUS_PASS: EXIT_PASS,
        STATUS_WARN: EXIT_WARN,
        STATUS_HUMAN_ESCALATION: EXIT_HUMAN_ESCALATION,
        STATUS_INCONSISTENT_STATE: EXIT_INCONSISTENT_STATE,
    }
    sys.exit(exit_map.get(status, EXIT_INCONSISTENT_STATE))


if __name__ == "__main__":
    main()

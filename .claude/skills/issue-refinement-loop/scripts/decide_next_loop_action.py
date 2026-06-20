#!/usr/bin/env python3
"""
decide_next_loop_action.py

Deterministic routing script for the issue-refinement-loop orchestrator.
Reads LOOP_STATE_V1 (from file or stdin JSON) and a compact review result,
then emits the next action to stdout.

This script is READ-ONLY with respect to loop state — it never mutates the
state file. All mutation is the orchestrator's responsibility.

Input:
  --loop-state-file <path>      Path to LOOP_STATE_V1 JSON file
  --loop-state-json <json>      Inline LOOP_STATE_V1 JSON (alternative to file)
  --review-result-verdict <v>   One of: approve | needs-fix | null
  --max-iterations <N>          Override max_iterations from state (optional)
  --phase-state-file <path>     Path to ISSUE_REFINEMENT_PHASE_STATE_V1 JSON (optional)

Output (stdout, budget < 2000 bytes):
  STATUS: pass | warn | human_escalation | inconsistent_state | router_error
  NEXT_ACTION: continue_to_step_4 | proceed_to_step_4_5 | human_escalation | terminate | rebuild_phase_state
  COMMANDS: (optional) argv-array invocation hints
  BLOCKERS: (optional) blocker codes

Exit codes:
  0  pass      — NEXT_ACTION is actionable
  1  warn      — NEXT_ACTION is actionable but has notes
  2  human_escalation — stop and report to human
  3  inconsistent_state — state is corrupt or contradictory

Priority: inconsistent_state (3) > human_escalation (2) > warn (1) > pass (0).

Phase gate (evaluated BEFORE schema validation):
  If --phase-state-file is provided and this router is in forbidden_routers,
  output ISSUE_REFINEMENT_ROUTER_ERROR_V1 and exit 3.
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
PHASE_STATE_SCHEMA_VERSION = "ISSUE_REFINEMENT_PHASE_STATE_V1"
ROUTER_SELF_NAME = "decide_next_loop_action.py"

# Verdict constants
VERDICT_APPROVE = "approve"
VERDICT_NEEDS_FIX = "needs-fix"
VERDICT_NULL = None

# Next action constants
ACTION_CONTINUE_TO_STEP_4 = "continue_to_step_4"
ACTION_PROCEED_TO_STEP_4_5 = "proceed_to_step_4_5"
ACTION_HUMAN_ESCALATION = "human_escalation"
ACTION_TERMINATE = "terminate"
ACTION_REBUILD_PHASE_STATE = "rebuild_phase_state"

# Status constants
STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_HUMAN_ESCALATION = "human_escalation"
STATUS_INCONSISTENT_STATE = "inconsistent_state"
STATUS_ROUTER_ERROR = "router_error"

# Exit codes
EXIT_PASS = 0
EXIT_WARN = 1
EXIT_HUMAN_ESCALATION = 2
EXIT_INCONSISTENT_STATE = 3


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "loop_state.schema.json"
)


def _load_loop_state_schema() -> Optional[dict[str, Any]]:
    """Load loop_state.schema.json. Returns None if unavailable."""
    try:
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None


def validate_loop_state(data: Any) -> tuple[bool, str]:
    """
    Validate loop state data against loop_state.schema.json.

    Returns (valid, error_message). Never raises for ordinary failures.

    Fail-close contract:
    - If jsonschema is not importable → return (False, "jsonschema not available: ...")
    - If schema file is unreadable → return (False, "Schema file unavailable: ...")
    Fallback validation paths are intentionally absent.
    """
    if not isinstance(data, dict):
        return False, "loop state must be a JSON object"

    try:
        import jsonschema
    except ImportError as exc:
        return False, f"jsonschema not available: {exc}"

    schema = _load_loop_state_schema()
    if schema is None:
        return False, f"Schema file unavailable: {_SCHEMA_PATH}"

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        return False, f"Schema validation failed: {e.message}"

    return True, ""


# ---------------------------------------------------------------------------
# Phase gate (evaluated BEFORE schema validation)
# ---------------------------------------------------------------------------


def _load_phase_state(path: str) -> tuple[Optional[dict[str, Any]], str]:
    """Load ISSUE_REFINEMENT_PHASE_STATE_V1 from file. Returns (data, error_msg)."""
    p = Path(path)
    if not p.exists():
        return None, f"Phase state file not found: {path}"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in phase state file: {e}"
    if not isinstance(data, dict):
        return None, "Phase state must be a JSON object"
    return data, ""


def _check_phase_gate(
    phase_state: dict[str, Any],
) -> tuple[bool, Optional[dict[str, Any]]]:
    """
    Check whether this router (decide_next_loop_action.py) is forbidden in the
    current phase.

    Returns (allowed: bool, router_error_payload: dict | None).
    If not allowed, router_error_payload contains the ISSUE_REFINEMENT_ROUTER_ERROR_V1.
    """
    schema_version = phase_state.get("schema_version")
    if schema_version != PHASE_STATE_SCHEMA_VERSION:
        error = {
            "schema_version": "ISSUE_REFINEMENT_ROUTER_ERROR_V1",
            "status": "router_error",
            "reason_code": "missing_phase_state",
            "phase": phase_state.get("phase", "unknown"),
            "attempted_router": ROUTER_SELF_NAME,
            "allowed_routers": phase_state.get("allowed_routers", []),
            "forbidden_reason": (
                f"Phase state schema_version mismatch: expected "
                f"{PHASE_STATE_SCHEMA_VERSION!r}, got {schema_version!r}"
            ),
            "missing_fields": [],
            "unexpected_fields": [],
            "next_action": "rebuild_phase_state",
        }
        return False, error

    phase = phase_state.get("phase", "unknown")
    forbidden_routers = phase_state.get("forbidden_routers", [])
    allowed_routers = phase_state.get("allowed_routers", [])

    if ROUTER_SELF_NAME in forbidden_routers:
        error = {
            "schema_version": "ISSUE_REFINEMENT_ROUTER_ERROR_V1",
            "status": "router_error",
            "reason_code": "phase_not_allowed",
            "phase": phase,
            "attempted_router": ROUTER_SELF_NAME,
            "allowed_routers": allowed_routers,
            "forbidden_reason": (
                f"Router {ROUTER_SELF_NAME!r} is forbidden in phase {phase!r}. "
                f"In this phase, scope_signal_guard.triggered means 'continue_investigation' "
                f"(hard_stop_eligible=false). Use investigation or review phase routers instead."
            ),
            "missing_fields": [],
            "unexpected_fields": [],
            "next_action": "rebuild_phase_state",
        }
        return False, error

    return True, None


def _emit_router_error(error: dict[str, Any]) -> None:
    """Emit ISSUE_REFINEMENT_ROUTER_ERROR_V1 to stdout."""
    print(f"STATUS: {STATUS_ROUTER_ERROR}")
    print(f"NEXT_ACTION: {ACTION_REBUILD_PHASE_STATE}")
    print(f"ISSUE_REFINEMENT_ROUTER_ERROR_V1: {json.dumps(error, ensure_ascii=False)}")


# ---------------------------------------------------------------------------
# Core routing logic (pure function)
# ---------------------------------------------------------------------------


def decide_next_action(
    loop_state: dict[str, Any],
    review_verdict: Optional[str],
    max_iterations_override: Optional[int] = None,
) -> tuple[str, str, list[str], list[str], Optional[str]]:
    """
    Determine the next action for the refinement loop.

    Args:
        loop_state: Validated LOOP_STATE_V1 dict (read-only).
        review_verdict: "approve" | "needs-fix" | None
        max_iterations_override: If provided, overrides loop_state["max_iterations"].

    Returns:
        (status, next_action, commands, blockers, termination_cause_hint)

    Priority order:
        1. inconsistent_state — corrupt/contradictory state fields
        2. human_escalation  — max_iterations exceeded or hard stop signal
        3. routing on verdict
    """
    iteration: int = loop_state.get("iteration", 0)
    max_iterations: int = (
        max_iterations_override
        if max_iterations_override is not None
        else loop_state.get("max_iterations", 3)
    )
    termination_reason = loop_state.get("termination_reason")
    scope_signal = loop_state.get("scope_signal_guard", {})
    blockers: list[str] = []
    commands: list[str] = []

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

    # --- Priority 2: scope signal guard hard stop ---
    if scope_signal.get("triggered") and not scope_signal.get(
        "excluded_by_anchor_reframe", False
    ):
        # Normalize: orchestrator must use human_judgment_required as termination_cause.
        # scope_signal_guard.reason_code is NOT a valid termination_cause enum value.
        # reason_code is preserved in blockers so orchestrator can surface it in blockers_summary.
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

    # --- Priority 2b: max_iterations exceeded ---
    if iteration + 1 >= max_iterations:
        blockers = ["max_iterations_exceeded"]
        return (
            STATUS_HUMAN_ESCALATION,
            ACTION_HUMAN_ESCALATION,
            [],
            blockers,
            "max_iterations_exceeded",
        )

    # --- Priority 3: verdict routing ---
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

    # verdict is null or unknown — warn but allow continuation
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
        help="Review result verdict: approve | needs-fix | null",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        metavar="N",
        help="Override max_iterations from state.",
    )
    parser.add_argument(
        "--phase-state-file",
        metavar="PATH",
        default=None,
        help=(
            "Path to ISSUE_REFINEMENT_PHASE_STATE_V1 JSON file. "
            "When provided, phase gate is evaluated BEFORE schema validation. "
            "If this router is forbidden in the current phase, "
            "ISSUE_REFINEMENT_ROUTER_ERROR_V1 is emitted and exit 3 is returned."
        ),
    )
    return parser.parse_args(argv)


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

    # --- Phase gate (evaluated BEFORE schema validation) ---
    if args.phase_state_file:
        phase_state, phase_load_err = _load_phase_state(args.phase_state_file)
        if phase_load_err or phase_state is None:
            # Cannot read phase state — treat as missing_phase_state error
            error = {
                "schema_version": "ISSUE_REFINEMENT_ROUTER_ERROR_V1",
                "status": "router_error",
                "reason_code": "missing_phase_state",
                "phase": "unknown",
                "attempted_router": ROUTER_SELF_NAME,
                "allowed_routers": [],
                "forbidden_reason": phase_load_err or "Phase state data unavailable",
                "missing_fields": [],
                "unexpected_fields": [],
                "next_action": "rebuild_phase_state",
            }
            _emit_router_error(error)
            sys.exit(EXIT_INCONSISTENT_STATE)

        allowed, router_error = _check_phase_gate(phase_state)
        if not allowed and router_error is not None:
            _emit_router_error(router_error)
            sys.exit(EXIT_INCONSISTENT_STATE)

    # Load loop state
    loop_state, load_error = _load_loop_state(args)
    if load_error or loop_state is None:
        msg = load_error or "No loop state data"
        print(f"STATUS: {STATUS_INCONSISTENT_STATE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(f"BLOCKERS: {msg}")
        sys.exit(EXIT_INCONSISTENT_STATE)

    # Validate schema
    valid, error_msg = validate_loop_state(loop_state)
    if not valid:
        print(f"STATUS: {STATUS_INCONSISTENT_STATE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(f"BLOCKERS: {error_msg}")
        sys.exit(EXIT_INCONSISTENT_STATE)

    # Parse verdict
    raw_verdict = args.review_result_verdict
    if raw_verdict in (None, "null", ""):
        verdict: Optional[str] = None
    else:
        verdict = raw_verdict

    # Detect conflict: last_verdict in state vs --review-result-verdict CLI flag.
    # Both being non-null with different values is an inconsistent state.
    state_last_verdict = loop_state.get("last_verdict")
    if (
        verdict is not None
        and state_last_verdict is not None
        and verdict != state_last_verdict
    ):
        print(f"STATUS: {STATUS_INCONSISTENT_STATE}")
        print(f"NEXT_ACTION: {ACTION_HUMAN_ESCALATION}")
        print(
            f"BLOCKERS: last_verdict_conflict:"
            f" state={state_last_verdict!r} cli={verdict!r}"
        )
        sys.exit(EXIT_INCONSISTENT_STATE)

    # Compute next action
    status, next_action, commands, blockers, termination_cause_hint = decide_next_action(
        loop_state=loop_state,
        review_verdict=verdict,
        max_iterations_override=args.max_iterations,
    )

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

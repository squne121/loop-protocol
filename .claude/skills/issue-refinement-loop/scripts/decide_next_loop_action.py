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

Output (stdout, budget < 2000 bytes):
  STATUS: pass | warn | human_escalation | inconsistent_state
  NEXT_ACTION: continue_to_step_4 | proceed_to_step_4_5 | human_escalation | terminate
  COMMANDS: (optional) argv-array invocation hints
  BLOCKERS: (optional) blocker codes

Exit codes:
  0  pass      — NEXT_ACTION is actionable
  1  warn      — NEXT_ACTION is actionable but has notes
  2  human_escalation — stop and report to human
  3  inconsistent_state — state is corrupt or contradictory

Priority: inconsistent_state (3) > human_escalation (2) > warn (1) > pass (0).
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
    """
    if not isinstance(data, dict):
        return False, "loop state must be a JSON object"

    try:
        import jsonschema
    except ImportError:
        # jsonschema unavailable — do minimal field check only
        required = {"issue_number", "iteration", "max_iterations", "last_verdict"}
        missing = required - data.keys()
        if missing:
            return False, f"Missing required fields: {sorted(missing)}"
        return True, ""

    schema = _load_loop_state_schema()
    if schema is None:
        # Schema file missing — fall back to required-field check
        required = {"issue_number", "iteration", "max_iterations", "last_verdict"}
        missing = required - data.keys()
        if missing:
            return False, f"Missing required fields: {sorted(missing)}"
        return True, ""

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        return False, f"Schema validation failed: {e.message}"

    return True, ""


# ---------------------------------------------------------------------------
# Core routing logic (pure function)
# ---------------------------------------------------------------------------


def decide_next_action(
    loop_state: dict[str, Any],
    review_verdict: Optional[str],
    max_iterations_override: Optional[int] = None,
) -> tuple[str, str, list[str], list[str]]:
    """
    Determine the next action for the refinement loop.

    Args:
        loop_state: Validated LOOP_STATE_V1 dict (read-only).
        review_verdict: "approve" | "needs-fix" | None
        max_iterations_override: If provided, overrides loop_state["max_iterations"].

    Returns:
        (status, next_action, commands, blockers)

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
        )
    if max_iterations < 1:
        return (
            STATUS_INCONSISTENT_STATE,
            ACTION_HUMAN_ESCALATION,
            [],
            ["max_iterations_below_1"],
        )

    # --- Already terminated ---
    if termination_reason is not None:
        return (
            STATUS_PASS,
            ACTION_TERMINATE,
            [],
            [],
        )

    # --- Priority 2: scope signal guard hard stop ---
    if scope_signal.get("triggered") and not scope_signal.get(
        "excluded_by_anchor_reframe", False
    ):
        return (
            STATUS_HUMAN_ESCALATION,
            ACTION_HUMAN_ESCALATION,
            [],
            ["scope_signal_guard_triggered"],
        )

    # --- Priority 2: max_iterations exceeded (human escalation) ---
    # iteration is already incremented before calling this script.
    # escalate when iteration >= max_iterations.
    if review_verdict == VERDICT_NEEDS_FIX and iteration >= max_iterations:
        blockers = ["max_iterations_exceeded"]
        return (
            STATUS_HUMAN_ESCALATION,
            ACTION_HUMAN_ESCALATION,
            [],
            blockers,
        )

    # --- Priority 3: verdict routing ---
    if review_verdict == VERDICT_APPROVE:
        return (
            STATUS_PASS,
            ACTION_PROCEED_TO_STEP_4_5,
            [],
            [],
        )

    if review_verdict == VERDICT_NEEDS_FIX:
        # iteration + 1 < max_iterations is guaranteed here (else escalated above)
        return (
            STATUS_PASS,
            ACTION_CONTINUE_TO_STEP_4,
            [],
            [],
        )

    # verdict is null or unknown — warn but allow continuation
    blockers = [f"unknown_verdict:{review_verdict}"]
    return (
        STATUS_WARN,
        ACTION_HUMAN_ESCALATION,
        [],
        blockers,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_output(
    status: str,
    next_action: str,
    commands: list[str],
    blockers: list[str],
) -> str:
    """Format the stdout output (budget < 2000 bytes)."""
    lines = [
        f"STATUS: {status}",
        f"NEXT_ACTION: {next_action}",
    ]
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

    # Compute next action
    status, next_action, commands, blockers = decide_next_action(
        loop_state=loop_state,
        review_verdict=verdict,
        max_iterations_override=args.max_iterations,
    )

    # Emit output
    print(_format_output(status, next_action, commands, blockers))

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

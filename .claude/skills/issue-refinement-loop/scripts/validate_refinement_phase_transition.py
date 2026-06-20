#!/usr/bin/env python3
"""
validate_refinement_phase_transition.py

Validates whether a transition from one refinement phase to another is allowed,
and whether a given router is permitted in the current phase.

Usage:
  uv run python3 validate_refinement_phase_transition.py \\
    --phase-state-file <path> \\
    --attempted-router <router_name>

  uv run python3 validate_refinement_phase_transition.py \\
    --from-phase <phase> \\
    --to-phase <phase>

Exit codes:
  0   allowed
  1   forbidden (phase gate blocks the action)
  2   invalid input / unknown phase or router
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

# Valid phase transitions (from -> to)
_VALID_TRANSITIONS: dict[str, list[str]] = {
    "preflight": ["investigation", "review", "publish", "terminate"],
    "investigation": ["review", "publish", "terminate"],
    "review": ["rewrite", "post_rewrite_check", "decide_next_action", "publish", "terminate"],
    "rewrite": ["post_rewrite_check", "review", "publish", "terminate"],
    "post_rewrite_check": ["review", "decide_next_action", "publish", "terminate"],
    "decide_next_action": ["rewrite", "publish", "terminate"],
    "publish": ["terminate"],
    "terminate": [],
}

SCHEMA_VERSION = "ISSUE_REFINEMENT_PHASE_STATE_V1"


def _load_phase_state(path: str) -> tuple[Optional[dict[str, Any]], str]:
    """Load phase state from file. Returns (data, error_msg)."""
    p = Path(path)
    if not p.exists():
        return None, f"Phase state file not found: {path}"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in phase state file: {e}"
    if not isinstance(data, dict):
        return None, "Phase state must be a JSON object"
    if data.get("schema_version") != SCHEMA_VERSION:
        return None, (
            f"Unexpected schema_version: {data.get('schema_version')!r}, "
            f"expected {SCHEMA_VERSION!r}"
        )
    return data, ""


def validate_router_in_phase(
    phase_state: dict[str, Any],
    attempted_router: str,
) -> tuple[bool, str]:
    """
    Check whether `attempted_router` is allowed in the current phase.

    Uses allowlist semantics (B3): a router must be explicitly in allowed_routers
    to pass. If allowed_routers is empty or missing, ALL routers are blocked
    (fail-closed). The forbidden_routers list is informational only.

    Returns (allowed: bool, reason: str).
    """
    allowed = phase_state.get("allowed_routers", [])
    phase = phase_state.get("phase", "unknown")

    # Allowlist gate: router must be explicitly permitted
    if attempted_router not in allowed:
        forbidden = phase_state.get("forbidden_routers", [])
        return False, (
            f"Router {attempted_router!r} is not in allowed_routers for phase {phase!r}. "
            f"Allowed routers: {allowed}. Forbidden routers: {forbidden}"
        )
    return True, f"Router {attempted_router!r} is allowed in phase {phase!r}."


def validate_phase_transition(from_phase: str, to_phase: str) -> tuple[bool, str]:
    """
    Check whether a transition from `from_phase` to `to_phase` is valid.

    Returns (allowed: bool, reason: str).
    """
    valid_targets = _VALID_TRANSITIONS.get(from_phase)
    if valid_targets is None:
        return False, f"Unknown source phase: {from_phase!r}"
    if to_phase not in _VALID_TRANSITIONS:
        return False, f"Unknown target phase: {to_phase!r}"
    if to_phase in valid_targets:
        return True, f"Transition {from_phase!r} -> {to_phase!r} is valid."
    return False, (
        f"Transition {from_phase!r} -> {to_phase!r} is not allowed. "
        f"Valid targets from {from_phase!r}: {valid_targets}"
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate refinement phase transitions and router gate."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--phase-state-file",
        metavar="PATH",
        help="Path to ISSUE_REFINEMENT_PHASE_STATE_V1 JSON file.",
    )
    group.add_argument(
        "--from-phase",
        metavar="PHASE",
        help="Source phase for transition validation.",
    )
    parser.add_argument(
        "--attempted-router",
        metavar="ROUTER",
        help="Router name to validate against phase gate (used with --phase-state-file).",
    )
    parser.add_argument(
        "--to-phase",
        metavar="PHASE",
        help="Target phase for transition validation (used with --from-phase).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.phase_state_file:
        # Router gate validation mode
        if not args.attempted_router:
            print("STATUS: error")
            print("ERROR: --attempted-router is required with --phase-state-file")
            sys.exit(2)

        phase_state, load_err = _load_phase_state(args.phase_state_file)
        if load_err or phase_state is None:
            print("STATUS: error")
            print(f"ERROR: {load_err}")
            sys.exit(2)

        allowed, reason = validate_router_in_phase(phase_state, args.attempted_router)
        if allowed:
            print("STATUS: allowed")
            print(f"REASON: {reason}")
            sys.exit(0)
        else:
            print("STATUS: forbidden")
            print(f"REASON: {reason}")
            sys.exit(1)

    else:
        # Phase transition validation mode
        if not args.to_phase:
            print("STATUS: error")
            print("ERROR: --to-phase is required with --from-phase")
            sys.exit(2)

        allowed, reason = validate_phase_transition(args.from_phase, args.to_phase)
        if allowed:
            print("STATUS: allowed")
            print(f"REASON: {reason}")
            sys.exit(0)
        else:
            print("STATUS: forbidden")
            print(f"REASON: {reason}")
            sys.exit(1)


if __name__ == "__main__":
    main()

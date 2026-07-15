#!/usr/bin/env python3
"""
build_refinement_phase_state.py

Generates ISSUE_REFINEMENT_PHASE_STATE_V1 from a source artifact (preflight result,
review result, loop state, etc.) to indicate which phase the refinement loop is
currently in and which routers are allowed/forbidden.

Usage:
  uv run python3 build_refinement_phase_state.py \\
    --phase <phase_name> \\
    --source-kind <kind> \\
    --source-path <path> \\
    [--loop-state-path <path>] \\
    [--planner-result-path <path>] \\
    [--review-result-path <path>] \\
    [--review-validation-result-path <path>] \\
    --output-path <path>

Phases:
  preflight           After run_refinement_preflight.py, before investigation
  investigation       During Step 1 investigation
  review              During Step 2 review
  rewrite             During Step 4 rewrite
  post_rewrite_check  After rewrite, before final review verdict
  decide_next_action  When decide_next_loop_action.py is the intended router
  publish             During Step 5 publish / termination
  terminate           Loop is terminated

Issue #1507 AC24 (structural enforcement of the SKILL.md Step 2
validator-first mandate): when `--phase review` and
`--source-kind issue_review_result_compact_v1`, `--review-validation-result-path`
is REQUIRED and must point at a REVIEW_COMPACT_VALIDATION_RESULT_V1 JSON
file whose `validation_status` is `valid`. Any other combination of phase /
source_kind does not require this argument (Out of Scope: this gate applies
only to the `review` phase, not `post_rewrite_check` / `decide_next_action`,
which also accept `issue_review_result_compact_v1` per
`_SOURCE_KIND_ALLOWED_PHASES`).

Output:
  Writes ISSUE_REFINEMENT_PHASE_STATE_V1 JSON to --output-path.
  Prints STATUS: ok | error to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Phase definitions
# ---------------------------------------------------------------------------

VALID_PHASES = [
    "preflight",
    "investigation",
    "review",
    "rewrite",
    "post_rewrite_check",
    "decide_next_action",
    "publish",
    "terminate",
]

VALID_SOURCE_KINDS = [
    "refinement_preflight_result_v1",
    "issue_review_result_compact_v1",
    "issue_author_result_compact_v1",
    "loop_state_v1",
]

# source_kind → allowed phases mapping (for consistency checks)
_SOURCE_KIND_ALLOWED_PHASES: dict[str, list[str]] = {
    "refinement_preflight_result_v1": ["preflight", "investigation"],
    "issue_review_result_compact_v1": ["review", "post_rewrite_check", "decide_next_action"],
    "issue_author_result_compact_v1": ["rewrite", "post_rewrite_check"],
    "loop_state_v1": [
        "investigation", "review", "rewrite", "post_rewrite_check",
        "decide_next_action", "publish", "terminate",
    ],
}

# phases that require loop_state_path or review_result_path
_PHASES_REQUIRING_LOOP_STATE = ["post_rewrite_check", "decide_next_action"]
_PHASES_REQUIRING_REVIEW_RESULT = ["review", "post_rewrite_check", "decide_next_action"]

# Issue #1507 AC24: the review-phase validator-first gate applies ONLY to
# this exact (phase, source_kind) pair, per the Issue's Out of Scope note
# (the gate is not extended to post_rewrite_check / decide_next_action, nor
# to any other phase).
_REVIEW_VALIDATION_GATED_PHASE = "review"
_REVIEW_VALIDATION_GATED_SOURCE_KIND = "issue_review_result_compact_v1"

# Router name constants
ROUTER_DECIDE_NEXT_LOOP_ACTION = "decide_next_loop_action.py"
ROUTER_RUN_REFINEMENT_PREFLIGHT = "run_refinement_preflight.py"
ROUTER_PLAN_REFINEMENT_LOOP = "plan_refinement_loop.py"
ROUTER_DECIDE_REWRITE_ROUTE = "decide_rewrite_route.py"
ROUTER_PUBLISH_TERMINATION_REPORT = "publish_termination_report.py"
ROUTER_RENDER_TERMINATION_REPORT = "render_termination_report.py"

# Phase -> (allowed_routers, forbidden_routers, scope_signal_semantics)
_PHASE_ROUTER_RULES: dict[str, dict[str, Any]] = {
    "preflight": {
        "allowed_routers": [
            ROUTER_RUN_REFINEMENT_PREFLIGHT,
            ROUTER_PLAN_REFINEMENT_LOOP,
        ],
        "forbidden_routers": [
            ROUTER_DECIDE_NEXT_LOOP_ACTION,
            ROUTER_DECIDE_REWRITE_ROUTE,
            ROUTER_PUBLISH_TERMINATION_REPORT,
        ],
        "scope_signal_semantics": {
            "triggered_meaning": "continue_investigation",
            "hard_stop_eligible": False,
        },
    },
    "investigation": {
        "allowed_routers": [
            ROUTER_RUN_REFINEMENT_PREFLIGHT,
            ROUTER_PLAN_REFINEMENT_LOOP,
        ],
        "forbidden_routers": [
            ROUTER_DECIDE_NEXT_LOOP_ACTION,
            ROUTER_DECIDE_REWRITE_ROUTE,
        ],
        "scope_signal_semantics": {
            "triggered_meaning": "continue_investigation",
            "hard_stop_eligible": False,
        },
    },
    "review": {
        "allowed_routers": [
            ROUTER_PLAN_REFINEMENT_LOOP,
            ROUTER_DECIDE_REWRITE_ROUTE,
        ],
        "forbidden_routers": [
            ROUTER_DECIDE_NEXT_LOOP_ACTION,
            ROUTER_PUBLISH_TERMINATION_REPORT,
        ],
        "scope_signal_semantics": {
            "triggered_meaning": "continue_investigation",
            "hard_stop_eligible": False,
        },
    },
    "rewrite": {
        "allowed_routers": [
            ROUTER_DECIDE_REWRITE_ROUTE,
        ],
        "forbidden_routers": [
            ROUTER_DECIDE_NEXT_LOOP_ACTION,
            ROUTER_PUBLISH_TERMINATION_REPORT,
        ],
        "scope_signal_semantics": {
            "triggered_meaning": "ignored",
            "hard_stop_eligible": False,
        },
    },
    "post_rewrite_check": {
        "allowed_routers": [
            ROUTER_DECIDE_NEXT_LOOP_ACTION,
            ROUTER_DECIDE_REWRITE_ROUTE,
        ],
        "forbidden_routers": [
            ROUTER_PUBLISH_TERMINATION_REPORT,
        ],
        "scope_signal_semantics": {
            "triggered_meaning": "hard_stop_candidate",
            "hard_stop_eligible": True,
        },
    },
    "decide_next_action": {
        "allowed_routers": [
            ROUTER_DECIDE_NEXT_LOOP_ACTION,
        ],
        "forbidden_routers": [
            ROUTER_DECIDE_REWRITE_ROUTE,
        ],
        "scope_signal_semantics": {
            "triggered_meaning": "hard_stop_candidate",
            "hard_stop_eligible": True,
        },
    },
    "publish": {
        "allowed_routers": [
            ROUTER_PUBLISH_TERMINATION_REPORT,
            ROUTER_RENDER_TERMINATION_REPORT,
        ],
        "forbidden_routers": [
            ROUTER_DECIDE_NEXT_LOOP_ACTION,
            ROUTER_DECIDE_REWRITE_ROUTE,
        ],
        "scope_signal_semantics": {
            "triggered_meaning": "ignored",
            "hard_stop_eligible": False,
        },
    },
    "terminate": {
        "allowed_routers": [],
        "forbidden_routers": [
            ROUTER_DECIDE_NEXT_LOOP_ACTION,
            ROUTER_DECIDE_REWRITE_ROUTE,
            ROUTER_PUBLISH_TERMINATION_REPORT,
        ],
        "scope_signal_semantics": {
            "triggered_meaning": "ignored",
            "hard_stop_eligible": False,
        },
    },
}


def _reject_nonfinite_json(token: str) -> None:
    raise ValueError(f"Non-finite JSON constant rejected: {token}")


def _strict_json_loads(text: str) -> dict[str, Any]:
    return json.loads(text, parse_constant=_reject_nonfinite_json)


def _validate_json_input(path: str | None, *, label: str) -> None:
    if not path:
        return
    try:
        _strict_json_loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} json decode error: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"{label} strict json validation error: {exc}") from exc


def _validate_review_validation_gate(
    phase: str,
    source_kind: str,
    review_validation_result_path: Optional[str],
) -> None:
    """Issue #1507 AC24: structural enforcement of the SKILL.md Step 2
    validator-first mandate for the review phase.

    Raises ValueError (fail-closed, no phase-state file written) when:
      - the gate applies (phase == "review" and
        source_kind == "issue_review_result_compact_v1") but
        --review-validation-result-path was not supplied
      - the referenced file does not exist / is not valid JSON
      - the referenced file's validation_status is not "valid"
    """
    gate_applies = (
        phase == _REVIEW_VALIDATION_GATED_PHASE
        and source_kind == _REVIEW_VALIDATION_GATED_SOURCE_KIND
    )
    if not gate_applies:
        return

    if not review_validation_result_path:
        raise ValueError(
            "--review-validation-result-path is required when --phase review "
            "and --source-kind issue_review_result_compact_v1 "
            "(Issue #1507 AC24 structural validator-first gate)"
        )

    try:
        validation_payload = _strict_json_loads(
            Path(review_validation_result_path).read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise ValueError(
            f"review_validation_result_path not found: {review_validation_result_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"review_validation_result_path json decode error: {exc}"
        ) from exc

    validation_status = validation_payload.get("validation_status")
    if validation_status != "valid":
        raise ValueError(
            "review_validation_result_path validation_status must be 'valid', "
            f"got {validation_status!r} "
            "(Issue #1507 AC24 fail-closed review-phase gate; "
            "phase-state was NOT generated)"
        )


def build_phase_state(
    phase: str,
    source_kind: str,
    source_path: str,
    loop_state_path: Optional[str] = None,
    planner_result_path: Optional[str] = None,
    review_result_path: Optional[str] = None,
    review_validation_result_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Build ISSUE_REFINEMENT_PHASE_STATE_V1.

    Raises ValueError for invalid inputs (unknown phase, missing source_path,
    source_kind/phase inconsistency, missing required paths, review-phase
    validator-gate violations per AC24).

    NOTE: scope_signal_guard.hard_stop_eligible は現在 phase のみで決定される。
    signal_origin（existing_issue_body / rewrite_delta / review_delta）による
    細粒度判定は後続 Issue で対応予定。
    """
    rules = _PHASE_ROUTER_RULES.get(phase)
    if rules is None:
        raise ValueError(f"Unknown phase: {phase!r}. Valid phases: {VALID_PHASES}")

    # M1: source_path existence check
    if not Path(source_path).exists():
        raise ValueError(
            f"source_path does not exist: {source_path!r} "
            f"(phase={phase!r}, source_kind={source_kind!r})"
        )
    _validate_json_input(source_path, label="source_path")
    _validate_json_input(loop_state_path, label="loop_state_path")
    _validate_json_input(planner_result_path, label="planner_result_path")
    _validate_json_input(review_result_path, label="review_result_path")

    # M1: source_kind / phase consistency check
    allowed_phases_for_kind = _SOURCE_KIND_ALLOWED_PHASES.get(source_kind)
    if allowed_phases_for_kind is not None and phase not in allowed_phases_for_kind:
        raise ValueError(
            f"source_kind {source_kind!r} is not compatible with phase {phase!r}. "
            f"Allowed phases for this source_kind: {allowed_phases_for_kind}"
        )

    # Issue #1507 AC24: review-phase validator-first structural gate.
    # Raises ValueError (fail-closed) BEFORE the phase-state dict is built,
    # so no output is ever written for a missing/invalid/non-valid
    # validation result.
    _validate_review_validation_gate(phase, source_kind, review_validation_result_path)

    return {
        "schema_version": "ISSUE_REFINEMENT_PHASE_STATE_V1",
        "phase": phase,
        "source_artifact": {
            "kind": source_kind,
            "path": source_path,
        },
        "loop_state_path": loop_state_path,
        "planner_result_path": planner_result_path,
        "review_result_path": review_result_path,
        "review_validation_result_path": review_validation_result_path,
        "allowed_routers": list(rules["allowed_routers"]),
        "forbidden_routers": list(rules["forbidden_routers"]),
        "scope_signal_semantics": dict(rules["scope_signal_semantics"]),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build ISSUE_REFINEMENT_PHASE_STATE_V1 for issue-refinement-loop."
    )
    parser.add_argument(
        "--phase",
        required=True,
        choices=VALID_PHASES,
        help="Current phase of the refinement loop.",
    )
    parser.add_argument(
        "--source-kind",
        required=True,
        choices=VALID_SOURCE_KINDS,
        help="Kind of the source artifact.",
    )
    parser.add_argument(
        "--source-path",
        required=True,
        help="Path to the source artifact.",
    )
    parser.add_argument(
        "--loop-state-path",
        default=None,
        help="Path to the LOOP_STATE_V1 JSON file (optional).",
    )
    parser.add_argument(
        "--planner-result-path",
        default=None,
        help="Path to the REFINEMENT_LOOP_PLAN_V1 artifact (optional).",
    )
    parser.add_argument(
        "--review-result-path",
        default=None,
        help="Path to the ISSUE_REVIEW_RESULT_COMPACT_V1 artifact (optional).",
    )
    parser.add_argument(
        "--review-validation-result-path",
        default=None,
        help="Path to the REVIEW_COMPACT_VALIDATION_RESULT_V1 JSON file. "
        "Required (and must have validation_status: valid) when --phase review "
        "and --source-kind issue_review_result_compact_v1 (Issue #1507 AC24).",
    )
    parser.add_argument(
        "--output-path",
        required=True,
        help="Path to write the ISSUE_REFINEMENT_PHASE_STATE_V1 JSON.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    try:
        phase_state = build_phase_state(
            phase=args.phase,
            source_kind=args.source_kind,
            source_path=args.source_path,
            loop_state_path=args.loop_state_path,
            planner_result_path=args.planner_result_path,
            review_result_path=args.review_result_path,
            review_validation_result_path=args.review_validation_result_path,
        )
    except ValueError as e:
        print("STATUS: error")
        print(f"ERROR: {e}")
        sys.exit(1)

    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(phase_state, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    print("STATUS: ok")
    print(f"ARTIFACT: phase_state={output_path}")
    print(f"PHASE: {args.phase}")
    print(
        f"HARD_STOP_ELIGIBLE: {phase_state['scope_signal_semantics']['hard_stop_eligible']}"
    )


if __name__ == "__main__":
    main()

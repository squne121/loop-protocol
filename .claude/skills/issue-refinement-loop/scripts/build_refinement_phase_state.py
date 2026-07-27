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
import hashlib
import json
import re
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

# Issue #1755: the review-phase gate now binds to the child-intermediate
# validator (`review_compact.validate_intermediate_v1`,
# emit_parent_review_envelope_v2.py) output schema, not the legacy V1 final
# grammar validator schema. The legacy schema literal is explicitly rejected
# (AC2) to force callers onto the intermediate-validator wiring mandated by
# SKILL.md Step 2.
_REVIEW_VALIDATION_INTERMEDIATE_SCHEMA = "REVIEW_COMPACT_INTERMEDIATE_VALIDATION_RESULT_V1"
_REVIEW_VALIDATION_LEGACY_SCHEMA = "REVIEW_COMPACT_VALIDATION_RESULT_V1"
_REVIEW_VALIDATION_SCHEMA_VERSION = "1"
_REVIEW_VALIDATION_VALID_ENVELOPE_KINDS = frozenset({"approve", "needs_fix_intermediate"})

# Same lexical shape as validate_review_compact_output.py's _ARTIFACT_PATH_RE
# (Issue #1755 AC5: issue-number binding via normalized_payload.ARTIFACT).
_REVIEW_VALIDATION_COMPACT_ARTIFACT_PREFIX = "compact_review_result_v1="
_REVIEW_VALIDATION_ARTIFACT_ISSUE_SEGMENT_RE = re.compile(
    r"^\.claude/artifacts/issue-refinement-loop/(?P<segment>[0-9]+|unknown)/"
)

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


def _extract_artifact_issue_segment(normalized_payload: Any) -> Optional[str]:
    """Issue #1755 AC5: extract the issue-number path segment from
    normalized_payload["ARTIFACT"] (e.g.
    "compact_review_result_v1=.claude/artifacts/issue-refinement-loop/1755/x.json"
    -> "1755"). Returns None if the shape does not match (fail-closed by the
    caller, which treats None as a binding failure)."""
    if not isinstance(normalized_payload, dict):
        return None
    artifact = normalized_payload.get("ARTIFACT")
    if not isinstance(artifact, str):
        return None
    path = artifact
    if path.startswith(_REVIEW_VALIDATION_COMPACT_ARTIFACT_PREFIX):
        path = path[len(_REVIEW_VALIDATION_COMPACT_ARTIFACT_PREFIX):]
    match = _REVIEW_VALIDATION_ARTIFACT_ISSUE_SEGMENT_RE.match(path)
    if not match:
        return None
    return match.group("segment")


def _validate_review_validation_gate(
    phase: str,
    source_kind: str,
    source_path: str,
    review_validation_result_path: Optional[str],
    issue_number: Optional[int],
) -> None:
    """Issue #1507 AC24 / Issue #1755 AC2-AC5: structural enforcement of the
    SKILL.md Step 2 validator-first mandate for the review phase, bound to
    the child-intermediate validator (`review_compact.validate_intermediate_v1`)
    output rather than caller-supplied claims alone.

    Raises ValueError (fail-closed, no phase-state file written) when:
      - the gate applies (phase == "review" and
        source_kind == "issue_review_result_compact_v1") but
        --review-validation-result-path or --issue-number was not supplied
      - the referenced file does not exist / is not valid JSON
      - `schema` is the legacy `REVIEW_COMPACT_VALIDATION_RESULT_V1` literal
        or anything other than `REVIEW_COMPACT_INTERMEDIATE_VALIDATION_RESULT_V1`
      - `schema_version != "1"`
      - `validation_status != "valid"`
      - `envelope_kind` is not one of {"approve", "needs_fix_intermediate"}
      - `violations` is not an empty list
      - the SHA256 recomputed from `--source-path`'s actual bytes does not
        match the validation result's `input_sha256` (stale / cross-input
        receipt rejection)
      - `normalized_payload.ARTIFACT`'s issue-number segment does not match
        `--issue-number` (cross-issue receipt rejection)
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
            "(Issue #1507 AC24 / #1755 structural validator-first gate)"
        )

    if issue_number is None:
        raise ValueError(
            "--issue-number is required when --phase review "
            "and --source-kind issue_review_result_compact_v1 "
            "(Issue #1755 AC5 issue-number binding gate)"
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

    schema = validation_payload.get("schema")
    if schema == _REVIEW_VALIDATION_LEGACY_SCHEMA:
        raise ValueError(
            f"review_validation_result_path schema must be "
            f"{_REVIEW_VALIDATION_INTERMEDIATE_SCHEMA!r}, got legacy literal "
            f"{_REVIEW_VALIDATION_LEGACY_SCHEMA!r} "
            "(Issue #1755 AC2: legacy V1 final-grammar schema is rejected "
            "for the review-phase gate; use review_compact.validate_intermediate_v1)"
        )
    if schema != _REVIEW_VALIDATION_INTERMEDIATE_SCHEMA:
        raise ValueError(
            f"review_validation_result_path schema must be "
            f"{_REVIEW_VALIDATION_INTERMEDIATE_SCHEMA!r}, got {schema!r} "
            "(Issue #1755 AC3 fail-closed review-phase gate; "
            "phase-state was NOT generated)"
        )

    schema_version = validation_payload.get("schema_version")
    if schema_version != _REVIEW_VALIDATION_SCHEMA_VERSION:
        raise ValueError(
            f"review_validation_result_path schema_version must be "
            f"{_REVIEW_VALIDATION_SCHEMA_VERSION!r}, got {schema_version!r} "
            "(Issue #1755 AC3 fail-closed review-phase gate; "
            "phase-state was NOT generated)"
        )

    validation_status = validation_payload.get("validation_status")
    if validation_status != "valid":
        raise ValueError(
            "review_validation_result_path validation_status must be 'valid', "
            f"got {validation_status!r} "
            "(Issue #1507 AC24 / #1755 AC3 fail-closed review-phase gate; "
            "phase-state was NOT generated)"
        )

    envelope_kind = validation_payload.get("envelope_kind")
    if envelope_kind not in _REVIEW_VALIDATION_VALID_ENVELOPE_KINDS:
        raise ValueError(
            "review_validation_result_path envelope_kind must be one of "
            f"{sorted(_REVIEW_VALIDATION_VALID_ENVELOPE_KINDS)}, got {envelope_kind!r} "
            "(Issue #1755 AC3 fail-closed review-phase gate; "
            "phase-state was NOT generated)"
        )

    violations = validation_payload.get("violations")
    if violations != []:
        raise ValueError(
            f"review_validation_result_path violations must be an empty list, "
            f"got {violations!r} "
            "(Issue #1755 AC3 fail-closed review-phase gate; "
            "phase-state was NOT generated)"
        )

    # Issue #1755 AC4: bind the validation result to the ACTUAL bytes of
    # --source-path (never trust the caller-supplied input_sha256 alone --
    # stale / different-input validation results must be rejected).
    try:
        source_bytes = Path(source_path).read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(
            f"source_path not found for input_sha256 binding check: {source_path}"
        ) from exc
    recomputed_sha256 = f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
    input_sha256 = validation_payload.get("input_sha256")
    if input_sha256 != recomputed_sha256:
        raise ValueError(
            f"review_validation_result_path input_sha256 mismatch: "
            f"validation result claims {input_sha256!r}, recomputed "
            f"{recomputed_sha256!r} from --source-path bytes "
            "(Issue #1755 AC4 fail-closed: stale / different-input "
            "validation result rejected; phase-state was NOT generated)"
        )

    # Issue #1755 AC5: bind the validation result to the active --issue-number
    # via normalized_payload.ARTIFACT's issue segment (rejects cross-issue
    # receipts -- a valid validation result for a different issue number
    # combined with the current source artifact).
    normalized_payload = validation_payload.get("normalized_payload")
    artifact_segment = _extract_artifact_issue_segment(normalized_payload)
    if artifact_segment is None:
        raise ValueError(
            "review_validation_result_path normalized_payload.ARTIFACT issue "
            "segment could not be extracted "
            "(Issue #1755 AC5 fail-closed issue-number binding gate; "
            "phase-state was NOT generated)"
        )
    try:
        artifact_issue_number = int(artifact_segment)
    except ValueError as exc:
        raise ValueError(
            "review_validation_result_path normalized_payload.ARTIFACT issue "
            f"segment is not numeric: {artifact_segment!r} "
            "(Issue #1755 AC5 fail-closed issue-number binding gate)"
        ) from exc
    if artifact_issue_number != issue_number:
        raise ValueError(
            f"review_validation_result_path normalized_payload.ARTIFACT issue "
            f"segment {artifact_issue_number} does not match --issue-number "
            f"{issue_number} "
            "(Issue #1755 AC5 fail-closed: cross-issue receipt rejected; "
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
    issue_number: Optional[int] = None,
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
    # Issue #1755: when the review-validation gate applies, --source-path is
    # bound to the EXACT raw child stdout BYTES that were fed to
    # review_compact.validate_intermediate_v1 (the source of its input_sha256
    # digest) -- this is plain field:value text, NOT JSON, so the generic
    # strict-JSON check is skipped for this specific (phase, source_kind)
    # combination. The SHA256 binding check inside
    # _validate_review_validation_gate() is the structural validation for
    # this source_path instead.
    _review_validation_gate_applies = (
        phase == _REVIEW_VALIDATION_GATED_PHASE
        and source_kind == _REVIEW_VALIDATION_GATED_SOURCE_KIND
    )
    if not _review_validation_gate_applies:
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
    _validate_review_validation_gate(
        phase, source_kind, source_path, review_validation_result_path, issue_number
    )

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
        help="Path to the REVIEW_COMPACT_INTERMEDIATE_VALIDATION_RESULT_V1 JSON "
        "file (review_compact.validate_intermediate_v1 output). Required (and "
        "must have schema/schema_version/validation_status/envelope_kind/"
        "violations/input_sha256 bound to --source-path) when --phase review "
        "and --source-kind issue_review_result_compact_v1 (Issue #1507 AC24 / "
        "Issue #1755 AC2-AC4).",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=None,
        help="Active issue number (positive integer). Required when --phase "
        "review and --source-kind issue_review_result_compact_v1; binds "
        "normalized_payload.ARTIFACT's issue segment to this value "
        "(Issue #1755 AC5).",
    )
    parser.add_argument(
        "--output-path",
        required=True,
        help="Path to write the ISSUE_REFINEMENT_PHASE_STATE_V1 JSON.",
    )
    args = parser.parse_args(argv)
    if args.issue_number is not None and args.issue_number <= 0:
        parser.error("--issue-number must be a positive integer")
    return args


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
            issue_number=args.issue_number,
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

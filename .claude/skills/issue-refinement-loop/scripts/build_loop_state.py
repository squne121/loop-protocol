#!/usr/bin/env python3
"""
build_loop_state.py

Builder script that constructs a LOOP_STATE_V1 JSON from:
  - REFINEMENT_LOOP_PLAN_V1 (from plan_refinement_loop.py)
  - ISSUE_REVIEW_RESULT_COMPACT_V1 (from compact_review_result.py)

This script is read-only with respect to GitHub — it never calls gh commands.
It does not determine next_action (that is decide_next_loop_action.py's job).

Output: LOOP_STATE_BUILD_RESULT_V1 JSON to stdout

Usage:
    uv run python3 build_loop_state.py \\
      --planner-result-file <path> \\
      --review-result-file <path> \\
      --issue-number <int> \\
      --iteration <int> \\
      [--max-iterations <int>] \\
      [--blockers-history-file <path>] \\
      [--schema-file <path>] \\
      --out <path>

Exit codes:
    0 - success (status: ok)
    1 - validation failed (status: invalid)
    2 - input error / blocked (status: blocked or invalid with input error)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "loop_state/v1"
BUILD_RESULT_SCHEMA = "LOOP_STATE_BUILD_RESULT_V1"

# Default schema path relative to this script
_DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "loop_state.schema.json"
)

# Allowed verdicts in compact review result
VALID_VERDICTS = {"approve", "needs-fix"}

# Required top-level decision fields in REFINEMENT_LOOP_PLAN_V1
REQUIRED_DECISION_FIELDS = [
    "web_research_policy",
    "scope_signal_guard",
    "delivery_rollup",
    "follow_up_materialization",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Compute sha256 of a file's contents. Returns 'sha256:<hex>'."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return f"sha256:{h.hexdigest()}"


def _sha256_bytes(data: bytes) -> str:
    """Compute sha256 of bytes. Returns 'sha256:<hex>'."""
    h = hashlib.sha256()
    h.update(data)
    return f"sha256:{h.hexdigest()}"


def load_json_object(path: Path) -> tuple[Optional[dict[str, Any]], str]:
    """Load a JSON object from path.

    Returns (data, error_msg). error_msg is '' on success.
    Fails with an error if the JSON value is not a dict.
    """
    if not path.exists():
        return None, f"File not found: {path}"
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"JSON parse error in {path}: {e}"
    except OSError as e:
        return None, f"Cannot read {path}: {e}"
    if not isinstance(data, dict):
        return None, f"Expected JSON object in {path}, got {type(data).__name__}"
    return data, ""


def load_json_array(path: Path) -> tuple[Optional[list[Any]], str]:
    """Load a JSON array from path.

    Returns (data, error_msg). error_msg is '' on success.
    Fails with an error if the file does not exist or the JSON value is not a list.
    """
    if not path.exists():
        return None, f"File not found: {path}"
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"JSON parse error in {path}: {e}"
    except OSError as e:
        return None, f"Cannot read {path}: {e}"
    if not isinstance(data, list):
        return None, f"Expected JSON array in {path}, got {type(data).__name__}"
    return data, ""


# Keep load_json as an alias for load_json_object for backward compatibility
# with schema loading (schema files are always objects).
def load_json(path: Path) -> tuple[Optional[dict[str, Any]], str]:
    """Load JSON object from path (alias for load_json_object)."""
    return load_json_object(path)


def write_json_deterministic(path: Path, obj: Any) -> None:
    """Write JSON with sort_keys=True, ensure_ascii=False, indent=2."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_loop_state(
    state: Any, schema: dict[str, Any]
) -> list[dict[str, str]]:
    """
    Validate loop_state against schema using jsonschema.Validator.iter_errors().
    Collects ALL errors (not just first). Returns list of error dicts.
    Each error dict has: path, message, schema_path.

    fail-closed: if jsonschema unavailable, raises ImportError.
    If schema file unavailable, caller handles it.
    """
    try:
        import jsonschema
        import jsonschema.validators
    except ImportError as exc:
        raise ImportError(f"jsonschema not available: {exc}") from exc

    # Auto-select validator based on $schema in the schema document.
    # Falls back to Draft7Validator if $schema is missing or unrecognised.
    try:
        validator_cls = jsonschema.validators.validator_for(schema)
    except Exception:
        validator_cls = jsonschema.Draft7Validator
    validator = validator_cls(schema)

    errors = []
    for error in sorted(validator.iter_errors(state), key=lambda e: list(e.path)):
        path_str = "/".join(str(p) for p in error.path) if error.path else ""
        schema_path_str = "/".join(str(p) for p in error.absolute_schema_path)
        errors.append(
            {
                "path": path_str,
                "message": error.message,
                "schema_path": schema_path_str,
            }
        )
    return errors


# ---------------------------------------------------------------------------
# critical_external_claims projection
# ---------------------------------------------------------------------------


# ExternalClaim schema (schemas/refinement_loop_plan_v1.json #/definitions/ExternalClaim):
#   required: claim, affects, source_hint
#   additionalProperties: false
#   claim: string
#   affects: enum [Outcome, InScope, AC, VC, StopCondition]
#   source_hint: [string, null]
_EXTERNAL_CLAIM_REQUIRED_KEYS = frozenset({"claim", "affects", "source_hint"})
_EXTERNAL_CLAIM_ALLOWED_KEYS = frozenset({"claim", "affects", "source_hint"})
_EXTERNAL_CLAIM_AFFECTS_ENUM = frozenset(
    {"Outcome", "InScope", "AC", "VC", "StopCondition"}
)


def _project_critical_external_claims(
    claims_raw: Any,
) -> tuple[Optional[list[str]], list[str]]:
    """Project ExternalClaim[] (object[] per refinement_loop_plan_v1.json) into
    the string[] shape required by loop_state.schema.json's
    web_research_policy.critical_external_claims.

    Each element of claims_raw is validated against the full ExternalClaim
    schema (required keys: claim/affects/source_hint; additionalProperties:
    false; claim: string; affects: enum; source_hint: string|null). Only the
    'claim' text (stripped of surrounding whitespace) is kept in the
    projected output.

    fail-closed: any element that fails ExternalClaim validation (non-object
    item, missing required key, extra/unknown key, claim not a non-empty
    string, affects outside the enum, source_hint not string/null) produces a
    structured error under the `critical_external_claims[idx]` path instead
    of being silently stringified or dropped. If any errors are present, no
    projected output is returned (fail-closed on the whole list).

    Returns (projected_claims, errors). If errors is non-empty,
    projected_claims is None.
    """
    if not isinstance(claims_raw, list):
        return None, [
            "critical_external_claims_invalid_type: expected a list, got "
            f"{type(claims_raw).__name__}"
        ]

    projected: list[str] = []
    errors: list[str] = []

    for idx, item in enumerate(claims_raw):
        item_path = f"critical_external_claims[{idx}]"

        if not isinstance(item, dict):
            errors.append(
                f"{item_path}_not_object: expected an object, "
                f"got {type(item).__name__}"
            )
            continue

        item_keys = set(item.keys())

        missing_keys = _EXTERNAL_CLAIM_REQUIRED_KEYS - item_keys
        if missing_keys:
            errors.append(
                f"{item_path}_missing_required_keys: missing "
                f"{sorted(missing_keys)}"
            )

        extra_keys = item_keys - _EXTERNAL_CLAIM_ALLOWED_KEYS
        if extra_keys:
            errors.append(
                f"{item_path}_unknown_keys: additionalProperties is false, "
                f"got unexpected keys {sorted(extra_keys)}"
            )

        if missing_keys or extra_keys:
            # Structural shape is already wrong; skip further field-level
            # checks for this item but keep validating other items.
            continue

        claim = item.get("claim")
        if not isinstance(claim, str):
            errors.append(
                f"{item_path}_claim_not_string: 'claim' must "
                f"be a string, got {type(claim).__name__}"
            )
        elif claim.strip() == "":
            errors.append(
                f"{item_path}_claim_empty: 'claim' must be "
                "a non-empty string"
            )

        affects = item.get("affects")
        if affects not in _EXTERNAL_CLAIM_AFFECTS_ENUM:
            errors.append(
                f"{item_path}_affects_invalid: 'affects' must be one of "
                f"{sorted(_EXTERNAL_CLAIM_AFFECTS_ENUM)}, got {affects!r}"
            )

        source_hint = item.get("source_hint")
        if source_hint is not None and not isinstance(source_hint, str):
            errors.append(
                f"{item_path}_source_hint_invalid: 'source_hint' must be a "
                f"string or null, got {type(source_hint).__name__}"
            )

        if isinstance(claim, str) and claim.strip() != "":
            projected.append(claim.strip())

    if errors:
        return None, errors

    return projected, []


# ---------------------------------------------------------------------------
# Builder core
# ---------------------------------------------------------------------------


def build_loop_state(
    plan: dict[str, Any],
    review: dict[str, Any],
    issue_number: int,
    iteration: int,
    max_iterations: int = 3,
    blockers_history: Optional[list[Any]] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """
    Build LOOP_STATE_V1 from planner and review results.

    Returns (loop_state_dict, blocked_reasons).
    If blocked_reasons is non-empty, loop_state_dict is None.

    Constraints:
    - Does NOT compute next_action (AC7)
    - Does NOT call gh commands (AC12)
    - Validates issue_number and iteration consistency
    """
    blocked: list[str] = []

    # --- Iteration guard ---
    if iteration < 0:
        blocked.append(f"iteration_regression: iteration={iteration} is negative")

    # --- Issue number consistency check ---
    plan_source = plan.get("source", {})
    plan_issue = plan_source.get("issue_number")
    review_issue = review.get("issue_number")  # may not always be present

    if plan_issue is not None:
        try:
            if int(plan_issue) != issue_number:
                blocked.append(
                    f"issue_number_mismatch: planner has issue_number={plan_issue}, "
                    f"CLI --issue-number={issue_number}"
                )
        except (ValueError, TypeError):
            blocked.append(
                f"issue_number_invalid: planner issue_number={plan_issue!r} "
                f"cannot be converted to int"
            )

    if review_issue is not None:
        try:
            if int(review_issue) != issue_number:
                blocked.append(
                    f"issue_number_mismatch: review result has issue_number={review_issue}, "
                    f"CLI --issue-number={issue_number}"
                )
        except (ValueError, TypeError):
            blocked.append(
                f"issue_number_invalid: review issue_number={review_issue!r} "
                f"cannot be converted to int"
            )

    if blocked:
        return None, blocked

    # --- Extract from planner ---
    decisions = plan.get("decisions")
    if decisions is None:
        blocked.append(
            "missing_required_field: planner artifact is missing 'decisions' field"
        )
        return None, blocked

    # Validate required decision sub-fields (fail-closed)
    for field in REQUIRED_DECISION_FIELDS:
        if field not in decisions:
            blocked.append(
                f"missing_required_field: planner decisions missing required field '{field}'"
            )

    if blocked:
        return None, blocked

    web_research_policy_raw = decisions.get("web_research_policy", {})
    if not isinstance(web_research_policy_raw, dict):
        blocked.append(
            "web_research_policy_invalid_type: expected an object, got "
            f"{type(web_research_policy_raw).__name__}"
        )
        return None, blocked

    if "critical_external_claims" not in web_research_policy_raw:
        blocked.append(
            "missing_required_field: web_research_policy is missing required "
            "field 'critical_external_claims'"
        )
        return None, blocked

    critical_external_claims_raw = web_research_policy_raw["critical_external_claims"]
    projected_claims, projection_errors = _project_critical_external_claims(
        critical_external_claims_raw
    )
    if projection_errors:
        blocked.extend(projection_errors)
        return None, blocked

    web_research_policy = {
        "required": bool(web_research_policy_raw.get("required", False)),
        "reason": web_research_policy_raw.get("reason_code"),
        "critical_external_claims": projected_claims,
        "skip_reason": None,
    }
    if not web_research_policy_raw.get("required", False):
        web_research_policy["skip_reason"] = web_research_policy_raw.get(
            "reason_code"
        )
        web_research_policy["reason"] = None

    scope_signal_raw = decisions.get("scope_signal_guard", {})
    scope_signal_guard = {
        "triggered": bool(scope_signal_raw.get("triggered", False)),
        "excluded_by_anchor_reframe": bool(
            scope_signal_raw.get("excluded_by_anchor_reframe", False)
        ),
        "reason_code": scope_signal_raw.get("reason_code"),
    }

    delivery_rollup_raw = decisions.get("delivery_rollup", {})
    delivery_rollup = {
        "applicable": bool(delivery_rollup_raw.get("applicable", False)),
        "unmaterialized_slots": list(
            delivery_rollup_raw.get("unmaterialized_slots", [])
        ),
    }

    follow_up_raw = decisions.get("follow_up_materialization", {})
    follow_up_materialization = {
        "candidates": list(follow_up_raw.get("candidates", []))
    }

    # --- Extract from review (fail-closed on missing VERDICT) ---
    verdict = review.get("VERDICT") or review.get("verdict")
    if verdict is None:
        blocked.append(
            "missing_required_field: review artifact is missing 'VERDICT' (or 'verdict') field"
        )
        return None, blocked

    if verdict not in VALID_VERDICTS:
        blocked.append(f"unknown_verdict: verdict={verdict!r} is not in {sorted(VALID_VERDICTS)}")
        return None, blocked

    # --- Assemble LOOP_STATE_V1 ---
    loop_state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "issue_number": issue_number,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "last_verdict": verdict,
        "termination_reason": None,
        "scope_signal_guard": scope_signal_guard,
        "web_research_policy": web_research_policy,
        "delivery_rollup": delivery_rollup,
        "follow_up_materialization": follow_up_materialization,
        "blockers_history": blockers_history if blockers_history is not None else [],
    }

    return loop_state, []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build LOOP_STATE_V1 from planner and review results.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--planner-result-file",
        metavar="PATH",
        required=True,
        help="Path to REFINEMENT_LOOP_PLAN_V1 JSON file.",
    )
    parser.add_argument(
        "--review-result-file",
        metavar="PATH",
        required=True,
        help="Path to ISSUE_REVIEW_RESULT_COMPACT_V1 JSON file.",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        metavar="INT",
        required=True,
        help="Issue number (integer).",
    )
    parser.add_argument(
        "--iteration",
        type=int,
        metavar="INT",
        required=True,
        help="0-indexed iteration counter.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        metavar="INT",
        default=3,
        help="Maximum iterations (default: 3).",
    )
    parser.add_argument(
        "--blockers-history-file",
        metavar="PATH",
        default=None,
        help="Path to previous blockers_history JSON array file (optional).",
    )
    parser.add_argument(
        "--schema-file",
        metavar="PATH",
        default=None,
        help="Path to loop_state.schema.json (default: schemas/loop_state.schema.json).",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        required=True,
        help="Output path for LOOP_STATE_V1 JSON.",
    )
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    schema_path = (
        Path(args.schema_file) if args.schema_file else _DEFAULT_SCHEMA_PATH
    )
    planner_path = Path(args.planner_result_file)
    review_path = Path(args.review_result_file)
    out_path = Path(args.out)

    # --- Load inputs ---
    plan, plan_err = load_json_object(planner_path)
    if plan_err:
        result = _make_build_result(
            status="invalid",
            loop_state_path=str(out_path),
            errors=[{"path": "", "message": plan_err, "schema_path": ""}],
            warnings=[],
            provenance=_make_provenance(
                planner_path=planner_path,
                review_path=review_path,
                issue_number=args.issue_number,
                iteration=args.iteration,
                schema_path=schema_path,
                planner_hash=None,
                review_hash=None,
                schema_hash=None,
            ),
        )
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
        return 2

    review, review_err = load_json_object(review_path)
    if review_err:
        result = _make_build_result(
            status="invalid",
            loop_state_path=str(out_path),
            errors=[{"path": "", "message": review_err, "schema_path": ""}],
            warnings=[],
            provenance=_make_provenance(
                planner_path=planner_path,
                review_path=review_path,
                issue_number=args.issue_number,
                iteration=args.iteration,
                schema_path=schema_path,
                planner_hash=_sha256_file(planner_path) if planner_path.exists() else None,
                review_hash=None,
                schema_hash=None,
            ),
        )
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
        return 2

    # Load optional blockers history — must be a JSON array (fail-closed)
    blockers_history: list[Any] = []
    warnings: list[str] = []
    if args.blockers_history_file:
        bh_path = Path(args.blockers_history_file)
        bh_data, bh_err = load_json_array(bh_path)
        if bh_err:
            # Propagate error as a warning so callers notice the malformed file.
            warnings.append(f"blockers_history_file error: {bh_err}")
        else:
            assert bh_data is not None
            blockers_history = bh_data

    # --- Load schema ---
    schema_hash: Optional[str] = None
    schema: Optional[dict[str, Any]] = None
    if schema_path.exists():
        schema_hash = _sha256_file(schema_path)
        schema_data, schema_err = load_json_object(schema_path)
        if not schema_err:
            schema = schema_data

    # Compute provenance hashes
    planner_hash = _sha256_file(planner_path)
    review_hash = _sha256_file(review_path)

    provenance = _make_provenance(
        planner_path=planner_path,
        review_path=review_path,
        issue_number=args.issue_number,
        iteration=args.iteration,
        schema_path=schema_path,
        planner_hash=planner_hash,
        review_hash=review_hash,
        schema_hash=schema_hash,
    )

    # --- Build LOOP_STATE ---
    loop_state, blocked_reasons = build_loop_state(
        plan=plan,
        review=review,
        issue_number=args.issue_number,
        iteration=args.iteration,
        max_iterations=args.max_iterations,
        blockers_history=blockers_history,
    )

    if blocked_reasons:
        errors = [
            {"path": "", "message": r, "schema_path": ""}
            for r in blocked_reasons
        ]
        result = _make_build_result(
            status="invalid",
            loop_state_path=str(out_path),
            errors=errors,
            warnings=warnings,
            provenance=provenance,
        )
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
        return 2

    assert loop_state is not None

    # --- Validate against schema ---
    validation_errors: list[dict[str, str]] = []
    if schema is not None:
        try:
            validation_errors = validate_loop_state(loop_state, schema)
        except ImportError as exc:
            errors = [{"path": "", "message": str(exc), "schema_path": ""}]
            result = _make_build_result(
                status="invalid",
                loop_state_path=str(out_path),
                errors=errors,
                warnings=warnings,
                provenance=provenance,
            )
            print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
            return 1
    else:
        validation_errors = [
            {
                "path": "",
                "message": f"Schema file unavailable: {schema_path}",
                "schema_path": "",
            }
        ]

    if validation_errors:
        result = _make_build_result(
            status="invalid",
            loop_state_path=str(out_path),
            errors=validation_errors,
            warnings=warnings,
            provenance=provenance,
        )
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
        return 1

    # --- Write output ---
    write_json_deterministic(out_path, loop_state)
    loop_state_hash = _sha256_file(out_path)

    result = _make_build_result(
        status="ok",
        loop_state_path=str(out_path),
        loop_state_sha256=loop_state_hash,
        errors=[],
        warnings=warnings,
        provenance=provenance,
    )
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
    return 0


def _make_provenance(
    planner_path: Path,
    review_path: Path,
    issue_number: int,
    iteration: int,
    schema_path: Path,
    planner_hash: Optional[str],
    review_hash: Optional[str],
    schema_hash: Optional[str],
) -> dict[str, Any]:
    return {
        "issue_number": issue_number,
        "iteration": iteration,
        "planner_result_path": str(planner_path),
        "planner_result_hash": planner_hash,
        "review_result_path": str(review_path),
        "review_result_hash": review_hash,
        "schema_path": str(schema_path),
        "schema_hash": schema_hash,
    }


def _make_build_result(
    status: str,
    loop_state_path: str,
    errors: list[dict[str, str]],
    warnings: list[str],
    provenance: dict[str, Any],
    loop_state_sha256: Optional[str] = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": BUILD_RESULT_SCHEMA,
        "status": status,
        "loop_state_path": loop_state_path,
        "loop_state_sha256": loop_state_sha256,
        "errors": errors,
        "warnings": warnings,
        "provenance": provenance,
    }
    return result


if __name__ == "__main__":
    sys.exit(main())

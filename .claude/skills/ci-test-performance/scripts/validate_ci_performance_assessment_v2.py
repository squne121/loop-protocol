#!/usr/bin/env python3
"""
validate_ci_performance_assessment_v2.py

Fixture-driven semantic validator for CI_TEST_PERFORMANCE_ASSESSMENT_V2.

Separates:
- structural_valid: parses as strict JSON and validates against
  schemas/ci_test_performance_assessment_v2.schema.json (Draft 2020-12).
- semantic_valid: cross-field claim/evidence/observation/claim_evaluation
  state-machine consistency (beyond what JSON Schema alone can express).
- approval_eligible: reviewer-gate eligibility. A semantically valid
  assessment can still be approval_eligible=false (e.g. insufficient
  samples, unusable functional evidence) -- these are two separate axes.

Does NOT read or modify CI_TEST_PERFORMANCE_DECISION_V1 / ci_runtime_delta_v1
(references/decision-matrix.md, templates/runtime-delta.md). Those V1
contracts are unaffected by this validator.

Exit codes:
  0 = valid (structural_valid and semantic_valid; approval_eligible is
      reported but does not change the exit code)
  2 = structural or semantic invalid
  3 = operational failure (file not found, strict-JSON parse failure:
      duplicate key / NaN / Infinity / syntax error)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

SCHEMA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "schemas"
)
ASSESSMENT_SCHEMA_PATH = os.path.normpath(
    os.path.join(SCHEMA_DIR, "ci_test_performance_assessment_v2.schema.json")
)

EXIT_VALID = 0
EXIT_INVALID = 2
EXIT_OPERATIONAL_FAILURE = 3

COHORT_COMPARABILITY_FIELDS = [
    "runner_image",
    "workers",
    "scheduler",
    "command_manifest_digest",
    "test_selection_digest",
]

MIN_DECISION_SAMPLE_RUN_COUNT = 20


class StrictJSONError(ValueError):
    """Raised for duplicate keys / NaN / Infinity / syntax errors."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise StrictJSONError(f"duplicate_json_key: {key!r}")
        seen[key] = value
    return seen


def _reject_constant(constant: str) -> float:
    # json module calls this for NaN / Infinity / -Infinity tokens.
    raise StrictJSONError(f"non_finite_json_constant: {constant}")


def strict_json_loads(raw_text: str) -> dict[str, Any]:
    try:
        return json.loads(
            raw_text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except StrictJSONError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictJSONError(f"json_syntax_error: {exc}") from exc


# --------------------------------------------------------------------------- #
# Semantic rule engine
# --------------------------------------------------------------------------- #
def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _usable_checks(selected_checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usable = []
    for check in selected_checks:
        if (
            _is_positive_int(check.get("check_run_id"))
            and check.get("status") == "completed"
            and check.get("conclusion") == "success"
            and check.get("head_sha_match") is True
            and check.get("classification") in ("required", "evidence")
            and check.get("provenance") != "needs_result_synthetic"
        ):
            usable.append(check)
    return usable


def _append_unique(items: list[str], code: str) -> None:
    if code not in items:
        items.append(code)


def _check_claim_state_machine(
    assessment: dict[str, Any], errors: list[str], warnings: list[str]
) -> None:
    claim = assessment["claim"]
    kind = claim["kind"]
    status = assessment["performance_evidence"]["status"]
    outcome = assessment["observation"]["outcome"]
    claim_outcome = assessment["claim_evaluation"]["outcome"]

    if kind == "none":
        if claim_outcome != "not_applicable":
            _append_unique(errors, "no_claim_claim_evaluation_must_be_not_applicable")
        if outcome in (
            "improved",
            "budget_met",
            "budget_exceeded",
            "equivalent_within_threshold",
        ):
            _append_unique(errors, "no_claim_but_observation_implies_active_claim")
        if outcome == "regressed":
            warnings.append("no_claim_observed_regression_review_recommended")
    else:
        if claim_outcome == "not_applicable":
            _append_unique(
                errors, "claim_present_claim_evaluation_cannot_be_not_applicable"
            )
        if status != "complete":
            if claim_outcome in ("satisfied", "not_satisfied"):
                _append_unique(
                    errors, "claim_evaluation_conclusive_without_complete_evidence"
                )

    if status == "insufficient_samples" and outcome == "improved":
        _append_unique(errors, "insufficient_samples_observation_improved")

    if kind == "improvement" and status == "insufficient_samples":
        _append_unique(errors, "improvement_claim_insufficient_samples")

    if kind == "non_regression" and status == "incomparable_cohort":
        _append_unique(errors, "non_regression_claim_incomparable_cohort")


def _check_docs_only_runtime_delta(
    assessment: dict[str, Any], warnings: list[str]
) -> None:
    if (
        assessment.get("decision_scope") == "docs_only"
        and assessment["performance_evidence"].get("runtime_delta") is not None
    ):
        warnings.append("docs_only_change_with_runtime_delta_reported")


def _check_functional_evidence(
    functional_evidence: dict[str, Any], blockers: list[str]
) -> None:
    ref = functional_evidence.get("ci_verdict_summary_ref")
    if ref is None:
        _append_unique(blockers, "functional_evidence_missing_ci_verdict_summary_ref")
        return

    selected_checks = ref.get("selected_checks", [])
    if len(selected_checks) == 0:
        _append_unique(blockers, "functional_evidence_zero_selected_checks")
        return

    for check in selected_checks:
        if check.get("provenance") == "needs_result_synthetic":
            _append_unique(blockers, "functional_evidence_synthetic_needs_result")
        if not _is_positive_int(check.get("check_run_id")):
            _append_unique(
                blockers, "functional_evidence_missing_or_invalid_check_run_id"
            )
        if check.get("head_sha_match") is not True:
            _append_unique(blockers, "functional_evidence_stale_or_null_head_sha")
        if check.get("conclusion") in ("skipped", "neutral"):
            _append_unique(
                blockers, "functional_evidence_skipped_or_neutral_conclusion"
            )

    if not _usable_checks(selected_checks):
        _append_unique(blockers, "functional_evidence_no_usable_checks")


def _check_cohort_comparability(
    performance_evidence: dict[str, Any], errors: list[str], blockers: list[str]
) -> None:
    status = performance_evidence["status"]
    runtime_delta = performance_evidence.get("runtime_delta")

    if status == "insufficient_samples":
        _append_unique(blockers, "insufficient_samples_gate_blocked")

    if runtime_delta is None:
        return

    if runtime_delta.get("mode") != "comparative":
        return

    before = runtime_delta.get("before", {})
    after = runtime_delta.get("after", {})
    mismatched = [
        field
        for field in COHORT_COMPARABILITY_FIELDS
        if before.get(field) != after.get(field)
    ]
    if mismatched and status != "incomparable_cohort":
        _append_unique(
            errors,
            "cohort_mismatch_but_status_not_incomparable_cohort: "
            + ",".join(mismatched),
        )

    run_count = min(
        before.get("run_count", 0),
        after.get("run_count", 0),
    )
    if run_count < MIN_DECISION_SAMPLE_RUN_COUNT and status == "complete":
        _append_unique(errors, "insufficient_sample_run_count_but_status_complete")


def _check_approval_gates(
    assessment: dict[str, Any], blockers: list[str]
) -> None:
    claim = assessment["claim"]
    status = assessment["performance_evidence"]["status"]
    outcome = assessment["observation"]["outcome"]

    if claim["kind"] != "none" and status not in ("complete", "not_required"):
        _append_unique(blockers, "claim_requires_complete_evidence_for_approval")

    if claim["kind"] == "absolute_budget" and outcome == "budget_exceeded":
        _append_unique(blockers, "budget_exceeded_blocks_approval")


def run_semantic_checks(assessment: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []

    _check_claim_state_machine(assessment, errors, warnings)
    _check_docs_only_runtime_delta(assessment, warnings)
    _check_functional_evidence(assessment["functional_evidence"], blockers)
    _check_cohort_comparability(assessment["performance_evidence"], errors, blockers)
    _check_approval_gates(assessment, blockers)

    semantic_valid = len(errors) == 0
    approval_eligible = semantic_valid and len(blockers) == 0

    return {
        "structural_valid": True,
        "semantic_valid": semantic_valid,
        "approval_eligible": approval_eligible,
        "errors": errors,
        "blockers": blockers,
        "warnings": warnings,
    }


def structural_invalid_decision(errors: list[str]) -> dict[str, Any]:
    return {
        "structural_valid": False,
        "semantic_valid": False,
        "approval_eligible": False,
        "errors": errors,
        "blockers": [],
        "warnings": [],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_assessment_schema() -> dict[str, Any]:
    with open(ASSESSMENT_SCHEMA_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def validate_assessment(assessment_path: str) -> tuple[int, dict[str, Any]]:
    try:
        with open(assessment_path, encoding="utf-8") as handle:
            raw_text = handle.read()
    except OSError as exc:
        return EXIT_OPERATIONAL_FAILURE, {
            "operational_error": f"file_not_readable: {exc}"
        }

    try:
        assessment = strict_json_loads(raw_text)
    except StrictJSONError as exc:
        return EXIT_OPERATIONAL_FAILURE, {"operational_error": str(exc)}

    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        return EXIT_OPERATIONAL_FAILURE, {
            "operational_error": f"jsonschema_not_installed: {exc}"
        }

    schema = load_assessment_schema()
    validator = Draft202012Validator(schema)
    schema_errors = sorted(validator.iter_errors(assessment), key=lambda e: e.path)
    if schema_errors:
        error_messages = [
            f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
            for err in schema_errors
        ]
        return EXIT_INVALID, structural_invalid_decision(error_messages)

    decision = run_semantic_checks(assessment)
    exit_code = EXIT_VALID if decision["semantic_valid"] else EXIT_INVALID
    return exit_code, decision


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a CI_TEST_PERFORMANCE_ASSESSMENT_V2 JSON document"
    )
    parser.add_argument(
        "--assessment", required=True, help="Path to the assessment JSON file"
    )
    parser.add_argument(
        "--output", required=True, help="Path to write the decision result JSON"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    exit_code, decision = validate_assessment(args.assessment)

    result: dict[str, Any] = {
        "schema": "CI_TEST_PERFORMANCE_ASSESSMENT_V2_VALIDATION_RESULT",
        "schema_version": 1,
        "assessment_path": args.assessment,
        "exit_code": exit_code,
    }
    result.update(decision)

    os.makedirs(
        os.path.dirname(args.output) if os.path.dirname(args.output) else ".",
        exist_ok=True,
    )
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

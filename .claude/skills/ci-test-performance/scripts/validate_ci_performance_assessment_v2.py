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

`decision` is NOT part of the CI_TEST_PERFORMANCE_ASSESSMENT_V2 *input*
schema. It is an output-only contract produced by this script (see
CI_TEST_PERFORMANCE_ASSESSMENT_V2_VALIDATION_RESULT below). Producers can
never embed a fabricated verdict into the assessment they author.

functional_evidence.ci_verdict_summary_ref.selected_checks is a
self-report and is NEVER sufficient on its own for approval_eligible.
approval_eligible additionally requires the trusted canonical
ci_verdict_summary_v2 artifact to be supplied out-of-band via
--ci-verdict-summary (plus --expected-head-sha, and optionally
--expected-artifact-digest), and the artifact's own (not self-reported)
required-check set must independently satisfy the merge-ready floor.

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
import hashlib
import json
import math
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
DELTA_TOLERANCE = 1e-6
PCT_TOLERANCE = 1e-6

# Outcomes that only make sense when an active (non-"none") claim/threshold
# is being evaluated. "improved" is deliberately excluded: an unclaimed
# incidental improvement is a legitimate observation, not a contradiction.
_ACTIVE_CLAIM_ONLY_OUTCOMES = (
    "budget_met",
    "budget_exceeded",
    "equivalent_within_threshold",
)

_NON_COMPLETE_ALLOWED_OUTCOMES = ("not_observed", "inconclusive")


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
        if outcome in _ACTIVE_CLAIM_ONLY_OUTCOMES:
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
        if status == "not_required":
            _append_unique(errors, "active_claim_status_not_required_forbidden")
        if (
            kind in ("improvement", "non_regression")
            and outcome == "regressed"
            and claim_outcome == "satisfied"
        ):
            _append_unique(errors, "claim_evaluation_contradicts_regressed_observation")

    if status == "insufficient_samples" and outcome == "improved":
        _append_unique(errors, "insufficient_samples_observation_improved")

    # performance_evidence.status != complete: no conclusive measured
    # outcome may be reported (only not_observed / inconclusive are valid).
    if status != "complete" and outcome not in _NON_COMPLETE_ALLOWED_OUTCOMES:
        _append_unique(errors, "observation_requires_complete_evidence")


def _check_docs_only_runtime_delta(
    assessment: dict[str, Any], warnings: list[str]
) -> None:
    if (
        assessment.get("decision_scope") == "docs_only"
        and assessment["performance_evidence"].get("runtime_delta") is not None
    ):
        warnings.append("docs_only_change_with_runtime_delta_reported")


def _check_functional_evidence_self_report(
    functional_evidence: dict[str, Any], blockers: list[str]
) -> None:
    """Structural self-report floor. Never sufficient alone for approval --
    see _check_trusted_functional_artifact for the mandatory cross-check."""
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


def _sha256_of_file(path: str) -> str:
    with open(path, "rb") as handle:
        return "sha256:" + hashlib.sha256(handle.read()).hexdigest()


def _check_trusted_functional_artifact(
    assessment: dict[str, Any],
    blockers: list[str],
    ci_verdict_summary_path: str | None,
    expected_head_sha: str | None,
    expected_artifact_digest: str | None,
) -> None:
    """Cross-checks functional_evidence against the canonical
    ci_verdict_summary_v2 artifact, supplied out-of-band. Self-reported
    check details inside the assessment (ci_verdict_summary_ref.selected_checks)
    are advisory only and are never trusted for approval_eligible."""
    ref = assessment.get("functional_evidence", {}).get("ci_verdict_summary_ref")
    if ref is None:
        # already caught by _check_functional_evidence_self_report
        return

    if ci_verdict_summary_path is None:
        _append_unique(blockers, "functional_evidence_missing_trusted_artifact_input")
        return

    try:
        with open(ci_verdict_summary_path, "rb") as handle:
            raw_bytes = handle.read()
    except OSError:
        _append_unique(blockers, "functional_evidence_trusted_artifact_not_readable")
        return

    if expected_artifact_digest is not None:
        actual_digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        if actual_digest != expected_artifact_digest:
            _append_unique(blockers, "functional_evidence_artifact_digest_mismatch")
            return

    try:
        artifact = strict_json_loads(raw_bytes.decode("utf-8"))
    except StrictJSONError:
        _append_unique(blockers, "functional_evidence_trusted_artifact_not_parseable")
        return

    if artifact.get("schema") != "ci_verdict_summary_v2":
        _append_unique(blockers, "functional_evidence_artifact_schema_mismatch")
        return

    if expected_head_sha is None:
        _append_unique(blockers, "functional_evidence_missing_expected_head_sha_input")
        return

    if artifact.get("expected_head_sha") != expected_head_sha:
        _append_unique(blockers, "functional_evidence_artifact_head_sha_mismatch")
        return

    if ref.get("expected_head_sha") != expected_head_sha:
        _append_unique(
            blockers, "functional_evidence_self_reported_head_sha_mismatch"
        )
        return

    if artifact.get("overall_status") != "merge_ready":
        _append_unique(blockers, "functional_evidence_artifact_not_merge_ready")
        return

    checks = artifact.get("checks", [])
    required_checks = [c for c in checks if c.get("classification") == "required"]
    if not required_checks:
        _append_unique(blockers, "functional_evidence_artifact_no_required_checks")
        return

    for check in required_checks:
        if (
            check.get("status") != "completed"
            or check.get("conclusion") != "success"
            or not _is_positive_int(check.get("check_run_id"))
            or check.get("head_sha_match") is not True
            or check.get("provenance") == "needs_result_synthetic"
        ):
            _append_unique(blockers, "functional_evidence_required_check_incomplete")
            return

    # Self-reported selected_checks (if any carry a check_run_id) must match
    # the trusted artifact's entry for that same check_run_id -- a self
    # report that disagrees with the canonical artifact is untrustworthy.
    by_id = {c.get("check_run_id"): c for c in checks if c.get("check_run_id")}
    for self_reported in ref.get("selected_checks", []):
        run_id = self_reported.get("check_run_id")
        if run_id is None:
            continue
        artifact_check = by_id.get(run_id)
        if artifact_check is None:
            _append_unique(
                blockers, "functional_evidence_self_report_unknown_check_run_id"
            )
            continue
        for field in ("status", "conclusion", "head_sha_match", "classification"):
            if self_reported.get(field) != artifact_check.get(field):
                _append_unique(
                    blockers,
                    "functional_evidence_self_report_mismatch_trusted_artifact",
                )
                break


def _recompute_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float]:
    p50_delta = before["p50_seconds"] - after["p50_seconds"]
    p95_delta = before["p95_seconds"] - after["p95_seconds"]
    p50_pct = (
        (p50_delta / before["p50_seconds"]) * 100.0
        if before["p50_seconds"]
        else 0.0
    )
    p95_pct = (
        (p95_delta / before["p95_seconds"]) * 100.0
        if before["p95_seconds"]
        else 0.0
    )
    return {
        "p50_delta_seconds": p50_delta,
        "p95_delta_seconds": p95_delta,
        "p50_improvement_pct": p50_pct,
        "p95_improvement_pct": p95_pct,
    }


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    """Issue #2159 AC8: `nearest_rank_v1` percentile method -- the smallest
    value such that at least `percentile`% of the (sorted) sample is <=
    that value. 1-indexed nearest-rank method. Kept as the same method
    name/semantics as `tests/ci/test_ci_performance_gate.py`'s
    `_nearest_rank_percentile` (duplicated rather than imported across the
    Allowed Paths boundary between the two Issue #2159 contracts)."""
    if not values:
        raise ValueError("nearest_rank_v1 requires at least one value")
    ordered = sorted(values)
    n = len(ordered)
    rank = max(1, min(n, math.ceil((percentile / 100.0) * n)))
    return ordered[rank - 1]


# #2159 P0-7 (fix_delta after adversarial review issuecomment-5295659213):
# raw-sample percentile recomputation was previously OPTIONAL whenever
# `run_details` was absent, so a producer could self-report p50/p95 with no
# raw samples at all and sail through unchecked. `run_details` is now
# MANDATORY whenever `performance_evidence.status == "complete"` AND
# `claim.kind != "none"` (i.e. whenever a real, non-smoke-test performance
# claim is being asserted). Backward compatibility is preserved ONLY for
# `claim.kind == "none"` artifacts (the no-op smoke-test producer, #2159
# P0-8).
MIN_RAW_SAMPLE_COUNT = 20


def _check_percentile_recomputed_from_raw_samples(
    assessment: dict[str, Any], errors: list[str], blockers: list[str]
) -> None:
    """Issue #2159 AC8 (P0-7/P0-9): recompute P50/P95 from
    `runtime_delta.<cohort>.run_details[].duration_seconds` raw samples and
    cross-check against the cohort's self-reported `p50_seconds` /
    `p95_seconds`. Also enforces the P0-7 raw-sample invariants:

    - `run_details` is REQUIRED (not optional) whenever
      `performance_evidence.status == "complete"` AND `claim.kind != "none"`
      -- a real claim.kind (improvement/regression/absolute_budget/etc.)
      backed by `status: complete` must always be accompanied by the raw
      samples that back it, closing the P0-7 self-report bypass where a
      producer omits `run_details` to dodge recomputation.
    - `run_count == len(run_ids) == len(run_details)`.
    - every `run_details[].workflow_run_id` is present in `run_ids`
      (exact set match, no orphaned or missing raw-sample records).
    - `duration_seconds` is a finite positive number (rejects NaN/Infinity/
      non-numeric/negative/zero).
    - no duplicate `workflow_run_id` within a single cohort's `run_details`.
    - `run_details` must contain >= MIN_RAW_SAMPLE_COUNT valid samples once
      the above invariants hold.
    - the declared `p50_seconds`/`p95_seconds` must always equal the
      recomputed value (recomputation is no longer skipped just because a
      cohort happens not to declare one)."""
    claim = assessment.get("claim", {})
    claim_kind = claim.get("kind")
    performance_evidence = assessment["performance_evidence"]
    status = performance_evidence.get("status")
    runtime_delta = performance_evidence.get("runtime_delta")

    run_details_required = status == "complete" and claim_kind != "none"

    if not runtime_delta:
        if run_details_required:
            _append_unique(blockers, "run_details_required_but_runtime_delta_missing")
        return

    for cohort_key in ("before", "after"):
        cohort = runtime_delta.get(cohort_key)
        if not cohort:
            continue

        run_details = cohort.get("run_details")

        if not run_details:
            if run_details_required:
                _append_unique(
                    blockers,
                    f"run_details_required_for_complete_non_none_claim_but_missing: {cohort_key}",
                )
            continue

        run_ids = cohort.get("run_ids", [])
        run_count = cohort.get("run_count")

        if run_count is not None and run_count != len(run_details):
            _append_unique(errors, f"run_count_mismatches_run_details_length: {cohort_key}")

        detail_run_ids = [
            rd.get("workflow_run_id") for rd in run_details if isinstance(rd, dict)
        ]
        if len(detail_run_ids) != len(set(detail_run_ids)):
            _append_unique(errors, f"run_details_duplicate_workflow_run_id: {cohort_key}")

        if set(detail_run_ids) != set(run_ids):
            _append_unique(errors, f"run_details_workflow_run_id_mismatches_run_ids: {cohort_key}")

        durations: list[float] = []
        for rd in run_details:
            if not isinstance(rd, dict):
                _append_unique(errors, f"run_details_entry_not_object: {cohort_key}")
                continue
            duration = rd.get("duration_seconds")
            if (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not math.isfinite(duration)
                or duration <= 0
            ):
                _append_unique(errors, f"run_details_invalid_duration_seconds: {cohort_key}")
                continue
            durations.append(float(duration))

        if run_details_required and len(durations) < MIN_RAW_SAMPLE_COUNT:
            _append_unique(
                blockers,
                f"run_details_below_min_raw_sample_count: {cohort_key} "
                f"({len(durations)} < {MIN_RAW_SAMPLE_COUNT})",
            )

        if not durations:
            continue

        recomputed_p50 = _nearest_rank_percentile(durations, 50)
        recomputed_p95 = _nearest_rank_percentile(durations, 95)

        declared_p50 = cohort.get("p50_seconds")
        declared_p95 = cohort.get("p95_seconds")

        if run_details_required and declared_p50 is None:
            _append_unique(errors, f"declared_p50_missing_with_run_details_present: {cohort_key}")
        if run_details_required and declared_p95 is None:
            _append_unique(errors, f"declared_p95_missing_with_run_details_present: {cohort_key}")

        if declared_p50 is not None and not math.isclose(
            declared_p50, recomputed_p50, rel_tol=1e-6, abs_tol=DELTA_TOLERANCE
        ):
            _append_unique(errors, f"percentile_recomputation_mismatch_p50: {cohort_key}")
        if declared_p95 is not None and not math.isclose(
            declared_p95, recomputed_p95, rel_tol=1e-6, abs_tol=DELTA_TOLERANCE
        ):
            _append_unique(errors, f"percentile_recomputation_mismatch_p95: {cohort_key}")

    before_ids = {
        rd.get("workflow_run_id")
        for rd in (runtime_delta.get("before", {}) or {}).get("run_details", []) or []
        if isinstance(rd, dict)
    }
    after_ids = {
        rd.get("workflow_run_id")
        for rd in (runtime_delta.get("after", {}) or {}).get("run_details", []) or []
        if isinstance(rd, dict)
    }
    if before_ids & after_ids:
        _append_unique(errors, "run_details_workflow_run_id_overlap_before_after")


def _check_cohort_comparability(
    performance_evidence: dict[str, Any],
    errors: list[str],
    blockers: list[str],
) -> None:
    status = performance_evidence["status"]
    runtime_delta = performance_evidence.get("runtime_delta")

    if status == "insufficient_samples":
        _append_unique(blockers, "insufficient_samples_gate_blocked")

    if status == "incomparable_cohort":
        _append_unique(blockers, "incomparable_cohort_gate_blocked")

    if status == "complete" and runtime_delta is None:
        _append_unique(blockers, "complete_status_missing_runtime_delta_evidence")

    if runtime_delta is None:
        return

    for cohort_key in ("before", "after"):
        cohort = runtime_delta.get(cohort_key)
        if not cohort:
            continue
        run_ids = cohort.get("run_ids", [])
        run_count = cohort.get("run_count")
        if run_count is not None and run_count != len(run_ids):
            _append_unique(
                errors,
                f"run_count_mismatches_run_ids_length: {cohort_key}",
            )

    if runtime_delta.get("mode") != "comparative":
        return

    before = runtime_delta.get("before", {})
    after = runtime_delta.get("after", {})

    before_ids = set(before.get("run_ids", []))
    after_ids = set(after.get("run_ids", []))
    overlap = before_ids & after_ids
    if overlap:
        _append_unique(errors, "run_ids_overlap_before_after")

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

    delta = runtime_delta.get("delta")
    if delta and "p50_seconds" in before and "p50_seconds" in after:
        recomputed = _recompute_delta(before, after)
        for field, recomputed_value in recomputed.items():
            declared_value = delta.get(field)
            if declared_value is None:
                continue
            tolerance = (
                PCT_TOLERANCE if field.endswith("_pct") else DELTA_TOLERANCE
            )
            if not math.isclose(
                declared_value, recomputed_value, rel_tol=1e-6, abs_tol=tolerance
            ):
                _append_unique(errors, "delta_recomputation_mismatch")
                break


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

    if outcome == "regressed":
        _append_unique(blockers, "observed_regression_blocks_approval")


def run_semantic_checks(
    assessment: dict[str, Any],
    ci_verdict_summary_path: str | None = None,
    expected_head_sha: str | None = None,
    expected_artifact_digest: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []

    _check_claim_state_machine(assessment, errors, warnings)
    _check_docs_only_runtime_delta(assessment, warnings)
    _check_functional_evidence_self_report(assessment["functional_evidence"], blockers)
    _check_trusted_functional_artifact(
        assessment,
        blockers,
        ci_verdict_summary_path,
        expected_head_sha,
        expected_artifact_digest,
    )
    _check_cohort_comparability(assessment["performance_evidence"], errors, blockers)
    _check_percentile_recomputed_from_raw_samples(assessment, errors, blockers)
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


def _build_format_checker():
    """jsonschema only enforces format: date-time when an optional
    rfc3339-validator/isodate extra is installed; without it FormatChecker
    silently accepts any string. Register an explicit RFC3339-ish check
    (stdlib only) so this repo's pinned dependency set does not silently
    disable format validation."""
    from jsonschema import FormatChecker
    from datetime import datetime

    checker = FormatChecker()

    @checker.checks("date-time", raises=ValueError)
    def _check_date_time(value: object) -> bool:
        if not isinstance(value, str):
            return True
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        datetime.fromisoformat(candidate)
        return True

    return checker


def validate_assessment(
    assessment_path: str,
    ci_verdict_summary_path: str | None = None,
    expected_head_sha: str | None = None,
    expected_artifact_digest: str | None = None,
) -> tuple[int, dict[str, Any]]:
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
    Draft202012Validator.check_schema(schema)
    validator_instance = Draft202012Validator(schema, format_checker=_build_format_checker())
    schema_errors = sorted(
        validator_instance.iter_errors(assessment), key=lambda e: e.path
    )
    if schema_errors:
        error_messages = [
            f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
            for err in schema_errors
        ]
        return EXIT_INVALID, structural_invalid_decision(error_messages)

    decision = run_semantic_checks(
        assessment,
        ci_verdict_summary_path=ci_verdict_summary_path,
        expected_head_sha=expected_head_sha,
        expected_artifact_digest=expected_artifact_digest,
    )
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
    parser.add_argument(
        "--ci-verdict-summary",
        default=None,
        help=(
            "Path to the canonical ci_verdict_summary_v2 artifact JSON. "
            "Required for approval_eligible=true when functional_evidence "
            "declares a ci_verdict_summary_ref; the assessment's own "
            "self-reported selected_checks are never trusted alone."
        ),
    )
    parser.add_argument(
        "--expected-head-sha",
        default=None,
        help="Trusted PR head SHA (e.g. from `git rev-parse HEAD` / gh CLI).",
    )
    parser.add_argument(
        "--expected-artifact-digest",
        default=None,
        help="Expected sha256:<hex> digest of the --ci-verdict-summary file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    exit_code, decision = validate_assessment(
        args.assessment,
        ci_verdict_summary_path=args.ci_verdict_summary,
        expected_head_sha=args.expected_head_sha,
        expected_artifact_digest=args.expected_artifact_digest,
    )

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

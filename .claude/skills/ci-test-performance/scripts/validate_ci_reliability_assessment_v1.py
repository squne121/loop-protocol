#!/usr/bin/env python3
"""
validate_ci_reliability_assessment_v1.py

Fixture-driven semantic validator for CI_TEST_RELIABILITY_ASSESSMENT_V1
(schemas/ci_test_reliability_assessment_v1.schema.json).

Separates:
- structural_valid: parses as strict JSON and validates against the
  JSON Schema (Draft 2020-12).
- semantic_valid: recomputes reliability_metrics.{before,after} and
  non_inferiority_evaluation from raw_attempts and cross-checks the
  self-reported aggregates. Self-reported aggregates alone are never
  trusted -- see docs/dev/ci-test-reliability-assessment.md.

This schema/validator is versioned independently from
CI_TEST_PERFORMANCE_ASSESSMENT_V2 / validate_ci_performance_assessment_v2.py
(latency/performance only) and does not modify that schema or validator.

Terminology (see docs/dev/ci-test-reliability-assessment.md for the full
definitions):
- Reliability's independent sample unit is workflow_run_id at run_attempt 1
  (attempt-1 terminal outcome). A GitHub Actions rerun (run_attempt > 1) is
  recorded but is never counted as an additional independent sample.
- A Playwright "flaky" test is one whose *final* internal retry attempt
  (Playwright's own `retries` config, all within a single run_attempt)
  passed after at least one earlier attempt failed/timedOut/interrupted.
  This is distinct from a GitHub Actions workflow rerun, which produces a
  *separate* PlaywrightLogicalTestRun record at a different run_attempt and
  is excluded from recomputation entirely.
- cancelled / timed_out / action_required workflow run conclusions are
  classified as infrastructure failure and are excluded from both the
  numerator and denominator of workflow_failure_rate (they are not counted
  as terminal failure, nor as success).

Exit codes:
  0 = valid (structural_valid and semantic_valid; non_inferiority outcome is
      reported but does not change the exit code)
  2 = structural or semantic invalid
  3 = operational failure (file not found, strict-JSON parse failure)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any

SCHEMA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "schemas"
)
ASSESSMENT_SCHEMA_PATH = os.path.normpath(
    os.path.join(SCHEMA_DIR, "ci_test_reliability_assessment_v1.schema.json")
)

EXIT_VALID = 0
EXIT_INVALID = 2
EXIT_OPERATIONAL_FAILURE = 3

RATE_TOLERANCE = 1e-6
CI_TOLERANCE = 1e-4

_INFRA_CONCLUSIONS = ("cancelled", "timed_out", "action_required")
_TERMINAL_FAILURE_TEST_STATUSES = ("failed", "timedOut", "interrupted")


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
# Clopper-Pearson exact interval (pure stdlib -- no scipy/numpy dependency)
# --------------------------------------------------------------------------- #
def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float, max_iter: int = 300, eps: float = 1e-14) -> float:
    """Continued fraction for the incomplete beta function (Numerical
    Recipes lentz algorithm)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = _log_beta(a, b) * -1.0 + a * math.log(x) + b * math.log(1.0 - x)
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(a: float, b: float, p: float, tol: float = 1e-12, max_iter: int = 200) -> float:
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        if _betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def clopper_pearson_interval(
    numerator: int, denominator: int, confidence_level: float
) -> tuple[float, float]:
    """Two-sided exact (Clopper-Pearson) confidence interval for a binomial
    proportion. Handles numerator == 0 and numerator == denominator without
    treating them as a point estimate of exactly 0 or 1 (see
    docs/dev/ci-test-reliability-assessment.md)."""
    if denominator <= 0:
        return (0.0, 1.0)
    alpha = 1.0 - confidence_level
    lower = (
        0.0
        if numerator == 0
        else _beta_quantile(numerator, denominator - numerator + 1, alpha / 2.0)
    )
    upper = (
        1.0
        if numerator == denominator
        else _beta_quantile(numerator + 1, denominator - numerator, 1.0 - alpha / 2.0)
    )
    return (lower, upper)


# --------------------------------------------------------------------------- #
# Raw attempt classification / recomputation
# --------------------------------------------------------------------------- #
def _classify_workflow_run(conclusion: str) -> str:
    """Returns one of: success, terminal_failure, infrastructure_failure,
    excluded (skipped)."""
    if conclusion == "success":
        return "success"
    if conclusion == "failure":
        return "terminal_failure"
    if conclusion in _INFRA_CONCLUSIONS:
        return "infrastructure_failure"
    return "excluded"


def _classify_playwright_test(record: dict[str, Any]) -> str:
    """Returns one of: success, flaky, terminal_failure, excluded (skipped
    final attempt)."""
    attempts = record.get("attempts", [])
    if not attempts:
        return "excluded"
    final_status = attempts[-1].get("status")
    if final_status == "skipped":
        return "excluded"
    earlier_failed = any(
        a.get("status") in _TERMINAL_FAILURE_TEST_STATUSES for a in attempts[:-1]
    )
    if final_status == "passed":
        return "flaky" if earlier_failed else "success"
    if final_status in _TERMINAL_FAILURE_TEST_STATUSES:
        return "terminal_failure"
    return "excluded"


def _recompute_metrics_block(cohort: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Recomputes the 3 reliability metrics for a single (before or after)
    raw_attempts cohort. run_attempt > 1 records (reruns) are excluded from
    every metric -- they are recorded for audit only."""
    workflow_success = 0
    workflow_terminal_failure = 0
    workflow_eligible = 0
    for run in cohort.get("workflow_runs", []):
        if run.get("run_attempt") != 1:
            continue
        classification = _classify_workflow_run(run.get("conclusion", ""))
        if classification == "success":
            workflow_success += 1
            workflow_eligible += 1
        elif classification == "terminal_failure":
            workflow_terminal_failure += 1
            workflow_eligible += 1
        # infrastructure_failure / excluded: recorded but not eligible

    test_flaky = 0
    test_terminal_failure = 0
    test_executed = 0
    for test in cohort.get("playwright_tests", []):
        if test.get("run_attempt") != 1:
            continue
        classification = _classify_playwright_test(test)
        if classification == "excluded":
            continue
        test_executed += 1
        if classification == "flaky":
            test_flaky += 1
        elif classification == "terminal_failure":
            test_terminal_failure += 1

    def _rate_fields(numerator: int, denominator: int) -> dict[str, Any]:
        return {
            "numerator": numerator,
            "denominator": denominator,
            "rate": (numerator / denominator) if denominator else None,
        }

    return {
        "workflow_failure_rate": _rate_fields(workflow_terminal_failure, workflow_eligible),
        "playwright_flaky_test_rate": _rate_fields(test_flaky, test_executed),
        "playwright_terminal_failure_rate": _rate_fields(
            test_terminal_failure, test_executed
        ),
    }


def _append_unique(items: list[str], code: str) -> None:
    if code not in items:
        items.append(code)


def _compare_rate_fields(
    metric_name: str,
    cohort_label: str,
    declared: dict[str, Any],
    recomputed: dict[str, Any],
    errors: list[str],
) -> None:
    if declared.get("numerator") != recomputed["numerator"]:
        _append_unique(
            errors,
            f"reliability_metric_recomputation_mismatch: {cohort_label}.{metric_name}.numerator",
        )
    if declared.get("denominator") != recomputed["denominator"]:
        _append_unique(
            errors,
            f"reliability_metric_recomputation_mismatch: {cohort_label}.{metric_name}.denominator",
        )
    recomputed_rate = recomputed["rate"]
    declared_rate = declared.get("rate")
    if recomputed_rate is not None and declared_rate is not None:
        if not math.isclose(
            declared_rate, recomputed_rate, rel_tol=1e-6, abs_tol=RATE_TOLERANCE
        ):
            _append_unique(
                errors,
                f"reliability_metric_recomputation_mismatch: {cohort_label}.{metric_name}.rate",
            )


def _check_reliability_metrics(
    assessment: dict[str, Any], errors: list[str]
) -> dict[str, dict[str, Any]]:
    raw_attempts = assessment["raw_attempts"]
    recomputed_by_cohort: dict[str, dict[str, Any]] = {}
    for cohort_label in ("before", "after"):
        recomputed = _recompute_metrics_block(raw_attempts[cohort_label])
        recomputed_by_cohort[cohort_label] = recomputed
        declared = assessment["reliability_metrics"][cohort_label]
        for metric_name, recomputed_fields in recomputed.items():
            _compare_rate_fields(
                metric_name,
                cohort_label,
                declared[metric_name],
                recomputed_fields,
                errors,
            )
    return recomputed_by_cohort


def _check_non_inferiority_evaluation(
    assessment: dict[str, Any],
    recomputed_metrics: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    target_metric = assessment["target_metric"]
    evaluation = assessment["non_inferiority_evaluation"]
    confidence_level = assessment["confidence_level"]
    margin = assessment["non_inferiority_margin"]
    required_sample_count = assessment["sample_count_rule"]["required_sample_count"]

    if evaluation["metric"] != target_metric:
        _append_unique(errors, "non_inferiority_evaluation_metric_mismatches_target_metric")
        return

    for cohort_label in ("before", "after"):
        recomputed_field = recomputed_metrics[cohort_label][target_metric]
        declared_interval = evaluation[cohort_label]

        if declared_interval["numerator"] != recomputed_field["numerator"]:
            _append_unique(
                errors,
                f"non_inferiority_evaluation_numerator_mismatch: {cohort_label}",
            )
        if declared_interval["denominator"] != recomputed_field["denominator"]:
            _append_unique(
                errors,
                f"non_inferiority_evaluation_denominator_mismatch: {cohort_label}",
            )
            continue

        recomputed_lower, recomputed_upper = clopper_pearson_interval(
            recomputed_field["numerator"],
            recomputed_field["denominator"],
            confidence_level,
        )
        if not math.isclose(
            declared_interval["ci_lower"], recomputed_lower, abs_tol=CI_TOLERANCE
        ) or not math.isclose(
            declared_interval["ci_upper"], recomputed_upper, abs_tol=CI_TOLERANCE
        ):
            _append_unique(
                errors, f"clopper_pearson_interval_recomputation_mismatch: {cohort_label}"
            )

    before_rate = recomputed_metrics["before"][target_metric]["rate"] or 0.0
    after_denominator = recomputed_metrics["after"][target_metric]["denominator"]
    after_upper = clopper_pearson_interval(
        recomputed_metrics["after"][target_metric]["numerator"],
        recomputed_metrics["after"][target_metric]["denominator"],
        confidence_level,
    )[1]

    if after_denominator < required_sample_count:
        expected_outcome = "inconclusive"
    elif after_upper <= before_rate + margin:
        expected_outcome = "non_inferior"
    else:
        expected_outcome = "inferior"

    if evaluation["outcome"] != expected_outcome:
        _append_unique(
            errors,
            f"non_inferiority_outcome_recomputation_mismatch: "
            f"declared={evaluation['outcome']} expected={expected_outcome}",
        )


def run_semantic_checks(assessment: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []

    recomputed_metrics = _check_reliability_metrics(assessment, errors)
    _check_non_inferiority_evaluation(assessment, recomputed_metrics, errors)

    semantic_valid = len(errors) == 0
    return {
        "structural_valid": True,
        "semantic_valid": semantic_valid,
        "errors": errors,
        "recomputed_reliability_metrics": recomputed_metrics,
    }


def structural_invalid_decision(errors: list[str]) -> dict[str, Any]:
    return {
        "structural_valid": False,
        "semantic_valid": False,
        "errors": errors,
        "recomputed_reliability_metrics": {},
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_assessment_schema() -> dict[str, Any]:
    with open(ASSESSMENT_SCHEMA_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _build_format_checker():
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

    decision = run_semantic_checks(assessment)
    exit_code = EXIT_VALID if decision["semantic_valid"] else EXIT_INVALID
    return exit_code, decision


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a CI_TEST_RELIABILITY_ASSESSMENT_V1 JSON document"
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
        "schema": "CI_TEST_RELIABILITY_ASSESSMENT_V1_VALIDATION_RESULT",
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

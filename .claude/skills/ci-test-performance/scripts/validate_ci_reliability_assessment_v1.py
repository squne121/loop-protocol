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
import statistics
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
# Newcombe/Wilson hybrid score (MOVER) two-independent-proportions interval
# (pure stdlib -- statistics.NormalDist for the normal quantile, no
# scipy/statsmodels dependency). This is the AUTHORITATIVE method for
# non_inferiority_evaluation.outcome; the Clopper-Pearson interval above is
# per-arm audit-only (see docs/dev/ci-test-reliability-assessment.md).
# --------------------------------------------------------------------------- #
def _wilson_score_interval_one_sided(
    numerator: int, denominator: int, confidence_level: float
) -> tuple[float, float]:
    """One-sided Wilson score interval for a single binomial proportion.
    Both endpoints are returned; callers use only the endpoint relevant to
    their one-sided hypothesis. z is the (1 - alpha) normal quantile (NOT
    1 - alpha/2 -- that would be the two-sided quantile)."""
    if denominator <= 0:
        return (0.0, 1.0)
    z = statistics.NormalDist().inv_cdf(confidence_level)
    n = float(denominator)
    x = float(numerator)
    p = x / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (x + z2 / 2.0) / (n + z2)
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    lower = max(0.0, center - half)
    upper = min(1.0, center + half)
    return (lower, upper)


def newcombe_risk_difference_one_sided_upper(
    numerator_after: int,
    denominator_after: int,
    numerator_before: int,
    denominator_before: int,
    confidence_level: float,
) -> tuple[float, float]:
    """One-sided upper confidence bound for the risk difference
    p_after - p_before, via Newcombe's hybrid score (MOVER) method built
    from one-sided Wilson score intervals for each independent arm. This is
    a genuine two-independent-proportions comparison (not a single-arm
    interval compared against a point estimate). Returns
    (point_estimate, ci_upper)."""
    p_after = numerator_after / denominator_after
    p_before = numerator_before / denominator_before
    _, u_after = _wilson_score_interval_one_sided(
        numerator_after, denominator_after, confidence_level
    )
    l_before, _ = _wilson_score_interval_one_sided(
        numerator_before, denominator_before, confidence_level
    )
    diff = p_after - p_before
    upper = diff + math.sqrt((u_after - p_after) ** 2 + (p_before - l_before) ** 2)
    return diff, upper


def evaluate_non_inferiority(
    before_metric: dict[str, Any],
    after_metric: dict[str, Any],
    confidence_level: float,
    margin: float,
    required_sample_count: int,
) -> dict[str, Any]:
    """Genuine two-independent-proportions one-sided non-inferiority
    evaluation of p_after - p_before. Both before AND after must
    individually meet required_sample_count -- otherwise outcome is
    inconclusive (fixes the false PASS where only the after side's sample
    count was checked, e.g. before=1/1, after=20/20, margin=0.01 would
    previously report non_inferior)."""
    before_num = before_metric["numerator"]
    before_den = before_metric["denominator"]
    after_num = after_metric["numerator"]
    after_den = after_metric["denominator"]

    if before_den < required_sample_count or after_den < required_sample_count:
        return {"outcome": "inconclusive", "point_estimate": None, "ci_upper": None}

    point_estimate, ci_upper = newcombe_risk_difference_one_sided_upper(
        after_num, after_den, before_num, before_den, confidence_level
    )
    outcome = "non_inferior" if ci_upper <= margin else "inferior"
    return {"outcome": outcome, "point_estimate": point_estimate, "ci_upper": ci_upper}


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


def _attempt_is_expected(attempt: dict[str, Any]) -> bool:
    """An attempt is "expected" when its status matches expected_status
    (Playwright's own expectedStatus, e.g. for test.fail()). expected_status
    defaults to "passed" when absent -- this preserves prior behavior for
    producers that do not emit it (a plain failed/timedOut/interrupted
    attempt with no expected_status is still "unexpected", i.e. a real
    failure)."""
    return attempt.get("status") == attempt.get("expected_status", "passed")


def _classify_playwright_test(record: dict[str, Any]) -> str:
    """Returns one of: success, flaky, terminal_failure, excluded (skipped
    final attempt).

    Respects Playwright's outcome semantics via expected_status: a final
    attempt whose status matches its expected_status (e.g. test.fail()
    expecting "failed" and getting "failed") is treated as an expected
    result (success / flaky-if-earlier-unexpected-failure), never as a
    terminal_failure regression. Only an *unexpected* final status
    (status != expected_status) that is failed/timedOut/interrupted counts
    as terminal_failure."""
    attempts = record.get("attempts", [])
    if not attempts:
        return "excluded"
    final = attempts[-1]
    final_status = final.get("status")
    if final_status == "skipped":
        return "excluded"
    final_expected = _attempt_is_expected(final)
    earlier_unexpected_failure = any(
        not _attempt_is_expected(a) and a.get("status") in _TERMINAL_FAILURE_TEST_STATUSES
        for a in attempts[:-1]
    )
    if final_expected:
        return "flaky" if earlier_unexpected_failure else "success"
    if final_status in _TERMINAL_FAILURE_TEST_STATUSES:
        return "terminal_failure"
    return "excluded"


def _check_duplicate_sample_identities(
    cohort: dict[str, Any], cohort_label: str, errors: list[str]
) -> None:
    """sample_identity.key = workflow_run_id is a declared contract, not
    just documentation -- hard-fail (reject, never silently dedupe) on
    duplicate raw evidence records, since silent summation lets a producer
    dilute the failure rate arbitrarily by duplicating a low-failure
    record N times."""
    seen_run_records: set[tuple[Any, Any]] = set()
    for run in cohort.get("workflow_runs", []):
        key = (run.get("workflow_run_id"), run.get("run_attempt"))
        if key in seen_run_records:
            _append_unique(
                errors,
                f"duplicate_workflow_run_record: {cohort_label} "
                f"workflow_run_id={key[0]} run_attempt={key[1]}",
            )
        seen_run_records.add(key)

    seen_attempt1_ids: set[Any] = set()
    for run in cohort.get("workflow_runs", []):
        if run.get("run_attempt") != 1:
            continue
        workflow_run_id = run.get("workflow_run_id")
        if workflow_run_id in seen_attempt1_ids:
            _append_unique(
                errors,
                f"duplicate_workflow_run_id: {cohort_label} "
                f"workflow_run_id={workflow_run_id}",
            )
        seen_attempt1_ids.add(workflow_run_id)

    seen_test_records: set[tuple[Any, Any, Any]] = set()
    for test in cohort.get("playwright_tests", []):
        key = (
            test.get("workflow_run_id"),
            test.get("run_attempt"),
            test.get("test_id"),
        )
        if key in seen_test_records:
            _append_unique(
                errors,
                f"duplicate_playwright_test_record: {cohort_label} "
                f"workflow_run_id={key[0]} run_attempt={key[1]} test_id={key[2]}",
            )
        seen_test_records.add(key)


def _check_attempt_number_ordering(
    cohort: dict[str, Any], cohort_label: str, errors: list[str]
) -> None:
    """attempt_number must be unique and strictly increasing within a
    PlaywrightLogicalTestRun.attempts array -- the validator (and
    Playwright itself) treats attempts[-1] as authoritative for the final
    outcome, so an unordered/duplicated array can silently flip a flaky
    result into a false terminal_failure or vice versa."""
    for test in cohort.get("playwright_tests", []):
        numbers = [a.get("attempt_number") for a in test.get("attempts", [])]
        if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
            _append_unique(
                errors,
                f"attempt_number_not_unique_or_ordered: {cohort_label} "
                f"test_id={test.get('test_id')} "
                f"workflow_run_id={test.get('workflow_run_id')} "
                f"run_attempt={test.get('run_attempt')}",
            )


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
        _check_duplicate_sample_identities(raw_attempts[cohort_label], cohort_label, errors)
        _check_attempt_number_ordering(raw_attempts[cohort_label], cohort_label, errors)
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
    """Cross-checks non_inferiority_evaluation against raw_attempts.

    Two independent things are recomputed and cross-checked:
    1. before/after: per-arm two-sided Clopper-Pearson intervals -- audit
       only, NOT the basis for outcome (see IntervalEstimate description).
    2. risk_difference + outcome: the AUTHORITATIVE genuine
       two-independent-proportions one-sided non-inferiority test of
       p_after - p_before (see evaluate_non_inferiority()). required_sample_count
       is enforced on BOTH arms, not just after -- this is the P0-1 fix:
       previously before=1/1, after=20/20, margin=0.01 recomputed as
       non_inferior because only after's sample count and a single-arm
       CI-vs-point-estimate comparison were checked.
    """
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

    before_field = recomputed_metrics["before"][target_metric]
    after_field = recomputed_metrics["after"][target_metric]
    expected = evaluate_non_inferiority(
        before_field, after_field, confidence_level, margin, required_sample_count
    )

    declared_rd = evaluation["risk_difference"]
    declared_point = declared_rd["point_estimate"]
    declared_upper = declared_rd["ci_upper"]
    expected_point = expected["point_estimate"]
    expected_upper = expected["ci_upper"]

    def _matches(declared: float | None, computed: float | None) -> bool:
        if declared is None or computed is None:
            return declared is None and computed is None
        return math.isclose(declared, computed, abs_tol=CI_TOLERANCE)

    if not _matches(declared_point, expected_point):
        _append_unique(errors, "risk_difference_point_estimate_recomputation_mismatch")
    if not _matches(declared_upper, expected_upper):
        _append_unique(errors, "risk_difference_ci_upper_recomputation_mismatch")

    if evaluation["outcome"] != expected["outcome"]:
        _append_unique(
            errors,
            f"non_inferiority_outcome_recomputation_mismatch: "
            f"declared={evaluation['outcome']} expected={expected['outcome']}",
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

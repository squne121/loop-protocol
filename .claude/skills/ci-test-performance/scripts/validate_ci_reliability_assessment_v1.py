#!/usr/bin/env python3
"""Strict semantic validator for CI_TEST_RELIABILITY_ASSESSMENT_V1.

Reliability V1 has one repo-static statistical design.  The retained
Newcombe/Wilson hybrid MOVER predicate decides each outcome pair; exact
binomial enumeration computes the actual power of that same predicate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from functools import lru_cache
from typing import Any

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "schemas")
ASSESSMENT_SCHEMA_PATH = os.path.normpath(os.path.join(SCHEMA_DIR, "ci_test_reliability_assessment_v1.schema.json"))
EXIT_VALID = 0
EXIT_INVALID = 2
EXIT_OPERATIONAL_FAILURE = 3
RATE_TOLERANCE = 1e-6
CI_TOLERANCE = 1e-4

POWER_DESIGNS = {
    "newcombe_wilson_hybrid_exact_binomial_power_v1": {
        "alpha": 0.05,
        "target_power": 0.80,
        "margin": 0.20,
        "assumed_before_rate": 0.05,
        "assumed_after_rate": 0.05,
        "allocation": "equal_1_to_1",
        "sample_count": "per_arm",
        "maximum_runs_per_arm": 100,
        "over_budget": "design_infeasible",
    }
}
POWER_DESIGN_ID = "newcombe_wilson_hybrid_exact_binomial_power_v1"
METRICS = (
    "workflow_failure_rate",
    "playwright_flaky_test_rate",
    "playwright_terminal_failure_rate",
)
ELIGIBLE_WORKFLOW_CONCLUSIONS = ("success", "failure")


class StrictJSONError(ValueError):
    """Raised for duplicate keys, non-finite constants, and invalid JSON."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate_json_key: {key!r}")
        result[key] = value
    return result


def _reject_constant(constant: str) -> float:
    raise StrictJSONError(f"non_finite_json_constant: {constant}")


def strict_json_loads(raw_text: str) -> dict[str, Any]:
    try:
        return json.loads(raw_text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except StrictJSONError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictJSONError(f"json_syntax_error: {exc}") from exc


# Audit-only Clopper-Pearson intervals -------------------------------------------------
def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float, max_iter: int = 300, eps: float = 1e-14) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1e-300 if abs(d) < 1e-300 else d
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1e-300 if abs(d) < 1e-300 else d
        c = 1.0 + aa / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1e-300 if abs(d) < 1e-300 else d
        c = 1.0 + aa / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(-_log_beta(a, b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(a: float, b: float, p: float, tol: float = 1e-12) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def clopper_pearson_interval(numerator: int, denominator: int, confidence_level: float) -> tuple[float, float]:
    if denominator <= 0:
        return (0.0, 1.0)
    alpha = 1.0 - confidence_level
    lower = 0.0 if numerator == 0 else _beta_quantile(numerator, denominator - numerator + 1, alpha / 2.0)
    upper = (
        1.0 if numerator == denominator else _beta_quantile(numerator + 1, denominator - numerator, 1.0 - alpha / 2.0)
    )
    return lower, upper


# Retained fixed decision function -----------------------------------------------------
def _wilson_score_interval_one_sided(numerator: int, denominator: int, confidence_level: float) -> tuple[float, float]:
    if denominator <= 0:
        return (0.0, 1.0)
    z = statistics.NormalDist().inv_cdf(confidence_level)
    n, x = float(denominator), float(numerator)
    p = x / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (x + z2 / 2.0) / (n + z2)
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def newcombe_risk_difference_one_sided_upper(
    numerator_after: int,
    denominator_after: int,
    numerator_before: int,
    denominator_before: int,
    confidence_level: float,
) -> tuple[float, float]:
    """Newcombe/Wilson hybrid MOVER upper bound for p_after - p_before."""
    p_after = numerator_after / denominator_after
    p_before = numerator_before / denominator_before
    _, upper_after = _wilson_score_interval_one_sided(numerator_after, denominator_after, confidence_level)
    lower_before, _ = _wilson_score_interval_one_sided(numerator_before, denominator_before, confidence_level)
    point = p_after - p_before
    return point, point + math.sqrt((upper_after - p_after) ** 2 + (p_before - lower_before) ** 2)


def _is_rejected_pair(x_before: int, x_after: int, n: int, design: dict[str, Any]) -> bool:
    # In this non-inferiority decision, rejection of inferiority is exactly a
    # close-evidence pass under the retained ci_upper <= margin predicate.
    _, ci_upper = newcombe_risk_difference_one_sided_upper(x_after, n, x_before, n, 1.0 - design["alpha"])
    return ci_upper <= design["margin"]


def binomial_pmf(n: int, x: int, p: float) -> float:
    """Stable stdlib PMF with explicit p=0/1 and out-of-domain behavior."""
    if x < 0 or x > n or n < 0 or not 0.0 <= p <= 1.0:
        return 0.0
    if p == 0.0:
        return 1.0 if x == 0 else 0.0
    if p == 1.0:
        return 1.0 if x == n else 0.0
    log_pmf = (
        math.lgamma(n + 1) - math.lgamma(x + 1) - math.lgamma(n - x + 1) + x * math.log(p) + (n - x) * math.log1p(-p)
    )
    return math.exp(log_pmf)


def exact_power_for_n(n: int, design: dict[str, Any] | None = None) -> float:
    design = POWER_DESIGNS[POWER_DESIGN_ID] if design is None else design
    before_pmfs = [binomial_pmf(n, x, design["assumed_before_rate"]) for x in range(n + 1)]
    after_pmfs = [binomial_pmf(n, x, design["assumed_after_rate"]) for x in range(n + 1)]
    return math.fsum(
        before_pmfs[x_before] * after_pmfs[x_after]
        for x_before in range(n + 1)
        for x_after in range(n + 1)
        if _is_rejected_pair(x_before, x_after, n, design)
    )


@lru_cache(maxsize=1)
def enumerate_static_power_design() -> dict[str, Any]:
    """Increment n=1..100 without assuming monotonicity; return first pass."""
    design = POWER_DESIGNS[POWER_DESIGN_ID]
    powers: list[tuple[int, float]] = []
    for n in range(1, design["maximum_runs_per_arm"] + 1):
        power = exact_power_for_n(n, design)
        powers.append((n, power))
        if power >= design["target_power"]:
            return {"status": "ok", "required_sample_count_per_arm": n, "power": power, "powers": powers}
    return {
        "status": design["over_budget"],
        "required_sample_count_per_arm": None,
        "power": powers[-1][1],
        "powers": powers,
    }


def evaluate_non_inferiority(
    before: dict[str, Any], after: dict[str, Any], required_sample_count_per_arm: int
) -> dict[str, Any]:
    # Fail-closed shape reused for both insufficient-count and the two new
    # equal-arm / actual-power gates below (see docs: "count 不足時の outcome
    # は inconclusive" now also covers unequal cohorts and under-powered n).
    inconclusive = {"outcome": "inconclusive", "point_estimate": None, "ci_upper": None}
    if before["denominator"] < required_sample_count_per_arm or after["denominator"] < required_sample_count_per_arm:
        return inconclusive
    # The fixed design is equal 1:1 allocation, per-arm sample count; an
    # unbalanced cohort never satisfies the design even if both arms
    # individually clear required_sample_count_per_arm.
    if before["denominator"] != after["denominator"]:
        return inconclusive
    n = before["denominator"]
    design = POWER_DESIGNS[POWER_DESIGN_ID]
    # Re-evaluate the actual achieved power at the actual observed n; power is
    # not monotonic in n (see exact_power_for_n(20) vs exact_power_for_n(21)),
    # so required_sample_count_per_arm alone does not guarantee target_power
    # is met at every n that happens to exceed it.
    if exact_power_for_n(n, design) < design["target_power"]:
        return inconclusive
    point, ci_upper = newcombe_risk_difference_one_sided_upper(
        after["numerator"], after["denominator"], before["numerator"], before["denominator"], 0.95
    )
    return {
        "outcome": "non_inferior" if ci_upper <= 0.20 else "inferior",
        "point_estimate": point,
        "ci_upper": ci_upper,
    }


def _append_unique(errors: list[str], error: str) -> None:
    if error not in errors:
        errors.append(error)


def _canonical_classification(metric: str, record: dict[str, Any], test_cases: list[dict[str, Any]]) -> str:
    if metric == "workflow_failure_rate":
        return "failure" if record["conclusion"] == "failure" else "success"
    outcomes = [case["outcome"] for case in test_cases]
    affected_outcome = "flaky" if metric == "playwright_flaky_test_rate" else "unexpected"
    return "affected" if affected_outcome in outcomes else "not_affected"


def _validate_record_graph(
    assessment: dict[str, Any], errors: list[str]
) -> tuple[dict[tuple[int, str, int], dict[str, Any]], dict[tuple[int, str, int], list[dict[str, Any]]]]:
    records: dict[tuple[int, str, int], dict[str, Any]] = {}
    run_arms: dict[int, str] = {}
    for record in assessment["workflow_records"]:
        key = (record["workflow_run_id"], record["arm"], record["run_attempt"])
        if key in records:
            _append_unique(
                errors,
                "duplicate_workflow_run_record: "
                f"workflow_run_id={record['workflow_run_id']} "
                f"run_attempt={record['run_attempt']}",
            )
        records[key] = record
        existing_arm = run_arms.get(record["workflow_run_id"])
        if existing_arm is not None and existing_arm != record["arm"]:
            _append_unique(errors, f"cross_arm_same_workflow_run_id: workflow_run_id={record['workflow_run_id']}")
        run_arms[record["workflow_run_id"]] = record["arm"]

    test_cases_by_record: dict[tuple[int, str, int], list[dict[str, Any]]] = {}
    seen_test_cases: set[tuple[int, str, int, str]] = set()
    for case in assessment["playwright_test_cases"]:
        key = (case["workflow_run_id"], case["arm"], case["run_attempt"])
        case_key = (*key, case["test_id"])
        if case_key in seen_test_cases:
            _append_unique(
                errors,
                f"duplicate_playwright_test_case: workflow_run_id={case['workflow_run_id']} test_id={case['test_id']}",
            )
        seen_test_cases.add(case_key)
        if key not in records:
            if case["workflow_run_id"] in run_arms:
                _append_unique(
                    errors, f"mismatched_arm_playwright_workflow_run_id: workflow_run_id={case['workflow_run_id']}"
                )
            else:
                _append_unique(errors, f"orphan_playwright_workflow_run_id: workflow_run_id={case['workflow_run_id']}")
        test_cases_by_record.setdefault(key, []).append(case)
    return records, test_cases_by_record


def _recompute_from_provenance(assessment: dict[str, Any], errors: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    records, test_cases_by_record = _validate_record_graph(assessment, errors)
    recomputed: dict[str, dict[str, dict[str, Any]]] = {"before": {}, "after": {}}
    for arm in ("before", "after"):
        eligible_records = {
            record["workflow_run_id"]: record
            for record in records.values()
            if record["arm"] == arm
            and record["run_attempt"] == 1
            and record["conclusion"] in ELIGIBLE_WORKFLOW_CONCLUSIONS
        }
        for metric in METRICS:
            provenance = assessment["sample_provenance"][arm][metric]
            observations = provenance["observations"]
            seen: set[int] = set()
            valid_observations: list[dict[str, Any]] = []
            for observation in observations:
                run_id = observation["workflow_run_id"]
                if observation["arm"] != arm:
                    _append_unique(errors, f"mismatched_arm_provenance: {arm}.{metric} workflow_run_id={run_id}")
                    continue
                if observation["run_attempt"] != 1:
                    _append_unique(errors, f"non_attempt_1_sample: {arm}.{metric} workflow_run_id={run_id}")
                    _append_unique(errors, f"retry_or_rerun_sample_inclusion: {arm}.{metric} workflow_run_id={run_id}")
                    continue
                key = (run_id, arm, 1)
                if key not in records:
                    if any(record_id == run_id for record_id, _, _ in records):
                        _append_unique(errors, f"mismatched_arm_provenance: {arm}.{metric} workflow_run_id={run_id}")
                    else:
                        _append_unique(errors, f"orphan_workflow_run_id: {arm}.{metric} workflow_run_id={run_id}")
                    continue
                if run_id in seen:
                    _append_unique(errors, f"duplicate_workflow_run_id: {arm}.{metric} workflow_run_id={run_id}")
                    continue
                seen.add(run_id)
                record = records[key]
                if record["conclusion"] not in ELIGIBLE_WORKFLOW_CONCLUSIONS:
                    _append_unique(errors, f"ineligible_workflow_run_sample: {arm}.{metric} workflow_run_id={run_id}")
                    continue
                expected = _canonical_classification(metric, record, test_cases_by_record.get(key, []))
                if observation["classification"] != expected:
                    _append_unique(
                        errors, f"canonical_classification_mismatch: {arm}.{metric} workflow_run_id={run_id}"
                    )
                    continue
                valid_observations.append(observation)
            if seen != set(eligible_records):
                missing = sorted(set(eligible_records) - seen)
                if missing:
                    _append_unique(
                        errors,
                        f"sample_provenance_missing_eligible_workflow_run: {arm}.{metric} workflow_run_id={missing[0]}",
                    )
                extra = sorted(seen - set(eligible_records))
                if extra:
                    _append_unique(
                        errors,
                        "sample_provenance_includes_noneligible_workflow_run: "
                        f"{arm}.{metric} workflow_run_id={extra[0]}",
                    )
            numerator = sum(
                observation["classification"] in ("failure", "affected") for observation in valid_observations
            )
            denominator = len(valid_observations)
            recomputed[arm][metric] = {
                "numerator": numerator,
                "denominator": denominator,
                "rate": numerator / denominator if denominator else None,
            }
    return recomputed


def _check_declared_metrics(
    assessment: dict[str, Any], recomputed: dict[str, dict[str, dict[str, Any]]], errors: list[str]
) -> None:
    for arm in ("before", "after"):
        for metric in METRICS:
            declared = assessment["reliability_metrics"][arm][metric]
            expected = recomputed[arm][metric]
            for field in ("numerator", "denominator"):
                if declared[field] != expected[field]:
                    _append_unique(errors, f"reliability_metric_recomputation_mismatch: {arm}.{metric}.{field}")
            if expected["rate"] is not None and not math.isclose(
                declared["rate"], expected["rate"], rel_tol=1e-6, abs_tol=RATE_TOLERANCE
            ):
                _append_unique(errors, f"reliability_metric_recomputation_mismatch: {arm}.{metric}.rate")


def _check_non_inferiority_evaluation(
    assessment: dict[str, Any], recomputed: dict[str, dict[str, dict[str, Any]]], errors: list[str]
) -> None:
    power = enumerate_static_power_design()
    supplied_count = assessment["sample_count_rule"]["required_sample_count_per_arm"]
    if power["status"] != "ok" or supplied_count != power["required_sample_count_per_arm"]:
        _append_unique(errors, "required_sample_count_per_arm_mismatch")
    evaluation = assessment["non_inferiority_evaluation"]
    target = assessment["target_metric"]
    if evaluation["metric"] != target:
        _append_unique(errors, "non_inferiority_evaluation_metric_mismatches_target_metric")
        return
    for arm in ("before", "after"):
        fields = recomputed[arm][target]
        declared = evaluation[arm]
        if declared["numerator"] != fields["numerator"]:
            _append_unique(errors, f"non_inferiority_evaluation_numerator_mismatch: {arm}")
        if declared["denominator"] != fields["denominator"]:
            _append_unique(errors, f"non_inferiority_evaluation_denominator_mismatch: {arm}")
        lower, upper = clopper_pearson_interval(fields["numerator"], fields["denominator"], 0.95)
        if not math.isclose(declared["ci_lower"], lower, abs_tol=CI_TOLERANCE) or not math.isclose(
            declared["ci_upper"], upper, abs_tol=CI_TOLERANCE
        ):
            _append_unique(errors, f"clopper_pearson_interval_recomputation_mismatch: {arm}")
    expected = evaluate_non_inferiority(recomputed["before"][target], recomputed["after"][target], supplied_count)
    declared = evaluation["risk_difference"]
    for field in ("point_estimate", "ci_upper"):
        if declared[field] is None or expected[field] is None:
            matches = declared[field] is None and expected[field] is None
        else:
            matches = math.isclose(declared[field], expected[field], abs_tol=CI_TOLERANCE)
        if not matches:
            _append_unique(errors, f"risk_difference_{field}_recomputation_mismatch")
    if evaluation["outcome"] != expected["outcome"]:
        _append_unique(
            errors,
            "non_inferiority_outcome_recomputation_mismatch: "
            f"declared={evaluation['outcome']} expected={expected['outcome']}",
        )


def run_semantic_checks(assessment: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    recomputed = _recompute_from_provenance(assessment, errors)
    _check_declared_metrics(assessment, recomputed, errors)
    _check_non_inferiority_evaluation(assessment, recomputed, errors)
    return {
        "structural_valid": True,
        "semantic_valid": not errors,
        "errors": errors,
        "recomputed_reliability_metrics": recomputed,
        "power_design": enumerate_static_power_design(),
    }


def structural_invalid_decision(errors: list[str]) -> dict[str, Any]:
    return {
        "structural_valid": False,
        "semantic_valid": False,
        "errors": errors,
        "recomputed_reliability_metrics": {},
        "power_design": enumerate_static_power_design(),
    }


def load_assessment_schema() -> dict[str, Any]:
    with open(ASSESSMENT_SCHEMA_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _build_format_checker():
    from datetime import datetime
    from jsonschema import FormatChecker

    checker = FormatChecker()

    @checker.checks("date-time", raises=ValueError)
    def _check_date_time(value: object) -> bool:
        if not isinstance(value, str):
            return True
        datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        return True

    return checker


def validate_assessment(assessment_path: str) -> tuple[int, dict[str, Any]]:
    try:
        with open(assessment_path, encoding="utf-8") as handle:
            assessment = strict_json_loads(handle.read())
    except OSError as exc:
        return EXIT_OPERATIONAL_FAILURE, {"operational_error": f"file_not_readable: {exc}"}
    except StrictJSONError as exc:
        return EXIT_OPERATIONAL_FAILURE, {"operational_error": str(exc)}
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        return EXIT_OPERATIONAL_FAILURE, {"operational_error": f"jsonschema_not_installed: {exc}"}
    schema = load_assessment_schema()
    Draft202012Validator.check_schema(schema)
    validation = Draft202012Validator(schema, format_checker=_build_format_checker())
    schema_errors = sorted(validation.iter_errors(assessment), key=lambda error: list(error.path))
    if schema_errors:
        return EXIT_INVALID, structural_invalid_decision(
            [f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in schema_errors]
        )
    decision = run_semantic_checks(assessment)
    return (EXIT_VALID if decision["semantic_valid"] else EXIT_INVALID), decision


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate CI_TEST_RELIABILITY_ASSESSMENT_V1 evidence")
    parser.add_argument("--assessment", required=True)
    parser.add_argument("--output", required=True)
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
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

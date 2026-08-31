"""Reliability V1 fixed-design and workflow-provenance contract tests."""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

SCRIPTS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, SCRIPTS_DIR)
import validate_ci_reliability_assessment_v1 as validator  # noqa: E402

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
FIXTURES_DIR = os.path.join(REPO_ROOT, "fixtures", "ci-test-reliability")


def _rate(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator, "rate": numerator / denominator}


def _make_assessment() -> dict:
    records = []
    cases = []
    provenance = {arm: {} for arm in ("before", "after")}
    metrics = {arm: {} for arm in ("before", "after")}
    for arm, first_id in (("before", 1000), ("after", 2000)):
        arm_records = [
            {
                "workflow_run_id": first_id + index,
                "arm": arm,
                "run_attempt": 1,
                "conclusion": "failure" if index == 0 else "success",
            }
            for index in range(22)
        ]
        records.extend(arm_records)
        # The official TestCase outcome is the sole Playwright source.
        cases.extend(
            [
                {
                    "test_id": f"{arm}-flaky",
                    "workflow_run_id": first_id + 1,
                    "arm": arm,
                    "run_attempt": 1,
                    "outcome": "flaky",
                },
                {
                    "test_id": f"{arm}-unexpected",
                    "workflow_run_id": first_id + 2,
                    "arm": arm,
                    "run_attempt": 1,
                    "outcome": "unexpected",
                },
            ]
        )
        for metric in validator.METRICS:
            observations = []
            for record in arm_records:
                related_cases = [case for case in cases if case["workflow_run_id"] == record["workflow_run_id"]]
                observations.append(
                    {
                        "workflow_run_id": record["workflow_run_id"],
                        "arm": arm,
                        "run_attempt": 1,
                        "classification": validator._canonical_classification(metric, record, related_cases),
                    }
                )
            provenance[arm][metric] = {"design_id": validator.POWER_DESIGN_ID, "observations": observations}
            numerator = sum(item["classification"] in ("failure", "affected") for item in observations)
            metrics[arm][metric] = _rate(numerator, len(observations))
    required = validator.enumerate_static_power_design()["required_sample_count_per_arm"]
    before = metrics["before"]["workflow_failure_rate"]
    after = metrics["after"]["workflow_failure_rate"]
    before_ci = validator.clopper_pearson_interval(before["numerator"], before["denominator"], 0.95)
    after_ci = validator.clopper_pearson_interval(after["numerator"], after["denominator"], 0.95)
    outcome = validator.evaluate_non_inferiority(before, after, required)
    return {
        "schema": "CI_TEST_RELIABILITY_ASSESSMENT_V1",
        "schema_version": 1,
        "issue_number": 2432,
        "pr_number": None,
        "measured_at": "2026-08-31T00:00:00Z",
        "target_metric": "workflow_failure_rate",
        "reliability_metrics": metrics,
        "sample_identity": {"key": "workflow_run_id", "required_run_attempt": 1},
        "confidence_level": 0.95,
        "non_inferiority_margin": 0.2,
        "sample_count_rule": {"design_id": validator.POWER_DESIGN_ID, "required_sample_count_per_arm": required},
        "non_inferiority_evaluation": {
            "metric": "workflow_failure_rate",
            "effect_measure": "risk_difference",
            "method": "newcombe_wilson_hybrid_mover_v1",
            "sidedness": "one_sided",
            "before": {
                "numerator": before["numerator"],
                "denominator": before["denominator"],
                "ci_lower": before_ci[0],
                "ci_upper": before_ci[1],
            },
            "after": {
                "numerator": after["numerator"],
                "denominator": after["denominator"],
                "ci_lower": after_ci[0],
                "ci_upper": after_ci[1],
            },
            "risk_difference": {
                "method": "newcombe_wilson_hybrid_mover_v1",
                "point_estimate": outcome["point_estimate"],
                "ci_upper": outcome["ci_upper"],
            },
            "outcome": outcome["outcome"],
        },
        "workflow_records": records,
        "playwright_test_cases": cases,
        "sample_provenance": provenance,
        # Deliberately contradicting retry history: it remains audit-only.
        "raw_attempts": [
            {
                "workflow_run_id": 1001,
                "run_attempt": 1,
                "test_id": "before-flaky",
                "attempt_number": 1,
                "status": "passed",
            }
        ],
    }


def _validate_document(document: dict, tmp_path) -> tuple[int, dict]:
    path = tmp_path / "assessment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return validator.validate_assessment(str(path))


def test_static_power_designs_all_metrics(tmp_path):
    document = _make_assessment()
    exit_code, decision = _validate_document(document, tmp_path)
    assert exit_code == 0
    assert decision["semantic_valid"] is True
    assert validator.POWER_DESIGNS == {
        validator.POWER_DESIGN_ID: {
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
    for arm in ("before", "after"):
        for metric in validator.METRICS:
            assert document["sample_provenance"][arm][metric]["design_id"] == validator.POWER_DESIGN_ID


@pytest.mark.parametrize(
    "mutator, structural",
    [
        (lambda item: item["sample_count_rule"].update({"design_id": "post_hoc_design"}), True),
        (lambda item: item["sample_count_rule"].update({"required_sample_count": 22}), True),
        (lambda item: item["sample_count_rule"].update({"allocation": "unequal_2_to_1"}), True),
        (lambda item: item["non_inferiority_evaluation"].update({"method": "farrington_manning"}), True),
        (lambda item: item["sample_count_rule"].update({"required_sample_count_per_arm": 21}), False),
    ],
)
def test_power_contract_rejections(mutator, structural, tmp_path):
    document = _make_assessment()
    mutator(document)
    exit_code, decision = _validate_document(document, tmp_path)
    assert exit_code == 2
    if structural:
        assert decision["structural_valid"] is False
    else:
        assert "required_sample_count_per_arm_mismatch" in decision["errors"]


def test_exact_enumeration_algorithm():
    # n is enumerated incrementally: n=21 dips below n=20, so binary search
    # would be unjustified; the first >= 0.80 is n=22.
    assert validator.binomial_pmf(3, 0, 0.0) == 1.0
    assert validator.binomial_pmf(3, 3, 1.0) == 1.0
    assert validator.binomial_pmf(3, 4, 0.5) == 0.0
    assert validator.exact_power_for_n(20) == pytest.approx(0.790213011479415, abs=1e-12)
    assert validator.exact_power_for_n(21) == pytest.approx(0.7787023565542808, abs=1e-12)
    result = validator.enumerate_static_power_design()
    assert result["status"] == "ok"
    assert result["required_sample_count_per_arm"] == 22
    assert result["power"] == pytest.approx(0.8454900944198372, abs=1e-12)


def test_exact_enumeration_golden_vectors_and_newcombe_boundary():
    point, upper = validator.newcombe_risk_difference_one_sided_upper(0, 22, 0, 22, 0.95)
    assert point == 0.0
    assert upper > 0.0
    assert validator._is_rejected_pair(0, 0, 22, validator.POWER_DESIGNS[validator.POWER_DESIGN_ID]) is True
    lower, upper_cp = validator.clopper_pearson_interval(0, 20, 0.95)
    assert lower == 0.0
    assert upper_cp == pytest.approx(0.1684334709830182, abs=1e-9)
    lower, upper_cp = validator.clopper_pearson_interval(20, 20, 0.95)
    assert upper_cp == 1.0
    assert lower == pytest.approx(0.8315665290169818, abs=1e-9)


@pytest.mark.parametrize(
    "mutator, code",
    [
        (
            lambda item: item["playwright_test_cases"].append(
                {"test_id": "orphan", "workflow_run_id": 9999, "arm": "before", "run_attempt": 1, "outcome": "flaky"}
            ),
            "orphan_playwright_workflow_run_id",
        ),
        (
            lambda item: item["sample_provenance"]["before"]["workflow_failure_rate"]["observations"].__setitem__(
                0, {"workflow_run_id": 1000, "arm": "after", "run_attempt": 1, "classification": "failure"}
            ),
            "mismatched_arm_provenance",
        ),
        (
            lambda item: item["sample_provenance"]["before"]["workflow_failure_rate"]["observations"].append(
                copy.deepcopy(item["sample_provenance"]["before"]["workflow_failure_rate"]["observations"][0])
            ),
            "duplicate_workflow_run_id",
        ),
        (
            lambda item: item["workflow_records"].append(
                {"workflow_run_id": 1000, "arm": "after", "run_attempt": 1, "conclusion": "success"}
            ),
            "cross_arm_same_workflow_run_id",
        ),
        (
            lambda item: item["sample_provenance"]["before"]["workflow_failure_rate"]["observations"].__setitem__(
                0, {"workflow_run_id": 1000, "arm": "before", "run_attempt": 2, "classification": "failure"}
            ),
            "non_attempt_1_sample",
        ),
    ],
)
def test_sample_provenance_rejections(mutator, code, tmp_path):
    document = _make_assessment()
    mutator(document)
    exit_code, decision = _validate_document(document, tmp_path)
    assert exit_code == 2
    assert any(code in error for error in decision["errors"])


def test_legacy_logical_test_denominator_false_green(tmp_path):
    document = _make_assessment()
    # Many tests in one workflow run are not statistical observations.
    document["playwright_test_cases"].extend(
        [
            {
                "test_id": f"many-{number}",
                "workflow_run_id": 1001,
                "arm": "before",
                "run_attempt": 1,
                "outcome": "expected",
            }
            for number in range(100)
        ]
    )
    exit_code, decision = _validate_document(document, tmp_path)
    assert exit_code == 0
    assert decision["recomputed_reliability_metrics"]["before"]["playwright_flaky_test_rate"]["denominator"] == 22
    document["reliability_metrics"]["before"]["playwright_flaky_test_rate"]["denominator"] = 122
    exit_code, decision = _validate_document(document, tmp_path)
    assert exit_code == 2
    assert any("playwright_flaky_test_rate.denominator" in error for error in decision["errors"])


def test_official_playwright_outcome_run_classification(tmp_path):
    document = _make_assessment()
    document["raw_attempts"].append(
        {
            "workflow_run_id": 1002,
            "run_attempt": 1,
            "test_id": "invented-retry",
            "attempt_number": 99,
            "status": "failed",
        }
    )
    exit_code, decision = _validate_document(document, tmp_path)
    assert exit_code == 0
    assert decision["recomputed_reliability_metrics"]["before"]["playwright_flaky_test_rate"]["numerator"] == 1
    document["playwright_test_cases"][0]["outcome"] = "expected"
    exit_code, decision = _validate_document(document, tmp_path)
    assert exit_code == 2
    assert any("canonical_classification_mismatch" in error for error in decision["errors"])


def test_independence_claim_is_not_evidence(tmp_path):
    document = _make_assessment()
    document["independence_claim"] = {"class": "independent", "evidence": True}
    exit_code, decision = _validate_document(document, tmp_path)
    assert exit_code == 2
    assert decision["structural_valid"] is False


def test_main_writes_result_and_fixture_is_valid(tmp_path):
    fixture = os.path.join(FIXTURES_DIR, "valid_fixed_design_workflow_runs.json")
    output = tmp_path / "result.json"
    assert validator.main(["--assessment", fixture, "--output", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["schema"] == "CI_TEST_RELIABILITY_ASSESSMENT_V1_VALIDATION_RESULT"
    assert result["power_design"]["required_sample_count_per_arm"] == 22

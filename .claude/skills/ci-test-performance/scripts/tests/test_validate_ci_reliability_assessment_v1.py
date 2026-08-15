"""Tests for validate_ci_reliability_assessment_v1.py

Covers structural validation (JSON Schema), semantic recomputation
(reliability_metrics + non_inferiority_evaluation cross-checked against
raw_attempts), and the Clopper-Pearson exact interval helper.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."
)
sys.path.insert(0, os.path.normpath(SCRIPTS_DIR))

import validate_ci_reliability_assessment_v1 as validator  # noqa: E402

REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..")
)
FIXTURES_DIR = os.path.join(REPO_ROOT, "fixtures", "ci-test-reliability")


def _fixture_path(name: str) -> str:
    return os.path.join(FIXTURES_DIR, name)


def _load_fixture(name: str) -> dict:
    with open(_fixture_path(name), encoding="utf-8") as handle:
        return json.load(handle)


def _validate(name: str, tmp_path) -> tuple[int, dict]:
    output_path = str(tmp_path / "result.json")
    exit_code, decision = validator.validate_assessment(_fixture_path(name))
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(decision, handle)
    return exit_code, decision


# --------------------------------------------------------------------------- #
# Clopper-Pearson exact interval
# --------------------------------------------------------------------------- #
class TestClopperPearsonInterval:
    def test_clopper_pearson_zero_failure_before_cohort_has_finite_nonzero_upper_bound(self):
        lower, upper = validator.clopper_pearson_interval(0, 20, 0.95)
        assert lower == 0.0
        # matches the ~16.8% TWO-sided 95% exact upper bound (this is the
        # value historically cited as "one-sided" in issue #2170's Background
        # -- that labeling was wrong; clopper_pearson_interval() uses alpha/2
        # on each tail, i.e. two-sided. The correct one-sided 95% upper bound
        # for 0/20 is ~13.91% -- see
        # TestNewcombeWilsonRiskDifference.test_clopper_pearson_two_sided_label_matches_math
        # and docs/dev/ci-test-reliability-assessment.md.
        assert upper == pytest.approx(0.1684334709830182, abs=1e-9)

    def test_clopper_pearson_all_failures_upper_bound_is_one(self):
        lower, upper = validator.clopper_pearson_interval(20, 20, 0.95)
        assert upper == 1.0
        assert lower == pytest.approx(0.8315665290169818, abs=1e-9)

    def test_clopper_pearson_zero_denominator_returns_full_interval(self):
        assert validator.clopper_pearson_interval(0, 0, 0.95) == (0.0, 1.0)

    def test_clopper_pearson_midpoint_interval_is_symmetric_around_estimate(self):
        lower, upper = validator.clopper_pearson_interval(10, 20, 0.95)
        assert lower < 0.5 < upper


# --------------------------------------------------------------------------- #
# End-to-end fixture-driven validation
# --------------------------------------------------------------------------- #
class TestPositiveFixtures:
    def test_valid_assessment_recomputed_from_raw_attempts_matches_self_report(self, tmp_path):
        exit_code, decision = _validate(
            "valid_workflow_failure_rate_non_inferior.json", tmp_path
        )
        assert exit_code == 0
        assert decision["structural_valid"] is True
        assert decision["semantic_valid"] is True
        assert decision["errors"] == []
        recomputed = decision["recomputed_reliability_metrics"]
        assert recomputed["before"]["workflow_failure_rate"] == {
            "numerator": 1,
            "denominator": 20,
            "rate": pytest.approx(0.05),
        }
        assert recomputed["after"]["workflow_failure_rate"] == {
            "numerator": 1,
            "denominator": 25,
            "rate": pytest.approx(0.04),
        }

    def test_valid_zero_before_failure_clopper_pearson_non_inferiority_passes(self, tmp_path):
        exit_code, decision = _validate(
            "valid_zero_before_failure_clopper_pearson.json", tmp_path
        )
        assert exit_code == 0
        assert decision["semantic_valid"] is True
        assert (
            decision["recomputed_reliability_metrics"]["before"]["workflow_failure_rate"][
                "numerator"
            ]
            == 0
        )


class TestNegativeFixtures:
    def test_invalid_denominator_mismatch_is_rejected(self, tmp_path):
        exit_code, decision = _validate("invalid_denominator_mismatch.json", tmp_path)
        assert exit_code == 2
        assert decision["semantic_valid"] is False
        assert any("denominator" in err for err in decision["errors"])

    def test_invalid_retry_rerun_confusion_rerun_not_counted_as_flaky(self, tmp_path):
        exit_code, decision = _validate("invalid_retry_rerun_confusion.json", tmp_path)
        assert exit_code == 2
        assert decision["semantic_valid"] is False
        assert any(
            "playwright_flaky_test_rate" in err for err in decision["errors"]
        )
        # the correct recomputation excludes the run_attempt-2 rerun record
        recomputed = decision["recomputed_reliability_metrics"]
        assert recomputed["before"]["playwright_flaky_test_rate"]["numerator"] == 1

    def test_invalid_cancelled_misclassification_cancelled_timed_out_classification(
        self, tmp_path
    ):
        exit_code, decision = _validate(
            "invalid_cancelled_misclassification.json", tmp_path
        )
        assert exit_code == 2
        assert decision["semantic_valid"] is False
        recomputed = decision["recomputed_reliability_metrics"]
        # cancelled run is excluded from both numerator and denominator
        assert recomputed["before"]["workflow_failure_rate"] == {
            "numerator": 1,
            "denominator": 20,
            "rate": pytest.approx(0.05),
        }


class TestClassificationHelpers:
    def test_cancelled_timed_out_classification_is_not_terminal_failure(self):
        assert validator._classify_workflow_run("cancelled") == "infrastructure_failure"
        assert validator._classify_workflow_run("timed_out") == "infrastructure_failure"
        assert validator._classify_workflow_run("action_required") == "infrastructure_failure"
        assert validator._classify_workflow_run("failure") == "terminal_failure"
        assert validator._classify_workflow_run("success") == "success"

    def test_rerun_not_counted_as_flaky_excludes_run_attempt_greater_than_one(self):
        cohort = {
            "workflow_runs": [],
            "playwright_tests": [
                {
                    "test_id": "t1",
                    "workflow_run_id": 1,
                    "run_attempt": 1,
                    "attempts": [
                        {"attempt_number": 1, "status": "failed"},
                    ],
                },
                {
                    # GitHub Actions rerun of the whole workflow -- a *separate*
                    # record at run_attempt 2, not a Playwright internal retry.
                    "test_id": "t1",
                    "workflow_run_id": 1,
                    "run_attempt": 2,
                    "attempts": [
                        {"attempt_number": 1, "status": "passed"},
                    ],
                },
            ],
        }
        recomputed = validator._recompute_metrics_block(cohort)
        # only the run_attempt == 1 record counts: single attempt, failed,
        # no internal retries recorded within that attempt -> terminal_failure,
        # not flaky (the rerun success must not be conflated with a retry).
        assert recomputed["playwright_flaky_test_rate"]["numerator"] == 0
        assert recomputed["playwright_terminal_failure_rate"]["numerator"] == 1

    def test_recomputed_from_raw_attempts_playwright_flaky_requires_earlier_failure(self):
        cohort = {
            "workflow_runs": [],
            "playwright_tests": [
                {
                    "test_id": "flaky-test",
                    "workflow_run_id": 1,
                    "run_attempt": 1,
                    "attempts": [
                        {"attempt_number": 1, "status": "failed"},
                        {"attempt_number": 2, "status": "passed"},
                    ],
                }
            ],
        }
        recomputed = validator._recompute_metrics_block(cohort)
        assert recomputed["playwright_flaky_test_rate"] == {
            "numerator": 1,
            "denominator": 1,
            "rate": 1.0,
        }


class TestStructuralValidation:
    def test_structural_missing_required_property_is_rejected(self, tmp_path):
        base = _load_fixture("valid_workflow_failure_rate_non_inferior.json")
        del base["sample_identity"]
        broken_path = tmp_path / "structural_missing_required.json"
        broken_path.write_text(json.dumps(base), encoding="utf-8")
        exit_code, decision = validator.validate_assessment(str(broken_path))
        assert exit_code == 2
        assert decision["structural_valid"] is False

    def test_structural_wrong_schema_const_is_rejected(self, tmp_path):
        base = _load_fixture("valid_workflow_failure_rate_non_inferior.json")
        base["schema"] = "SOME_OTHER_SCHEMA"
        broken_path = tmp_path / "structural_wrong_schema.json"
        broken_path.write_text(json.dumps(base), encoding="utf-8")
        exit_code, decision = validator.validate_assessment(str(broken_path))
        assert exit_code == 2
        assert decision["structural_valid"] is False

    def test_structural_duplicate_json_key_is_operational_failure(self, tmp_path):
        broken_path = tmp_path / "structural_duplicate_key.json"
        broken_path.write_text('{"a": 1, "a": 2}', encoding="utf-8")
        exit_code, decision = validator.validate_assessment(str(broken_path))
        assert exit_code == 3
        assert "operational_error" in decision


class TestP0RegressionFindings:
    """Regression tests for the OWNER adversarial review on PR #2175 (two
    P0 false-green findings: non-inferiority not a real two-sample test,
    and sample_identity.key = workflow_run_id not enforced)."""

    def test_duplicate_workflow_run_id_is_rejected(self):
        cohort = {
            "workflow_runs": [
                {"workflow_run_id": 1, "run_attempt": 1, "conclusion": "failure"},
                {"workflow_run_id": 1, "run_attempt": 1, "conclusion": "success"},
            ],
            "playwright_tests": [],
        }
        errors: list[str] = []
        validator._check_duplicate_sample_identities(cohort, "before", errors)
        assert any("duplicate_workflow_run_id" in err for err in errors)
        assert any("duplicate_workflow_run_record" in err for err in errors)

    def test_duplicate_playwright_test_record_is_rejected(self):
        cohort = {
            "workflow_runs": [],
            "playwright_tests": [
                {
                    "test_id": "t1",
                    "workflow_run_id": 1,
                    "run_attempt": 1,
                    "attempts": [{"attempt_number": 1, "status": "passed"}],
                },
                {
                    "test_id": "t1",
                    "workflow_run_id": 1,
                    "run_attempt": 1,
                    "attempts": [{"attempt_number": 1, "status": "passed"}],
                },
            ],
        }
        errors: list[str] = []
        validator._check_duplicate_sample_identities(cohort, "before", errors)
        assert any("duplicate_playwright_test_record" in err for err in errors)

    def test_duplicate_inflation_cannot_dilute_workflow_failure_rate(self):
        # A producer duplicating the same low-failure record 99x must not
        # be able to silently dilute the recomputed failure rate -- the
        # duplication itself must be surfaced as a hard error, even though
        # naive summation would recompute a diluted (and self-consistent)
        # rate.
        cohort = {
            "workflow_runs": [
                {"workflow_run_id": 1, "run_attempt": 1, "conclusion": "failure"}
            ]
            + [
                {"workflow_run_id": 2, "run_attempt": 1, "conclusion": "success"}
                for _ in range(99)
            ],
            "playwright_tests": [],
        }
        errors: list[str] = []
        validator._check_duplicate_sample_identities(cohort, "before", errors)
        # 99 identical duplicate records of workflow_run_id=2 -- rejected.
        assert any("workflow_run_id=2" in err for err in errors)

    def test_before_sample_count_below_required_is_inconclusive(self):
        before_field = {"numerator": 0, "denominator": 5, "rate": 0.0}
        after_field = {"numerator": 0, "denominator": 25, "rate": 0.0}
        result = validator.evaluate_non_inferiority(
            before_field, after_field, confidence_level=0.95, margin=0.2,
            required_sample_count=20,
        )
        assert result["outcome"] == "inconclusive"
        assert result["point_estimate"] is None
        assert result["ci_upper"] is None

    def test_two_sample_noninferiority_rejects_false_green(self):
        # Exact false-PASS scenario from the OWNER adversarial review:
        # before=1 failure/1 run, after=20 failures/20 runs, margin=0.01.
        # The old single-arm-only logic reported non_inferior. The fixed
        # two-sample logic must NOT report non_inferior.
        before_field = {"numerator": 1, "denominator": 1, "rate": 1.0}
        after_field = {"numerator": 20, "denominator": 20, "rate": 1.0}
        result = validator.evaluate_non_inferiority(
            before_field, after_field, confidence_level=0.95, margin=0.01,
            required_sample_count=20,
        )
        assert result["outcome"] != "non_inferior"
        # before's denominator (1) is below required_sample_count (20) --
        # correctly forced inconclusive rather than any spurious PASS.
        assert result["outcome"] == "inconclusive"

    def test_two_sample_noninferiority_detects_genuine_regression(self):
        # Both arms meet required_sample_count; after is genuinely much
        # worse than before -- must be "inferior", not "non_inferior".
        before_field = {"numerator": 1, "denominator": 20, "rate": 0.05}
        after_field = {"numerator": 18, "denominator": 20, "rate": 0.9}
        result = validator.evaluate_non_inferiority(
            before_field, after_field, confidence_level=0.95, margin=0.2,
            required_sample_count=20,
        )
        assert result["outcome"] == "inferior"

    def test_expected_failure_is_not_terminal_regression(self):
        # A Playwright test.fail() style test: the attempt's status
        # ("failed") matches its own expected_status ("failed"). This must
        # be classified as "success" (an expected result), never as
        # terminal_failure, even though the raw status literal is "failed".
        record = {
            "test_id": "e2e/expected-fail.spec.ts::always-fails",
            "workflow_run_id": 1,
            "run_attempt": 1,
            "attempts": [
                {"attempt_number": 1, "status": "failed", "expected_status": "failed"}
            ],
        }
        assert validator._classify_playwright_test(record) == "success"

    def test_unexpected_pass_of_expected_fail_test_is_not_silently_success(self):
        # An expected-to-fail test that unexpectedly passes: status
        # ("passed") does not match expected_status ("failed") -- this is
        # NOT a matching/expected outcome. The final-attempt branch
        # (final_expected is False, final status is "passed", not in
        # _TERMINAL_FAILURE_TEST_STATUSES) is excluded rather than
        # silently counted as a normal success, since it does not match
        # the declared expectation.
        record = {
            "test_id": "e2e/expected-fail.spec.ts::unexpectedly-passes",
            "workflow_run_id": 1,
            "run_attempt": 1,
            "attempts": [
                {"attempt_number": 1, "status": "passed", "expected_status": "failed"}
            ],
        }
        assert validator._classify_playwright_test(record) == "excluded"

    def test_expected_status_absent_preserves_prior_behavior(self):
        # No expected_status present -- defaults to "passed", identical to
        # pre-fix classification.
        record = {
            "test_id": "e2e/normal.spec.ts::normal",
            "workflow_run_id": 1,
            "run_attempt": 1,
            "attempts": [{"attempt_number": 1, "status": "failed"}],
        }
        assert validator._classify_playwright_test(record) == "terminal_failure"

    def test_attempt_numbers_must_be_unique_and_ordered(self):
        cohort = {
            "workflow_runs": [],
            "playwright_tests": [
                {
                    "test_id": "t1",
                    "workflow_run_id": 1,
                    "run_attempt": 1,
                    "attempts": [
                        {"attempt_number": 2, "status": "passed"},
                        {"attempt_number": 1, "status": "failed"},
                    ],
                }
            ],
        }
        errors: list[str] = []
        validator._check_attempt_number_ordering(cohort, "before", errors)
        assert any("attempt_number_not_unique_or_ordered" in err for err in errors)

    def test_attempt_numbers_duplicate_is_rejected(self):
        cohort = {
            "workflow_runs": [],
            "playwright_tests": [
                {
                    "test_id": "t1",
                    "workflow_run_id": 1,
                    "run_attempt": 1,
                    "attempts": [
                        {"attempt_number": 1, "status": "failed"},
                        {"attempt_number": 1, "status": "passed"},
                    ],
                }
            ],
        }
        errors: list[str] = []
        validator._check_attempt_number_ordering(cohort, "before", errors)
        assert any("attempt_number_not_unique_or_ordered" in err for err in errors)

    def test_attempt_numbers_ordered_and_unique_is_not_rejected(self):
        cohort = {
            "workflow_runs": [],
            "playwright_tests": [
                {
                    "test_id": "t1",
                    "workflow_run_id": 1,
                    "run_attempt": 1,
                    "attempts": [
                        {"attempt_number": 1, "status": "failed"},
                        {"attempt_number": 2, "status": "passed"},
                    ],
                }
            ],
        }
        errors: list[str] = []
        validator._check_attempt_number_ordering(cohort, "before", errors)
        assert errors == []

    def test_fixed_declared_sample_count_forces_is_power_derived_false_structurally(
        self, tmp_path
    ):
        base = _load_fixture("valid_workflow_failure_rate_non_inferior.json")
        base["sample_count_rule"] = {
            "method": "fixed_declared",
            "is_power_derived": True,
            "required_sample_count": 20,
            "justification": "fixed sample size, not power-derived",
        }
        broken_path = tmp_path / "fixed_declared_wrong_is_power_derived.json"
        broken_path.write_text(json.dumps(base), encoding="utf-8")
        exit_code, decision = validator.validate_assessment(str(broken_path))
        assert exit_code == 2
        assert decision["structural_valid"] is False


class TestNewcombeWilsonRiskDifference:
    def test_wilson_score_one_sided_zero_failure_lower_bound_is_zero(self):
        lower, _ = validator._wilson_score_interval_one_sided(0, 20, 0.95)
        assert lower == 0.0

    def test_newcombe_risk_difference_identical_rates_upper_bound_is_positive(self):
        point_estimate, ci_upper = validator.newcombe_risk_difference_one_sided_upper(
            10, 20, 10, 20, 0.95
        )
        assert point_estimate == pytest.approx(0.0)
        assert ci_upper > 0.0

    def test_clopper_pearson_two_sided_label_matches_math(self):
        # 0/20 two-sided 95% CP upper endpoint (alpha/2 tail) is ~16.84%,
        # NOT the one-sided 95% upper bound (~13.91%). This is the P1
        # sidedness finding from the OWNER adversarial review -- the
        # existing clopper_pearson_interval() implementation is correct
        # (it computes the two-sided interval), but its docstring/tests
        # must not claim "one-sided".
        _, two_sided_upper = validator.clopper_pearson_interval(0, 20, 0.95)
        assert two_sided_upper == pytest.approx(0.1684334709830182, abs=1e-9)


class TestMainCLI:
    def test_main_writes_output_file_and_returns_exit_code(self, tmp_path):
        output_path = tmp_path / "result.json"
        exit_code = validator.main(
            [
                "--assessment",
                _fixture_path("valid_workflow_failure_rate_non_inferior.json"),
                "--output",
                str(output_path),
            ]
        )
        assert exit_code == 0
        with open(output_path, encoding="utf-8") as handle:
            result = json.load(handle)
        assert result["schema"] == "CI_TEST_RELIABILITY_ASSESSMENT_V1_VALIDATION_RESULT"
        assert result["semantic_valid"] is True

    def test_main_file_not_found_returns_operational_failure(self, tmp_path):
        output_path = tmp_path / "result.json"
        exit_code = validator.main(
            [
                "--assessment",
                str(tmp_path / "does_not_exist.json"),
                "--output",
                str(output_path),
            ]
        )
        assert exit_code == 3

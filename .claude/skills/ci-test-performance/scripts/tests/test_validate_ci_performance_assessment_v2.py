"""
Test suite for validate_ci_performance_assessment_v2.py.

Verifies:
- AC1: schemas/ci_test_performance_assessment_v2.schema.json and
  schemas/ci_runtime_delta_v2.schema.json are valid Draft 2020-12 schemas,
  ci_runtime_delta_v2 branches on mode (comparative/absolute), and
  CI_TEST_PERFORMANCE_DECISION_V1 (decision-matrix.md) is unchanged.
- AC2: the CLI (--assessment/--output) returns 0=valid / 2=structural or
  semantic invalid / 3=operational failure, and strict JSON parsing rejects
  duplicate keys / NaN / Infinity.
- AC3: the 4-axis claim/performance_evidence/observation/claim_evaluation
  state machine, functional smoke provenance floor, cohort comparability,
  and risk_acknowledgement fixtures all resolve to the expected
  structural_valid/semantic_valid/approval_eligible tuple.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[5]
SCRIPT_DIR = Path(__file__).parents[1]
SCRIPT_PATH = SCRIPT_DIR / "validate_ci_performance_assessment_v2.py"
FIXTURE_DIR = SCRIPT_DIR / "fixtures"

ASSESSMENT_SCHEMA_PATH = (
    REPO_ROOT / "schemas" / "ci_test_performance_assessment_v2.schema.json"
)
RUNTIME_DELTA_SCHEMA_PATH = REPO_ROOT / "schemas" / "ci_runtime_delta_v2.schema.json"
DECISION_MATRIX_PATH = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "ci-test-performance"
    / "references"
    / "decision-matrix.md"
)
RUNTIME_DELTA_TEMPLATE_PATH = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "ci-test-performance"
    / "templates"
    / "runtime-delta.md"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "validate_ci_performance_assessment_v2_under_test", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_module()


def _run_cli(assessment_path: Path, output_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--assessment",
            str(assessment_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )


# --------------------------------------------------------------------------- #
# AC1: schema files exist / are valid Draft 2020-12 / V1 untouched
# --------------------------------------------------------------------------- #
class TestSchemaFiles:
    def test_assessment_schema_exists(self):
        assert ASSESSMENT_SCHEMA_PATH.exists()

    def test_runtime_delta_schema_exists(self):
        assert RUNTIME_DELTA_SCHEMA_PATH.exists()

    def test_assessment_schema_is_valid_draft_2020_12(self):
        from jsonschema import Draft202012Validator

        with ASSESSMENT_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)

    def test_runtime_delta_schema_is_valid_draft_2020_12(self):
        from jsonschema import Draft202012Validator

        with RUNTIME_DELTA_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)

    def test_assessment_schema_closed(self):
        with ASSESSMENT_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        assert schema.get("unevaluatedProperties") is False

    def test_runtime_delta_schema_closed(self):
        with RUNTIME_DELTA_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        assert schema.get("unevaluatedProperties") is False

    def test_runtime_delta_schema_mode_branches_comparative_vs_absolute(self):
        from jsonschema import Draft202012Validator

        with RUNTIME_DELTA_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        validator_ = Draft202012Validator(schema)

        comparative_missing_before = {
            "schema": "ci_runtime_delta_v2",
            "schema_version": 2,
            "issue_number": 1724,
            "pr_number": 1730,
            "mode": "comparative",
            "after": {
                "job": "python-test",
                "p50_seconds": 1.0,
                "p95_seconds": 2.0,
                "run_ids": ["1"],
                "run_count": 1,
                "runner_image": "ubuntu-24.04/x",
                "workers": 4,
                "scheduler": "loadscope",
                "command_manifest_digest": "sha256:" + "a" * 64,
                "test_selection_digest": "sha256:" + "b" * 64,
            },
            "outlier_exclusions": [],
        }
        assert list(validator_.iter_errors(comparative_missing_before))

        absolute_after_and_budget_only = {
            "schema": "ci_runtime_delta_v2",
            "schema_version": 2,
            "issue_number": 1724,
            "pr_number": 1730,
            "mode": "absolute",
            "after": comparative_missing_before["after"],
            "budget": {"metric": "p95_seconds", "maximum_value_ms": 900000},
            "outlier_exclusions": [],
        }
        assert not list(validator_.iter_errors(absolute_after_and_budget_only))


# --------------------------------------------------------------------------- #
# AC1: CI_TEST_PERFORMANCE_DECISION_V1 (decision-matrix.md) content unchanged
# --------------------------------------------------------------------------- #
def _normalize(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip() + "\n"


# Golden normalized snapshot of the enum/required-key defining lines of
# CI_TEST_PERFORMANCE_DECISION_V1. This snapshot is intentionally narrow
# (exact strings, not a broad rg substring) so that any change to the V1
# enum or required-key set fails this test rather than being silently
# absorbed by a loose `rg` identifier check.
V1_DECISION_MATRIX_GOLDEN_LINES = [
    "## CI_TEST_PERFORMANCE_DECISION_V1 完全スキーマ定義",
    "CI_TEST_PERFORMANCE_DECISION_V1:",
    "  schema: CI_TEST_PERFORMANCE_DECISION_V1",
    "  decision_scope: docs_only | ci_change | dependency_change | review_only",
    "    fast_static:",
    "    python_unit:",
    "    contract_artifact:",
    "    integration:",
    "    ci_runtime_baseline_v1_available: true | false",
    "  reviewer_gate:",
    "    approve_allowed: true | false",
    "      - TEST_VERDICT_MACHINE            # test-runner による TEST_VERDICT_MACHINE/v1",
    "      - CI_CHECK_RUN_SCOPED             # GitHub CI check の成功",
]

V1_RUNTIME_DELTA_TEMPLATE_GOLDEN_LINES = [
    "ci_runtime_delta_v1:",
    "  baseline_source: ci_runtime_baseline_v1",
    "    verdict: improved | regression | no_change | insufficient_data",
]


class TestV1FenceSnapshot:
    """B5-equivalent snapshot: catches enum/required-key drift in V1 that a
    plain `rg -n CI_TEST_PERFORMANCE_DECISION_V1` identifier check would miss.
    """

    def test_decision_matrix_v1_golden_lines_present_verbatim(self):
        normalized = _normalize(DECISION_MATRIX_PATH.read_text(encoding="utf-8"))
        normalized_lines = set(normalized.splitlines())
        missing = [
            line for line in V1_DECISION_MATRIX_GOLDEN_LINES if line not in normalized_lines
        ]
        assert not missing, f"V1 decision-matrix.md golden lines missing/changed: {missing}"

    def test_runtime_delta_template_v1_golden_lines_present_verbatim(self):
        normalized = _normalize(RUNTIME_DELTA_TEMPLATE_PATH.read_text(encoding="utf-8"))
        normalized_lines = set(normalized.splitlines())
        missing = [
            line
            for line in V1_RUNTIME_DELTA_TEMPLATE_GOLDEN_LINES
            if line not in normalized_lines
        ]
        assert not missing, f"V1 runtime-delta.md golden lines missing/changed: {missing}"


# --------------------------------------------------------------------------- #
# AC2: strict JSON parsing
# --------------------------------------------------------------------------- #
class TestStrictJsonParsing:
    def test_duplicate_key_raises(self):
        with pytest.raises(validator.StrictJSONError):
            validator.strict_json_loads('{"a": 1, "a": 2}')

    def test_nan_raises(self):
        with pytest.raises(validator.StrictJSONError):
            validator.strict_json_loads('{"a": NaN}')

    def test_infinity_raises(self):
        with pytest.raises(validator.StrictJSONError):
            validator.strict_json_loads('{"a": Infinity}')

    def test_negative_infinity_raises(self):
        with pytest.raises(validator.StrictJSONError):
            validator.strict_json_loads('{"a": -Infinity}')

    def test_syntax_error_raises(self):
        with pytest.raises(validator.StrictJSONError):
            validator.strict_json_loads("{not json")

    def test_well_formed_json_parses(self):
        assert validator.strict_json_loads('{"a": 1}') == {"a": 1}


# --------------------------------------------------------------------------- #
# AC2/AC3: fixture -> exit code / decision contract (function-level)
# --------------------------------------------------------------------------- #
# fixture_name -> (expected_exit_code, expected_structural_valid,
#                   expected_semantic_valid, expected_approval_eligible)
FIXTURE_EXPECTATIONS: dict[str, tuple[int, bool, bool, bool]] = {
    "valid_no_claim_not_instrumented.json": (0, True, True, True),
    "valid_no_claim_observed_regression.json": (0, True, True, True),
    "valid_insufficient_samples_structurally_valid_gate_blocked.json": (
        0,
        True,
        True,
        False,
    ),
    "warning_docs_only_with_runtime_delta.json": (0, True, True, True),
    "invalid_no_claim_improved_observation.json": (2, True, False, False),
    "invalid_improvement_claim_insufficient_samples.json": (2, True, False, False),
    "invalid_non_regression_claim_incomparable_cohort.json": (2, True, False, False),
    "invalid_insufficient_samples_observation_improved.json": (2, True, False, False),
    "invalid_no_claim_claim_evaluation_satisfied.json": (2, True, False, False),
    "functional_zero_selected_checks.json": (0, True, True, False),
    "functional_synthetic_needs_result.json": (0, True, True, False),
    "functional_missing_check_run_id.json": (0, True, True, False),
    "functional_stale_null_head_sha.json": (0, True, True, False),
    "functional_skipped_neutral_conclusion.json": (0, True, True, False),
    "structural_missing_required_property.json": (2, False, False, False),
    "structural_unknown_property.json": (2, False, False, False),
    "structural_wrong_schema_id.json": (2, False, False, False),
    "structural_invalid_sha_digest.json": (2, False, False, False),
    "structural_duplicate_json_key.json": (3, None, None, None),
    "structural_nan_infinity.json": (3, None, None, None),
}


def test_all_fixture_files_are_covered_by_expectations():
    actual_fixture_names = {p.name for p in FIXTURE_DIR.glob("*.json")}
    assert actual_fixture_names == set(FIXTURE_EXPECTATIONS.keys())


@pytest.mark.parametrize("fixture_name", sorted(FIXTURE_EXPECTATIONS.keys()))
def test_fixture_exit_code_and_decision(fixture_name: str, tmp_path):
    fixture_path = FIXTURE_DIR / fixture_name
    assert fixture_path.exists(), fixture_path
    (
        expected_exit,
        expected_structural,
        expected_semantic,
        expected_approval,
    ) = FIXTURE_EXPECTATIONS[fixture_name]

    exit_code, decision = validator.validate_assessment(str(fixture_path))
    assert exit_code == expected_exit, f"{fixture_name}: {decision}"

    if expected_exit == 3:
        assert "operational_error" in decision
        return

    assert decision["structural_valid"] is expected_structural, fixture_name
    assert decision["semantic_valid"] is expected_semantic, fixture_name
    assert decision["approval_eligible"] is expected_approval, fixture_name


# --------------------------------------------------------------------------- #
# AC2: CLI-level (subprocess) contract — exit code + --output file content
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fixture_name,expected_exit",
    [(name, exp[0]) for name, exp in FIXTURE_EXPECTATIONS.items()],
)
def test_cli_exit_code_matches_expectation(fixture_name, expected_exit, tmp_path):
    fixture_path = FIXTURE_DIR / fixture_name
    output_path = tmp_path / "result.json"
    proc = _run_cli(fixture_path, output_path)
    assert proc.returncode == expected_exit, proc.stderr
    assert output_path.exists()
    with output_path.open(encoding="utf-8") as handle:
        result = json.load(handle)
    assert result["exit_code"] == expected_exit
    assert result["schema"] == "CI_TEST_PERFORMANCE_ASSESSMENT_V2_VALIDATION_RESULT"


def test_cli_missing_assessment_file_is_operational_failure(tmp_path):
    output_path = tmp_path / "result.json"
    proc = _run_cli(tmp_path / "does-not-exist.json", output_path)
    assert proc.returncode == 3


def test_cli_requires_assessment_and_output_flags():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)], capture_output=True, text=True
    )
    assert proc.returncode != 0


# --------------------------------------------------------------------------- #
# AC3: semantic rule codes (specific, not just pass/fail)
# --------------------------------------------------------------------------- #
class TestSemanticErrorCodes:
    def test_no_claim_improved_observation_error_code(self):
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "invalid_no_claim_improved_observation.json")
        )
        assert "no_claim_but_observation_implies_active_claim" in decision["errors"]

    def test_improvement_claim_insufficient_samples_error_code(self):
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "invalid_improvement_claim_insufficient_samples.json")
        )
        assert "improvement_claim_insufficient_samples" in decision["errors"]

    def test_non_regression_claim_incomparable_cohort_error_code(self):
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "invalid_non_regression_claim_incomparable_cohort.json")
        )
        assert "non_regression_claim_incomparable_cohort" in decision["errors"]

    def test_insufficient_samples_observation_improved_error_code(self):
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "invalid_insufficient_samples_observation_improved.json")
        )
        assert "insufficient_samples_observation_improved" in decision["errors"]

    def test_no_claim_claim_evaluation_satisfied_error_code(self):
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "invalid_no_claim_claim_evaluation_satisfied.json")
        )
        assert "no_claim_claim_evaluation_must_be_not_applicable" in decision["errors"]

    def test_docs_only_runtime_delta_warning_code(self):
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "warning_docs_only_with_runtime_delta.json")
        )
        assert "docs_only_change_with_runtime_delta_reported" in decision["warnings"]

    def test_no_claim_observed_regression_is_warning_not_error(self):
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "valid_no_claim_observed_regression.json")
        )
        assert decision["errors"] == []
        assert "no_claim_observed_regression_review_recommended" in decision["warnings"]

    def test_insufficient_samples_gate_blocked_blocker_code(self):
        _, decision = validator.validate_assessment(
            str(
                FIXTURE_DIR
                / "valid_insufficient_samples_structurally_valid_gate_blocked.json"
            )
        )
        assert "insufficient_samples_gate_blocked" in decision["blockers"]

    @pytest.mark.parametrize(
        "fixture_name,expected_blocker",
        [
            ("functional_zero_selected_checks.json", "functional_evidence_zero_selected_checks"),
            (
                "functional_synthetic_needs_result.json",
                "functional_evidence_synthetic_needs_result",
            ),
            (
                "functional_missing_check_run_id.json",
                "functional_evidence_missing_or_invalid_check_run_id",
            ),
            (
                "functional_stale_null_head_sha.json",
                "functional_evidence_stale_or_null_head_sha",
            ),
            (
                "functional_skipped_neutral_conclusion.json",
                "functional_evidence_skipped_or_neutral_conclusion",
            ),
        ],
    )
    def test_functional_evidence_blocker_codes(self, fixture_name, expected_blocker):
        _, decision = validator.validate_assessment(str(FIXTURE_DIR / fixture_name))
        assert expected_blocker in decision["blockers"]


# --------------------------------------------------------------------------- #
# AC3: usable-check floor (check_run_id>0, completed/success, head match,
# classification in [required, evidence], not synthetic)
# --------------------------------------------------------------------------- #
class TestUsableChecksFloor:
    def test_usable_check_passes_floor(self):
        checks = [
            {
                "check_run_id": 1,
                "status": "completed",
                "conclusion": "success",
                "head_sha_match": True,
                "classification": "required",
            }
        ]
        assert validator._usable_checks(checks) == checks

    def test_zero_check_run_id_is_not_usable(self):
        checks = [
            {
                "check_run_id": 0,
                "status": "completed",
                "conclusion": "success",
                "head_sha_match": True,
                "classification": "required",
            }
        ]
        assert validator._usable_checks(checks) == []

    def test_advisory_classification_is_not_usable(self):
        checks = [
            {
                "check_run_id": 1,
                "status": "completed",
                "conclusion": "success",
                "head_sha_match": True,
                "classification": "advisory",
            }
        ]
        assert validator._usable_checks(checks) == []

    def test_bool_check_run_id_is_not_usable(self):
        # bool is a subclass of int in Python; must not be treated as a valid id.
        checks = [
            {
                "check_run_id": True,
                "status": "completed",
                "conclusion": "success",
                "head_sha_match": True,
                "classification": "required",
            }
        ]
        assert validator._usable_checks(checks) == []


# --------------------------------------------------------------------------- #
# AC1: existing CI_TEST_PERFORMANCE_DECISION_V1 identifier still present
# (kept as a coarse smoke check in addition to the golden-line snapshot above)
# --------------------------------------------------------------------------- #
def test_v1_identifier_still_present_in_decision_matrix():
    text = DECISION_MATRIX_PATH.read_text(encoding="utf-8")
    assert re.search(r"CI_TEST_PERFORMANCE_DECISION_V1", text)

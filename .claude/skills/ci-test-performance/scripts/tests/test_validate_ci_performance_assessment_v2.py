"""
Test suite for validate_ci_performance_assessment_v2.py.

Verifies:
- AC1: schemas/ci_test_performance_assessment_v2.schema.json and
  schemas/ci_runtime_delta_v2.schema.json are valid Draft 2020-12 schemas,
  ci_runtime_delta_v2 branches on mode (comparative/absolute) with the
  opposite branch's properties forbidden, and CI_TEST_PERFORMANCE_DECISION_V1
  (decision-matrix.md) is unchanged (exact fenced-block digest, not a loose
  identifier/line-membership check).
- AC2: the CLI (--assessment/--output/--ci-verdict-summary/
  --expected-head-sha/--expected-artifact-digest) returns 0=valid /
  2=structural or semantic invalid / 3=operational failure, and strict JSON
  parsing rejects duplicate keys / NaN / Infinity.
- AC3: the 4-axis claim/performance_evidence/observation/claim_evaluation
  state machine (as a truth table, not ad-hoc if-statements), functional
  smoke provenance floor cross-checked against a trusted out-of-band
  ci_verdict_summary_v2 artifact (never trusting the assessment's own
  self-report alone), cohort comparability (including delta
  recomputation from before/after p50/p95), and risk_acknowledgement
  fixtures all resolve to the expected
  structural_valid/semantic_valid/approval_eligible tuple.
"""
from __future__ import annotations

import hashlib
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
ARTIFACT_FIXTURE_DIR = FIXTURE_DIR / "trusted_artifacts"

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
PACKAGE_JSON_PATH = REPO_ROOT / "package.json"

TRUSTED_ARTIFACT_PATH = ARTIFACT_FIXTURE_DIR / "trusted_ci_verdict_summary_v2_artifact.json"
TRUSTED_ARTIFACT_HEAD_SHA = "1234567890abcdef1234567890abcdef12345678"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "validate_ci_performance_assessment_v2_under_test", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_module()


def _run_cli(assessment_path: Path, output_path: Path, extra_args=None):
    args = [
        sys.executable,
        str(SCRIPT_PATH),
        "--assessment",
        str(assessment_path),
        "--output",
        str(output_path),
    ]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(args, capture_output=True, text=True)


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

    def test_assessment_schema_has_no_decision_input_property(self):
        """P1-2: `decision` must never be an author-suppliable input field."""
        with ASSESSMENT_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        assert "decision" not in schema.get("properties", {})
        assert "Decision" not in schema.get("$defs", {})

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

    def test_runtime_delta_schema_rejects_absolute_with_before_or_delta(self):
        """P1-3: mode=absolute must forbid before/delta, not just leave them
        structurally reachable via unevaluatedProperties: false alone."""
        from jsonschema import Draft202012Validator

        with RUNTIME_DELTA_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        validator_ = Draft202012Validator(schema)

        after_cohort = {
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
        }
        absolute_with_before = {
            "schema": "ci_runtime_delta_v2",
            "schema_version": 2,
            "issue_number": 1724,
            "pr_number": 1730,
            "mode": "absolute",
            "before": after_cohort,
            "after": after_cohort,
            "budget": {"metric": "p95_seconds", "maximum_value_ms": 900000},
            "outlier_exclusions": [],
        }
        assert list(validator_.iter_errors(absolute_with_before))

    def test_runtime_delta_schema_rejects_comparative_with_budget(self):
        from jsonschema import Draft202012Validator

        with RUNTIME_DELTA_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        validator_ = Draft202012Validator(schema)

        cohort = {
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
        }
        comparative_with_budget = {
            "schema": "ci_runtime_delta_v2",
            "schema_version": 2,
            "issue_number": 1724,
            "pr_number": 1730,
            "mode": "comparative",
            "before": cohort,
            "after": cohort,
            "delta": {
                "p50_delta_seconds": 0.0,
                "p95_delta_seconds": 0.0,
                "p50_improvement_pct": 0.0,
                "p95_improvement_pct": 0.0,
            },
            "budget": {"metric": "p95_seconds", "maximum_value_ms": 900000},
            "outlier_exclusions": [],
        }
        assert list(validator_.iter_errors(comparative_with_budget))

    def test_run_ids_must_be_unique(self):

        with RUNTIME_DELTA_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        cohort_schema = schema["$defs"]["Cohort"]
        assert cohort_schema["properties"]["run_ids"]["uniqueItems"] is True

    def test_measured_at_uses_date_time_format(self):
        with ASSESSMENT_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        assert schema["properties"]["measured_at"]["format"] == "date-time"

    def test_invalid_measured_at_is_rejected_by_validator(self, tmp_path):
        assessment = json.loads(
            (FIXTURE_DIR / "valid_no_claim_not_instrumented.json").read_text(
                encoding="utf-8"
            )
        )
        assessment["measured_at"] = "not-a-date"
        bad_path = tmp_path / "bad_measured_at.json"
        bad_path.write_text(json.dumps(assessment), encoding="utf-8")
        exit_code, decision = validator.validate_assessment(str(bad_path))
        assert exit_code == 2
        assert decision["structural_valid"] is False

    def test_claim_kind_none_forbids_threshold_fields(self):
        from jsonschema import Draft202012Validator

        with ASSESSMENT_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        claim_schema = {"$defs": schema["$defs"], **schema["$defs"]["Claim"]}
        validator_ = Draft202012Validator(claim_schema)
        assert list(validator_.iter_errors({"kind": "none", "metric": "x"}))

    def test_claim_kind_improvement_requires_minimum_improvement_pct(self):
        from jsonschema import Draft202012Validator

        with ASSESSMENT_SCHEMA_PATH.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        claim_schema = {"$defs": schema["$defs"], **schema["$defs"]["Claim"]}
        validator_ = Draft202012Validator(claim_schema)
        assert list(
            validator_.iter_errors({"kind": "improvement", "metric": "x"})
        )
        assert not list(
            validator_.iter_errors(
                {
                    "kind": "improvement",
                    "metric": "x",
                    "minimum_improvement_pct": 5.0,
                }
            )
        )


# --------------------------------------------------------------------------- #
# AC1: CI_TEST_PERFORMANCE_DECISION_V1 (decision-matrix.md) content unchanged
# --------------------------------------------------------------------------- #
def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip() + "\n"


def _extract_first_fenced_block(text: str, marker: str) -> str:
    """Extracts the full contents of the first ```yaml fenced block whose
    first non-blank line starts with `marker`. Unlike a loose `rg`
    identifier/substring search, this captures the entire block verbatim
    (line order, all enum/required-key lines) so any drift is detected."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    fence_starts = [i for i, line in enumerate(lines) if line.strip() == "```yaml"]
    for start in fence_starts:
        end = None
        for j in range(start + 1, len(lines)):
            if lines[j].strip() == "```":
                end = j
                break
        if end is None:
            continue
        block = lines[start + 1 : end]
        if block and block[0].startswith(marker):
            return "\n".join(line.rstrip() for line in block).strip() + "\n"
    raise AssertionError(f"no fenced yaml block starting with {marker!r} found")


# Golden line membership snapshot (coarse, kept for readability of failures).
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

# Exact fenced-block digests (P1-4). Any single-character change to the
# CI_TEST_PERFORMANCE_DECISION_V1 / ci_runtime_delta_v1 fenced blocks --
# enum value, required key, or line order -- changes this hash.
V1_DECISION_MATRIX_FENCE_SHA256 = (
    "f69c51ec3498b6a1561d795f756f1b31370626c5b8545b7608d93097684e7156"
)
V1_RUNTIME_DELTA_TEMPLATE_FENCE_SHA256 = (
    "1d77239dc13cd1aed62962038c7a38b52374b25d85f907d9d13c6bc5f79d639d"
)


class TestV1FenceSnapshot:
    """Catches enum/required-key/line-order drift in V1 that a plain
    `rg -n CI_TEST_PERFORMANCE_DECISION_V1` identifier check would miss.
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

    def test_decision_matrix_v1_fenced_block_exact_digest(self):
        block = _extract_first_fenced_block(
            DECISION_MATRIX_PATH.read_text(encoding="utf-8"),
            "CI_TEST_PERFORMANCE_DECISION_V1:",
        )
        digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
        assert digest == V1_DECISION_MATRIX_FENCE_SHA256, (
            "CI_TEST_PERFORMANCE_DECISION_V1 fenced block changed "
            "(enum/required-key/line-order drift):\n" + block
        )

    def test_runtime_delta_template_v1_fenced_block_exact_digest(self):
        block = _extract_first_fenced_block(
            RUNTIME_DELTA_TEMPLATE_PATH.read_text(encoding="utf-8"),
            "ci_runtime_delta_v1:",
        )
        digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
        assert digest == V1_RUNTIME_DELTA_TEMPLATE_FENCE_SHA256, (
            "ci_runtime_delta_v1 fenced block changed "
            "(enum/required-key/line-order drift):\n" + block
        )

    def test_fenced_block_digest_detects_single_character_change(self):
        """Regression test #16: a single-character mutation to the fenced
        block must change the digest (proves the snapshot is exact, not a
        loose substring/line-membership check)."""
        block = _extract_first_fenced_block(
            DECISION_MATRIX_PATH.read_text(encoding="utf-8"),
            "CI_TEST_PERFORMANCE_DECISION_V1:",
        )
        mutated = block[:-2] + "X" + block[-1]
        assert mutated != block
        original_digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
        mutated_digest = hashlib.sha256(mutated.encode("utf-8")).hexdigest()
        assert original_digest != mutated_digest


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
# AC2/AC3: fixture -> exit code / decision contract (function-level, no
# out-of-band trusted artifact supplied -- approval_eligible is always
# false here for fixtures that declare ci_verdict_summary_ref, because a
# self-report alone is never sufficient; see TestTrustedFunctionalArtifact
# below for the artifact-supplied positive path).
# --------------------------------------------------------------------------- #
# fixture_name -> (expected_exit_code, expected_structural_valid,
#                   expected_semantic_valid, expected_approval_eligible)
FIXTURE_EXPECTATIONS: dict[str, tuple[int, bool, bool, bool]] = {
    "valid_no_claim_not_instrumented.json": (0, True, True, False),
    "valid_no_claim_observed_regression.json": (0, True, True, False),
    "valid_insufficient_samples_structurally_valid_gate_blocked.json": (
        0,
        True,
        True,
        False,
    ),
    "valid_functional_evidence_trusted_artifact_match.json": (0, True, True, False),
    "warning_docs_only_with_runtime_delta.json": (0, True, True, False),
    "invalid_no_claim_improved_observation.json": (0, True, True, False),
    "invalid_improvement_claim_insufficient_samples.json": (0, True, True, False),
    "invalid_non_regression_claim_incomparable_cohort.json": (0, True, True, False),
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
    def test_no_claim_improved_observation_is_now_valid(self):
        """Truth table fix: no claim + improved observation is a legitimate
        incidental observation, not a contradiction."""
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "invalid_no_claim_improved_observation.json")
        )
        assert decision["errors"] == []

    def test_improvement_claim_insufficient_samples_is_valid_but_blocked(self):
        """Truth table fix: insufficient evidence is a legitimate blocked
        state, not a semantic error."""
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "invalid_improvement_claim_insufficient_samples.json")
        )
        assert decision["errors"] == []
        assert "insufficient_samples_gate_blocked" in decision["blockers"]
        assert decision["approval_eligible"] is False

    def test_non_regression_claim_incomparable_cohort_is_valid_but_blocked(self):
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "invalid_non_regression_claim_incomparable_cohort.json")
        )
        assert decision["errors"] == []
        assert "incomparable_cohort_gate_blocked" in decision["blockers"]
        assert decision["approval_eligible"] is False

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

    def test_no_claim_observed_regression_is_semantic_valid_but_approval_blocked(self):
        """Truth table fix: observed regression always blocks approval,
        with or without an active claim -- no longer a warning-only escape
        hatch to approval_eligible=true."""
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "valid_no_claim_observed_regression.json")
        )
        assert decision["errors"] == []
        assert "no_claim_observed_regression_review_recommended" in decision["warnings"]
        assert "observed_regression_blocks_approval" in decision["blockers"]
        assert decision["approval_eligible"] is False

    def test_insufficient_samples_gate_blocked_blocker_code(self):
        _, decision = validator.validate_assessment(
            str(
                FIXTURE_DIR
                / "valid_insufficient_samples_structurally_valid_gate_blocked.json"
            )
        )
        assert "insufficient_samples_gate_blocked" in decision["blockers"]

    def test_active_claim_forbids_not_required_status(self):
        assessment = json.loads(
            (FIXTURE_DIR / "invalid_improvement_claim_insufficient_samples.json").read_text(
                encoding="utf-8"
            )
        )
        assessment["performance_evidence"] = {"status": "not_required"}
        assessment["observation"] = {"outcome": "not_observed"}
        assessment["claim_evaluation"] = {"outcome": "inconclusive"}
        errors: list[str] = []
        warnings: list[str] = []
        validator._check_claim_state_machine(assessment, errors, warnings)
        assert "active_claim_status_not_required_forbidden" in errors

    def test_claim_evaluation_cannot_be_satisfied_when_regressed(self):
        assessment = json.loads(
            (FIXTURE_DIR / "invalid_improvement_claim_insufficient_samples.json").read_text(
                encoding="utf-8"
            )
        )
        assessment["performance_evidence"] = {"status": "complete"}
        assessment["observation"] = {"outcome": "regressed"}
        assessment["claim_evaluation"] = {"outcome": "satisfied"}
        errors: list[str] = []
        warnings: list[str] = []
        validator._check_claim_state_machine(assessment, errors, warnings)
        assert "claim_evaluation_contradicts_regressed_observation" in errors

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
# P0-1 / AC3: functional evidence is cross-checked against a trusted,
# out-of-band ci_verdict_summary_v2 artifact -- self-report alone (any
# head_sha_match: true / classification: required / status: completed /
# conclusion: success / positive check_run_id an author asserts) is never
# sufficient for approval_eligible.
# --------------------------------------------------------------------------- #
class TestTrustedFunctionalArtifact:
    MATCHING_FIXTURE = FIXTURE_DIR / "valid_functional_evidence_trusted_artifact_match.json"

    def test_self_reported_head_sha_match_alone_does_not_grant_approval(self):
        """Regression test #1: forging head_sha_match: true in the
        self-report must not grant approval_eligible without the trusted
        artifact input."""
        _, decision = validator.validate_assessment(str(self.MATCHING_FIXTURE))
        assert decision["approval_eligible"] is False
        assert "functional_evidence_missing_trusted_artifact_input" in decision["blockers"]

    def test_trusted_artifact_present_and_matching_grants_approval(self):
        digest = validator._sha256_of_file(str(TRUSTED_ARTIFACT_PATH))
        _, decision = validator.validate_assessment(
            str(self.MATCHING_FIXTURE),
            ci_verdict_summary_path=str(TRUSTED_ARTIFACT_PATH),
            expected_head_sha=TRUSTED_ARTIFACT_HEAD_SHA,
            expected_artifact_digest=digest,
        )
        assert decision["blockers"] == []
        assert decision["approval_eligible"] is True

    def test_expected_head_sha_mismatch_is_rejected(self):
        """Regression test #1 (continued): a trusted-SHA mismatch is
        rejected even when an artifact is supplied."""
        digest = validator._sha256_of_file(str(TRUSTED_ARTIFACT_PATH))
        _, decision = validator.validate_assessment(
            str(self.MATCHING_FIXTURE),
            ci_verdict_summary_path=str(TRUSTED_ARTIFACT_PATH),
            expected_head_sha="0" * 40,
            expected_artifact_digest=digest,
        )
        assert "functional_evidence_artifact_head_sha_mismatch" in decision["blockers"]
        assert decision["approval_eligible"] is False

    def test_artifact_digest_mismatch_is_rejected(self):
        """Regression test #4: artifact digest mismatch is rejected."""
        _, decision = validator.validate_assessment(
            str(self.MATCHING_FIXTURE),
            ci_verdict_summary_path=str(TRUSTED_ARTIFACT_PATH),
            expected_head_sha=TRUSTED_ARTIFACT_HEAD_SHA,
            expected_artifact_digest="sha256:" + "0" * 64,
        )
        assert "functional_evidence_artifact_digest_mismatch" in decision["blockers"]
        assert decision["approval_eligible"] is False

    def test_artifact_wrong_schema_is_rejected(self, tmp_path):
        bad_artifact = tmp_path / "wrong_schema.json"
        artifact = json.loads(TRUSTED_ARTIFACT_PATH.read_text(encoding="utf-8"))
        artifact["schema"] = "ci_verdict_summary"
        bad_artifact.write_text(json.dumps(artifact), encoding="utf-8")
        _, decision = validator.validate_assessment(
            str(self.MATCHING_FIXTURE),
            ci_verdict_summary_path=str(bad_artifact),
            expected_head_sha=TRUSTED_ARTIFACT_HEAD_SHA,
        )
        assert "functional_evidence_artifact_schema_mismatch" in decision["blockers"]

    def test_artifact_not_merge_ready_is_rejected(self, tmp_path):
        """Regression test #3: overall_status: blocked artifacts are
        rejected even if the assessment self-reports success."""
        bad_artifact = tmp_path / "blocked.json"
        artifact = json.loads(TRUSTED_ARTIFACT_PATH.read_text(encoding="utf-8"))
        artifact["overall_status"] = "blocked"
        bad_artifact.write_text(json.dumps(artifact), encoding="utf-8")
        _, decision = validator.validate_assessment(
            str(self.MATCHING_FIXTURE),
            ci_verdict_summary_path=str(bad_artifact),
            expected_head_sha=TRUSTED_ARTIFACT_HEAD_SHA,
        )
        assert "functional_evidence_artifact_not_merge_ready" in decision["blockers"]

    def test_artifact_single_required_check_success_but_missing_others_is_not_enough(
        self, tmp_path
    ):
        """Regression test #2: only one required check succeeding while the
        required-check set is otherwise incomplete/failing must not grant
        approval (the artifact's own required-check floor must all pass)."""
        bad_artifact = tmp_path / "partial.json"
        artifact = json.loads(TRUSTED_ARTIFACT_PATH.read_text(encoding="utf-8"))
        artifact["checks"][1]["conclusion"] = "failure"
        artifact["checks"][1]["status"] = "completed"
        bad_artifact.write_text(json.dumps(artifact), encoding="utf-8")
        _, decision = validator.validate_assessment(
            str(self.MATCHING_FIXTURE),
            ci_verdict_summary_path=str(bad_artifact),
            expected_head_sha=TRUSTED_ARTIFACT_HEAD_SHA,
        )
        assert "functional_evidence_required_check_incomplete" in decision["blockers"]
        assert decision["approval_eligible"] is False

    def test_artifact_missing_and_ref_declared_blocks_approval(self):
        _, decision = validator.validate_assessment(str(self.MATCHING_FIXTURE))
        assert decision["approval_eligible"] is False


# --------------------------------------------------------------------------- #
# AC3: cohort delta recomputation / run-id integrity (P0-3)
# --------------------------------------------------------------------------- #
class TestCohortDeltaRecomputation:
    def _base_assessment(self):
        return json.loads(
            (
                FIXTURE_DIR
                / "valid_insufficient_samples_structurally_valid_gate_blocked.json"
            ).read_text(encoding="utf-8")
        )

    def test_delta_matches_before_after_is_accepted(self):
        assessment = self._base_assessment()
        errors: list[str] = []
        blockers: list[str] = []
        validator._check_cohort_comparability(
            assessment["performance_evidence"], errors, blockers
        )
        assert not any(e.startswith("delta_recomputation_mismatch") for e in errors)

    def test_delta_mismatch_is_rejected(self):
        """Regression test #11."""
        assessment = self._base_assessment()
        assessment["performance_evidence"]["runtime_delta"]["delta"][
            "p50_delta_seconds"
        ] = 999.0
        errors: list[str] = []
        blockers: list[str] = []
        validator._check_cohort_comparability(
            assessment["performance_evidence"], errors, blockers
        )
        assert "delta_recomputation_mismatch" in errors

    def test_run_count_mismatch_is_rejected(self):
        """Regression test #12."""
        assessment = self._base_assessment()
        assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = 999
        errors: list[str] = []
        blockers: list[str] = []
        validator._check_cohort_comparability(
            assessment["performance_evidence"], errors, blockers
        )
        assert any(e.startswith("run_count_mismatches_run_ids_length") for e in errors)

    def test_before_after_run_id_overlap_is_rejected(self):
        """Regression test #13."""
        assessment = self._base_assessment()
        rd = assessment["performance_evidence"]["runtime_delta"]
        rd["after"]["run_ids"] = list(rd["before"]["run_ids"])
        rd["after"]["run_count"] = len(rd["after"]["run_ids"])
        errors: list[str] = []
        blockers: list[str] = []
        validator._check_cohort_comparability(
            assessment["performance_evidence"], errors, blockers
        )
        assert "run_ids_overlap_before_after" in errors

    def test_complete_status_without_runtime_delta_is_blocked(self):
        """Regression test #10."""
        _, decision = validator.validate_assessment(
            str(FIXTURE_DIR / "valid_no_claim_observed_regression.json")
        )
        assert "complete_status_missing_runtime_delta_evidence" in decision["blockers"]


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
# (kept as a coarse smoke check in addition to the golden-line/digest
# snapshots above)
# --------------------------------------------------------------------------- #
def test_v1_identifier_still_present_in_decision_matrix():
    text = DECISION_MATRIX_PATH.read_text(encoding="utf-8")
    assert re.search(r"CI_TEST_PERFORMANCE_DECISION_V1", text)


# --------------------------------------------------------------------------- #
# P1-1 / regression test #18: pnpm policy:check must actually run the
# ci-performance validator test suite, not just a disconnected script.
# --------------------------------------------------------------------------- #
class TestPolicyCheckWiring:
    def test_policy_check_ci_performance_script_runs_pytest_against_this_suite(self):
        package_json = json.loads(PACKAGE_JSON_PATH.read_text(encoding="utf-8"))
        scripts = package_json["scripts"]
        assert "policy:check:ci-performance" in scripts
        assert "pytest" in scripts["policy:check:ci-performance"]
        assert (
            "test_validate_ci_performance_assessment_v2.py"
            in scripts["policy:check:ci-performance"]
        )

    def test_policy_check_aggregate_includes_ci_performance(self):
        """Regression test #18: `pnpm policy:check` must invoke the
        ci-performance validator test suite, not silently skip it."""
        package_json = json.loads(PACKAGE_JSON_PATH.read_text(encoding="utf-8"))
        scripts = package_json["scripts"]
        assert "policy:check:ci-performance" in scripts["policy:check"]

    def test_policy_validate_ci_performance_script_invokes_cli(self):
        package_json = json.loads(PACKAGE_JSON_PATH.read_text(encoding="utf-8"))
        scripts = package_json["scripts"]
        assert "policy:validate:ci-performance" in scripts
        assert "validate_ci_performance_assessment_v2.py" in scripts[
            "policy:validate:ci-performance"
        ]


# --------------------------------------------------------------------------- #
# Regression test #17: an integration test that goes through the real CLI
# with a trusted artifact file, not just the function-level API.
# --------------------------------------------------------------------------- #
def test_cli_integration_with_trusted_ci_verdict_summary_artifact(tmp_path):
    output_path = tmp_path / "result.json"
    digest = validator._sha256_of_file(str(TRUSTED_ARTIFACT_PATH))
    proc = _run_cli(
        TestTrustedFunctionalArtifact.MATCHING_FIXTURE,
        output_path,
        extra_args=[
            "--ci-verdict-summary",
            str(TRUSTED_ARTIFACT_PATH),
            "--expected-head-sha",
            TRUSTED_ARTIFACT_HEAD_SHA,
            "--expected-artifact-digest",
            digest,
        ],
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["approval_eligible"] is True
    assert result["blockers"] == []

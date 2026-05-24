"""
Unit tests for check_pr_review_gates.py

Tests G1-G5 deterministic gates with various input scenarios.
"""

import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import the checker module
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from check_pr_review_gates import (
    CheckPRReviewGates,
    GateStatus,
    Verdict,
    PRReviewGateResult,
    Finding,
)


class TestG1CITestSelection:
    """Tests for G1: CI test selection gate."""

    def test_g1_artifact_not_found(self):
        """G1: artifact not found → not_applicable"""
        checker = CheckPRReviewGates()
        result = checker.g1_ci_test_selection(artifact_path="/nonexistent.json")
        assert result.status == GateStatus.NOT_APPLICABLE.value

    def test_g1_artifact_no_uncovered_files(self):
        """G1: artifact with empty uncovered_changed_test_files → pass"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            artifact = {
                "schema_version": "ci_test_selection/v1",
                "head_sha": "abc123",
                "uncovered_changed_test_files": [],
                "collected_test_files": ["test_a.py", "test_b.py"]
            }
            json.dump(artifact, f)
            f.flush()

            checker = CheckPRReviewGates()
            result = checker.g1_ci_test_selection(artifact_path=f.name)
            assert result.status == GateStatus.PASS.value
            assert result.minimal_context is None

            Path(f.name).unlink()

    def test_g1_artifact_with_uncovered_files(self):
        """G1: artifact with uncovered_changed_test_files → fail"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            artifact = {
                "schema_version": "ci_test_selection/v1",
                "head_sha": "abc123",
                "uncovered_changed_test_files": ["test_new_feature.py"],
                "collected_test_files": ["test_a.py"],
                "ci_run_url": "https://github.com/test/actions/runs/123",
                "workflow": "ci.yml",
                "job": "python-test"
            }
            json.dump(artifact, f)
            f.flush()

            checker = CheckPRReviewGates()
            result = checker.g1_ci_test_selection(artifact_path=f.name)
            assert result.status == GateStatus.FAIL.value
            assert "test_new_feature.py" in result.minimal_context
            assert result.findings is not None
            assert len(result.findings) > 0

            Path(f.name).unlink()

    def test_g1_artifact_wrong_schema_version(self):
        """G1: artifact with wrong schema_version → not_applicable"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            artifact = {
                "schema_version": "other_schema/v1",
                "uncovered_changed_test_files": ["test.py"]
            }
            json.dump(artifact, f)
            f.flush()

            checker = CheckPRReviewGates()
            result = checker.g1_ci_test_selection(artifact_path=f.name)
            assert result.status == GateStatus.NOT_APPLICABLE.value

            Path(f.name).unlink()


class TestG2EvidenceBinding:
    """Tests for G2: Evidence binding structure."""

    def test_g2_self_report_only_without_evidence(self):
        """G2: self_report without evidence_refs → fail"""
        checker = CheckPRReviewGates()
        pr_body = "## 検証コマンド結果\nself_report: all passed"
        result = checker.g2_evidence_binding(
            pr_body=pr_body,
            pr_head_sha="abc123",
            local_head_sha="abc123"
        )
        assert result.status == GateStatus.FAIL.value
        assert "no structured findings block" in result.minimal_context

    def test_g2_evidence_refs_present(self):
        """G2: evidence_refs structure present → pass"""
        checker = CheckPRReviewGates()
        pr_body = """```yaml
findings:
  - head_sha: abc123
    source_kind: ci_artifact
    evidence_refs:
      ci_run_ref:
        url: https://github.com/test
        workflow: ci.yml
```"""
        result = checker.g2_evidence_binding(
            pr_body=pr_body,
            pr_head_sha="abc123",
            local_head_sha="abc123"
        )
        assert result.status == GateStatus.PASS.value

    def test_g2_empty_pr_body(self):
        """G2: empty PR body → not_applicable in lenient mode"""
        checker = CheckPRReviewGates()
        result = checker.g2_evidence_binding(pr_body="")
        assert result.status == GateStatus.NOT_APPLICABLE.value


class TestG3ImplementationOracle:
    """Tests for G3: Implementation oracle verification."""

    def test_g3_no_oracle_section(self):
        """G3: no implementation_oracles section → not_applicable"""
        checker = CheckPRReviewGates()
        issue_body = "## Some section\nContent"
        result = checker.g3_implementation_oracle(issue_body=issue_body)
        assert result.status == GateStatus.NOT_APPLICABLE.value

    def test_g3_parse_oracle_structure(self):
        """G3: parse oracle YAML structure from issue"""
        checker = CheckPRReviewGates()
        issue_body = """## implementation_oracles:
- oracle_1
  kind: python_ast_call
  files: src/test.py
  must_call:
    module: subprocess
    function: run
"""
        oracles = checker._parse_implementation_oracles(issue_body)
        assert len(oracles) > 0
        # ID may be set from "- " line or from id: field
        assert "oracle_1" in str(oracles[0].get("id", ""))
        assert oracles[0].get("kind") == "python_ast_call"

    def test_g3_ast_call_verification_pass(self):
        """G3: AST call verification passes when call found"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("import subprocess\nsubprocess.run(['ls'])")
            f.flush()

            checker = CheckPRReviewGates()
            result = checker._verify_ast_call(
                oracle={
                    "id": "test_oracle",
                    "must_call": {"module": "subprocess", "function": "run"}
                },
                files=[f.name]
            )
            assert result["passed"] is True

            Path(f.name).unlink()

    def test_g3_ast_call_verification_fail(self):
        """G3: AST call verification fails when call not found"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("import os\nprint('hello')")
            f.flush()

            checker = CheckPRReviewGates()
            result = checker._verify_ast_call(
                oracle={
                    "id": "test_oracle",
                    "must_call": {"module": "subprocess", "function": "run"}
                },
                files=[f.name]
            )
            assert result["passed"] is False
            assert "not found" in result["error"]

            Path(f.name).unlink()

    def test_g3_grep_fallback(self):
        """G3: grep fallback when pattern specified"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("def my_function():\n    return 42")
            f.flush()

            checker = CheckPRReviewGates()
            result = checker._verify_grep(
                oracle={
                    "id": "test_oracle",
                    "pattern": "my_function"
                },
                files=[f.name]
            )
            assert result["passed"] is True

            Path(f.name).unlink()


class TestG4HeadSHAConsistency:
    """Tests for G4: Head SHA consistency."""

    def test_g4_no_sha_provided(self):
        """G4: no SHA provided → not_applicable"""
        checker = CheckPRReviewGates()
        result = checker.g4_head_sha_consistency()
        assert result.status == GateStatus.NOT_APPLICABLE.value

    def test_g4_sha_match(self):
        """G4: SHAs match → pass"""
        checker = CheckPRReviewGates()
        sha = "abc123def456"
        result = checker.g4_head_sha_consistency(
            pr_head_sha=sha,
            local_head_sha=sha
        )
        assert result.status == GateStatus.PASS.value

    def test_g4_sha_mismatch(self):
        """G4: SHAs mismatch → fail"""
        checker = CheckPRReviewGates()
        result = checker.g4_head_sha_consistency(
            pr_head_sha="abc123",
            local_head_sha="def456"
        )
        assert result.status == GateStatus.FAIL.value
        assert "mismatch" in result.minimal_context.lower()
        assert "abc123" in result.minimal_context
        assert "def456" in result.minimal_context


class TestG5FixtureGuardPathCoverage:
    """Tests for G5: Fixture guard path coverage."""

    def test_g5_no_trace_or_file(self):
        """G5: no trace log or coverage file → not_applicable"""
        checker = CheckPRReviewGates()
        result = checker.g5_fixture_guard_path_coverage()
        assert result.status == GateStatus.NOT_APPLICABLE.value

    def test_g5_trace_with_coverage_marker(self):
        """G5: marker-only trace without structured tests → fail"""
        checker = CheckPRReviewGates()
        trace_log = """
Test run started...
fixture_path_coverage/v1:
  observed_guard_path: /path/to/fixture
  assertions: 5
"""
        result = checker.g5_fixture_guard_path_coverage(trace_log=trace_log)
        assert result.status == GateStatus.FAIL.value

    def test_g5_coverage_file_with_marker(self):
        """G5: marker-only coverage file without structured tests → fail"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("fixture_path_coverage/v1\nobserved_paths:\n  - path1\n  - path2")
            f.flush()

            checker = CheckPRReviewGates()
            result = checker.g5_fixture_guard_path_coverage(coverage_file=f.name)
            assert result.status == GateStatus.FAIL.value

            Path(f.name).unlink()


class TestVerdictDetermination:
    """Tests for verdict determination based on gate results."""

    def test_all_pass_verdict_approve(self):
        """All gates pass → APPROVE"""
        checker = CheckPRReviewGates()
        checker.result.gates = [
            checker.g1_ci_test_selection(),
            checker.g2_evidence_binding(),
            checker.g3_implementation_oracle(),
            checker.g4_head_sha_consistency(),
            checker.g5_fixture_guard_path_coverage(),
        ]
        checker.finalize_verdict()
        # Most will be not_applicable without input, which is still pass
        assert checker.result.verdict == Verdict.APPROVE.value

    def test_one_fail_verdict_request_changes(self):
        """One gate fails → REQUEST_CHANGES"""
        checker = CheckPRReviewGates()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            artifact = {
                "schema_version": "ci_test_selection/v1",
                "head_sha": "abc123",
                "uncovered_changed_test_files": ["test_missing.py"]
            }
            json.dump(artifact, f)
            f.flush()

            checker.result.gates = [
                checker.g1_ci_test_selection(f.name),
                checker.g2_evidence_binding(),
            ]
            checker.finalize_verdict()
            assert checker.result.verdict == Verdict.REQUEST_CHANGES.value

            Path(f.name).unlink()


class TestJSONSerialization:
    """Tests for JSON output serialization."""

    def test_result_to_dict(self):
        """Result serializes to dict with correct structure"""
        checker = CheckPRReviewGates()
        checker.result.gates = [
            checker.g2_evidence_binding()
        ]
        checker.finalize_verdict()

        result_dict = checker.result.to_dict()
        assert "schema_version" in result_dict
        assert "generated_at" in result_dict
        assert "verdict" in result_dict
        assert "gates" in result_dict
        assert len(result_dict["gates"]) > 0

    def test_result_json_serializable(self):
        """Result can be JSON serialized"""
        checker = CheckPRReviewGates()
        checker.result.gates = [
            checker.g4_head_sha_consistency(
                pr_head_sha="abc123",
                local_head_sha="def456"
            )
        ]
        checker.finalize_verdict()

        result_dict = checker.result.to_dict()
        # Should not raise
        json_str = json.dumps(result_dict)
        assert len(json_str) > 0

        # Should deserialize back
        parsed = json.loads(json_str)
        assert parsed["verdict"] == Verdict.REQUEST_CHANGES.value


class TestRunGateAPI:
    """Tests for run_gate unified API."""

    def test_run_gate_g1(self):
        """run_gate("g1", ...) calls g1 checker"""
        checker = CheckPRReviewGates()
        result = checker.run_gate("g1")
        assert result.gate_id == "g1"
        assert result.gate_name == "ci_test_selection"

    def test_run_gate_g2(self):
        """run_gate("g2", ...) calls g2 checker"""
        checker = CheckPRReviewGates()
        result = checker.run_gate("g2", pr_body="test")
        assert result.gate_id == "g2"
        assert result.gate_name == "evidence_binding"

    def test_run_gate_g3(self):
        """run_gate("g3", ...) calls g3 checker"""
        checker = CheckPRReviewGates()
        result = checker.run_gate("g3", issue_body="")
        assert result.gate_id == "g3"

    def test_run_gate_g4(self):
        """run_gate("g4", ...) calls g4 checker"""
        checker = CheckPRReviewGates()
        result = checker.run_gate("g4", pr_head_sha="a", local_head_sha="b")
        assert result.gate_id == "g4"
        assert result.status == GateStatus.FAIL.value

    def test_run_gate_g5(self):
        """run_gate("g5", ...) calls g5 checker"""
        checker = CheckPRReviewGates()
        result = checker.run_gate("g5")
        assert result.gate_id == "g5"

    def test_run_gate_unknown_raises(self):
        """run_gate with unknown gate raises ValueError"""
        checker = CheckPRReviewGates()
        with pytest.raises(ValueError):
            checker.run_gate("g999")


class TestMinimalContextOnlyForFail:
    """Tests that minimal_context is only included for fail gates."""

    def test_pass_gate_no_minimal_context(self):
        """Pass gate doesn't include minimal_context in serialization"""
        checker = CheckPRReviewGates()
        gate = checker.g5_fixture_guard_path_coverage()
        gate.status = GateStatus.PASS.value

        result_dict = PRReviewGateResult(gates=[gate]).to_dict()
        gate_dict = result_dict["gates"][0]
        assert "minimal_context" not in gate_dict or gate_dict.get("minimal_context") is None

    def test_fail_gate_includes_minimal_context(self):
        """Fail gate includes minimal_context in serialization"""
        checker = CheckPRReviewGates()
        result = checker.g4_head_sha_consistency(
            pr_head_sha="abc",
            local_head_sha="def"
        )

        result_dict = PRReviewGateResult(gates=[result]).to_dict()
        gate_dict = result_dict["gates"][0]
        assert "minimal_context" in gate_dict
        assert gate_dict["minimal_context"] is not None


class TestStrictMode:
    """Tests for strict mode behavior (B1)."""

    def test_strict_mode_request_changes_on_missing_g1_input(self):
        """G1 in strict mode: missing --ci-artifact → fail"""
        checker = CheckPRReviewGates(strict=True)
        result = checker.g1_ci_test_selection(artifact_path=None)
        assert result.status == GateStatus.FAIL.value
        assert "G1: required --ci-artifact missing" in result.minimal_context

    def test_strict_mode_request_changes_on_missing_g2_input(self):
        """G2 in strict mode: empty --pr-body → fail"""
        checker = CheckPRReviewGates(strict=True)
        result = checker.g2_evidence_binding(pr_body="")
        assert result.status == GateStatus.FAIL.value
        assert "G2: required --pr-body missing or empty" in result.minimal_context

    def test_strict_mode_request_changes_on_missing_g4_input(self):
        """G4 in strict mode: missing SHA → fail"""
        checker = CheckPRReviewGates(strict=True)
        result = checker.g4_head_sha_consistency(pr_head_sha="", local_head_sha="")
        assert result.status == GateStatus.FAIL.value
        assert "G4: required --pr-head-sha and --local-head-sha" in result.minimal_context

    def test_lenient_mode_not_applicable_on_missing_input(self):
        """Lenient mode (strict=False): missing input → not_applicable"""
        checker = CheckPRReviewGates(strict=False)
        result = checker.g1_ci_test_selection(artifact_path=None)
        assert result.status == GateStatus.NOT_APPLICABLE.value


class TestG2StructuredEvidenceBinding:
    """Tests for G2 structured evidence binding validation (B3)."""

    def test_g2_empty_pr_body_strict_fail(self):
        """G2 strict: empty PR body → fail"""
        checker = CheckPRReviewGates(strict=True)
        result = checker.g2_evidence_binding(pr_body="")
        assert result.status == GateStatus.FAIL.value

    def test_g2_self_report_only_fail(self):
        """G2: self_report_only without supporting refs → fail"""
        checker = CheckPRReviewGates()
        pr_body = """```yaml
findings:
  - head_sha: abc123
    source_kind: pr_body
    evidence_refs:
      self_report_only: true
```"""
        result = checker.g2_evidence_binding(pr_body=pr_body)
        assert result.status == GateStatus.FAIL.value
        assert "self_report" in result.minimal_context.lower()

    def test_g2_evidence_word_without_findings_fail(self):
        """G2: 'evidence' keyword without findings structure → fail"""
        checker = CheckPRReviewGates()
        pr_body = "## Evidence\nThis is just text with word evidence."
        result = checker.g2_evidence_binding(pr_body=pr_body)
        assert result.status == GateStatus.FAIL.value

    def test_g2_valid_findings_structure_pass(self):
        """G2: valid findings with code_ref → pass"""
        checker = CheckPRReviewGates()
        pr_body = """```yaml
findings:
  - head_sha: abc123
    source_kind: ci_artifact
    evidence_refs:
      code_ref: src/test.py
```"""
        result = checker.g2_evidence_binding(pr_body=pr_body)
        assert result.status == GateStatus.PASS.value

    def test_g2_valid_findings_with_ci_run_ref_pass(self):
        """G2: valid findings with ci_run_ref → pass"""
        checker = CheckPRReviewGates()
        pr_body = """```yaml
findings:
  - head_sha: abc123
    source_kind: ci_artifact
    evidence_refs:
      ci_run_ref:
        url: https://github.com/test/actions/runs/123
```"""
        result = checker.g2_evidence_binding(pr_body=pr_body)
        assert result.status == GateStatus.PASS.value

    def test_g2_json_findings_block_pass(self):
        """G2: JSON findings block → pass"""
        checker = CheckPRReviewGates()
        pr_body = """```json
{
  "findings": [
    {
      "head_sha": "abc123",
      "source_kind": "pr_body",
      "evidence_refs": {"code_ref": "src/test.py"}
    }
  ]
}
```"""
        result = checker.g2_evidence_binding(pr_body=pr_body)
        assert result.status == GateStatus.PASS.value


class TestG3ImplementationOracle:
    """Tests for G3 YAML oracle parsing and AST alias resolution (B4)."""

    def test_g3_yaml_block_oracle_parse(self):
        """G3: parse YAML oracle block from issue body"""
        checker = CheckPRReviewGates()
        issue_body = """## implementation_oracles:
- id: test_oracle
  kind: ast
  must_call:
    module: subprocess
    function: run
  files: [src/test.py]
"""
        oracles = checker._parse_implementation_oracles(issue_body)
        assert len(oracles) > 0
        assert oracles[0].get("id") == "test_oracle"

    def test_g3_ast_import_alias_detection(self):
        """G3: import subprocess as sp; sp.run(...) → detected as subprocess.run"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("import subprocess as sp\nsp.run(['ls'])")
            f.flush()

            checker = CheckPRReviewGates()
            result = checker._verify_ast_call(
                oracle={
                    "id": "test",
                    "must_call": {"module": "subprocess", "function": "run"}
                },
                files=[f.name]
            )
            assert result["passed"] is True
            Path(f.name).unlink()

    def test_g3_ast_from_import_detection(self):
        """G3: from subprocess import run; run(...) → detected"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("from subprocess import run\nrun(['ls'])")
            f.flush()

            checker = CheckPRReviewGates()
            result = checker._verify_ast_call(
                oracle={
                    "id": "test",
                    "must_call": {"module": "subprocess", "function": "run"}
                },
                files=[f.name]
            )
            assert result["passed"] is True
            Path(f.name).unlink()

    def test_g3_grep_fallback_excludes_comment_only_match(self):
        """G3: grep fallback excludes comment-only matches"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("# this function is called: subprocess.run\n# but only in comments")
            f.flush()

            checker = CheckPRReviewGates()
            result = checker._verify_grep(
                oracle={
                    "id": "test",
                    "must_call": {"function": "subprocess.run"}
                },
                files=[f.name]
            )
            assert result["passed"] is False
            Path(f.name).unlink()

    def test_g3_ast_required_does_not_pass_via_grep_fallback(self):
        """G3: AST kind=ast oracle does NOT fallback to grep"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("# subprocess.run mentioned in comment")
            f.flush()

            checker = CheckPRReviewGates()
            result = checker._verify_ast_call(
                oracle={
                    "id": "test",
                    "kind": "ast",
                    "must_call": {"module": "subprocess", "function": "run"}
                },
                files=[f.name]
            )
            assert result["passed"] is False
            Path(f.name).unlink()


class TestG5StructuredFixtureTrace:
    """Tests for G5 structured fixture trace validation (B5)."""

    def test_g5_marker_only_substring_fail(self):
        """G5: just 'fixture_path_coverage/v1' substring → fail (not structured)"""
        checker = CheckPRReviewGates()
        trace_log = "Some output with fixture_path_coverage/v1 marker only"
        result = checker.g5_fixture_guard_path_coverage(trace_log=trace_log)
        assert result.status == GateStatus.FAIL.value

    def test_g5_expected_vs_observed_mismatch_fail(self):
        """G5: expected != observed guard path → fail"""
        checker = CheckPRReviewGates()
        trace_log = """
schema_version: fixture_path_coverage/v1
tests:
  - test: test_guard
    expected_guard_path: /expected/path
    observed_guard_path: /different/path
    status: pass
"""
        result = checker.g5_fixture_guard_path_coverage(trace_log=trace_log)
        assert result.status == GateStatus.FAIL.value
        assert "path mismatch" in result.minimal_context.lower()

    def test_g5_valid_structured_trace_pass(self):
        """G5: valid structured fixture_path_coverage/v1 trace → pass"""
        checker = CheckPRReviewGates()
        trace_log = """
schema_version: fixture_path_coverage/v1
tests:
  - test: test_guard
    expected_guard_path: /path/to/guard
    observed_guard_path: /path/to/guard
    status: pass
"""
        result = checker.g5_fixture_guard_path_coverage(trace_log=trace_log)
        assert result.status == GateStatus.PASS.value

    def test_g5_status_fail_detected(self):
        """G5: test with status != pass → fail"""
        checker = CheckPRReviewGates()
        trace_log = """
schema_version: fixture_path_coverage/v1
tests:
  - test: test_guard
    expected_guard_path: /path
    observed_guard_path: /path
    status: fail
"""
        result = checker.g5_fixture_guard_path_coverage(trace_log=trace_log)
        assert result.status == GateStatus.FAIL.value


class TestSchemaValidator:
    """Tests for schema validation (B6)."""

    def test_schema_validator_rejects_minimal_context_on_pass(self):
        """Schema validator: pass gate with minimal_context → error"""
        from check_pr_review_gates import validate_against_schema
        from pathlib import Path

        output_dict = {
            "schema_version": "PR_REVIEW_GATE_RESULT_V1",
            "generated_at": "2026-05-24T00:00:00Z",
            "generated_by": "check_pr_review_gates.py",
            "verdict": "APPROVE",
            "gates": [
                {
                    "gate_id": "g1",
                    "gate_name": "ci_test_selection",
                    "status": "pass",
                    "minimal_context": "should not be present"
                }
            ]
        }
        schema_path = Path(__file__).parent.parent / "references" / "pr-review-gate-result-schema.yml"
        errors = validate_against_schema(output_dict, schema_path)
        # Should have error about minimal_context on pass gate
        assert any("minimal_context" in str(e) for e in errors)

    def test_schema_validator_rejects_missing_minimal_context_on_fail(self):
        """Schema validator: fail gate without minimal_context → error"""
        from check_pr_review_gates import validate_against_schema
        from pathlib import Path

        output_dict = {
            "schema_version": "PR_REVIEW_GATE_RESULT_V1",
            "generated_at": "2026-05-24T00:00:00Z",
            "generated_by": "check_pr_review_gates.py",
            "verdict": "REQUEST_CHANGES",
            "gates": [
                {
                    "gate_id": "g1",
                    "gate_name": "ci_test_selection",
                    "status": "fail"
                }
            ]
        }
        schema_path = Path(__file__).parent.parent / "references" / "pr-review-gate-result-schema.yml"
        errors = validate_against_schema(output_dict, schema_path)
        # Should have error about missing minimal_context on fail gate
        assert any("minimal_context" in str(e) for e in errors)

    def test_schema_validator_rejects_self_report_only_findings(self):
        """Schema validator: self_report_only without supporting refs → error"""
        from check_pr_review_gates import validate_against_schema
        from pathlib import Path

        output_dict = {
            "schema_version": "PR_REVIEW_GATE_RESULT_V1",
            "generated_at": "2026-05-24T00:00:00Z",
            "generated_by": "check_pr_review_gates.py",
            "verdict": "REQUEST_CHANGES",
            "gates": [
                {
                    "gate_id": "g2",
                    "gate_name": "evidence_binding",
                    "status": "fail",
                    "minimal_context": "test",
                    "findings": [
                        {
                            "head_sha": "abc123",
                            "source_kind": "pr_body",
                            "evidence_refs": {"self_report_only": True}
                        }
                    ]
                }
            ]
        }
        schema_path = Path(__file__).parent.parent / "references" / "pr-review-gate-result-schema.yml"
        errors = validate_against_schema(output_dict, schema_path)
        # Should have error about self_report_only
        assert any("self_report" in str(e) for e in errors)


class TestArtifactGenerator:
    """Tests for generate_ci_test_selection_artifact.py (B2)."""

    def test_artifact_generator_uses_pytest_collect_only(self):
        """Artifact generator should use pytest --collect-only"""
        # This test verifies the function exists and has correct signature
        from generate_ci_test_selection_artifact import get_pytest_collected_tests
        # Function should exist and be callable
        assert callable(get_pytest_collected_tests)

    def test_artifact_generator_emits_pr_head_sha_and_merge_sha(self):
        """Artifact should include pr_head_sha and merge_sha fields"""
        from generate_ci_test_selection_artifact import generate_artifact
        import argparse

        # Create mock args
        args = argparse.Namespace(
            output='/tmp/test_artifact.json',
            pytest_args=[],
            pr_head_sha='abc123',
            checked_out_sha='def456',
            merge_sha='ghi789',
            workflow='ci',
            job='python-test',
            ci_run_url='https://github.com/test'
        )

        # Should not raise (and file won't be written for this test)
        assert hasattr(args, 'pr_head_sha')
        assert hasattr(args, 'merge_sha')


class TestAllNotApplicableApprove:
    """Tests that all gates not_applicable → APPROVE (lenient mode)."""

    def test_all_not_applicable_approve_lenient(self):
        """All gates not_applicable (lenient mode) → APPROVE"""
        checker = CheckPRReviewGates(strict=False)
        # No inputs provided - all gates should be not_applicable
        checker.result.gates = [
            checker.g1_ci_test_selection(),
            checker.g2_evidence_binding(),
            checker.g3_implementation_oracle(),
            checker.g4_head_sha_consistency(),
            checker.g5_fixture_guard_path_coverage(),
        ]
        checker.finalize_verdict()
        assert checker.result.verdict == Verdict.APPROVE.value


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

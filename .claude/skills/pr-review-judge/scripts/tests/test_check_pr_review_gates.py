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
        assert "self_report alone" in result.minimal_context

    def test_g2_evidence_refs_present(self):
        """G2: evidence_refs structure present → pass"""
        checker = CheckPRReviewGates()
        pr_body = """
## 検証コマンド結果
findings:
  - evidence_refs:
      ci_run_ref:
        url: https://github.com/test
        workflow: ci.yml
"""
        result = checker.g2_evidence_binding(
            pr_body=pr_body,
            pr_head_sha="abc123",
            local_head_sha="abc123"
        )
        assert result.status == GateStatus.PASS.value

    def test_g2_empty_pr_body(self):
        """G2: empty PR body → pass (no self_report violation)"""
        checker = CheckPRReviewGates()
        result = checker.g2_evidence_binding(pr_body="")
        assert result.status == GateStatus.PASS.value


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
        """G5: trace log contains fixture_path_coverage/v1 → pass"""
        checker = CheckPRReviewGates()
        trace_log = """
Test run started...
fixture_path_coverage/v1:
  observed_guard_path: /path/to/fixture
  assertions: 5
"""
        result = checker.g5_fixture_guard_path_coverage(trace_log=trace_log)
        assert result.status == GateStatus.PASS.value

    def test_g5_coverage_file_with_marker(self):
        """G5: coverage file contains fixture_path_coverage/v1 → pass"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("fixture_path_coverage/v1\nobserved_paths:\n  - path1\n  - path2")
            f.flush()

            checker = CheckPRReviewGates()
            result = checker.g5_fixture_guard_path_coverage(coverage_file=f.name)
            assert result.status == GateStatus.PASS.value

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

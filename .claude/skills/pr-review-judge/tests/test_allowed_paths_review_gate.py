#!/usr/bin/env python3
"""
Tests for allowed_paths_review_gate.py

Covers AC5-AC10, AC12:
- AC5: Allowed Paths outside → fail_closed, exact/recursive allowed → ok, stale snapshots
- AC8: head_sha binding and changed_files_source verification
- AC9: Fingerprint vs execution_context separation
- AC10: Matcher rules (exact, recursive, single segment, invalid paths)
- AC12: Single gate evaluator in pr-review-judge, no duplicate in impl-review-loop
"""

import json
import pytest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys
import os

# Import the module under test
import_path = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(import_path))

from allowed_paths_review_gate import (
    AllowedPathsMatcher,
    AllowedPathsGateEvaluator,
    GateStatus,
)


class TestAllowedPathsMatcher:
    """AC10: Allowed Paths matcher with POSIX normalization."""

    def test_exact_file_match(self):
        """AC10: exact file path matches exactly."""
        assert AllowedPathsMatcher.is_file_allowed(
            "src/main.ts",
            ["src/main.ts"]
        )

    def test_exact_file_no_partial_match(self):
        """AC10: exact match is strict (no partial)."""
        assert not AllowedPathsMatcher.is_file_allowed(
            "src/main.tsx",
            ["src/main.ts"]
        )

    def test_recursive_glob_match(self):
        """AC10: trailing /** matches recursive subdirectories."""
        assert AllowedPathsMatcher.is_file_allowed(
            "src/components/Button.ts",
            ["src/components/**"]
        )

    def test_recursive_glob_directory_itself(self):
        """AC10: trailing /** matches the directory itself."""
        assert AllowedPathsMatcher.is_file_allowed(
            "src/components",
            ["src/components/**"]
        )

    def test_single_segment_wildcard(self):
        """AC10: * matches single segment only (no / crossing)."""
        assert AllowedPathsMatcher.is_file_allowed(
            "docs/README.md",
            ["docs/*"]
        )
        assert not AllowedPathsMatcher.is_file_allowed(
            "docs/guides/README.md",
            ["docs/*"]
        )

    def test_parent_directory_traversal_rejected(self):
        """AC10: .. is rejected as fail_closed."""
        result = AllowedPathsMatcher.normalize_path("src/../main.ts")
        assert result is None

    def test_absolute_path_rejected(self):
        """AC10: absolute paths are rejected as fail_closed."""
        result = AllowedPathsMatcher.normalize_path("/src/main.ts")
        assert result is None

    def test_backslash_path_normalized_then_checked(self):
        """AC10: backslash paths are normalized but still checked."""
        # Backslash is converted to forward slash
        normalized = AllowedPathsMatcher.normalize_path("src\\main.ts")
        assert normalized == "src/main.ts"

        # But if the normalized path doesn't match, it's still rejected
        assert not AllowedPathsMatcher.is_file_allowed(
            "src\\main.ts",
            ["other/**"]
        )


class TestContractFingerprintAndExecutionContext:
    """AC9: Fingerprint and execution context are separated."""

    def test_fingerprint_contains_contract_info(self):
        """AC9: Fingerprint contains issue, contract_body_sha256, allowed_paths hash."""
        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["src/**", "tests/**"],
            contract_body_sha256="contract_sha256_value",
            issue_number=758,
        )

        fp = evaluator.compute_contract_fingerprint()
        assert fp["issue_number"] == 758
        assert fp["contract_body_sha256"] == "contract_sha256_value"
        assert fp["base_ref"] == "main"
        assert fp["base_sha_at_snapshot"] == "abc123"
        # allowed_paths_normalized_sha256 should be present and deterministic
        assert "allowed_paths_normalized_sha256" in fp

    def test_fingerprint_changes_on_contract_sha_change(self):
        """AC9: Fingerprint changes if contract_body_sha256 changes."""
        evaluator1 = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["src/**"],
            contract_body_sha256="sha256_v1",
            issue_number=758,
        )
        fp1 = evaluator1.compute_contract_fingerprint()

        evaluator2 = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["src/**"],
            contract_body_sha256="sha256_v2",
            issue_number=758,
        )
        fp2 = evaluator2.compute_contract_fingerprint()

        assert fp1 != fp2

    def test_execution_context_does_not_affect_freshness(self):
        """AC9: generated_at and worktree_root in execution_context are NOT used for freshness."""
        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["src/**"],
            contract_body_sha256="contract_sha",
            issue_number=758,
        )

        # execution_context is present in result but should not affect freshness hash
        fp = evaluator.compute_contract_fingerprint()
        # Fingerprint should only contain contract info, not execution context
        assert "generated_at" not in fp
        assert "worktree_root" not in fp

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_execution_context_change_does_not_trigger_stale(self, mock_get_changed_files):
        """AC9: differing execution_context (generated_at/worktree_root) does NOT make
        the gate stale; only contract_fingerprint divergence does."""
        mock_get_changed_files.return_value = ["src/main.ts"]

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123, base_ref="main", base_sha="abc123",
            head_sha="def456", reviewed_head_sha="def456",
            allowed_paths=["src/**"], contract_body_sha256="contract_sha",
            issue_number=758,
        )
        # Use this run's own fingerprint as the expected snapshot. The result's
        # execution_context (generated_at / worktree_root) is generated at runtime and
        # is irrelevant to freshness, so the gate must NOT be stale.
        expected_fp = evaluator.compute_contract_fingerprint()
        evaluator.expected_contract_fingerprint = expected_fp

        result = evaluator.evaluate()
        assert result.status != GateStatus.STALE_SNAPSHOT.value
        assert result.status == GateStatus.OK.value
        # execution_context is still recorded for audit, separate from the fingerprint
        assert "generated_at" in result.execution_context
        assert "worktree_root" in result.execution_context
        assert "generated_at" not in result.contract_fingerprint


class TestHeadShaBinding:
    """AC8: head_sha and reviewed_head_sha binding."""

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_head_sha_mismatch_is_indeterminate(self, mock_get_changed_files):
        """AC8: head_sha != reviewed_head_sha → indeterminate (merge-blocking)."""
        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="current_sha",
            reviewed_head_sha="reviewed_sha",  # Different!
            allowed_paths=["src/**"],
            contract_body_sha256="contract_sha",
            issue_number=758,
        )

        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        # Should NOT call git diff if head_sha doesn't match
        mock_get_changed_files.assert_not_called()

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_head_sha_match_allows_evaluation(self, mock_get_changed_files):
        """AC8: head_sha == reviewed_head_sha → proceed to evaluation."""
        mock_get_changed_files.return_value = ["src/main.ts"]

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="same_sha",
            reviewed_head_sha="same_sha",  # Same!
            allowed_paths=["src/**"],
            contract_body_sha256="contract_sha",
            issue_number=758,
        )

        result = evaluator.evaluate()
        # Should proceed past head_sha check and evaluate the (allowed) changed file → ok
        assert result.status == GateStatus.OK.value
        assert len(result.violations) == 0
        mock_get_changed_files.assert_called_once()

    def test_changed_files_source_is_git_diff_base_head(self):
        """AC8: changed_files_source is 'git_diff_base_head' (not from worker report)."""
        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["src/**"],
            contract_body_sha256="contract_sha",
            issue_number=758,
        )

        # The evaluator should have changed_files_source hardcoded
        assert evaluator is not None
        # After evaluation, result should have changed_files_source as git_diff_base_head
        # (verified in next test)

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_result_has_correct_changed_files_source(self, mock_get_changed_files):
        """AC8: Result field changed_files_source is 'git_diff_base_head'."""
        mock_get_changed_files.return_value = ["src/main.ts"]

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["src/**"],
            contract_body_sha256="contract_sha",
            issue_number=758,
        )

        result = evaluator.evaluate()
        assert result.changed_files_source == "git_diff_base_head"


class TestAllowedPathsValidation:
    """AC5: Allowed Paths validation logic."""

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_changed_file_outside_allowed_paths_is_fail_closed(self, mock_get_changed_files):
        """AC5: changed file outside allowed paths → fail_closed."""
        mock_get_changed_files.return_value = ["forbidden/bad.ts"]

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["src/**", "tests/**"],
            contract_body_sha256="contract_sha",
            issue_number=758,
        )

        result = evaluator.evaluate()
        assert result.status == GateStatus.FAIL_CLOSED.value
        assert len(result.violations) == 1
        assert result.violations[0]["file"] == "forbidden/bad.ts"

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_exact_allowed_file_is_ok(self, mock_get_changed_files):
        """AC5: exact allowed file → ok."""
        mock_get_changed_files.return_value = ["src/main.ts"]

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["src/main.ts"],
            contract_body_sha256="contract_sha",
            issue_number=758,
        )

        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert len(result.violations) == 0

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_recursive_allowed_file_is_ok(self, mock_get_changed_files):
        """AC5: file under recursive allowed directory → ok."""
        mock_get_changed_files.return_value = [
            "src/components/Button.ts",
            "src/utils/helpers.ts",
        ]

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["src/**"],
            contract_body_sha256="contract_sha",
            issue_number=758,
        )

        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert len(result.violations) == 0

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_contract_snapshot_body_sha_change_is_stale(self, mock_get_changed_files):
        """AC5/AC9: contract_body_sha256 change vs snapshot fingerprint → stale_snapshot."""
        mock_get_changed_files.return_value = ["src/main.ts"]

        # Snapshot-time fingerprint (captured at contract-review go time)
        snapshot = AllowedPathsGateEvaluator(
            pr_number=123, base_ref="main", base_sha="abc123",
            head_sha="def456", reviewed_head_sha="def456",
            allowed_paths=["src/**"], contract_body_sha256="old_contract_sha",
            issue_number=758,
        )
        expected_fp = snapshot.compute_contract_fingerprint()

        # Review-time evaluator observes a CHANGED contract body sha
        evaluator = AllowedPathsGateEvaluator(
            pr_number=123, base_ref="main", base_sha="abc123",
            head_sha="def456", reviewed_head_sha="def456",
            allowed_paths=["src/**"], contract_body_sha256="new_contract_sha",
            issue_number=758, expected_contract_fingerprint=expected_fp,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.STALE_SNAPSHOT.value
        # git diff must not be consulted once stale is detected (merge-blocking short-circuit)
        mock_get_changed_files.assert_not_called()

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_base_sha_change_is_stale(self, mock_get_changed_files):
        """AC5/AC9: base_sha change vs snapshot fingerprint → stale_snapshot."""
        mock_get_changed_files.return_value = ["src/main.ts"]

        snapshot = AllowedPathsGateEvaluator(
            pr_number=123, base_ref="main", base_sha="base_at_snapshot",
            head_sha="def456", reviewed_head_sha="def456",
            allowed_paths=["src/**"], contract_body_sha256="contract_sha",
            issue_number=758,
        )
        expected_fp = snapshot.compute_contract_fingerprint()

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123, base_ref="main", base_sha="base_moved_forward",
            head_sha="def456", reviewed_head_sha="def456",
            allowed_paths=["src/**"], contract_body_sha256="contract_sha",
            issue_number=758, expected_contract_fingerprint=expected_fp,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.STALE_SNAPSHOT.value

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_fresh_snapshot_is_not_stale(self, mock_get_changed_files):
        """AC9: identical snapshot fingerprint → NOT stale (proceeds to ok)."""
        mock_get_changed_files.return_value = ["src/main.ts"]

        snapshot = AllowedPathsGateEvaluator(
            pr_number=123, base_ref="main", base_sha="abc123",
            head_sha="def456", reviewed_head_sha="def456",
            allowed_paths=["src/**"], contract_body_sha256="contract_sha",
            issue_number=758,
        )
        expected_fp = snapshot.compute_contract_fingerprint()

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123, base_ref="main", base_sha="abc123",
            head_sha="def456", reviewed_head_sha="def456",
            allowed_paths=["src/**"], contract_body_sha256="contract_sha",
            issue_number=758, expected_contract_fingerprint=expected_fp,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_missing_allowed_paths_is_indeterminate(self, mock_get_changed_files):
        """AC5: missing / empty Allowed Paths → indeterminate (cannot judge)."""
        mock_get_changed_files.return_value = ["src/main.ts"]

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123, base_ref="main", base_sha="abc123",
            head_sha="def456", reviewed_head_sha="def456",
            allowed_paths=[], contract_body_sha256="contract_sha",
            issue_number=758,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        mock_get_changed_files.assert_not_called()

    @patch('allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git')
    def test_worker_report_is_not_input(self, mock_get_changed_files):
        """AC5: Worker report is NOT used as input to changed files (git_diff_base_head only)."""
        mock_get_changed_files.return_value = ["actual/changed/file.ts"]

        evaluator = AllowedPathsGateEvaluator(
            pr_number=123,
            base_ref="main",
            base_sha="abc123",
            head_sha="def456",
            reviewed_head_sha="def456",
            allowed_paths=["allowed_by_contract/**"],
            contract_body_sha256="contract_sha",
            issue_number=758,
        )

        result = evaluator.evaluate()

        # Result should use git diff output, not a hypothetical worker report
        assert result.changed_files == ["actual/changed/file.ts"]
        assert result.changed_files_source == "git_diff_base_head"


class TestSingleSourceOfTruth:
    """AC12: Single gate evaluator in pr-review-judge, no duplicate in impl-review-loop."""

    def test_gate_evaluator_script_exists_in_pr_review_judge(self):
        """AC12: allowed_paths_review_gate.py exists in pr-review-judge."""
        script_path = Path(__file__).parent.parent / "scripts" / "allowed_paths_review_gate.py"
        assert script_path.exists()

    def test_no_duplicate_gate_evaluator_in_impl_review_loop(self):
        """AC12: No allowed_paths_gate.py or similar evaluator in impl-review-loop."""
        impl_review_loop_path = Path(__file__).parent.parent.parent.parent / "impl-review-loop" / "scripts"
        if impl_review_loop_path.exists():
            # Check that no duplicate gate evaluator exists
            for script in impl_review_loop_path.glob("**/*allowed*gate*.py"):
                if script.name != "allowed_paths_review_gate.py":
                    pytest.fail(f"Duplicate gate evaluator found: {script}")
        # If impl-review-loop/scripts doesn't exist, that's fine (single source confirmed)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

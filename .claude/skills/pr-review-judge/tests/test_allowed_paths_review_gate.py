#!/usr/bin/env python3
"""Tests for allowed_paths_review_gate.py."""

from pathlib import Path
from unittest.mock import patch
import sys

import pytest

import_path = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(import_path))

from allowed_paths_review_gate import AllowedPathsGateEvaluator, AllowedPathsMatcher, GateStatus


BASE_ARGS = {
    "pr_number": 123,
    "base_ref": "main",
    "base_sha": "abc123",
    "head_sha": "def456",
    "reviewed_head_sha": "def456",
    "allowed_paths": ["src/**"],
    "contract_body_sha256": "contract_sha",
    "contract_source_kind": "issue_comment",
    "contract_source_id": "456789",
    "issue_number": 758,
}


def make_evaluator(**overrides):
    args = dict(BASE_ARGS)
    args.update(overrides)
    if args.get("expected_contract_fingerprint") == "SELF":
        snapshot_args = dict(args)
        snapshot_args.pop("expected_contract_fingerprint", None)
        snapshot_args["expected_contract_fingerprint"] = None
        snapshot = AllowedPathsGateEvaluator(**snapshot_args)
        args["expected_contract_fingerprint"] = snapshot.compute_contract_fingerprint()
    return AllowedPathsGateEvaluator(**args)


class TestAllowedPathsMatcher:
    def test_exact_file_match(self):
        assert AllowedPathsMatcher.is_file_allowed("src/main.ts", ["src/main.ts"])

    def test_exact_file_no_partial_match(self):
        assert not AllowedPathsMatcher.is_file_allowed("src/main.tsx", ["src/main.ts"])

    def test_recursive_glob_match(self):
        assert AllowedPathsMatcher.is_file_allowed("src/components/Button.ts", ["src/**"])

    def test_recursive_glob_directory_itself(self):
        assert AllowedPathsMatcher.is_file_allowed("src", ["src/**"])

    def test_single_segment_wildcard(self):
        assert AllowedPathsMatcher.is_file_allowed("docs/README.md", ["docs/*"])
        assert not AllowedPathsMatcher.is_file_allowed("docs/guides/README.md", ["docs/*"])

    def test_parent_directory_traversal_rejected(self):
        assert AllowedPathsMatcher.normalize_path("src/../main.ts") is None

    def test_absolute_path_rejected(self):
        assert AllowedPathsMatcher.normalize_path("/src/main.ts") is None

    def test_backslash_path_is_rejected_fail_closed(self):
        assert AllowedPathsMatcher.normalize_path(r"src\main.ts") is None
        assert not AllowedPathsMatcher.is_file_allowed(r"src\main.ts", ["src/**"])

    def test_literal_regex_metacharacters_do_not_expand(self):
        assert AllowedPathsMatcher.is_file_allowed("docs/v1.0+test/file.md", ["docs/v1.0+test/*"])
        assert AllowedPathsMatcher.is_file_allowed("docs/[draft]/file.md", ["docs/[draft]/*"])
        assert AllowedPathsMatcher.is_file_allowed("packages/foo?bar/file.ts", ["packages/foo?bar/*"])

    def test_invalid_allowed_pattern_is_rejected(self):
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/**/nested") is None
        assert AllowedPathsMatcher.normalize_allowed_pattern(r"src\**") is None

    # AC1: trailing-slash is normalized to /**
    def test_trailing_slash_normalized_to_double_glob(self):
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/ui/") == "src/ui/**"

    # AC2: file under trailing-slash directory is allowed
    def test_is_file_allowed_trailing_slash_directory(self):
        assert AllowedPathsMatcher.is_file_allowed("src/ui/HudController.ts", ["src/ui/"])

    # AC3: another trailing-slash directory match
    def test_is_file_allowed_tests_trailing_slash(self):
        assert AllowedPathsMatcher.is_file_allowed("tests/foo.test.ts", ["tests/"])

    # AC4: existing patterns unchanged
    def test_existing_patterns_unchanged(self):
        # exact file
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/main.ts") == "src/main.ts"
        # recursive glob
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/ui/**") == "src/ui/**"
        # single-level wildcard
        assert AllowedPathsMatcher.normalize_allowed_pattern("docs/*") == "docs/*"
        # invalid nested double glob
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/**/nested") is None
        # backslash
        assert AllowedPathsMatcher.normalize_allowed_pattern(r"src\main.ts") is None
        # absolute path
        assert AllowedPathsMatcher.normalize_allowed_pattern("/src/main.ts") is None
        # parent traversal
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/../main.ts") is None

    # AC6: hash equivalence - trailing-slash and /** produce same hash
    def test_compute_allowed_paths_hash_trailing_slash_equals_double_glob(self):
        from allowed_paths_review_gate import AllowedPathsGateEvaluator
        base = {
            "pr_number": 1,
            "base_ref": "main",
            "base_sha": "abc",
            "head_sha": "def",
            "reviewed_head_sha": "def",
            "contract_body_sha256": "sha",
            "contract_source_kind": "issue_comment",
            "contract_source_id": "123",
            "expected_contract_fingerprint": None,
            "issue_number": 0,
        }
        ev1 = AllowedPathsGateEvaluator(**{**base, "allowed_paths": ["src/ui/"]})
        ev2 = AllowedPathsGateEvaluator(**{**base, "allowed_paths": ["src/ui/**"]})
        assert ev1.compute_allowed_paths_hash() == ev2.compute_allowed_paths_hash()

    # AC7: wildcard + trailing-slash is invalid
    def test_wildcard_trailing_slash_is_invalid(self):
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/*/") is None


class TestContractFingerprintAndExecutionContext:
    def test_fingerprint_contains_contract_info(self):
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        fp = evaluator.compute_contract_fingerprint()
        assert fp["issue_number"] == 758
        assert fp["contract_source_kind"] == "issue_comment"
        assert fp["contract_source_id"] == "456789"
        assert fp["contract_body_sha256"] == "contract_sha"
        assert fp["base_ref"] == "main"
        assert fp["base_sha_at_snapshot"] == "abc123"
        assert "allowed_paths_normalized_sha256" in fp

    def test_fingerprint_changes_on_contract_sha_change(self):
        fp1 = make_evaluator(expected_contract_fingerprint="SELF").compute_contract_fingerprint()
        fp2 = make_evaluator(contract_body_sha256="contract_sha_v2", expected_contract_fingerprint="SELF").compute_contract_fingerprint()
        assert fp1 != fp2

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_execution_context_does_not_affect_freshness(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert "generated_at" in result.execution_context
        assert "worktree_root" in result.execution_context
        assert "generated_at" not in result.contract_fingerprint
        assert "worktree_root" not in result.contract_fingerprint

    def test_allowed_paths_hash_uses_canonicalized_values(self):
        evaluator1 = make_evaluator(allowed_paths=["./src/**", "src/**"], expected_contract_fingerprint="SELF")
        evaluator2 = make_evaluator(allowed_paths=["src/**"], expected_contract_fingerprint="SELF")
        assert evaluator1.compute_allowed_paths_hash() == evaluator2.compute_allowed_paths_hash()


class TestHeadShaBinding:
    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_head_sha_mismatch_is_indeterminate(self, mock_get_changed_files):
        evaluator = make_evaluator(head_sha="current_sha", reviewed_head_sha="reviewed_sha", expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        mock_get_changed_files.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_head_sha_match_allows_evaluation(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert len(result.violations) == 0

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_result_has_correct_changed_files_source(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.changed_files_source == "git_diff_base_head"


class TestAllowedPathsValidation:
    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_changed_file_outside_allowed_paths_is_fail_closed(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["forbidden/bad.ts"]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["src/**", "tests/**"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.FAIL_CLOSED.value
        assert result.violations[0]["file"] == "forbidden/bad.ts"

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_exact_allowed_file_is_ok(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["src/main.ts"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_recursive_allowed_file_is_ok(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/components/Button.ts", "src/utils/helpers.ts"]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_contract_snapshot_body_sha_change_is_stale(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        expected_fp = make_evaluator(contract_body_sha256="old_contract_sha", expected_contract_fingerprint="SELF").compute_contract_fingerprint()
        evaluator = make_evaluator(expected_contract_fingerprint=expected_fp)
        result = evaluator.evaluate()
        assert result.status == GateStatus.STALE_SNAPSHOT.value
        mock_get_changed_files.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_base_sha_change_is_stale(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        expected_fp = make_evaluator(base_sha="base_at_snapshot", expected_contract_fingerprint="SELF").compute_contract_fingerprint()
        evaluator = make_evaluator(base_sha="base_moved_forward", expected_contract_fingerprint=expected_fp)
        result = evaluator.evaluate()
        assert result.status == GateStatus.STALE_SNAPSHOT.value

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_missing_allowed_paths_is_indeterminate(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        evaluator = make_evaluator(allowed_paths=[], expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        mock_get_changed_files.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_missing_expected_contract_fingerprint_is_indeterminate(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        evaluator = make_evaluator(expected_contract_fingerprint=None)
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert result.reason == "expected_contract_fingerprint_missing (merge-blocking)"
        mock_get_changed_files.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_missing_contract_source_id_is_indeterminate(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        evaluator = make_evaluator(contract_source_id="", expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert result.reason == "contract_source_kind/source_id missing (merge-blocking)"
        mock_get_changed_files.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_invalid_allowed_pattern_is_indeterminate(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        expected_fp = make_evaluator(expected_contract_fingerprint="SELF").compute_contract_fingerprint()
        evaluator = make_evaluator(allowed_paths=["src/**/nested"], expected_contract_fingerprint=expected_fp)
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert "invalid allowed path pattern" in result.reason
        mock_get_changed_files.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_worker_report_is_not_input(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["actual/changed/file.ts"]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["allowed_by_contract/**"])
        result = evaluator.evaluate()
        assert result.changed_files == ["actual/changed/file.ts"]
        assert result.changed_files_source == "git_diff_base_head"


class TestSingleSourceOfTruth:
    def test_gate_evaluator_script_exists_in_pr_review_judge(self):
        script_path = Path(__file__).parent.parent / "scripts" / "allowed_paths_review_gate.py"
        assert script_path.exists()

    def test_no_duplicate_gate_evaluator_in_impl_review_loop(self):
        impl_review_loop_path = Path(__file__).parent.parent.parent.parent / "impl-review-loop" / "scripts"
        if impl_review_loop_path.exists():
            for script in impl_review_loop_path.glob("**/*allowed*gate*.py"):
                if script.name != "allowed_paths_review_gate.py":
                    pytest.fail(f"Duplicate gate evaluator found: {script}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

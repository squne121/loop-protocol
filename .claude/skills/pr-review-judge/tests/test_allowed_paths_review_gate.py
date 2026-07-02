#!/usr/bin/env python3
"""Tests for allowed_paths_review_gate.py."""

from pathlib import Path
import subprocess
from unittest.mock import patch
import sys

import pytest

import_path = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(import_path))

from allowed_paths_review_gate import AllowedPathsGateEvaluator, AllowedPathsMatcher, GateStatus


BASE_ARGS = {
    "pr_number": 123,
    "base_ref": "main",
    "base_sha_at_snapshot": "abc123",
    "diff_base_sha": "abc123",
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
        # matcher v2 grammar: mid-path ** is now a valid full segment
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/**/nested") == "src/**/nested"
        # partial-segment globs remain invalid (fail-closed)
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/**suffix") is None
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
        # matcher v2 grammar: mid-path ** is now a valid full segment
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/**/nested") == "src/**/nested"
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
            "base_sha_at_snapshot": "abc",
            "diff_base_sha": "abc",
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

    def test_repeated_trailing_slash_is_rejected(self):
        assert AllowedPathsMatcher.normalize_allowed_pattern("src//") is None
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/ui//") is None


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
        fp2 = make_evaluator(
            contract_body_sha256="contract_sha_v2",
            expected_contract_fingerprint="SELF").compute_contract_fingerprint(
        )
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
        evaluator = make_evaluator(
            head_sha="current_sha",
            reviewed_head_sha="reviewed_sha",
            expected_contract_fingerprint="SELF"
        )
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
        assert result.changed_files_source == "git_diff_current_merge_base_head"
        assert result.diff_base_sha == "abc123"
        assert result.base_sha == "abc123"


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
        expected_fp = make_evaluator(
            contract_body_sha256="old_contract_sha",
            expected_contract_fingerprint="SELF").compute_contract_fingerprint(
        )
        evaluator = make_evaluator(expected_contract_fingerprint=expected_fp)
        result = evaluator.evaluate()
        assert result.status == GateStatus.STALE_SNAPSHOT.value
        mock_get_changed_files.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_base_sha_at_snapshot_change_is_stale(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        expected_fp = make_evaluator(
            base_sha_at_snapshot="base_at_snapshot",
            expected_contract_fingerprint="SELF").compute_contract_fingerprint(
        )
        evaluator = make_evaluator(
            base_sha_at_snapshot="base_moved_forward",
            expected_contract_fingerprint=expected_fp,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.STALE_SNAPSHOT.value

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_diff_base_sha_does_not_affect_freshness(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        expected_fp = make_evaluator(
            base_sha_at_snapshot="base_at_snapshot",
            diff_base_sha="diff_base_at_snapshot",
            expected_contract_fingerprint="SELF",
        ).compute_contract_fingerprint()
        evaluator = make_evaluator(
            base_sha_at_snapshot="base_at_snapshot",
            diff_base_sha="diff_base_after_update_branch",
            expected_contract_fingerprint=expected_fp,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert result.contract_fingerprint["base_sha_at_snapshot"] == "base_at_snapshot"
        assert result.diff_base_sha == "diff_base_after_update_branch"

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
        evaluator = make_evaluator(allowed_paths=["src/**suffix"], expected_contract_fingerprint=expected_fp)
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
        assert result.changed_files_source == "git_diff_current_merge_base_head"

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_files_from_git")
    def test_result_schema_includes_diff_base_sha_and_snapshot_fingerprint(self, mock_get_changed_files):
        mock_get_changed_files.return_value = ["src/main.ts"]
        evaluator = make_evaluator(
            base_sha_at_snapshot="snapshot_sha",
            diff_base_sha="merge_base_sha",
            expected_contract_fingerprint="SELF",
        )
        result = evaluator.evaluate().to_dict()
        assert result["diff_base_sha"] == "merge_base_sha"
        assert result["base_sha"] == "merge_base_sha"
        assert result["contract_fingerprint"]["base_sha_at_snapshot"] == "snapshot_sha"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write(repo: Path, relative_path: str, content: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _create_update_branch_repo(tmp_path: Path) -> dict[str, str | Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")

    _write(repo, "README.md", "base\n")
    base_sha_at_snapshot = _commit(repo, "base")

    _git(repo, "checkout", "-b", "feature")
    _write(repo, "allowed/feature.txt", "feature change\n")
    _commit(repo, "feature change")

    _git(repo, "checkout", "main")
    _write(repo, "outside/unrelated.txt", "main side change\n")
    current_base_tip = _commit(repo, "main unrelated change")

    _git(repo, "checkout", "feature")
    _git(repo, "merge", "--no-ff", "main", "-m", "update branch")
    head_sha = _git(repo, "rev-parse", "HEAD")

    return {
        "repo": repo,
        "base_sha_at_snapshot": base_sha_at_snapshot,
        "diff_base_sha": _git(repo, "merge-base", current_base_tip, head_sha),
        "head_sha": head_sha,
        "reviewed_head_sha": head_sha,
    }


class TestUpdateBranchGitDag:
    def test_update_branch_no_false_positive(self, tmp_path, monkeypatch):
        fixture = _create_update_branch_repo(tmp_path)
        monkeypatch.chdir(fixture["repo"])
        evaluator = make_evaluator(
            base_sha_at_snapshot=fixture["base_sha_at_snapshot"],
            diff_base_sha=fixture["diff_base_sha"],
            head_sha=fixture["head_sha"],
            reviewed_head_sha=fixture["reviewed_head_sha"],
            allowed_paths=["allowed/**"],
            expected_contract_fingerprint="SELF",
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert result.changed_files == ["allowed/feature.txt"]
        assert "outside/unrelated.txt" not in result.changed_files

    def test_update_branch_true_violation_blocks(self, tmp_path, monkeypatch):
        fixture = _create_update_branch_repo(tmp_path)
        repo = fixture["repo"]
        _write(repo, "forbidden/violation.txt", "feature violation\n")
        _commit(repo, "feature violation")
        head_sha = _git(repo, "rev-parse", "HEAD")
        monkeypatch.chdir(repo)
        evaluator = make_evaluator(
            base_sha_at_snapshot=fixture["base_sha_at_snapshot"],
            diff_base_sha=_git(repo, "merge-base", fixture["diff_base_sha"], head_sha),
            head_sha=head_sha,
            reviewed_head_sha=head_sha,
            allowed_paths=["allowed/**"],
            expected_contract_fingerprint="SELF",
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.FAIL_CLOSED.value
        assert any(item["file"] == "forbidden/violation.txt" for item in result.violations)


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

"""Unit tests for scripts/ci/codex_execpolicy_matrix.py (Issue #1150)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "scripts" / "ci" / "codex_execpolicy_matrix.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("codex_execpolicy_matrix_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _completed(stdout: str, stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["codex"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_given_valid_execpolicy_json_when_parsed_then_decision_and_rules_are_returned():
    result = mod.parse_execpolicy_response(
        label="ok",
        argv=["git", "status"],
        completed=_completed('{"decision":"allow","matchedRules":[{"source":"rules"}]}'),
    )
    assert result["decision"] == "allow"
    assert result["matchedRules"] == [{"source": "rules"}]


def test_given_nonzero_return_code_when_parsed_then_fail_closed():
    with pytest.raises(mod.MatrixError):
        mod.parse_execpolicy_response(
            label="nonzero",
            argv=["git", "status"],
            completed=_completed('{"decision":"allow","matchedRules":[]}', returncode=2),
        )


def test_given_missing_decision_when_parsed_then_fail_closed():
    with pytest.raises(mod.MatrixError):
        mod.parse_execpolicy_response(
            label="missing_decision",
            argv=["git", "status"],
            completed=_completed('{"matchedRules":[]}'),
        )


def test_given_extra_top_level_key_when_parsed_then_fail_closed():
    with pytest.raises(mod.MatrixError):
        mod.parse_execpolicy_response(
            label="extra_key",
            argv=["git", "status"],
            completed=_completed('{"decision":"allow","matchedRules":[],"extra":true}'),
        )


def test_given_non_list_matched_rules_when_parsed_then_fail_closed():
    with pytest.raises(mod.MatrixError):
        mod.parse_execpolicy_response(
            label="bad_rules",
            argv=["git", "status"],
            completed=_completed('{"decision":"allow","matchedRules":{}}'),
        )


def test_given_fixture_repo_when_cases_built_then_exact_cleanup_and_invalid_variants_exist(tmp_path):
    fixture = mod.build_fixture_repo(tmp_path)
    labels = {case["label"] for case in mod.execpolicy_case_definitions(fixture)}
    assert "exact_worktree_remove" in labels
    assert "exact_branch_delete" in labels
    assert "malformed_worktree_remove_contract" in labels
    assert "worktree_remove_missing_target" in labels
    assert "branch_delete_df_combined_flag" in labels
    assert "branch_delete_fd_combined_flag" in labels
    assert "branch_delete_long_unique_prefix" in labels
    assert "branch_delete_force_shortcut" in labels


def test_given_cleanup_guard_stderr_when_reason_extracted_then_reason_code_is_returned():
    reason = mod._extract_guard_reason(
        "[worktree_scope_guard] blocked: cleanup operation denied\nreason: cleanup_contract_present_but_invalid"
    )
    assert reason == "cleanup_contract_present_but_invalid"


def test_given_branch_force_alias_cases_when_built_then_execpolicy_strict_is_skipped(tmp_path):
    fixture = mod.build_fixture_repo(tmp_path)
    case = next(
        case for case in mod.execpolicy_case_definitions(fixture)
        if case["label"] == "branch_delete_df_combined_flag"
    )
    assert case["skip_execpolicy_strict"] is True
    assert case["expected_execpolicy"] == []


def test_given_npm_alias_platform_dir_when_selected_package_resolved_then_dir_name_drives_detection(tmp_path):
    openai_dir = tmp_path / "node_modules" / "@openai"
    umbrella_dir = openai_dir / "codex"
    platform_dir = openai_dir / "codex-linux-x64"
    umbrella_dir.mkdir(parents=True)
    platform_dir.mkdir(parents=True)
    (umbrella_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "@openai/codex",
                "version": "0.142.0",
                "optionalDependencies": {
                    "@openai/codex-linux-x64": "npm:@openai/codex@0.142.0-linux-x64"
                },
            }
        ),
        encoding="utf-8",
    )
    (platform_dir / "package.json").write_text(
        json.dumps({"name": "@openai/codex", "version": "0.142.0-linux-x64"}),
        encoding="utf-8",
    )
    selected_dir, selected_pkg = mod._find_selected_platform_package(umbrella_dir)
    assert selected_dir == platform_dir
    assert selected_pkg["version"] == "0.142.0-linux-x64"

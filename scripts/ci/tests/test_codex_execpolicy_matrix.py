"""Unit tests for scripts/ci/codex_execpolicy_matrix.py (Issue #1150)."""

from __future__ import annotations

import importlib.util
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
    assert "worktree_remove_missing_target" in labels
    assert "branch_delete_force_shortcut" in labels

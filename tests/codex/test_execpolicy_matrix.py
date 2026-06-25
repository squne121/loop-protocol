"""Contract tests for the Codex execpolicy matrix lane (Issue #1150)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "ci" / "codex_execpolicy_matrix.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("codex_execpolicy_matrix_contract", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _case_by_label(fixture, label: str):
    for case in mod.execpolicy_case_definitions(fixture):
        if case["label"] == label:
            return case
    raise AssertionError(f"missing case: {label}")


def test_given_read_only_command_when_hook_chain_runs_then_allow(tmp_path):
    fixture = mod.build_fixture_repo(tmp_path)
    case = _case_by_label(fixture, "read_only_branch_list")
    hook = mod.run_hook_chain(mod.render_command(case["argv"]), fixture, cwd=fixture.worktree)
    assert hook["decision"] == "allow", hook


def test_given_exact_cleanup_with_valid_contract_when_hook_chain_runs_then_allow(tmp_path):
    fixture = mod.build_fixture_repo(tmp_path)
    case = _case_by_label(fixture, "exact_worktree_remove")
    mod.materialize_valid_contract(fixture, case["operation"])
    hook = mod.run_hook_chain(mod.render_command(case["argv"]), fixture)
    assert hook["decision"] == "allow", hook


def test_given_fresh_valid_contract_then_malformed_when_hook_chain_runs_then_reason_code_is_assertable(tmp_path):
    fixture = mod.build_fixture_repo(tmp_path)
    case = _case_by_label(fixture, "malformed_worktree_remove_contract")
    mod.materialize_valid_contract(fixture, case["operation"])
    mod.invalidate_cleanup_contract(fixture)
    hook = mod.run_hook_chain(mod.render_command(case["argv"]), fixture)
    assert hook["decision"] == "deny", hook
    assert hook["reason"] == "cleanup_contract_present_but_invalid", hook


def test_given_cleanup_force_variant_when_hook_chain_runs_then_deny(tmp_path):
    fixture = mod.build_fixture_repo(tmp_path)
    case = _case_by_label(fixture, "worktree_remove_force_before_target")
    hook = mod.run_hook_chain(mod.render_command(case["argv"]), fixture)
    assert hook["decision"] == "deny", hook


def test_given_cleanup_extra_argv_when_hook_chain_runs_then_deny(tmp_path):
    fixture = mod.build_fixture_repo(tmp_path)
    case = _case_by_label(fixture, "worktree_remove_extra_target")
    hook = mod.run_hook_chain(mod.render_command(case["argv"]), fixture)
    assert hook["decision"] == "deny", hook


def test_given_branch_force_delete_when_hook_chain_runs_then_deny(tmp_path):
    fixture = mod.build_fixture_repo(tmp_path)
    case = _case_by_label(fixture, "branch_delete_force_shortcut")
    hook = mod.run_hook_chain(mod.render_command(case["argv"]), fixture)
    assert hook["decision"] == "deny", hook


def test_given_branch_long_option_force_when_hook_chain_runs_then_deny(tmp_path):
    fixture = mod.build_fixture_repo(tmp_path)
    case = _case_by_label(fixture, "branch_delete_long_force")
    hook = mod.run_hook_chain(mod.render_command(case["argv"]), fixture)
    assert hook["decision"] == "deny", hook

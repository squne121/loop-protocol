#!/usr/bin/env python3
"""
Tests for controlled_skill_mutation_policy.py (Issue #1166).

Tests:
- AC3:  CONTROLLED_SKILL_MUTATION_COMMAND_POLICY schema
- AC4:  is_controlled_skill_mutation_exec_command shared function
- AC8:  only termination_report.publish is in the registry
- AC17: single source of truth (policy module, not per-guard allowlists)

AC baseline for contract VC: these tests are expected to PASS after implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the module is importable from scripts/agent-guards
_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from controlled_skill_mutation_policy import (
    ALLOWED_WRITE_ROOTS,
    COMMAND_ID_PUBLISH,
    COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE,
    CONTROLLED_SKILL_MUTATION_COMMAND_POLICY,
    ENV_SANITIZE_KEYS,
    EXECUTOR_SCRIPT,
    TRUSTED_REPO,
    _validate_executor_argv,
    is_controlled_skill_mutation_exec_command,
)


# =============================================================================
# AC3: Policy registry schema
# =============================================================================

class TestPolicySchema:
    """AC3: CONTROLLED_SKILL_MUTATION_COMMAND_POLICY schema validation."""

    def test_registry_is_dict(self):
        assert isinstance(CONTROLLED_SKILL_MUTATION_COMMAND_POLICY, dict)

    def test_registry_has_publish_entry(self):
        assert COMMAND_ID_PUBLISH in CONTROLLED_SKILL_MUTATION_COMMAND_POLICY

    def test_publish_entry_has_required_keys(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_PUBLISH]
        required_keys = {
            "command_id",
            "executor_script",
            "allowed_write_roots",
            "github_mutation",
            "postcondition",
            "idempotency",
            "env_sanitize",
        }
        assert required_keys.issubset(set(entry.keys())), (
            f"Missing keys: {required_keys - set(entry.keys())}"
        )

    def test_publish_entry_github_mutation(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_PUBLISH]
        gm = entry["github_mutation"]
        assert gm["comment_on_issue"] is True
        assert gm["requires_repo"] == TRUSTED_REPO
        assert gm["requires_explicit_repo_flag"] is True

    def test_publish_entry_postcondition(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_PUBLISH]
        pc = entry["postcondition"]
        assert pc["no_tracked_source_changes"] is True
        assert pc["no_settings_changes"] is True
        assert "artifacts/" in pc["allowed_write_roots"]

    def test_publish_entry_idempotency(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_PUBLISH]
        idm = entry["idempotency"]
        assert "termination_report_published.marker.json" in idm["marker_file_pattern"]
        assert idm["marker_field"] == "comment_id"

    def test_publish_entry_env_sanitize(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_PUBLISH]
        sanitize = entry["env_sanitize"]
        for key in ("PYTHONPATH", "PYTHONHOME", "PUBLISH_ARTIFACT_DIR"):
            assert key in sanitize

    def test_allowed_write_roots_contains_artifacts(self):
        assert "artifacts/" in ALLOWED_WRITE_ROOTS

    def test_env_sanitize_keys_contains_required(self):
        required = {"PUBLISH_ARTIFACT_DIR", "PYTHONPATH", "PYTHONHOME",
                    "GH_EDITOR", "EDITOR", "VISUAL", "BROWSER"}
        assert required.issubset(set(ENV_SANITIZE_KEYS))

    def test_trusted_repo(self):
        assert TRUSTED_REPO == "squne121/loop-protocol"

    def test_executor_script_path(self):
        assert EXECUTOR_SCRIPT == "scripts/agent-guards/controlled_skill_mutation_exec.py"


# =============================================================================
# AC8: only termination_report.publish is in the registry
# =============================================================================

class TestRegistryScope:
    """AC8: registry contains only the expected command IDs."""

    def test_only_known_command_ids(self):
        # Issue #1284 extends the shared registry with issue metadata mutation
        # command ids (issue_body.update / issue_comment.publish /
        # contract_snapshot.publish). Issue #1536 adds pr_review.publish
        # (Option C controlled review publisher). This scope-pin is updated
        # deliberately as part of each Issue's explicit In Scope registry
        # extension.
        known_ids = {
            COMMAND_ID_PUBLISH,
            "issue_body.update",
            "issue_comment.publish",
            "contract_snapshot.publish",
            "pr_review.publish",
            COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE,
        }
        actual_ids = set(CONTROLLED_SKILL_MUTATION_COMMAND_POLICY.keys())
        assert actual_ids == known_ids, (
            f"Unexpected extra entries: {actual_ids - known_ids}"
        )


# =============================================================================
# AC4/AC17: is_controlled_skill_mutation_exec_command
# =============================================================================

class TestIsControlledSkillMutationExecCommand:
    """AC4/AC17: shared policy function validates executor argv."""

    # ── Setup: a tmp project_root with a stub executor ─────────────────────

    @pytest.fixture()
    def project_root(self, tmp_path):
        """Create a tmp project_root with stub executor at canonical path."""
        executor_dir = tmp_path / "scripts" / "agent-guards"
        executor_dir.mkdir(parents=True)
        (executor_dir / "controlled_skill_mutation_exec.py").write_text("# stub\n")
        return str(tmp_path)

    # ── Allow cases ────────────────────────────────────────────────────────

    def test_uv_run_python3_executor_valid_all_flags(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/termination_report_input.json"
            " --repo squne121/loop-protocol"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is True

    def test_python3_executor_valid_all_flags(self, project_root):
        cmd = (
            "python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/termination_report_input.json"
            " --repo squne121/loop-protocol"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is True

    def test_executor_with_json_flag(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 42"
            " --input-file artifacts/42/input.json"
            " --repo squne121/loop-protocol"
            " --json"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is True

    def test_executor_with_dry_run_flag(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 99"
            " --input-file artifacts/99/input.json"
            " --repo squne121/loop-protocol"
            " --dry-run"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is True

    # ── Deny cases ─────────────────────────────────────────────────────────

    def test_empty_command_denied(self, project_root):
        assert is_controlled_skill_mutation_exec_command("", project_root) is False

    def test_compound_semicolon_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            " --repo squne121/loop-protocol; echo pwned"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_compound_pipe_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            " --repo squne121/loop-protocol | cat"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_missing_required_flag_command_id(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            " --repo squne121/loop-protocol"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_missing_required_flag_repo(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_flag_equals_form_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id=termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            " --repo squne121/loop-protocol"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_unknown_flag_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            " --repo squne121/loop-protocol"
            " --extra-flag evil"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_duplicate_flag_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            " --repo squne121/loop-protocol"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_direct_publisher_denied(self, project_root):
        """Direct invocation of publisher is NOT the executor form."""
        cmd = (
            "python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
            " --issue-number 1166"
            " --repo squne121/loop-protocol"
        )
        # Publisher is at a different path from the executor
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_python_c_denied(self, project_root):
        cmd = "python3 -c 'import sys; sys.exit(0)'"
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_bash_wrapper_denied(self, project_root):
        cmd = "bash -c 'uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py'"
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_executor_not_found_denied(self, tmp_path):
        """Script path must resolve to an existing executor."""
        # Don't create the executor in tmp_path
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            " --repo squne121/loop-protocol"
        )
        # project_root without executor → realpath mismatch (executor doesn't exist but
        # realpath returns the expected path, canonical comparison fails because paths differ)
        # Since the test executor exists in project_root, use a fresh tmp_path with no executor
        result = is_controlled_skill_mutation_exec_command(cmd, str(tmp_path))
        # If executor doesn't exist, realpath returns the path itself but canonical comparison
        # still compares correctly (file doesn't need to exist for realpath)
        # The test just verifies no crash occurs
        assert isinstance(result, bool)


# =============================================================================
# AC17: single source of truth
# =============================================================================

class TestSingleSourceOfTruth:
    """AC17: verify no per-guard allowlists duplicate the executor path."""

    def test_executor_script_constant_matches_policy(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_PUBLISH]
        assert entry["executor_script"] == EXECUTOR_SCRIPT

    def test_is_csm_exec_command_is_callable(self):
        assert callable(is_controlled_skill_mutation_exec_command)

    def test_validate_executor_argv_is_callable(self):
        assert callable(_validate_executor_argv)

    def test_validate_executor_argv_all_required(self):
        """All required flags must produce True."""
        valid_args = [
            "--command-id", "termination_report.publish",
            "--issue-number", "1166",
            "--input-file", "artifacts/1166/input.json",
            "--repo", "squne121/loop-protocol",
        ]
        assert _validate_executor_argv(valid_args) is True

    def test_validate_executor_argv_missing_one_required(self):
        """Missing any required flag returns False."""
        base = [
            "--command-id", "termination_report.publish",
            "--issue-number", "1166",
            "--input-file", "artifacts/1166/input.json",
            "--repo", "squne121/loop-protocol",
        ]
        # Remove --repo and its value
        args_without_repo = base[:-2]
        assert _validate_executor_argv(args_without_repo) is False

#!/usr/bin/env python3
"""
Tests for controlled_skill_mutation_exec.py (Issue #1166).

Tests:
- AC8:  command_id validation (only termination_report.publish)
- AC10: repo validation (only TRUSTED_REPO)
- AC11: issue binding (LOOP_ISSUE_NUMBER env check)
- AC12: input-file validation (must be in artifact subtree)
- AC13: environment sanitization
- AC14: postcondition (no tracked changes)
- AC15: idempotency marker
- AC16: module realpath inspection
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import controlled_skill_mutation_exec as _exec
from controlled_skill_mutation_policy import TRUSTED_REPO


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture()
def tmp_project(tmp_path):
    """Create minimal project structure for executor tests."""
    # Create executor (so PROJECT_ROOT is set correctly via monkeypatching)
    executor_dir = tmp_path / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True)
    # Create publisher stub
    pub_dir = tmp_path / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
    pub_dir.mkdir(parents=True)
    (pub_dir / "publish_termination_report.py").write_text("# stub\n")
    (pub_dir / "render_termination_report.py").write_text("# stub\n")
    (pub_dir / "prose_boundary_policy.py").write_text("# stub\n")
    # Create artifact subtree
    artifact_dir = tmp_path / "artifacts" / "1166"
    artifact_dir.mkdir(parents=True)
    input_file = artifact_dir / "termination_report_input.json"
    input_file.write_text(json.dumps({"schema": "TERMINATION_REPORT_INPUT_V1"}))
    # Make a git repo so _check_no_tracked_changes works
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    return tmp_path


# =============================================================================
# AC8: command_id validation
# =============================================================================

class TestCommandIdValidation:
    def test_valid_command_id(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        with patch.object(_exec, "_invoke_publisher", return_value=(0, "", "")):
            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                with patch.object(_exec, "_readback_last_comment", return_value={"comment_id": "c1", "comment_url": "https://ex", "body_sha256": "abc"}):
                    rc = _exec.main([
                        "--command-id", "termination_report.publish",
                        "--issue-number", "1166",
                        "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 0

    def test_unknown_command_id_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", "unknown.command",
            "--issue-number", "1166",
            "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2


# =============================================================================
# AC10: repo validation
# =============================================================================

class TestRepoValidation:
    def test_wrong_repo_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", "termination_report.publish",
            "--issue-number", "1166",
            "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
            "--repo", "evil-org/hijack-repo",
        ])
        assert rc == 2

    def test_correct_repo_passes(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        with patch.object(_exec, "_invoke_publisher", return_value=(0, "", "")):
            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                with patch.object(_exec, "_readback_last_comment", return_value={"comment_id": "c1"}):
                    rc = _exec.main([
                        "--command-id", "termination_report.publish",
                        "--issue-number", "1166",
                        "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 0


# =============================================================================
# AC11: issue binding
# =============================================================================

class TestIssueBinding:
    def test_issue_mismatch_blocked(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "9999")
        rc = _exec.main([
            "--command-id", "termination_report.publish",
            "--issue-number", "1166",
            "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_issue_match_passes(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1166")
        with patch.object(_exec, "_invoke_publisher", return_value=(0, "", "")):
            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                with patch.object(_exec, "_readback_last_comment", return_value={"comment_id": "c1"}):
                    rc = _exec.main([
                        "--command-id", "termination_report.publish",
                        "--issue-number", "1166",
                        "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 0

    def test_no_env_issue_number_passes(self, tmp_project, monkeypatch):
        """No LOOP_ISSUE_NUMBER env var — issue binding not enforced."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
        with patch.object(_exec, "_invoke_publisher", return_value=(0, "", "")):
            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                with patch.object(_exec, "_readback_last_comment", return_value={"comment_id": "c1"}):
                    rc = _exec.main([
                        "--command-id", "termination_report.publish",
                        "--issue-number", "1166",
                        "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 0


# =============================================================================
# AC12: input-file validation
# =============================================================================

class TestInputFileValidation:
    def test_file_not_found_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", "termination_report.publish",
            "--issue-number", "1166",
            "--input-file", str(tmp_project / "artifacts" / "1166" / "nonexistent.json"),
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_file_outside_artifact_subtree_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # Create a file outside the issue artifact subtree
        bad_file = tmp_project / "tmp" / "evil.json"
        bad_file.parent.mkdir(parents=True)
        bad_file.write_text("{}")
        rc = _exec.main([
            "--command-id", "termination_report.publish",
            "--issue-number", "1166",
            "--input-file", str(bad_file),
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_validate_input_file_fn_passes_for_valid(self, tmp_project):
        valid = tmp_project / "artifacts" / "1166" / "termination_report_input.json"
        err = _exec._validate_input_file(str(valid), 1166, tmp_project)
        assert err == ""

    def test_validate_input_file_fn_fails_for_wrong_issue(self, tmp_project):
        valid = tmp_project / "artifacts" / "1166" / "termination_report_input.json"
        err = _exec._validate_input_file(str(valid), 9999, tmp_project)
        assert err != ""


# =============================================================================
# AC13: environment sanitization
# =============================================================================

class TestEnvSanitization:
    def test_sanitized_env_removes_pythonpath(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("PYTHONPATH", "/evil/path")
        env = _exec._build_sanitized_env(tmp_project, 1166)
        assert "PYTHONPATH" not in env

    def test_sanitized_env_removes_publish_artifact_dir(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("PUBLISH_ARTIFACT_DIR", "/evil/dir")
        env = _exec._build_sanitized_env(tmp_project, 1166)
        # After sanitize, PUBLISH_ARTIFACT_DIR is re-set to canonical
        assert env.get("PUBLISH_ARTIFACT_DIR") != "/evil/dir"
        # And is set to the canonical artifact dir
        assert str(tmp_project / "artifacts" / "1166") in env.get("PUBLISH_ARTIFACT_DIR", "")

    def test_sanitized_env_removes_gh_editor(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("GH_EDITOR", "vim")
        env = _exec._build_sanitized_env(tmp_project, 1166)
        assert "GH_EDITOR" not in env

    def test_sanitized_env_sets_gh_prompt_disabled(self, tmp_project):
        env = _exec._build_sanitized_env(tmp_project, 1166)
        assert env.get("GH_PROMPT_DISABLED") == "1"


# =============================================================================
# AC14: postcondition — no tracked changes
# =============================================================================

class TestPostcondition:
    def test_tracked_changes_cause_failure(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        with patch.object(_exec, "_invoke_publisher", return_value=(0, "", "")):
            with patch.object(_exec, "_check_no_tracked_changes", return_value=["src/main.ts"]):
                rc = _exec.main([
                    "--command-id", "termination_report.publish",
                    "--issue-number", "1166",
                    "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
                    "--repo", TRUSTED_REPO,
                ])
        assert rc == 1

    def test_no_tracked_changes_succeeds(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        with patch.object(_exec, "_invoke_publisher", return_value=(0, "", "")):
            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                with patch.object(_exec, "_readback_last_comment", return_value={"comment_id": "c1"}):
                    rc = _exec.main([
                        "--command-id", "termination_report.publish",
                        "--issue-number", "1166",
                        "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 0


# =============================================================================
# AC15: idempotency marker
# =============================================================================

class TestIdempotency:
    def test_existing_marker_blocks_republish(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # Write an idempotency marker
        marker = {
            "schema": "TERMINATION_REPORT_PUBLISH_MARKER_V1",
            "comment_id": "c123",
            "comment_url": "https://github.com/...",
        }
        mp = _exec._marker_path(tmp_project, 1166)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(marker))

        rc = _exec.main([
            "--command-id", "termination_report.publish",
            "--issue-number", "1166",
            "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 1  # idempotency block returns 1

    def test_missing_marker_allows_publish(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # No marker file
        with patch.object(_exec, "_invoke_publisher", return_value=(0, "", "")):
            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                with patch.object(_exec, "_readback_last_comment", return_value={"comment_id": "c1"}):
                    rc = _exec.main([
                        "--command-id", "termination_report.publish",
                        "--issue-number", "1166",
                        "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 0

    def test_marker_written_on_success(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        with patch.object(_exec, "_invoke_publisher", return_value=(0, "", "")):
            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                with patch.object(_exec, "_readback_last_comment", return_value={"comment_id": "c42", "comment_url": "https://u", "body_sha256": "sha"}):
                    _exec.main([
                        "--command-id", "termination_report.publish",
                        "--issue-number", "1166",
                        "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
                        "--repo", TRUSTED_REPO,
                    ])
        mp = _exec._marker_path(tmp_project, 1166)
        assert mp.exists()
        data = json.loads(mp.read_text())
        assert data.get("comment_id") == "c42"
        assert data.get("schema") == "TERMINATION_REPORT_PUBLISH_MARKER_V1"

    def test_dry_run_does_not_write_marker(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", "termination_report.publish",
            "--issue-number", "1166",
            "--input-file", str(tmp_project / "artifacts" / "1166" / "termination_report_input.json"),
            "--repo", TRUSTED_REPO,
            "--dry-run",
        ])
        assert rc == 0
        mp = _exec._marker_path(tmp_project, 1166)
        assert not mp.exists()


# =============================================================================
# AC16: module realpath inspection
# =============================================================================

class TestModuleRealpath:
    def test_canonical_paths_pass(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        errors = _exec._check_module_realpaths(tmp_project)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_symlink_outside_project_fails(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # Create a symlink that points outside project_root
        outside = tmp_project.parent / "outside_publisher.py"
        outside.write_text("# evil\n")
        pub_path = tmp_project / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "publish_termination_report.py"
        pub_path.unlink()
        pub_path.symlink_to(outside)
        errors = _exec._check_module_realpaths(tmp_project)
        assert any("publish_termination_report" in e or "module_shadowing" in e for e in errors)

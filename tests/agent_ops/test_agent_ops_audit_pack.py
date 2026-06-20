"""
test_agent_ops_audit_pack.py - Tests for agent_ops_audit_pack.py (Issue #1022).

Verifies:
- AC1: --artifact-out produces a JSON file
- AC2: stdout is <= 2048 bytes and starts with EVIDENCE:
- AC3: artifact contains required fields (schema, repo_root, cwd_valid, codex_hook_surface)
- AC4: artifact does not contain raw body / secret-like strings
- AC5: codex_hook_surface contains hooks_json_exists and config_toml_has_inline_hooks
- AC6: artifact contains coverage_gaps field
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

import agent_ops_audit_pack as aap


FAKE_REPO = "squne121/loop-protocol"
FAKE_ISSUE = 1014
FAKE_TASK_KIND = "issue-refinement-ops-review"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def run_audit_pack(extra_args: list[str] | None = None, artifact_out: Path | None = None) -> tuple[int, str, Path]:
    """Run agent_ops_audit_pack.py as subprocess. Returns (exit_code, stdout, artifact_path)."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)

    if artifact_out is not None:
        out_path = artifact_out

    cmd = [
        "uv", "run", "python3",
        str(SCRIPTS_DIR / "agent_ops_audit_pack.py"),
        "--task-kind", FAKE_TASK_KIND,
        "--issue-number", str(FAKE_ISSUE),
        "--repo", FAKE_REPO,
        "--artifact-out", str(out_path),
    ]
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    return result.returncode, result.stdout, out_path


# ──────────────────────────────────────────────────────────────────────────────
# AC1: artifact JSON is generated
# ──────────────────────────────────────────────────────────────────────────────

class TestAC1ArtifactGeneration:
    def test_artifact_file_created(self):
        """GIVEN valid args WHEN script runs THEN artifact JSON file is created."""
        exit_code, stdout, out_path = run_audit_pack()
        try:
            assert exit_code == 0, f"exit={exit_code}, stdout={stdout!r}"
            assert out_path.exists(), "artifact file was not created"
            with open(out_path) as f:
                data = json.load(f)
            assert isinstance(data, dict)
        finally:
            out_path.unlink(missing_ok=True)

    def test_artifact_valid_json(self):
        """GIVEN script runs THEN artifact is valid JSON."""
        exit_code, stdout, out_path = run_audit_pack()
        try:
            assert exit_code == 0
            with open(out_path) as f:
                content = f.read()
            # Should not raise
            data = json.loads(content)
            assert data["schema"] == "AGENT_OPS_AUDIT_PACK_V1"
        finally:
            out_path.unlink(missing_ok=True)

    def test_log_file_optional_absent(self):
        """GIVEN no --log-file WHEN script runs THEN artifact has no log_file_noted key."""
        exit_code, stdout, out_path = run_audit_pack()
        try:
            assert exit_code == 0
            with open(out_path) as f:
                data = json.load(f)
            # log_file_noted should be absent when not provided
            assert "log_file_noted" not in data
        finally:
            out_path.unlink(missing_ok=True)

    def test_log_file_optional_present(self, tmp_path):
        """GIVEN --log-file WHEN script runs THEN artifact records log file existence."""
        log_file = tmp_path / "sample.log"
        log_file.write_text("some log content\n")

        exit_code, stdout, out_path = run_audit_pack(
            extra_args=["--log-file", str(log_file)]
        )
        try:
            assert exit_code == 0
            with open(out_path) as f:
                data = json.load(f)
            assert "log_file_noted" in data
            assert data["log_file_exists"] is True
            # AC4: raw content must not appear
            artifact_str = json.dumps(data)
            assert "some log content" not in artifact_str
        finally:
            out_path.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# AC2: stdout constraints
# ──────────────────────────────────────────────────────────────────────────────

class TestAC2StdoutConstraints:
    def test_stdout_starts_with_evidence(self):
        """GIVEN script runs THEN stdout starts with 'EVIDENCE:'."""
        exit_code, stdout, out_path = run_audit_pack()
        try:
            assert stdout.startswith("EVIDENCE:"), f"stdout={stdout!r}"
        finally:
            out_path.unlink(missing_ok=True)

    def test_stdout_within_budget(self):
        """GIVEN script runs THEN stdout <= 2048 UTF-8 bytes."""
        exit_code, stdout, out_path = run_audit_pack()
        try:
            byte_len = len(stdout.encode("utf-8"))
            assert byte_len <= 2048, f"stdout {byte_len} bytes exceeds budget"
        finally:
            out_path.unlink(missing_ok=True)

    def test_stdout_no_raw_body(self):
        """GIVEN script runs THEN stdout does not contain raw issue body content."""
        exit_code, stdout, out_path = run_audit_pack()
        try:
            # stdout should be just the EVIDENCE line - no JSON dump of artifact
            lines = stdout.strip().splitlines()
            assert len(lines) <= 3, f"stdout has too many lines: {stdout!r}"
        finally:
            out_path.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# AC3: required artifact fields
# ──────────────────────────────────────────────────────────────────────────────

class TestAC3RequiredFields:
    @pytest.fixture(autouse=True)
    def load_artifact(self):
        exit_code, stdout, out_path = run_audit_pack()
        self._out_path = out_path
        try:
            with open(out_path) as f:
                self.data = json.load(f)
            yield
        finally:
            out_path.unlink(missing_ok=True)

    def test_schema_field(self):
        assert self.data["schema"] == "AGENT_OPS_AUDIT_PACK_V1"

    def test_repo_root_field(self):
        assert "repo_root" in self.data
        assert isinstance(self.data["repo_root"], str)
        assert len(self.data["repo_root"]) > 0

    def test_cwd_valid_field(self):
        assert "cwd_valid" in self.data
        assert isinstance(self.data["cwd_valid"], bool)

    def test_worktree_state_field(self):
        assert "worktree_state" in self.data
        assert self.data["worktree_state"] in ("clean", "dirty", "unknown")

    def test_hook_scripts_field(self):
        assert "hook_scripts" in self.data
        assert isinstance(self.data["hook_scripts"], list)

    def test_codex_hook_surface_field(self):
        assert "codex_hook_surface" in self.data
        assert isinstance(self.data["codex_hook_surface"], dict)

    def test_related_issues_field(self):
        assert "related_issues" in self.data
        assert isinstance(self.data["related_issues"], list)

    def test_task_kind_field(self):
        assert self.data["task_kind"] == FAKE_TASK_KIND

    def test_issue_number_field(self):
        assert self.data["issue_number"] == FAKE_ISSUE

    def test_repo_field(self):
        assert self.data["repo"] == FAKE_REPO


# ──────────────────────────────────────────────────────────────────────────────
# AC4: no raw body / secret-like values
# ──────────────────────────────────────────────────────────────────────────────

class TestAC4NoRawContent:
    SECRET_PATTERNS = ["/etc/passwd", ".env", "credential", "password", "api_key"]

    def _load_artifact(self) -> tuple[dict, Path]:
        exit_code, stdout, out_path = run_audit_pack()
        with open(out_path) as f:
            data = json.load(f)
        return data, out_path

    def test_no_secret_paths_in_artifact_values(self):
        """GIVEN artifact THEN no secret-like path strings appear as values."""
        data, out_path = self._load_artifact()
        try:
            artifact_str = json.dumps(data)
            # redacted_fields list is allowed to name the categories
            # but the actual sensitive values must not appear
            for pattern in ["/etc/passwd", "raw_body_content", "raw_comments_content"]:
                assert pattern not in artifact_str or pattern in str(data.get("redacted_fields", [])), \
                    f"secret pattern {pattern!r} found in artifact"
        finally:
            out_path.unlink(missing_ok=True)

    def test_redacted_fields_declared(self):
        """GIVEN artifact THEN redacted_fields declares what was excluded."""
        data, out_path = self._load_artifact()
        try:
            assert "redacted_fields" in data
            redacted = data["redacted_fields"]
            assert "log_file_content" in redacted
            assert "raw_issue_body" in redacted
            assert "raw_comments" in redacted
        finally:
            out_path.unlink(missing_ok=True)

    def test_no_raw_issue_body_key(self):
        """GIVEN artifact THEN raw_issue_body key is absent (not just null)."""
        data, out_path = self._load_artifact()
        try:
            assert "raw_issue_body" not in data
            assert "raw_comments" not in data
            assert "log_file_content" not in data
        finally:
            out_path.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# AC5: codex_hook_surface structure
# ──────────────────────────────────────────────────────────────────────────────

class TestAC5CodexHookSurface:
    def _load_surface(self) -> tuple[dict, Path]:
        exit_code, stdout, out_path = run_audit_pack()
        with open(out_path) as f:
            data = json.load(f)
        return data.get("codex_hook_surface", {}), out_path

    def test_hooks_json_exists_field(self):
        """GIVEN codex_hook_surface THEN hooks_json_exists is present."""
        surface, out_path = self._load_surface()
        try:
            assert "hooks_json_exists" in surface
            assert isinstance(surface["hooks_json_exists"], bool)
        finally:
            out_path.unlink(missing_ok=True)

    def test_config_toml_has_inline_hooks_field(self):
        """GIVEN codex_hook_surface THEN config_toml_has_inline_hooks is present."""
        surface, out_path = self._load_surface()
        try:
            assert "config_toml_has_inline_hooks" in surface
            assert isinstance(surface["config_toml_has_inline_hooks"], bool)
        finally:
            out_path.unlink(missing_ok=True)

    def test_hooks_json_events_field(self):
        """GIVEN codex_hook_surface THEN hooks_json_events is a list."""
        surface, out_path = self._load_surface()
        try:
            assert "hooks_json_events" in surface
            assert isinstance(surface["hooks_json_events"], list)
        finally:
            out_path.unlink(missing_ok=True)

    def test_codex_hooks_json_actually_exists(self):
        """GIVEN repo has .codex/hooks.json THEN hooks_json_exists is True."""
        surface, out_path = self._load_surface()
        try:
            # The repo does have .codex/hooks.json
            if (REPO_ROOT / ".codex" / "hooks.json").exists():
                assert surface["hooks_json_exists"] is True
        finally:
            out_path.unlink(missing_ok=True)

    def test_config_toml_no_inline_hooks(self):
        """GIVEN project convention THEN config.toml has no inline hooks."""
        surface, out_path = self._load_surface()
        try:
            # Per project convention: hooks live in hooks.json not config.toml
            assert surface["config_toml_has_inline_hooks"] is False
        finally:
            out_path.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# AC6: coverage_gaps field
# ──────────────────────────────────────────────────────────────────────────────

class TestAC6CoverageGaps:
    def test_coverage_gaps_field_present(self):
        """GIVEN artifact THEN coverage_gaps field is present."""
        exit_code, stdout, out_path = run_audit_pack()
        try:
            with open(out_path) as f:
                data = json.load(f)
            assert "coverage_gaps" in data
            assert isinstance(data["coverage_gaps"], list)
        finally:
            out_path.unlink(missing_ok=True)

    def test_coverage_gaps_is_list(self):
        """GIVEN artifact THEN coverage_gaps is a JSON array."""
        exit_code, stdout, out_path = run_audit_pack()
        try:
            with open(out_path) as f:
                data = json.load(f)
            gaps = data["coverage_gaps"]
            assert isinstance(gaps, list)
            # Each gap if present should be a dict
            for gap in gaps:
                assert isinstance(gap, dict), f"gap entry not a dict: {gap!r}"
        finally:
            out_path.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Unit tests for internal helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestInternalHelpers:
    def test_is_secret_like_detects_etc_passwd(self):
        assert aap._is_secret_like("/etc/passwd") is True

    def test_is_secret_like_detects_dotenv(self):
        assert aap._is_secret_like(".env") is True

    def test_is_secret_like_safe_path(self):
        assert aap._is_secret_like("scripts/agent_ops_audit_pack.py") is False

    def test_check_cwd_valid_returns_bool(self):
        result = aap._check_cwd_valid()
        assert isinstance(result, bool)
        assert result is True  # running in valid cwd

    def test_get_worktree_state_returns_known_value(self):
        state = aap._get_worktree_state()
        assert state in ("clean", "dirty", "unknown")

    def test_get_codex_hook_surface_structure(self):
        surface = aap._get_codex_hook_surface(REPO_ROOT)
        assert "hooks_json_exists" in surface
        assert "hooks_json_events" in surface
        assert "config_toml_has_inline_hooks" in surface

    def test_build_audit_pack_returns_dict(self):
        """GIVEN mocked gh api THEN build_audit_pack returns valid dict."""
        with patch("agent_ops_audit_pack._get_related_issues") as mock_issues, \
             patch("agent_ops_audit_pack._get_coverage_gaps") as mock_gaps:
            mock_issues.return_value = [{"number": 1014, "title": "test", "state": "open"}]
            mock_gaps.return_value = []

            result = aap.build_audit_pack(
                task_kind=FAKE_TASK_KIND,
                issue_number=FAKE_ISSUE,
                repo=FAKE_REPO,
                repo_root=REPO_ROOT,
            )

        assert result["schema"] == "AGENT_OPS_AUDIT_PACK_V1"
        assert result["issue_number"] == FAKE_ISSUE
        assert result["repo"] == FAKE_REPO
        assert "cwd_valid" in result
        assert "worktree_state" in result
        assert "codex_hook_surface" in result
        assert "tool_availability" in result
        assert "redacted_fields" in result

    def test_get_related_issues_api_error_returns_error_dict(self):
        """GIVEN gh api unavailable THEN returns error dict without raising."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = aap._get_related_issues("owner/repo", 999)
        assert isinstance(result, list)
        assert len(result) == 1
        assert "error" in result[0]

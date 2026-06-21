"""Tests for setup_check.py.

Test execution (with dependencies):
    uv run --with pytest --with pyyaml python -m pytest tests/

Cases covered:
    1. All checks pass → exit 0
    2. node missing → exit != 0
    3. trustedFolders.json existing TRUST_FOLDER (dict) → no-op (values preserved)
    4. trustedFolders.json existing TRUST_PARENT (dict) → no-op (values preserved)
    5. trustedFolders.json absent → file created with repo root in dict format
    6. .gemini/settings.json absent → template created
    7. .gemini/settings.json present → not overwritten
    8. Serena MCP available → ok True
    9. Serena MCP unavailable → recovery hint present
    10. trustedFolders.json dict existing entries preserved after adding new entry (AC3)
    11. New entry appended as dict {path: TRUST_FOLDER} (AC4)
    12. TRUST_PARENT ancestor in dict form → no-op (AC5)
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------


def load_setup_check():
    path = Path(__file__).resolve().parent.parent / "scripts" / "setup_check.py"
    spec = importlib.util.spec_from_file_location("setup_check", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    cp: subprocess.CompletedProcess = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


# ---------------------------------------------------------------------------
# AC1: All checks pass → exit 0
# ---------------------------------------------------------------------------


def test_all_checks_pass_exit_code_zero(tmp_path):
    """GIVEN all dependencies are present and configured
    WHEN run_all_checks is called
    THEN exit_code is 0 and ok is True."""
    sc = load_setup_check()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # Pre-create .gemini/settings.json so settings check finds it (exists).
    settings_dir = repo_root / ".gemini"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps({"mcp": {"allowed": ["serena"]}}), encoding="utf-8"
    )

    # Pre-create trustedFolders.json with repo root already trusted (dict schema).
    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    gemini_home = home_dir / ".gemini"
    gemini_home.mkdir()
    trusted_path = gemini_home / "trustedFolders.json"
    trusted_path.write_text(json.dumps({str(repo_root): "TRUST_FOLDER"}), encoding="utf-8")

    def _run_side_effect(command: list[str], timeout: int | None = None):
        tool = command[0] if command else ""
        if tool == "git" and "--show-toplevel" in command:
            return _make_completed(0, stdout=str(repo_root) + "\n")
        version_map = {
            "node": "v22.0.0",
            "gemini": "0.1.0",
            "python3": "Python 3.12.0",
            "uv": "uv 0.5.0",
            "uvx": "uvx 0.5.0",
        }
        if tool in version_map and "--version" in command:
            return _make_completed(0, stdout=version_map[tool])
        if "serena" in command and "--help" in command:
            return _make_completed(0, stdout="Usage: serena [OPTIONS]")
        if tool == "gemini" and "--prompt" in command:
            return _make_completed(0, stdout="ok")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        with patch.object(Path, "home", return_value=home_dir):
            result = sc.run_all_checks(repo_root=repo_root)

    assert result["ok"] is True
    assert result["exit_code"] == 0
    # AC1: all tool versions present in output
    for tool in sc.REQUIRED_TOOLS:
        assert result["tools"]["versions"][tool] is not None, f"{tool} version must be present"


# ---------------------------------------------------------------------------
# AC2 / AC5: node missing → exit != 0 + recovery hint
# ---------------------------------------------------------------------------


def test_node_missing_exit_nonzero(tmp_path):
    """GIVEN node is not installed
    WHEN check_tools is called
    THEN ok is False and recovery hints are present."""
    sc = load_setup_check()

    def _run_side_effect(command: list[str], timeout: int | None = None):
        tool = command[0] if command else ""
        if tool == "node":
            raise FileNotFoundError("node not found")
        version_map = {
            "gemini": "0.1.0",
            "python3": "Python 3.12.0",
            "uv": "uv 0.5.0",
            "uvx": "uvx 0.5.0",
        }
        if tool in version_map:
            return _make_completed(0, stdout=version_map[tool])
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_tools()

    assert result["ok"] is False
    assert "node" in result["missing"]
    assert "recovery" in result
    assert len(result["recovery"]) > 0


def test_node_missing_run_all_exit_nonzero(tmp_path):
    """GIVEN node is not installed
    WHEN run_all_checks is called
    THEN overall exit_code is non-zero."""
    sc = load_setup_check()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _run_side_effect(command: list[str], timeout: int | None = None):
        tool = command[0] if command else ""
        if tool == "node":
            raise FileNotFoundError("node not found")
        if tool == "git" and "--show-toplevel" in command:
            return _make_completed(0, stdout=str(repo_root) + "\n")
        version_map = {
            "gemini": "0.1.0",
            "python3": "Python 3.12.0",
            "uv": "uv 0.5.0",
            "uvx": "uvx 0.5.0",
        }
        if tool in version_map:
            return _make_completed(0, stdout=version_map[tool])
        if "serena" in command and "--help" in command:
            return _make_completed(0, stdout="Usage: serena [OPTIONS]")
        if tool == "gemini" and "--prompt" in command:
            return _make_completed(0, stdout="ok")
        return _make_completed(0)

    fake_home = tmp_path / "fakehome2"
    fake_home.mkdir()

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        with patch.object(Path, "home", return_value=fake_home):
            result = sc.run_all_checks(repo_root=repo_root)

    assert result["ok"] is False
    assert result["exit_code"] != 0


# ---------------------------------------------------------------------------
# AC2: trustedFolders.json — TRUST_FOLDER exact match → no-op (dict schema)
# ---------------------------------------------------------------------------


def test_trusted_folders_exact_match_noop(tmp_path):
    """GIVEN trustedFolders.json already contains the exact repo root as TRUST_FOLDER (dict schema)
    WHEN check_trusted_folders is called
    THEN no-op — status is already_trusted and existing dict entries are not modified."""
    sc = load_setup_check()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    gemini_dir = home_dir / ".gemini"
    gemini_dir.mkdir()
    trusted_file = gemini_dir / "trustedFolders.json"
    # Use dict schema matching the real gemini CLI format.
    initial_entries = {str(repo_root): "TRUST_FOLDER", "/some/other/path": "TRUST_FOLDER"}
    trusted_file.write_text(json.dumps(initial_entries), encoding="utf-8")

    with patch.object(Path, "home", return_value=home_dir):
        result = sc.check_trusted_folders(repo_root=repo_root)

    assert result["ok"] is True
    assert result["status"] == "already_trusted"
    # File must not have changed — existing dict entries are preserved.
    after = json.loads(trusted_file.read_text(encoding="utf-8"))
    assert after == initial_entries, "Existing dict entries must not be modified (idempotent)"


def test_trusted_folders_parent_match_noop(tmp_path):
    """GIVEN trustedFolders.json contains a parent directory as TRUST_PARENT (dict schema)
    WHEN check_trusted_folders is called
    THEN no-op — status is parent_trusted and no new entry is appended."""
    sc = load_setup_check()

    parent = tmp_path / "projects"
    parent.mkdir()
    repo_root = parent / "LOOP_PROTOCOL"
    repo_root.mkdir()

    home_dir = tmp_path / "fakehome2"
    home_dir.mkdir()
    gemini_dir = home_dir / ".gemini"
    gemini_dir.mkdir()
    trusted_file = gemini_dir / "trustedFolders.json"
    # Use dict schema with TRUST_PARENT for parent directory.
    initial_entries = {str(parent): "TRUST_PARENT"}
    trusted_file.write_text(json.dumps(initial_entries), encoding="utf-8")

    with patch.object(Path, "home", return_value=home_dir):
        result = sc.check_trusted_folders(repo_root=repo_root)

    assert result["ok"] is True
    assert result["status"] == "parent_trusted"
    after = json.loads(trusted_file.read_text(encoding="utf-8"))
    assert after == initial_entries, "Parent-trusted must not add duplicate entry"


def test_trusted_folders_absent_creates_file(tmp_path):
    """GIVEN trustedFolders.json does not exist
    WHEN check_trusted_folders is called with fix=True
    THEN file is created with repo root as dict entry {path: TRUST_FOLDER}."""
    sc = load_setup_check()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    home_dir = tmp_path / "fakehome3"
    home_dir.mkdir()

    with patch.object(Path, "home", return_value=home_dir):
        result = sc.check_trusted_folders(repo_root=repo_root, fix=True)

    assert result["ok"] is True
    assert result["status"] == "added"
    trusted_file = home_dir / ".gemini" / "trustedFolders.json"
    assert trusted_file.exists()
    # File should be a dict with the repo root mapped to TRUST_FOLDER.
    data = json.loads(trusted_file.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "trustedFolders.json must be a dict (not a list)"
    assert str(repo_root) in data
    assert data[str(repo_root)] == "TRUST_FOLDER"


# ---------------------------------------------------------------------------
# AC4: settings.json not overwritten if present
# ---------------------------------------------------------------------------


def test_gemini_settings_not_overwritten(tmp_path):
    """GIVEN .gemini/settings.json already exists
    WHEN check_gemini_settings is called
    THEN existing file is not overwritten."""
    sc = load_setup_check()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    gemini_dir = repo_root / ".gemini"
    gemini_dir.mkdir()
    settings_file = gemini_dir / "settings.json"
    original_content = '{"custom": true}\n'
    settings_file.write_text(original_content, encoding="utf-8")

    result = sc.check_gemini_settings(repo_root=repo_root)

    assert result["ok"] is True
    assert result["status"] == "exists"
    assert settings_file.read_text(encoding="utf-8") == original_content


def test_gemini_settings_created_when_absent(tmp_path):
    """GIVEN .gemini/settings.json does not exist
    WHEN check_gemini_settings is called with fix=True
    THEN template is created with serena MCP allowlist."""
    sc = load_setup_check()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = sc.check_gemini_settings(repo_root=repo_root, fix=True)

    assert result["ok"] is True
    assert result["status"] == "created"
    settings_file = repo_root / ".gemini" / "settings.json"
    assert settings_file.exists()
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["mcp"]["allowed"] == [sc.SERENA_MCP_SERVER_NAME]
    assert data["mcpServers"][sc.SERENA_MCP_SERVER_NAME]["command"] == "uvx"
    assert data["mcpServers"][sc.SERENA_MCP_SERVER_NAME]["trust"] is False


# ---------------------------------------------------------------------------
# AC7: Serena MCP check — uses 'serena' executable (not 'serena-mcp-server')
# ---------------------------------------------------------------------------


def test_serena_mcp_available(tmp_path):
    """GIVEN uvx is available and serena executable responds to --help
    WHEN check_serena_mcp is called
    THEN ok is True.
    (serena package provides 'serena' executable, not 'serena-mcp-server')"""
    sc = load_setup_check()

    def _run_side_effect(command: list[str], timeout: int | None = None):
        # Correct command: uvx --from <package> serena --help
        if "serena" in command and "--help" in command and "serena-mcp-server" not in command:
            return _make_completed(0, stdout="Usage: serena [OPTIONS]")
        return _make_completed(1, stderr="unexpected command")

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_serena_mcp()

    assert result["ok"] is True
    assert result["status"] == "available"


def test_serena_mcp_unavailable_has_recovery(tmp_path):
    """GIVEN serena command fails
    WHEN check_serena_mcp is called
    THEN ok is False and recovery hints are present."""
    sc = load_setup_check()

    def _run_side_effect(command: list[str], timeout: int | None = None):
        if "serena" in command and "--help" in command:
            return _make_completed(1, stderr="error: package not found")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_serena_mcp()

    assert result["ok"] is False
    assert "recovery" in result
    assert len(result["recovery"]) > 0


# ---------------------------------------------------------------------------
# AC3: dict existing entries are preserved when new entry is added
# ---------------------------------------------------------------------------


def test_trusted_folders_dict_existing_preserved(tmp_path):
    """GIVEN trustedFolders.json has 3 existing dict entries (preserve existing data)
    WHEN check_trusted_folders is called with fix=True and a new repo root
    THEN all 3 existing dict entries are preserved in the output dict.

    This verifies that setup_check preserves existing dict entries and does not
    destroy data when adding new TRUST_FOLDER entries.
    """
    sc = load_setup_check()

    repo_root = tmp_path / "new_repo"
    repo_root.mkdir()

    home_dir = tmp_path / "fakehome_preserve"
    home_dir.mkdir()
    gemini_dir = home_dir / ".gemini"
    gemini_dir.mkdir()
    trusted_file = gemini_dir / "trustedFolders.json"

    # 3 existing entries that must be preserved.
    existing_entries = {
        "/home/user/KindleAudiobookMakeSystem": "TRUST_FOLDER",
        "/home/user/claw-ecosystem_Deploy": "TRUST_FOLDER",
        "/home/user/projects": "TRUST_PARENT",
    }
    trusted_file.write_text(json.dumps(existing_entries), encoding="utf-8")

    with patch.object(Path, "home", return_value=home_dir):
        result = sc.check_trusted_folders(repo_root=repo_root, fix=True)

    assert result["ok"] is True
    assert result["status"] == "added"

    after = json.loads(trusted_file.read_text(encoding="utf-8"))
    assert isinstance(after, dict), "Output must be dict format to preserve existing entries"
    # All 3 original entries must still be present (preserve semantics).
    for key, value in existing_entries.items():
        assert key in after, f"Existing entry '{key}' must be preserved"
        assert after[key] == value, f"Value for '{key}' must be preserved as '{value}'"
    # New entry is also present.
    assert str(repo_root) in after


# ---------------------------------------------------------------------------
# AC4: new entry is appended as dict {path: "TRUST_FOLDER"}
# ---------------------------------------------------------------------------


def test_trusted_folders_append_as_dict_entry(tmp_path):
    """GIVEN trustedFolders.json exists but does not contain repo root
    WHEN check_trusted_folders is called
    THEN new entry is appended as {repo_root: "TRUST_FOLDER"} in dict format.

    Verifies that the trust_folder_added entry uses the correct dict schema,
    not the deprecated list schema.
    """
    sc = load_setup_check()

    repo_root = tmp_path / "append_repo"
    repo_root.mkdir()

    home_dir = tmp_path / "fakehome_append"
    home_dir.mkdir()
    gemini_dir = home_dir / ".gemini"
    gemini_dir.mkdir()
    trusted_file = gemini_dir / "trustedFolders.json"

    # Start with one unrelated entry.
    initial = {"/other/path": "TRUST_FOLDER"}
    trusted_file.write_text(json.dumps(initial), encoding="utf-8")

    with patch.object(Path, "home", return_value=home_dir):
        result = sc.check_trusted_folders(repo_root=repo_root, fix=True)

    assert result["ok"] is True
    assert result["status"] == "added"

    after = json.loads(trusted_file.read_text(encoding="utf-8"))
    assert isinstance(after, dict), "Output must be dict (not list) after trust_folder_added"
    assert str(repo_root) in after, "New repo root must appear in dict"
    assert after[str(repo_root)] == "TRUST_FOLDER", (
        "New entry value must be 'TRUST_FOLDER' (not appended to a list)"
    )


# ---------------------------------------------------------------------------
# AC5: TRUST_PARENT ancestor in dict form → no-op
# ---------------------------------------------------------------------------


def test_trusted_folders_parent_trust_noop_dict(tmp_path):
    """GIVEN trustedFolders.json contains a parent directory with value TRUST_PARENT (dict schema)
    WHEN check_trusted_folders is called for a child repo
    THEN no-op — status is parent_trusted and the dict is not modified.

    Tests that the TRUST_PARENT ancestor check works correctly with dict format,
    preserving dict schema integrity without adding a redundant TRUST_FOLDER entry.
    """
    sc = load_setup_check()

    grandparent = tmp_path / "workspace"
    grandparent.mkdir()
    parent = grandparent / "projects"
    parent.mkdir()
    repo_root = parent / "LOOP_PROTOCOL"
    repo_root.mkdir()

    home_dir = tmp_path / "fakehome_parent_noop"
    home_dir.mkdir()
    gemini_dir = home_dir / ".gemini"
    gemini_dir.mkdir()
    trusted_file = gemini_dir / "trustedFolders.json"

    # parent directory has TRUST_PARENT in dict schema.
    initial = {str(parent): "TRUST_PARENT", "/unrelated/path": "TRUST_FOLDER"}
    trusted_file.write_text(json.dumps(initial), encoding="utf-8")

    with patch.object(Path, "home", return_value=home_dir):
        result = sc.check_trusted_folders(repo_root=repo_root)

    assert result["ok"] is True
    assert result["status"] == "parent_trusted"
    # Dict must be completely unchanged — no new entry added.
    after = json.loads(trusted_file.read_text(encoding="utf-8"))
    assert after == initial, (
        "Dict must not be modified when TRUST_PARENT ancestor covers the repo root"
    )


# ---------------------------------------------------------------------------
# B4: GEMINI_CLI_TRUSTED_FOLDERS_PATH env override
# ---------------------------------------------------------------------------


def test_trusted_folders_path_honors_env_override(tmp_path, monkeypatch):
    """GIVEN GEMINI_CLI_TRUSTED_FOLDERS_PATH is set to a custom path
    WHEN _trusted_folders_path() is called
    THEN it returns that path (not ~/.gemini/trustedFolders.json)."""
    sc = load_setup_check()

    override_path = tmp_path / "trusted.json"
    monkeypatch.setenv("GEMINI_CLI_TRUSTED_FOLDERS_PATH", str(override_path))
    assert sc._trusted_folders_path() == override_path


def test_trusted_folders_path_default_without_env(monkeypatch):
    """GIVEN GEMINI_CLI_TRUSTED_FOLDERS_PATH is not set
    WHEN _trusted_folders_path() is called
    THEN it returns ~/.gemini/trustedFolders.json."""
    sc = load_setup_check()

    monkeypatch.delenv("GEMINI_CLI_TRUSTED_FOLDERS_PATH", raising=False)
    result = sc._trusted_folders_path()
    assert result == Path.home() / ".gemini" / "trustedFolders.json"


# ---------------------------------------------------------------------------
# AC1: oauth_sunset is classified
# ---------------------------------------------------------------------------


def test_auth_oauth_sunset_is_classified():
    """GIVEN gemini exits non-zero with sunset-related stderr
    WHEN check_auth is called
    THEN status is 'oauth_sunset' and recovery mentions GEMINI_API_KEY and #104."""
    sc = load_setup_check()

    def _run_side_effect(command, timeout=None):
        if "gemini" in command and "--prompt" in command:
            return _make_completed(
                1,
                stderr="Error: This service has been discontinued. Google OAuth login is no longer supported.",
            )
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_auth()

    assert result["ok"] is False
    assert result["status"] == "oauth_sunset"
    recovery_text = " ".join(result.get("recovery", []))
    assert "GEMINI_API_KEY" in recovery_text, "recovery must mention GEMINI_API_KEY"
    assert "#104" in recovery_text, "recovery must reference parent issue #104"


# ---------------------------------------------------------------------------
# AC1b: ineligible_tier is classified
# ---------------------------------------------------------------------------


def test_auth_ineligible_tier_is_classified():
    """GIVEN gemini exits non-zero with ineligible tier stderr
    WHEN check_auth is called
    THEN status is 'ineligible_tier' and recovery mentions GEMINI_API_KEY and #104."""
    sc = load_setup_check()

    def _run_side_effect(command, timeout=None):
        if "gemini" in command and "--prompt" in command:
            return _make_completed(
                1,
                stderr="Error: Account is not eligible for this tier. Please upgrade your plan.",
            )
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_auth()

    assert result["ok"] is False
    assert result["status"] == "ineligible_tier"
    recovery_text = " ".join(result.get("recovery", []))
    assert "GEMINI_API_KEY" in recovery_text, "recovery must mention GEMINI_API_KEY"
    assert "#104" in recovery_text, "recovery must reference parent issue #104"


# ---------------------------------------------------------------------------
# AC2: recovery for oauth_sunset mentions GEMINI_API_KEY and #104
# ---------------------------------------------------------------------------


def test_auth_recovery_mentions_api_key_and_issue_104():
    """GIVEN oauth_sunset scenario
    WHEN check_auth is called
    THEN recovery message contains both 'GEMINI_API_KEY' and '#104'."""
    sc = load_setup_check()

    def _run_side_effect(command, timeout=None):
        if "gemini" in command and "--prompt" in command:
            return _make_completed(1, stderr="sunset: no longer supported")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_auth()

    recovery = result.get("recovery", [])
    combined = " ".join(recovery)
    assert "GEMINI_API_KEY" in combined
    assert "#104" in combined


# ---------------------------------------------------------------------------
# AC2b: recovery for other auth failures also mentions GEMINI_API_KEY and #104
# ---------------------------------------------------------------------------


def test_auth_recovery_on_other_failure_mentions_api_key_and_issue_104():
    """GIVEN an unknown auth failure (auth_failed status)
    WHEN check_auth is called
    THEN recovery message contains both 'GEMINI_API_KEY' and '#104'."""
    sc = load_setup_check()

    def _run_side_effect(command, timeout=None):
        if "gemini" in command and "--prompt" in command:
            return _make_completed(1, stderr="unexpected error: connection reset")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_auth()

    assert result["ok"] is False
    assert result["status"] == "auth_failed"
    recovery = result.get("recovery", [])
    combined = " ".join(recovery)
    assert "GEMINI_API_KEY" in combined
    assert "#104" in combined


# ---------------------------------------------------------------------------
# AC5: GEMINI_API_KEY value is never leaked in output
# ---------------------------------------------------------------------------


def test_auth_does_not_leak_api_key_value(monkeypatch):
    """GIVEN GEMINI_API_KEY is set to a sentinel value
    WHEN check_auth is called
    THEN the sentinel value does NOT appear in any output field."""
    sc = load_setup_check()

    sentinel = "sk-SUPER_SECRET_VALUE_THAT_MUST_NOT_APPEAR_12345"
    monkeypatch.setenv("GEMINI_API_KEY", sentinel)

    def _run_side_effect(command, timeout=None):
        if "gemini" in command and "--prompt" in command:
            return _make_completed(1, stderr="auth error: ineligible tier")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_auth()

    # Serialize entire result to JSON and verify sentinel is absent.
    serialized = json.dumps(result)
    assert sentinel not in serialized, (
        f"GEMINI_API_KEY value '{sentinel}' must not appear in check_auth output"
    )


# ---------------------------------------------------------------------------
# Additional tests for B1-B5 fixes (human review blockers)
# ---------------------------------------------------------------------------


def test_auth_timeout_recovery_mentions_api_key_and_issue_104():
    """GIVEN smoke prompt times out
    WHEN check_auth is called
    THEN status is 'timeout' and recovery mentions GEMINI_API_KEY and #104."""
    sc = load_setup_check()

    def _run_side_effect(command, timeout=None):
        if "gemini" in command and "--prompt" in command:
            raise __import__("subprocess").TimeoutExpired(cmd=command, timeout=timeout)
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_auth()

    assert result["ok"] is False
    assert result["status"] == "timeout"
    combined = " ".join(result.get("recovery", []))
    assert "GEMINI_API_KEY" in combined
    assert "#104" in combined


def test_auth_does_not_leak_api_key_value_from_unauthenticated_detail(monkeypatch):
    """GIVEN GEMINI_API_KEY is set AND gemini stderr includes the key value
    WHEN check_auth falls into the unauthenticated branch
    THEN the key value does NOT appear anywhere in the returned dict."""
    sc = load_setup_check()

    sentinel = "sk-UNAUTHENTICATED-LEAK-CHECK-99999"
    monkeypatch.setenv("GEMINI_API_KEY", sentinel)

    def _run_side_effect(command, timeout=None):
        if "gemini" in command and "--prompt" in command:
            # Simulate Gemini CLI accidentally printing the key in its error output.
            return _make_completed(1, stderr=f"auth failed: key={sentinel} was rejected")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_auth()

    serialized = json.dumps(result)
    assert sentinel not in serialized, (
        "GEMINI_API_KEY value must be redacted from check_auth output even if CLI prints it"
    )


def test_auth_unrelated_no_longer_supported_is_not_oauth_sunset():
    """GIVEN gemini exits with 'model no longer supported' (not OAuth sunset)
    WHEN check_auth is called
    THEN status is NOT oauth_sunset (B3: avoid over-detection)."""
    sc = load_setup_check()

    def _run_side_effect(command, timeout=None):
        if "gemini" in command and "--prompt" in command:
            return _make_completed(1, stderr="Error: model gemini-1.0-pro is no longer supported.")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_auth()

    assert result["status"] != "oauth_sunset", (
        "'model no longer supported' must not trigger oauth_sunset (B3)"
    )


def test_auth_unrelated_tier_text_is_not_ineligible_tier():
    """GIVEN gemini exits with 'tier' in an unrelated context (e.g. 'frontier tier error')
    WHEN check_auth is called
    THEN status is NOT ineligible_tier (B3: avoid over-detection)."""
    sc = load_setup_check()

    def _run_side_effect(command, timeout=None):
        if "gemini" in command and "--prompt" in command:
            return _make_completed(1, stderr="Error: frontier tier request limit exceeded.")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_auth()

    assert result["status"] != "ineligible_tier", (
        "'frontier tier request limit exceeded' must not trigger ineligible_tier (B3)"
    )


def test_auth_status_always_in_auth_status_values():
    """GIVEN various gemini CLI responses
    THEN all returned auth status values are in AUTH_STATUS_VALUES (fixed enum)."""
    sc = load_setup_check()
    AUTH_VALUES = sc.AUTH_STATUS_VALUES

    scenarios = [
        ("ok", "", ""),
        ("1", "service has been discontinued", ""),
        ("1", "", "not eligible for this tier"),
        ("1", "", "auth failed: not logged in"),
        ("1", "", "unexpected random error"),
        ("gemini_not_found", "", ""),
    ]

    for returncode_or_exc, stdout, stderr in scenarios:
        if returncode_or_exc == "gemini_not_found":
            def make_exc_side_effect(command, timeout=None):
                if "gemini" in command and "--prompt" in command:
                    raise FileNotFoundError("gemini not found")
                return _make_completed(0)
            side_effect = make_exc_side_effect
        else:
            rc = int(returncode_or_exc) if returncode_or_exc != "ok" else 0
            _stdout, _stderr = stdout, stderr
            def make_side_effect(rc=rc, out=_stdout, err=_stderr):
                def _f(command, timeout=None):
                    if "gemini" in command and "--prompt" in command:
                        return _make_completed(rc, stdout=out, stderr=err)
                    return _make_completed(0)
                return _f
            side_effect = make_side_effect()

        with patch.object(sc, "_run", side_effect=side_effect):
            result = sc.check_auth()

        assert result.get("status") in AUTH_VALUES, (
            f"status '{result.get('status')}' not in AUTH_STATUS_VALUES for scenario: "
            f"rc={returncode_or_exc!r} stderr={stderr!r}"
        )

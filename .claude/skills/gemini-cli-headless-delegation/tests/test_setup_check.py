"""Tests for setup_check.py.

Test execution (with dependencies):
    uv run --with pytest --with pyyaml python -m pytest tests/

Cases covered:
    1. All checks pass → exit 0
    2. node missing → exit != 0
    3. trustedFolders.json existing TRUST_FOLDER → no-op (values preserved)
    4. trustedFolders.json existing TRUST_PARENT → no-op (values preserved)
    5. trustedFolders.json absent → file created with repo root
    6. .gemini/settings.json absent → template created
    7. .gemini/settings.json present → not overwritten
    8. Serena MCP available → ok True
    9. Serena MCP unavailable → recovery hint present
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

    # Pre-create trustedFolders.json with repo root already trusted.
    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    gemini_home = home_dir / ".gemini"
    gemini_home.mkdir()
    trusted_path = gemini_home / "trustedFolders.json"
    trusted_path.write_text(json.dumps([str(repo_root)]), encoding="utf-8")

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
        if "serena-mcp-server" in command:
            return _make_completed(0, stdout="Usage: serena-mcp-server")
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
        if "serena-mcp-server" in command:
            return _make_completed(0, stdout="Usage: serena-mcp-server")
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
# AC2: trustedFolders.json — TRUST_FOLDER exact match → no-op
# ---------------------------------------------------------------------------


def test_trusted_folders_exact_match_noop(tmp_path):
    """GIVEN trustedFolders.json already contains the exact repo root (TRUST_FOLDER)
    WHEN check_trusted_folders is called
    THEN no-op — status is already_trusted and existing entries are not modified."""
    sc = load_setup_check()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    gemini_dir = home_dir / ".gemini"
    gemini_dir.mkdir()
    trusted_file = gemini_dir / "trustedFolders.json"
    initial_entries = [str(repo_root), "/some/other/path"]
    trusted_file.write_text(json.dumps(initial_entries), encoding="utf-8")

    with patch.object(Path, "home", return_value=home_dir):
        result = sc.check_trusted_folders(repo_root=repo_root)

    assert result["ok"] is True
    assert result["status"] == "already_trusted"
    # File must not have changed.
    after = json.loads(trusted_file.read_text(encoding="utf-8"))
    assert after == initial_entries, "Existing entries must not be modified (idempotent)"


def test_trusted_folders_parent_match_noop(tmp_path):
    """GIVEN trustedFolders.json contains a parent directory (TRUST_PARENT)
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
    initial_entries = [str(parent)]
    trusted_file.write_text(json.dumps(initial_entries), encoding="utf-8")

    with patch.object(Path, "home", return_value=home_dir):
        result = sc.check_trusted_folders(repo_root=repo_root)

    assert result["ok"] is True
    assert result["status"] == "parent_trusted"
    after = json.loads(trusted_file.read_text(encoding="utf-8"))
    assert after == initial_entries, "Parent-trusted must not add duplicate entry"


def test_trusted_folders_absent_creates_file(tmp_path):
    """GIVEN trustedFolders.json does not exist
    WHEN check_trusted_folders is called
    THEN file is created with repo root as entry."""
    sc = load_setup_check()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    home_dir = tmp_path / "fakehome3"
    home_dir.mkdir()

    with patch.object(Path, "home", return_value=home_dir):
        result = sc.check_trusted_folders(repo_root=repo_root)

    assert result["ok"] is True
    assert result["status"] == "added"
    trusted_file = home_dir / ".gemini" / "trustedFolders.json"
    assert trusted_file.exists()
    entries = json.loads(trusted_file.read_text(encoding="utf-8"))
    assert str(repo_root) in entries


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
    WHEN check_gemini_settings is called
    THEN template is created with serena MCP allowlist."""
    sc = load_setup_check()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = sc.check_gemini_settings(repo_root=repo_root)

    assert result["ok"] is True
    assert result["status"] == "created"
    settings_file = repo_root / ".gemini" / "settings.json"
    assert settings_file.exists()
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["mcp"]["allowed"] == [sc.SERENA_MCP_SERVER_NAME]
    assert data["mcpServers"][sc.SERENA_MCP_SERVER_NAME]["command"] == "uvx"
    assert data["mcpServers"][sc.SERENA_MCP_SERVER_NAME]["trust"] is False


# ---------------------------------------------------------------------------
# AC3: Serena MCP check
# ---------------------------------------------------------------------------


def test_serena_mcp_available(tmp_path):
    """GIVEN uvx is available and serena responds to --help
    WHEN check_serena_mcp is called
    THEN ok is True."""
    sc = load_setup_check()

    def _run_side_effect(command: list[str], timeout: int | None = None):
        if "serena-mcp-server" in command:
            return _make_completed(0, stdout="Usage: serena-mcp-server [OPTIONS]")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_serena_mcp()

    assert result["ok"] is True
    assert result["status"] == "available"


def test_serena_mcp_unavailable_has_recovery(tmp_path):
    """GIVEN serena-mcp-server command fails
    WHEN check_serena_mcp is called
    THEN ok is False and recovery hints are present."""
    sc = load_setup_check()

    def _run_side_effect(command: list[str], timeout: int | None = None):
        if "serena-mcp-server" in command:
            return _make_completed(1, stderr="error: package not found")
        return _make_completed(0)

    with patch.object(sc, "_run", side_effect=_run_side_effect):
        result = sc.check_serena_mcp()

    assert result["ok"] is False
    assert "recovery" in result
    assert len(result["recovery"]) > 0

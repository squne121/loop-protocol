"""Tests for build_request.py.

AC coverage:
  AC3: build_request.py --help exits 0
  AC4: build_request.py generates a request that passes run_gemini_headless --validate-only
  AC10: this file exists and pytest passes
  AC11: failure JSON contains failure_class / failure_reason / next_action.argv / next_action.command
  AC12: next_action.command is shlex.join equivalent of next_action.argv
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers: module loader
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load_build_request():
    path = _SCRIPTS_DIR / "build_request.py"
    spec = importlib.util.spec_from_file_location("build_request", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_run_gemini_headless():
    path = _SCRIPTS_DIR / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# AC3: --help exits 0
# ---------------------------------------------------------------------------


def test_build_request_help_exits_0():
    """GIVEN build_request.py
    WHEN called with --help
    THEN exits 0."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "build_request.py"), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"--help returned {result.returncode}: {result.stderr}"
    assert "tool_profile" in result.stdout.lower() or "profile" in result.stdout.lower()


# ---------------------------------------------------------------------------
# AC4: generated request passes validate_request
# ---------------------------------------------------------------------------


def test_build_request_generates_valid_request_github_research(tmp_path, monkeypatch):
    """GIVEN a github_research profile request
    WHEN build_request.py generates it
    THEN validate_request returns no errors."""
    br = load_build_request()
    rgh = load_run_gemini_headless()

    # Patch _validate_local_asset_research_settings to avoid file system check
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")

    output = tmp_path / "request.json"
    exit_code = br.build_request(
        profile="github_research",
        objective="Investigate the latest PR for regression issues via gh pr list",
        instructions=None,
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code == 0, f"build_request returned {exit_code}"
    assert output.exists()

    request = json.loads(output.read_text(encoding="utf-8"))
    assert request["schema"] == "delegation_request_v1"
    assert request["tool_profile"] == "github_research"

    errors = rgh.validate_request(request, request_path=output)
    assert errors == [], f"validate_request returned errors: {errors}"


def test_build_request_generates_valid_request_no_tools(tmp_path):
    """GIVEN a no_tools profile request
    WHEN build_request.py generates it
    THEN validate_request returns no errors."""
    br = load_build_request()
    rgh = load_run_gemini_headless()

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")

    output = tmp_path / "request.json"
    exit_code = br.build_request(
        profile="no_tools",
        objective="Summarize the build failure from context file",
        instructions=[
            "Identify the root cause from the context.",
            "List any actionable recommendations.",
        ],
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code == 0
    request = json.loads(output.read_text(encoding="utf-8"))
    errors = rgh.validate_request(request, request_path=output)
    assert errors == [], f"validate_request returned errors: {errors}"


# ---------------------------------------------------------------------------
# AC11: failure JSON contains failure_class / failure_reason / next_action.argv / next_action.command
# ---------------------------------------------------------------------------


def test_build_request_invalid_profile_failure_json(tmp_path):
    """GIVEN an invalid tool_profile
    WHEN build_request.py is called
    THEN failure JSON contains failure_class, failure_reason, next_action.argv, next_action.command."""
    br = load_build_request()

    output = tmp_path / "failure.json"
    exit_code = br.build_request(
        profile="invalid_profile",
        objective="some objective",
        instructions=None,
        context_files=None,
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code != 0
    assert output.exists()

    result = json.loads(output.read_text(encoding="utf-8"))
    assert "failure_class" in result, "failure JSON must contain failure_class"
    assert "failure_reason" in result, "failure JSON must contain failure_reason"
    assert "next_action" in result, "failure JSON must contain next_action"
    assert "argv" in result["next_action"], "next_action must contain argv"
    assert "command" in result["next_action"], "next_action must contain command"


def test_build_request_missing_context_file_failure_json(tmp_path):
    """GIVEN a missing context file
    WHEN build_request.py is called
    THEN failure JSON contains failure_class=missing_context_file and next_action."""
    br = load_build_request()

    output = tmp_path / "failure.json"
    exit_code = br.build_request(
        profile="no_tools",
        objective="Summarize the build failure from context file",
        instructions=None,
        context_files=[str(tmp_path / "nonexistent.md")],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code != 0
    assert output.exists()

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result.get("failure_class") == "missing_context_file"
    assert "failure_reason" in result
    assert "next_action" in result
    assert "argv" in result["next_action"]
    assert "command" in result["next_action"]


# ---------------------------------------------------------------------------
# AC12: next_action.command is shlex.join equivalent of next_action.argv
# ---------------------------------------------------------------------------


def test_next_action_argv_primary(tmp_path):
    """GIVEN a failure result
    WHEN next_action is examined
    THEN next_action.argv is the primary representation and
         next_action.command equals shlex.join(next_action.argv)."""
    br = load_build_request()

    output = tmp_path / "failure.json"
    br.build_request(
        profile="invalid_profile",
        objective="test",
        instructions=None,
        context_files=None,
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    argv = result["next_action"]["argv"]
    command = result["next_action"]["command"]
    assert isinstance(argv, list), "next_action.argv must be a list"
    assert command == shlex.join(argv), (
        f"next_action.command must equal shlex.join(argv).\n"
        f"  argv={argv!r}\n"
        f"  command={command!r}\n"
        f"  shlex.join(argv)={shlex.join(argv)!r}"
    )


# ---------------------------------------------------------------------------
# AC4 (via CLI): build_request.py output passes --validate-only
# ---------------------------------------------------------------------------


def test_build_request_output_passes_validate_only(tmp_path):
    """GIVEN build_request.py generates a request JSON
    WHEN run_gemini_headless.py --validate-only is called with that file
    THEN it exits 0."""
    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "request.json"

    build_result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "build_request.py"),
            "--profile", "no_tools",
            "--objective", "Summarize the context file for testing purposes",
            "--context-file", str(context_file),
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert build_result.returncode == 0, f"build_request failed: {build_result.stderr}"

    validate_result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "run_gemini_headless.py"),
            "--validate-only",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert validate_result.returncode == 0, (
        f"--validate-only failed:\nstdout: {validate_result.stdout}\nstderr: {validate_result.stderr}"
    )
    assert "OK" in validate_result.stdout or "ok" in validate_result.stdout.lower()

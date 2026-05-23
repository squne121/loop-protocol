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
    THEN failure JSON contains failure_class=context_file_missing and next_action."""
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
    # B2: failure_class must be 'context_file_missing' (not 'missing_context_file')
    assert result.get("failure_class") == "context_file_missing", (
        f"Expected 'context_file_missing', got: {result.get('failure_class')}"
    )
    assert "failure_reason" in result
    assert "next_action" in result
    assert "argv" in result["next_action"]
    assert "command" in result["next_action"]


# ---------------------------------------------------------------------------
# B2: failure_class must be 'context_file_missing' (canonical name from contract)
# ---------------------------------------------------------------------------


def test_build_request_missing_context_file_uses_canonical_failure_class(tmp_path):
    """GIVEN a context file path that does not exist
    WHEN build_request.py is called
    THEN failure_class is exactly 'context_file_missing' (not 'missing_context_file')."""
    br = load_build_request()

    output = tmp_path / "failure.json"
    br.build_request(
        profile="github_research",
        objective="Investigate the PR history for regressions via gh pr list",
        instructions=None,
        context_files=["/nonexistent/path/to/context.md"],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result.get("failure_class") == "context_file_missing", (
        f"Expected failure_class='context_file_missing', got: {result.get('failure_class')!r}"
    )


# ---------------------------------------------------------------------------
# B3: next_action.argv must be a complete runnable command
# ---------------------------------------------------------------------------


def test_build_request_missing_context_next_action_argv_complete(tmp_path):
    """GIVEN a missing context file
    WHEN build_request.py produces a failure JSON
    THEN next_action.argv is a complete runnable command including --profile, --objective, --output."""
    br = load_build_request()

    output = tmp_path / "failure.json"
    br.build_request(
        profile="no_tools",
        objective="Summarize the build failure",
        instructions=None,
        context_files=[str(tmp_path / "nonexistent.md")],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    argv = result["next_action"]["argv"]
    argv_str = " ".join(argv)
    assert "--profile" in argv_str, f"--profile missing from next_action.argv: {argv}"
    assert "--objective" in argv_str, f"--objective missing from next_action.argv: {argv}"
    assert "--output" in argv_str, f"--output missing from next_action.argv: {argv}"
    assert "--context-file" in argv_str, f"--context-file missing from next_action.argv: {argv}"


# ---------------------------------------------------------------------------
# B4: --instruction fail-closed when count < 2 (explicit specification required)
# ---------------------------------------------------------------------------


def test_build_request_single_instruction_fails_closed(tmp_path):
    """GIVEN --instruction is specified exactly once
    WHEN build_request.py is called
    THEN it fails with failure_class='validation_error' (fail-closed)."""
    br = load_build_request()

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "failure.json"

    exit_code = br.build_request(
        profile="github_research",
        objective="Investigate the PR history via gh pr list",
        instructions=["one instruction only"],
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code != 0, "Expected non-zero exit when --instruction provided only once"
    assert output.exists()

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result.get("failure_class") == "validation_error", (
        f"Expected 'validation_error', got: {result.get('failure_class')!r}"
    )
    assert "--instruction must be specified at least twice" in result.get("failure_reason", ""), (
        f"Expected instruction count message, got: {result.get('failure_reason')!r}"
    )


def test_build_request_zero_instructions_uses_defaults(tmp_path, monkeypatch):
    """GIVEN --instruction is not specified (None)
    WHEN build_request.py is called
    THEN profile defaults are used (no error)."""
    br = load_build_request()
    rgh = load_run_gemini_headless()
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "request.json"

    exit_code = br.build_request(
        profile="github_research",
        objective="Investigate the PR history for regressions via gh pr list",
        instructions=None,  # not provided → use defaults
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code == 0, f"Expected success when instructions=None; got exit {exit_code}"


def test_build_request_two_instructions_succeeds(tmp_path, monkeypatch):
    """GIVEN --instruction is specified twice
    WHEN build_request.py is called
    THEN it succeeds (no fail-closed error)."""
    br = load_build_request()
    rgh = load_run_gemini_headless()
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "request.json"

    exit_code = br.build_request(
        profile="github_research",
        objective="Investigate the PR history for regressions via gh pr list",
        instructions=["First instruction here.", "Second instruction here."],
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code == 0, f"Expected success with 2 instructions; got exit {exit_code}"


# ---------------------------------------------------------------------------
# B5: --gh-pr / --gh-issue only allowed with github_research profile
# ---------------------------------------------------------------------------


def test_build_request_gh_issue_rejected_for_non_github_research(tmp_path):
    """GIVEN --gh-issue is specified with a non-github_research profile
    WHEN build_request.py is called
    THEN it fails with failure_class='validation_error'."""
    br = load_build_request()

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "failure.json"

    exit_code = br.build_request(
        profile="no_tools",
        objective="Summarize the context file for testing purposes",
        instructions=None,
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=313,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code != 0, "Expected failure when gh_issue used with non-github_research profile"
    assert output.exists()

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result.get("failure_class") == "validation_error", (
        f"Expected 'validation_error', got: {result.get('failure_class')!r}"
    )
    assert "gh_commands" in result.get("failure_reason", "") or "gh-pr" in result.get("failure_reason", "") or "github_research" in result.get("failure_reason", ""), (
        f"Expected profile restriction message, got: {result.get('failure_reason')!r}"
    )


def test_build_request_gh_pr_rejected_for_non_github_research(tmp_path):
    """GIVEN --gh-pr is specified with grounded_research profile
    WHEN build_request.py is called
    THEN it fails with failure_class='validation_error'."""
    br = load_build_request()

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "failure.json"

    exit_code = br.build_request(
        profile="grounded_research",
        objective="Investigate the search results for regression patterns",
        instructions=None,
        context_files=[str(context_file)],
        gh_pr=321,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code != 0, "Expected failure when gh_pr used with grounded_research profile"
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result.get("failure_class") == "validation_error"


def test_build_request_gh_issue_allowed_for_github_research(tmp_path, monkeypatch):
    """GIVEN --gh-issue is specified with github_research profile
    WHEN build_request.py is called
    THEN it succeeds."""
    br = load_build_request()
    rgh = load_run_gemini_headless()
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "request.json"

    exit_code = br.build_request(
        profile="github_research",
        objective="Investigate the PR history for regressions via gh issue view",
        instructions=None,
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=313,
        output=output,
        base_dir=tmp_path,
    )
    assert exit_code == 0, f"Expected success for github_research + gh_issue; got exit {exit_code}"


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


# ---------------------------------------------------------------------------
# B1: SKILL.md example command regression test
# ---------------------------------------------------------------------------


def test_skill_md_example_github_research_command_passes_validate_only(tmp_path):
    """GIVEN the SKILL.md example command for github_research profile
    (--context-file + --gh-issue + --gh-pr)
    WHEN build_request.py generates the request and --validate-only is run
    THEN both exit 0 (regression test for B1).

    This test mirrors the command shown in SKILL.md Workflow step 1:
        build_request.py --profile github_research \\
          --objective '...' \\
          --context-file <context> \\
          --gh-issue 313 --gh-pr 321 \\
          --output /tmp/gemini/request.json
    """
    # Use the real usage-contract.md as context file (just like SKILL.md example)
    # Fall back to a tmp context file if it doesn't exist in this environment
    skill_dir = _SCRIPTS_DIR.parent
    usage_contract = skill_dir / "references" / "usage-contract.md"
    if usage_contract.exists():
        context_file = usage_contract
    else:
        context_file = tmp_path / "usage-contract.md"
        context_file.write_text("# usage-contract placeholder", encoding="utf-8")

    output = tmp_path / "request.json"

    build_result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "build_request.py"),
            "--profile", "github_research",
            "--objective", "Issue #313 と PR #321 を gh issue view / gh pr view で調査する",
            "--context-file", str(context_file),
            "--gh-issue", "313",
            "--gh-pr", "321",
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert build_result.returncode == 0, (
        f"SKILL.md example build_request.py failed:\nstdout: {build_result.stdout}\nstderr: {build_result.stderr}"
    )
    assert output.exists(), "request.json was not created"

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
        f"SKILL.md example --validate-only failed:\nstdout: {validate_result.stdout}\nstderr: {validate_result.stderr}"
    )
    assert "OK" in validate_result.stdout or "ok" in validate_result.stdout.lower()

    # Verify the generated request contains gh_commands for issue and PR
    import json as _json
    request = _json.loads(output.read_text(encoding="utf-8"))
    assert request.get("tool_profile") == "github_research"
    gh_commands = request.get("gh_commands", [])
    assert len(gh_commands) >= 2, f"expected at least 2 gh_commands, got: {gh_commands}"
    argv_list = [cmd["argv"] for cmd in gh_commands]
    assert any(argv[0] == "issue" and argv[1] == "view" for argv in argv_list), (
        f"expected 'issue view' in gh_commands, got: {argv_list}"
    )
    assert any(argv[0] == "pr" and argv[1] == "view" for argv in argv_list), (
        f"expected 'pr view' in gh_commands, got: {argv_list}"
    )

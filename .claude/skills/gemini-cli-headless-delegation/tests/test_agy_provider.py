"""Tests for agy provider support in run_gemini_headless.py.

Covers AC1-AC14 for provider=agy path. Uses mock subprocess to avoid
requiring the agy CLI to be installed in the test environment.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Module loading helper (hermetic, no side-effects)
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


def _agy_request(**kwargs: Any) -> dict[str, Any]:
    """Return a minimal valid agy delegation request."""
    base = {
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "agy",
        "prompt": "Return exactly: LOOP_AGY_SMOKE_OK",
        "objective": "Smoke test for agy provider integration",
        "instructions": ["Return exactly: LOOP_AGY_SMOKE_OK", "Do not add any extra text"],
        "output_sections": ["response"],
        "context_files": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# AC1: no_tools profile — agy returns response text + result.json via wrapper
# ---------------------------------------------------------------------------


def test_ac1_no_tools_returns_response_text() -> None:
    """AC1: provider=agy, no_tools -> response text returned, ok=True."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed):
        result = rgh.run_delegation(_agy_request(tool_profile="no_tools"))
    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"
    assert result["transport"] == "agy"


# ---------------------------------------------------------------------------
# AC2: proposal_only profile — agy returns proposal text + result.json
# ---------------------------------------------------------------------------


def test_ac2_proposal_only_returns_response_text() -> None:
    """AC2: provider=agy, proposal_only -> proposal text returned, ok=True."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed):
        result = rgh.run_delegation(_agy_request(tool_profile="proposal_only"))
    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"


# ---------------------------------------------------------------------------
# AC6: unknown_provider fails closed
# ---------------------------------------------------------------------------


def test_ac6_unknown_provider_fails_closed() -> None:
    """AC6: provider=unknown -> validation error with unknown_provider."""
    req = _agy_request(provider="unknown_provider_xyz")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_reason"] is not None
    assert result["failure_class"] == "unknown_provider"
    assert "unknown_provider" in result["failure_reason"]


def test_ac6_gemini_provider_accepted() -> None:
    """AC6: provider=gemini is valid (default path)."""
    errors = rgh.validate_request({
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "gemini",
        "objective": "Test gemini provider validation with enough detail",
        "instructions": ["Step one", "Step two"],
        "output_sections": ["response"],
        "context_files": [],
    })
    # No unknown_provider error should be present
    assert not any("unknown_provider" in e for e in errors)


def test_ac6_missing_provider_defaults_to_gemini() -> None:
    """AC6: provider not specified -> gemini default, no unknown_provider error."""
    errors = rgh.validate_request({
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "objective": "Test default provider with enough detail here",
        "instructions": ["Step one", "Step two"],
        "output_sections": ["response"],
        "context_files": [],
    })
    assert not any("unknown_provider" in e for e in errors)


# ---------------------------------------------------------------------------
# AC7: unsupported profile for agy fails closed (no fallback to gemini)
# ---------------------------------------------------------------------------


def test_ac7_agy_grounded_research_rejected() -> None:
    """AC7: provider=agy with grounded_research -> unsupported_provider_profile."""
    req = _agy_request(tool_profile="grounded_research")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_class"] == "unsupported_provider_profile"


def test_ac7_agy_local_asset_research_rejected() -> None:
    """AC7: provider=agy with local_asset_research -> unsupported_provider_profile."""
    req = _agy_request(tool_profile="local_asset_research")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_class"] == "unsupported_provider_profile"


def test_ac7_agy_github_research_rejected() -> None:
    """AC7: provider=agy with github_research -> unsupported_provider_profile."""
    req = _agy_request(tool_profile="github_research")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_class"] == "unsupported_provider_profile"


# ---------------------------------------------------------------------------
# AC8: _normalize_agy_result does NOT call _parse_envelope
# ---------------------------------------------------------------------------


def test_ac8_normalize_agy_skips_parse_envelope() -> None:
    """AC8: _normalize_agy_result exists and doesn't call _parse_envelope."""
    # Ensure _normalize_agy_result is a function in the module
    assert callable(getattr(rgh, "_normalize_agy_result", None))

    # Call directly with a mock completed process — _parse_envelope should not be called
    completed = _make_completed(0, stdout="plain text response")
    with patch.object(rgh, "_parse_envelope", side_effect=AssertionError("_parse_envelope must not be called for agy")):
        result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is True
    assert result["response_text"] == "plain text response"


# ---------------------------------------------------------------------------
# AC9: agy exit 0 + empty stdout -> agy_output_missing / agy_empty_stdout
# ---------------------------------------------------------------------------


def test_ac9_exit0_empty_stdout_fails_closed() -> None:
    """AC9: provider=agy, exit 0, empty stdout -> fail with agy_output_missing."""
    completed = _make_completed(0, stdout="")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is False
    assert result["failure_reason"] == "agy_output_missing"
    assert result["failure_class"] == "agy_empty_stdout"


def test_ac9_exit0_whitespace_only_stdout_fails_closed() -> None:
    """AC9: provider=agy, exit 0, whitespace-only stdout -> fail with agy_output_missing."""
    completed = _make_completed(0, stdout="   \n  ")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is False
    assert result["failure_reason"] == "agy_output_missing"


# ---------------------------------------------------------------------------
# AC10: raw_command sanitization — no prompt text, absolute paths, or secrets
# ---------------------------------------------------------------------------


def test_ac10_raw_command_sanitized() -> None:
    """AC10: _build_agy_raw_command returns sanitized placeholder."""
    cmd = rgh._build_agy_raw_command("secret prompt with /absolute/path and token=ghp_abc123")
    assert cmd[0] in ("agy", "antigravity")  # basename only
    assert cmd[1] == "-p"
    assert cmd[2] == "<prompt>"  # placeholder, not actual prompt
    assert "secret" not in " ".join(cmd)
    assert "/absolute/path" not in " ".join(cmd)
    assert "ghp_abc123" not in " ".join(cmd)


def test_ac10_raw_command_uses_agy_bin_basename_only() -> None:
    """AC10: AGY_BIN with absolute path -> only basename in raw_command."""
    original = os.environ.get("AGY_BIN")
    try:
        os.environ["AGY_BIN"] = "/usr/local/bin/custom-agy"
        cmd = rgh._build_agy_raw_command("test")
        assert "/" not in cmd[0]
        assert cmd[0] == "custom-agy"
    finally:
        if original is None:
            os.environ.pop("AGY_BIN", None)
        else:
            os.environ["AGY_BIN"] = original


# ---------------------------------------------------------------------------
# AC11: post_to_issue_url forbidden for all agy profiles
# ---------------------------------------------------------------------------


def test_ac11_agy_no_tools_forbids_post_to_issue_url() -> None:
    """AC11: provider=agy, no_tools, post_to_issue_url -> provider_forbids_post_to_issue_url."""
    req = _agy_request(
        tool_profile="no_tools",
        post_to_issue_url="https://github.com/owner/repo/issues/1",
    )
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_class"] == "provider_forbids_post_to_issue_url"


def test_ac11_agy_proposal_only_forbids_post_to_issue_url() -> None:
    """AC11: provider=agy, proposal_only, post_to_issue_url -> provider_forbids_post_to_issue_url."""
    req = _agy_request(
        tool_profile="proposal_only",
        post_to_issue_url="https://github.com/owner/repo/issues/1",
    )
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_class"] == "provider_forbids_post_to_issue_url"


def test_agy_model_rejection_sets_failure_class() -> None:
    """provider=agy で explicit model は unsupported_provider_option を返す。"""
    result = rgh.run_delegation(_agy_request(model="gemini-3-pro"))
    assert result["ok"] is False
    assert result["failure_class"] == "unsupported_provider_option"


def test_agy_empty_prompt_sets_failure_class() -> None:
    """provider=agy で空 prompt は agy_empty_prompt を返す。"""
    result = rgh.run_delegation(_agy_request(prompt="   "))
    assert result["ok"] is False
    assert result["failure_class"] == "agy_empty_prompt"


# ---------------------------------------------------------------------------
# AC12: result contains provider="agy" and safety_mode="degraded_wrapper_only"
# ---------------------------------------------------------------------------


def test_ac12_result_contains_provider_and_safety_mode_on_success() -> None:
    """AC12: ok result includes provider=agy and safety_mode=degraded_wrapper_only."""
    completed = _make_completed(0, stdout="response text")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"


def test_ac12_result_contains_provider_and_safety_mode_on_failure() -> None:
    """AC12: failure result also includes provider=agy and safety_mode=degraded_wrapper_only."""
    completed = _make_completed(1, stdout="", stderr="some error")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# AC13: shell=False, isolated cwd, minimal env, AGY_BIN override
# ---------------------------------------------------------------------------


def test_ac13_run_agy_uses_shell_false_and_minimal_env() -> None:
    """AC13: _run_agy uses shell=False with minimal env."""
    captured_kwargs: dict[str, Any] = {}

    _original_run = subprocess.run

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_kwargs.update(kwargs)
        return _make_completed(0, stdout="ok")

    with patch("subprocess.run", side_effect=mock_run):
        rgh._run_agy("test prompt", 30)

    # shell=False (default when not specified, but must not be True)
    assert (
        captured_kwargs.get("shell") is False
        or "shell" not in captured_kwargs
        or captured_kwargs.get("shell") is False
    )
    # env must be present and minimal
    env = captured_kwargs.get("env")
    assert env is not None, "env must be explicitly set (minimal env required)"
    # Must NOT contain sensitive env vars like GEMINI_API_KEY
    assert "GEMINI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    # cwd must be set to a temp directory
    cwd = captured_kwargs.get("cwd")
    assert cwd is not None


def test_ac13_agy_bin_override() -> None:
    """AC13: AGY_BIN env var overrides the agy binary path."""
    captured_cmd: list[Any] = []

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd.extend(cmd)
        return _make_completed(0, stdout="ok")

    original = os.environ.get("AGY_BIN")
    try:
        os.environ["AGY_BIN"] = "/custom/path/to/my-agy"
        with patch("subprocess.run", side_effect=mock_run):
            rgh._run_agy("test", 30)
    finally:
        if original is None:
            os.environ.pop("AGY_BIN", None)
        else:
            os.environ["AGY_BIN"] = original

    # The actual binary path (not basename) is used for execution
    assert captured_cmd[0] == "/custom/path/to/my-agy"


def test_ac13_minimal_agy_env_allowlist() -> None:
    """AC13: _minimal_agy_env only includes allowlisted keys."""
    env = rgh._minimal_agy_env()
    # Must be a dict
    assert isinstance(env, dict)
    allowed_keys = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"}
    for key in env:
        assert key in allowed_keys, f"unexpected env key: {key!r}"


# ---------------------------------------------------------------------------
# AC14: model specification rejected for agy provider
# ---------------------------------------------------------------------------


def test_ac14_agy_with_model_rejected() -> None:
    """AC14: provider=agy with explicit model -> unsupported_provider_option error."""
    req = _agy_request(model="gemini-3-flash-preview")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    failure = result.get("failure_reason") or ""
    assert "unsupported_provider_option" in failure


def test_ac14_agy_without_model_accepted() -> None:
    """AC14: provider=agy without model -> no unsupported_provider_option error."""
    completed = _make_completed(0, stdout="test response")
    with patch.object(rgh, "_run_agy", return_value=completed):
        result = rgh.run_delegation(_agy_request())
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_agy_exit_nonzero_returns_failure() -> None:
    """agy exit non-0 -> ok=False with agy_exit_nonzero failure class."""
    completed = _make_completed(1, stdout="", stderr="agy error message")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is False
    assert result["failure_class"] == "agy_exit_nonzero"
    assert "agy_exit_nonzero" in result["failure_reason"]


def test_agy_result_surface_populated_on_success() -> None:
    """result_surface is properly populated for agy success."""
    completed = _make_completed(0, stdout="Hello from agy")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is True
    rs = result.get("result_surface", {})
    assert rs.get("mode") == "artifact-first"
    assert rs.get("primary_artifact_type") == "inline_response_text"


def test_agy_no_tools_run_delegation_integration() -> None:
    """Full run_delegation path for provider=agy, no_tools profile."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed) as mock_run:
        result = rgh.run_delegation(_agy_request(tool_profile="no_tools"))
    mock_run.assert_called_once()
    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"
    assert result["transport"] == "agy"


def test_agy_proposal_only_run_delegation_integration() -> None:
    """Full run_delegation path for provider=agy, proposal_only profile."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed) as mock_run:
        result = rgh.run_delegation(_agy_request(tool_profile="proposal_only"))
    mock_run.assert_called_once()
    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"


# ---------------------------------------------------------------------------
# Fix 4: additional edge case tests (empty prompt, invalid timeout, exception classes)
# ---------------------------------------------------------------------------


def test_agy_empty_prompt_fails_closed() -> None:
    """Fix4/AC: provider=agy with empty prompt -> agy_empty_prompt fail-closed."""
    req = _agy_request(prompt="")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    failure = result.get("failure_reason") or ""
    assert "agy_empty_prompt" in failure


def test_agy_whitespace_only_prompt_fails_closed() -> None:
    """Fix4/AC: provider=agy with whitespace-only prompt -> agy_empty_prompt fail-closed."""
    req = _agy_request(prompt="   \n  ")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    failure = result.get("failure_reason") or ""
    assert "agy_empty_prompt" in failure


def test_agy_none_prompt_fails_closed() -> None:
    """Fix4/AC: provider=agy with prompt=None -> agy_empty_prompt fail-closed."""
    req = _agy_request()
    req["prompt"] = None  # type: ignore[assignment]
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    failure = result.get("failure_reason") or ""
    assert "agy_empty_prompt" in failure


def test_agy_invalid_timeout_falls_back_to_default() -> None:
    """Fix4: timeout_sec='abc' -> falls back to DEFAULT_TIMEOUT_SEC, no uncaught ValueError."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed) as mock_run:
        result = rgh.run_delegation(_agy_request(timeout_sec="abc"))
    # Should not raise ValueError; result must be ok
    assert result["ok"] is True
    mock_run.assert_called_once()
    # timeout passed to _run_agy must be the default integer value
    call_args = mock_run.call_args
    actual_timeout = call_args[0][1] if call_args[0] else call_args[1].get("timeout_sec")
    assert isinstance(actual_timeout, int)
    assert actual_timeout == rgh.DEFAULT_TIMEOUT_SEC


def test_agy_timeout_expired_returns_failure_class() -> None:
    """Fix4: subprocess.TimeoutExpired -> failure_class='agy_timeout'."""
    with patch.object(rgh, "_run_agy", side_effect=subprocess.TimeoutExpired(cmd="agy", timeout=30)):
        result = rgh.run_delegation(_agy_request())
    assert result["ok"] is False
    assert result.get("failure_class") == "agy_timeout"
    assert "agy_timeout" in (result.get("failure_reason") or "")


def test_agy_file_not_found_returns_failure_class() -> None:
    """Fix4: FileNotFoundError -> failure_class='agy_not_found'."""
    with patch.object(rgh, "_run_agy", side_effect=FileNotFoundError("agy not found")):
        result = rgh.run_delegation(_agy_request())
    assert result["ok"] is False
    assert result.get("failure_class") == "agy_not_found"
    assert "agy_not_found" in (result.get("failure_reason") or "")

"""Tests for the agy invocation argv positional structure allowlist.

Issue #1807 (AC9 permission-bypass-flag-rejection, follow-up of #1265):
`run_gemini_headless.py` builds the agy execution argv from a single
canonical function (`_build_agy_inner_argv()`), and validates it with
`_validate_agy_invocation_argv()` -- a position-based structure allowlist --
before it is ever passed to `subprocess.run()`. This is a fail-closed
defense-in-depth check against permission-bypass flags (e.g.
`--dangerously-skip-permissions`, confirmed to exist via live `agy --help`
output in `references/agy-headless-tool-use-investigation.md`) reaching the
agy subprocess through an unapproved trailing argv option, and against any
*future* unknown flag as well (it is a structural allowlist, not a denylist
keyed on today's known flag names).

Test style mirrors test_agy_provider.py: importlib-based module load +
`unittest.mock.patch("subprocess.run", ...)` to avoid requiring the real
`agy` CLI in the test environment.
"""

from __future__ import annotations

import importlib.util
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# AC1: _validate_agy_invocation_argv() exists and enforces the positional
# structure allowlist directly (pure-function unit coverage).
# ---------------------------------------------------------------------------


def test_validate_agy_invocation_argv_exists() -> None:
    """AC1: the validator function is defined on the module."""
    assert hasattr(rgh, "_validate_agy_invocation_argv")
    assert callable(rgh._validate_agy_invocation_argv)


def test_build_agy_inner_argv_is_single_canonical_source() -> None:
    """AC1: both the real execution argv and the audit-display argv are
    derived from the same `_build_agy_inner_argv()` builder."""
    exec_argv = rgh._build_agy_inner_argv("agy", "hello world")
    assert exec_argv == ["agy", "-p", "hello world"]
    exec_argv_with_model = rgh._build_agy_inner_argv("agy", "hello world", "claude-sonnet-4-6")
    assert exec_argv_with_model == ["agy", "-p", "hello world", "--model", "claude-sonnet-4-6"]
    # `_build_agy_raw_command()` (audit display) shares the same builder.
    audit_argv = rgh._build_agy_raw_command("")
    assert audit_argv == rgh._build_agy_inner_argv("agy", "<prompt>")


# ---------------------------------------------------------------------------
# AC2: test_permission_bypass_allowed
# Approved-structure argv (no flags, or --model only) passes the allowlist
# and agy invocation proceeds/succeeds as before.
# ---------------------------------------------------------------------------


def test_permission_bypass_allowed() -> None:
    """AC2: approved-structure argv (no trailing flags) passes validation,
    and `_run_agy()` still invokes agy successfully (no regression)."""
    approved_argv = rgh._build_agy_inner_argv("agy", "hello world")
    # Must not raise.
    rgh._validate_agy_invocation_argv(approved_argv)

    approved_with_model = rgh._build_agy_inner_argv("agy", "hello world", "claude-sonnet-4-6")
    rgh._validate_agy_invocation_argv(approved_with_model)

    captured_cmd: list[Any] = []

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd.extend(cmd)
        return _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")

    with patch("subprocess.run", side_effect=mock_run):
        completed = rgh._run_agy("hello world", 30)

    assert completed.returncode == 0
    assert completed.stdout == "LOOP_AGY_SMOKE_OK"
    assert captured_cmd == ["agy", "-p", "hello world"]


# ---------------------------------------------------------------------------
# AC3: test_permission_bypass_rejected
# An argv with an unknown trailing option (--dangerously-skip-permissions)
# is rejected as a structure allowlist violation.
# ---------------------------------------------------------------------------


def test_permission_bypass_rejected() -> None:
    """AC3: an argv with `--dangerously-skip-permissions` mixed into the
    trailing options is rejected by `_validate_agy_invocation_argv()`."""
    bypass_argv = ["agy", "-p", "hello world", "--dangerously-skip-permissions"]
    try:
        rgh._validate_agy_invocation_argv(bypass_argv)
        raise AssertionError("expected AgyInvocationPolicyError to be raised")
    except rgh.AgyInvocationPolicyError:
        pass

    # Also reject the bypass flag appended after an otherwise-approved
    # --model clause (structure allowlist, not a substring/last-token check).
    bypass_with_model = [
        "agy",
        "-p",
        "hello world",
        "--model",
        "claude-sonnet-4-6",
        "--dangerously-skip-permissions",
    ]
    try:
        rgh._validate_agy_invocation_argv(bypass_with_model)
        raise AssertionError("expected AgyInvocationPolicyError to be raised")
    except rgh.AgyInvocationPolicyError:
        pass

    # And reject any other unrecognized trailing option, not just the one
    # known-real flag name (this is a structural allowlist, not a denylist).
    unknown_flag_argv = ["agy", "-p", "hello world", "--some-future-unknown-flag"]
    try:
        rgh._validate_agy_invocation_argv(unknown_flag_argv)
        raise AssertionError("expected AgyInvocationPolicyError to be raised")
    except rgh.AgyInvocationPolicyError:
        pass


def test_permission_bypass_flag_never_reaches_subprocess_run() -> None:
    """AC3: end-to-end -- if a permission-bypass flag were ever injected into
    `_run_agy()`'s command construction, the fail-closed guard raises before
    `subprocess.run()` is called (rather than silently executing it)."""
    captured_cmd: list[Any] = []

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd.extend(cmd)
        return _make_completed(0, stdout="should not be reached")

    def poisoned_builder(agy_bin: str, prompt: str, model: str | None = None) -> list[str]:
        return [agy_bin, "-p", prompt, "--dangerously-skip-permissions"]

    with patch.object(rgh, "_build_agy_inner_argv", side_effect=poisoned_builder):
        with patch("subprocess.run", side_effect=mock_run):
            try:
                rgh._run_agy("hello world", 30)
                raise AssertionError("expected AgyInvocationPolicyError to be raised")
            except rgh.AgyInvocationPolicyError:
                pass

    assert captured_cmd == [], "subprocess.run() must not be invoked when the argv is rejected"


# ---------------------------------------------------------------------------
# AC4: run_delegation() classifies the rejection into the new
# `agy_invocation_policy_denied` failure_class (distinct from
# `agy_permission_denied`), non-retryable.
# ---------------------------------------------------------------------------


def test_run_delegation_classifies_invocation_policy_denied() -> None:
    """AC4: when `_run_agy()` raises `AgyInvocationPolicyError`,
    `run_delegation()` returns `failure_class: agy_invocation_policy_denied`,
    not `agy_permission_denied` or `agy_unexpected_error`."""
    request = {
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "agy",
        "prompt": "Return exactly: LOOP_AGY_SMOKE_OK",
        "objective": "Smoke test for agy invocation policy denial classification",
        "instructions": ["Return exactly: LOOP_AGY_SMOKE_OK", "Do not add any extra text"],
        "output_sections": ["response"],
        "context_files": [],
    }

    def raising_run_agy(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        raise rgh.AgyInvocationPolicyError("agy invocation argv trailing options must be empty or --model only")

    with patch.object(rgh, "_run_agy", side_effect=raising_run_agy):
        result = rgh.run_delegation(request)

    assert result["ok"] is False
    assert result["failure_class"] == "agy_invocation_policy_denied"
    assert result["failure_class"] != "agy_permission_denied"
    assert result["failure_class"] != "agy_unexpected_error"
    assert "agy_invocation_policy_denied" in (result["failure_reason"] or "")

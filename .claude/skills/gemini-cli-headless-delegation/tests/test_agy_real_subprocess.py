"""Real subprocess coverage for the AGY provider failure boundary.

These tests intentionally do not mock ``_run_agy()`` or ``subprocess.run()``.
Each case supplies an executable through ``AGY_BIN`` and exercises the wrapper
through ``run_delegation()``.
"""
from __future__ import annotations

import importlib.util
import stat
import types
from pathlib import Path
from typing import Any

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"
_SUCCESS_SENTINEL = "LOOP_AGY_REAL_SUBPROCESS_OK"
_STDERR_SENTINEL = "LOOP_AGY_REAL_SUBPROCESS_STDERR"
_EXPECTED_PROMPT = "Return exactly: LOOP_AGY_REAL_SUBPROCESS"
_STRICT_ARGV_GUARD = (
    'test "$#" -eq 2 || exit 90\n'
    'test "$1" = "-p" || exit 91\n'
    f'test "$2" = "{_EXPECTED_PROMPT}" || exit 92\n'
)


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless_real_subprocess", _SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


rgh = _load_module()


def _agy_request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "agy",
        "prompt": _EXPECTED_PROMPT,
        "objective": "Exercise the AGY subprocess boundary with a fake executable.",
        "instructions": ["Return the requested result", "Do not use tools"],
        "output_sections": ["response"],
        "context_files": [],
    }
    request.update(overrides)
    return request


def _write_fake_agy(tmp_path: Path, name: str, body: str) -> Path:
    binary = tmp_path / name
    binary.write_text("#!/bin/sh\n" + _STRICT_ARGV_GUARD + body, encoding="utf-8")
    binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return binary


def test_agy_real_subprocess_success_round_trip(tmp_path: Path, monkeypatch: Any) -> None:
    """AC1: a spawned stdout sentinel reaches the caller's response surface."""
    fake_agy = _write_fake_agy(
        tmp_path,
        "agy-success",
        f"printf '%s\\n' '{_SUCCESS_SENTINEL}'\nexit 0\n",
    )
    monkeypatch.setenv("AGY_BIN", str(fake_agy))

    result = rgh.run_delegation(_agy_request())

    assert result["ok"] is True
    assert result["provider"] == "agy"
    assert result["exit_code"] == 0
    assert result["response_text"] == _SUCCESS_SENTINEL
    assert result["failure_class"] is None
    assert result["result_surface"]["primary_artifact_type"] == "inline_response_text"


@pytest.mark.parametrize(
    ("ci_value", "expected_failure_class"),
    [(None, "agy_empty_stdout"), ("1", "agy_output_missing")],
)
def test_agy_real_subprocess_empty_stdout_classified(
    tmp_path: Path,
    monkeypatch: Any,
    ci_value: str | None,
    expected_failure_class: str,
) -> None:
    """AC2: a spawned exit-0 executable with no stdout fails closed in both environments."""
    fake_agy = _write_fake_agy(
        tmp_path,
        "agy-empty",
        "exit 0\n",
    )
    monkeypatch.setenv("AGY_BIN", str(fake_agy))
    if ci_value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci_value)

    result = rgh.run_delegation(_agy_request())

    assert result["ok"] is False
    assert result["exit_code"] == 0
    assert result["failure_class"] == expected_failure_class
    assert result["response_text"] is None


def test_agy_real_subprocess_timeout_classified(tmp_path: Path, monkeypatch: Any) -> None:
    """AC2: a spawned hanging executable raises TimeoutExpired to the wrapper."""
    fake_agy = _write_fake_agy(
        tmp_path,
        "agy-timeout",
        "exec sleep 30\n",
    )
    monkeypatch.setenv("AGY_BIN", str(fake_agy))

    result = rgh.run_delegation(_agy_request(timeout_sec=1))

    assert result["ok"] is False
    assert result["failure_class"] == "agy_timeout"
    assert result["response_text"] is None


def test_agy_real_subprocess_nonzero_exact_argv_exit23_stderr(tmp_path: Path, monkeypatch: Any) -> None:
    """AC4: a spawned non-zero executable preserves exact argv, stderr, and exit code."""
    fake_agy = _write_fake_agy(
        tmp_path,
        "agy-nonzero",
        f"printf '%s\\n' '{_STDERR_SENTINEL}' >&2\nexit 23\n",
    )
    monkeypatch.setenv("AGY_BIN", str(fake_agy))

    result = rgh.run_delegation(_agy_request())

    assert result["ok"] is False
    assert result["failure_class"] == "agy_exit_nonzero"
    assert result["exit_code"] == 23
    assert result["stderr"] == _STDERR_SENTINEL
    assert result["response_text"] is None

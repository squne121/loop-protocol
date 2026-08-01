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


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


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
        "prompt": "Return exactly: LOOP_AGY_REAL_SUBPROCESS",
        "objective": "Exercise the AGY subprocess boundary with a fake executable.",
        "instructions": ["Return the requested result", "Do not use tools"],
        "output_sections": ["response"],
        "context_files": [],
    }
    request.update(overrides)
    return request


def _write_fake_agy(tmp_path: Path, name: str, body: str) -> Path:
    binary = tmp_path / name
    binary.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return binary


def test_agy_real_subprocess_empty_stdout_classified(tmp_path: Path, monkeypatch: Any) -> None:
    """AC1: a spawned exit-0 executable with no stdout fails closed."""
    fake_agy = _write_fake_agy(
        tmp_path,
        "agy-empty",
        'test "$1" = "-p" && test "$2" = "Return exactly: LOOP_AGY_REAL_SUBPROCESS" || exit 91\nexit 0\n',
    )
    monkeypatch.setenv("AGY_BIN", str(fake_agy))
    monkeypatch.delenv("CI", raising=False)

    result = rgh.run_delegation(_agy_request())

    assert result["ok"] is False
    assert result["failure_class"] == "agy_empty_stdout"
    assert result["response_text"] is None


def test_agy_real_subprocess_timeout_classified(tmp_path: Path, monkeypatch: Any) -> None:
    """AC2: a spawned hanging executable raises TimeoutExpired to the wrapper."""
    fake_agy = _write_fake_agy(
        tmp_path,
        "agy-timeout",
        'test "$1" = "-p" && test "$2" = "Return exactly: LOOP_AGY_REAL_SUBPROCESS" || exit 91\nexec sleep 30\n',
    )
    monkeypatch.setenv("AGY_BIN", str(fake_agy))

    result = rgh.run_delegation(_agy_request(timeout_sec=1))

    assert result["ok"] is False
    assert result["failure_class"] == "agy_timeout"
    assert result["response_text"] is None


def test_agy_real_subprocess_nonzero_empty_stdout_classified(tmp_path: Path, monkeypatch: Any) -> None:
    """AC3: a spawned non-zero executable takes precedence over empty stdout."""
    fake_agy = _write_fake_agy(
        tmp_path,
        "agy-nonzero",
        'test "$1" = "-p" && test "$2" = "Return exactly: LOOP_AGY_REAL_SUBPROCESS" || exit 91\nexit 23\n',
    )
    monkeypatch.setenv("AGY_BIN", str(fake_agy))

    result = rgh.run_delegation(_agy_request())

    assert result["ok"] is False
    assert result["failure_class"] == "agy_exit_nonzero"
    assert result["response_text"] is None

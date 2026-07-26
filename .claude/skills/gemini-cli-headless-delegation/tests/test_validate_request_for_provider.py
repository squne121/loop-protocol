"""Tests for run_gemini_headless.validate_request_for_provider() (Issue #1692).

AC coverage:
  AC3: validate_request_for_provider() dispatches by provider/tool_profile:
       provider="gemini" -> validate_request(); provider="agy" +
       tool_profile != local_asset_research -> _validate_agy_request() only;
       provider="agy" + tool_profile == local_asset_research ->
       _validate_agy_request() AND _validate_agy_local_asset_request().
  AC4: build_request.py's validation path and run_gemini_headless.py
       --validate-only share the single validate_request_for_provider()
       entrypoint; no private-validator direct call exists in either.
  AC7: --validate-only uses validate_request_for_provider() for provider=agy
       requests (not the Gemini validate_request()), so a missing `prompt`
       is caught at validate-only time.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load_run_gemini_headless():
    path = _SCRIPTS_DIR / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# AC3: dispatch by provider and tool_profile
# ---------------------------------------------------------------------------


def test_validate_request_for_provider_dispatches_by_provider_and_profile(monkeypatch):
    """GIVEN validate_request_for_provider()
    WHEN called with provider=gemini, provider=agy (non-local_asset_research),
         and provider=agy + tool_profile=local_asset_research
    THEN it calls validate_request() / _validate_agy_request() /
         _validate_agy_local_asset_request() exactly as documented."""
    rgh = load_run_gemini_headless()

    calls: list[str] = []
    monkeypatch.setattr(rgh, "validate_request", lambda *a, **k: calls.append("validate_request") or [])
    monkeypatch.setattr(rgh, "_validate_agy_request", lambda *a, **k: calls.append("_validate_agy_request") or [])
    monkeypatch.setattr(
        rgh,
        "_validate_agy_local_asset_request",
        lambda *a, **k: calls.append("_validate_agy_local_asset_request") or [],
    )

    # provider="gemini" -> validate_request() only.
    calls.clear()
    rgh.validate_request_for_provider({"provider": "gemini", "tool_profile": "no_tools"})
    assert calls == ["validate_request"]

    # provider omitted -> defaults to gemini, same as explicit "gemini".
    calls.clear()
    rgh.validate_request_for_provider({"tool_profile": "no_tools"})
    assert calls == ["validate_request"]

    # provider="agy", tool_profile != local_asset_research -> _validate_agy_request() only.
    calls.clear()
    rgh.validate_request_for_provider({"provider": "agy", "tool_profile": "no_tools", "prompt": "hi"})
    assert calls == ["_validate_agy_request"], (
        f"expected only _validate_agy_request for non-local_asset_research agy, got: {calls}"
    )

    # provider="agy", tool_profile == local_asset_research -> both validators, in order.
    calls.clear()
    rgh.validate_request_for_provider(
        {"provider": "agy", "tool_profile": "local_asset_research", "prompt": "hi"}
    )
    assert calls == ["_validate_agy_request", "_validate_agy_local_asset_request"], (
        f"expected both agy validators for local_asset_research, got: {calls}"
    )


def test_validate_request_for_provider_unknown_provider_fails_closed():
    """GIVEN an unknown provider value
    WHEN validate_request_for_provider() is called
    THEN it returns a non-empty error list (fail-closed) instead of raising
    or silently passing."""
    rgh = load_run_gemini_headless()
    errors = rgh.validate_request_for_provider({"provider": "unknown_provider_xyz"})
    assert errors, "unknown provider must fail-closed with at least one error"
    assert any("unknown_provider" in e for e in errors)


# ---------------------------------------------------------------------------
# AC4: build_request.py and --validate-only share validate_request_for_provider
# ---------------------------------------------------------------------------


def test_validate_only_and_builder_share_validate_request_for_provider():
    """GIVEN the source of build_request.py and run_gemini_headless.py
    WHEN inspected for direct private-validator calls
    THEN neither file calls _validate_agy_request(...) or
    _validate_agy_local_asset_request(...) directly outside of
    validate_request_for_provider() itself / the execution dispatcher
    (_run_delegation_core), AND both build_request.py's validation path and
    the --validate-only CLI branch call validate_request_for_provider."""
    build_request_lines = (_SCRIPTS_DIR / "build_request.py").read_text(encoding="utf-8").splitlines()
    rgh_src = (_SCRIPTS_DIR / "run_gemini_headless.py").read_text(encoding="utf-8")

    def _code_only(line: str) -> str:
        # Strip full-line and trailing comments for a simple call-site scan
        # (this file has no "#" inside string literals on the relevant lines).
        return line.split("#", 1)[0]

    build_request_code = "\n".join(_code_only(line) for line in build_request_lines)

    # build_request.py must never call the private AGY validators directly.
    assert "_validate_agy_request(" not in build_request_code
    assert "_validate_agy_local_asset_request(" not in build_request_code
    # build_request.py must reference the shared provider-aware entrypoint.
    assert "validate_request_for_provider" in build_request_code

    # The --validate-only CLI branch must call validate_request_for_provider,
    # not validate_request() directly.
    validate_only_block_match = re.search(
        r"if args\.validate_only:.*?return 0\n", rgh_src, re.DOTALL
    )
    assert validate_only_block_match, "could not locate --validate-only block in run_gemini_headless.py"
    validate_only_block = validate_only_block_match.group(0)
    assert "validate_request_for_provider(" in validate_only_block, (
        "--validate-only must call validate_request_for_provider(), not validate_request() directly"
    )
    assert re.search(r"(?<!_for_provider)\bvalidate_request\(", validate_only_block) is None, (
        "--validate-only must not call the bare Gemini validate_request() directly"
    )


def test_validate_only_uses_provider_aware_validator_for_agy(tmp_path):
    """GIVEN a provider=agy request file missing the required `prompt` field
    WHEN run_gemini_headless.py --validate-only is invoked on it
    THEN validation FAILS at validate-only time via the AGY validator
    (agy_empty_prompt), proving --validate-only dispatches through
    validate_request_for_provider() rather than the bare Gemini
    validate_request() (which does not know about `prompt` at all and would
    instead fail on missing objective/instructions/context_files -- a
    different, misleading failure_reason)."""
    request_file = tmp_path / "agy_request.json"
    request_file.write_text(
        json.dumps(
            {
                "schema": "delegation_request_v1",
                "provider": "agy",
                "tool_profile": "no_tools",
                # prompt intentionally omitted
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "run_gemini_headless.py"),
            "--validate-only",
            str(request_file),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, (
        f"expected --validate-only to fail for AGY request missing prompt, "
        f"got returncode={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "agy_empty_prompt" in result.stdout, (
        f"expected the AGY-specific agy_empty_prompt error, got stdout: {result.stdout}"
    )
    # The Gemini-only validator would instead complain about a missing
    # objective, which must NOT be the failure surfaced here.
    assert "objective must be a non-empty string" not in result.stdout


def test_validate_only_accepts_valid_agy_request(tmp_path):
    """GIVEN a well-formed provider=agy request (prompt present, no model)
    WHEN run_gemini_headless.py --validate-only is invoked on it
    THEN validation succeeds (exit 0)."""
    request_file = tmp_path / "agy_request.json"
    request_file.write_text(
        json.dumps(
            {
                "schema": "delegation_request_v1",
                "provider": "agy",
                "tool_profile": "no_tools",
                "prompt": "Return exactly: LOOP_AGY_SMOKE_OK",
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "run_gemini_headless.py"),
            "--validate-only",
            str(request_file),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"expected valid AGY request to pass --validate-only, "
        f"got returncode={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )

"""Tests for build_request.py's --provider agy prompt-first contract (Issue #1692).

AC coverage:
  AC5: --provider agy --prompt "..." generates a request that contains
       `prompt` and does not contain `model`. --provider agy --model X is
       builder-level fail-closed.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

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


def test_agy_request_is_prompt_first_and_rejects_explicit_model(tmp_path):
    """GIVEN --provider agy --prompt "..."
    WHEN build_request.py generates the request
    THEN the request contains `prompt` and does not contain `model`,
    AND it passes validate_request_for_provider(),
    AND --provider agy --model X fails closed at the builder level."""
    br = load_build_request()
    rgh = load_run_gemini_headless()

    # --- happy path: prompt-first AGY request ---
    output = tmp_path / "agy_request.json"
    exit_code = br.build_request(
        profile="no_tools",
        objective=None,
        instructions=None,
        context_files=None,
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
        provider="agy",
        prompt="Return exactly: LOOP_AGY_SMOKE_OK",
    )
    assert exit_code == 0, f"build_request returned {exit_code}"

    request = json.loads(output.read_text(encoding="utf-8"))
    assert request["provider"] == "agy"
    assert request["prompt"] == "Return exactly: LOOP_AGY_SMOKE_OK"
    assert "model" not in request, f"AGY request must never contain model, got: {request}"

    errors = rgh.validate_request_for_provider(request, request_path=output)
    assert errors == [], f"validate_request_for_provider returned errors: {errors}"

    # --- fail-closed: --provider agy --model X ---
    failure_output = tmp_path / "agy_model_failure.json"
    failure_exit_code = br.build_request(
        profile="no_tools",
        objective=None,
        instructions=None,
        context_files=None,
        gh_pr=None,
        gh_issue=None,
        output=failure_output,
        base_dir=tmp_path,
        provider="agy",
        prompt="Return exactly: LOOP_AGY_SMOKE_OK",
        model="gemini-3-pro-preview",
    )
    assert failure_exit_code == 1, f"expected exit_code 1, got {failure_exit_code}"
    failure_payload = json.loads(failure_output.read_text(encoding="utf-8"))
    assert failure_payload["ok"] is False
    assert failure_payload["failure_class"] == "agy_model_not_supported", (
        f"expected stable failure_class='agy_model_not_supported', "
        f"got: {failure_payload.get('failure_class')!r}"
    )


def test_agy_request_without_prompt_fails_closed(tmp_path):
    """GIVEN --provider agy without --prompt
    WHEN build_request.py is invoked
    THEN it fails closed with failure_class=agy_prompt_required (prompt-first
    contract: prompt is the AGY request's primary/required content)."""
    br = load_build_request()

    output = tmp_path / "agy_missing_prompt.json"
    exit_code = br.build_request(
        profile="no_tools",
        objective=None,
        instructions=None,
        context_files=None,
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
        provider="agy",
    )
    assert exit_code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["failure_class"] == "agy_prompt_required"


def test_agy_prompt_first_contract_via_cli_subprocess(tmp_path):
    """GIVEN the real CLI invocation --provider agy --prompt ...
    WHEN build_request.py is run as a subprocess
    THEN the output request contains prompt and not model, and it passes
    run_gemini_headless.py --validate-only."""
    output = tmp_path / "agy_request.json"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "build_request.py"),
            "--provider", "agy",
            "--profile", "no_tools",
            "--prompt", "Return exactly: LOOP_AGY_SMOKE_OK",
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"build_request.py failed: {result.stderr}"

    request = json.loads(output.read_text(encoding="utf-8"))
    assert request["prompt"] == "Return exactly: LOOP_AGY_SMOKE_OK"
    assert "model" not in request

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


def test_agy_model_and_role_conflict_still_takes_priority(tmp_path):
    """GIVEN --provider agy --model X --role Y (both model and role given)
    WHEN build_request.py is invoked
    THEN the general model_role_conflict check fires first (not
    agy_model_not_supported), keeping the fail-closed failure_class stable
    regardless of provider."""
    br = load_build_request()

    output = tmp_path / "agy_conflict.json"
    exit_code = br.build_request(
        profile="no_tools",
        objective=None,
        instructions=None,
        context_files=None,
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
        provider="agy",
        prompt="Return exactly: LOOP_AGY_SMOKE_OK",
        model="gemini-3-pro-preview",
        role="implementation",
    )
    assert exit_code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["failure_class"] == "model_role_conflict"

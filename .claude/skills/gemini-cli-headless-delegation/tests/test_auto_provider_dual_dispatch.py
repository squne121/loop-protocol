"""Tests for build_request.py's --provider auto guardrails (Issue #1692).

AC coverage:
  AC6: --provider auto --prompt "..." is builder-level fail-closed
       (auto is not allowed to accept an independently caller-specified
       prompt: provider_auto_dispatch() copies the request and only swaps
       `provider`, so a caller-specified prompt could diverge from the
       objective/instructions used by the gemini attempt -- OWNER review
       Blocker 1). --provider auto --model X is also builder-level
       fail-closed (an explicit model would carry over into an AGY
       fallback attempt and fail there with unsupported_provider_option).
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


def test_auto_request_rejects_explicit_prompt_and_explicit_model(tmp_path):
    """GIVEN --provider auto --prompt "..." (and, separately, --provider auto --model X)
    WHEN build_request.py is invoked
    THEN both fail closed at the builder level with stable, distinct
    failure_class values (auto_prompt_not_supported / auto_model_not_supported)."""
    br = load_build_request()

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")

    # --- --provider auto --prompt ... fails closed ---
    prompt_failure_output = tmp_path / "auto_prompt_failure.json"
    prompt_exit_code = br.build_request(
        profile="no_tools",
        objective="Summarize the build failure from context file",
        instructions=None,
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=prompt_failure_output,
        base_dir=tmp_path,
        provider="auto",
        prompt="This should not be accepted for provider=auto",
    )
    assert prompt_exit_code == 1, f"expected exit_code 1, got {prompt_exit_code}"
    prompt_payload = json.loads(prompt_failure_output.read_text(encoding="utf-8"))
    assert prompt_payload["ok"] is False
    assert prompt_payload["failure_class"] == "auto_prompt_not_supported", (
        f"expected stable failure_class='auto_prompt_not_supported', "
        f"got: {prompt_payload.get('failure_class')!r}"
    )

    # --- --provider auto --model X fails closed ---
    model_failure_output = tmp_path / "auto_model_failure.json"
    model_exit_code = br.build_request(
        profile="no_tools",
        objective="Summarize the build failure from context file",
        instructions=None,
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=model_failure_output,
        base_dir=tmp_path,
        provider="auto",
        model="gemini-3-pro-preview",
    )
    assert model_exit_code == 1, f"expected exit_code 1, got {model_exit_code}"
    model_payload = json.loads(model_failure_output.read_text(encoding="utf-8"))
    assert model_payload["ok"] is False
    assert model_payload["failure_class"] == "auto_model_not_supported", (
        f"expected stable failure_class='auto_model_not_supported', "
        f"got: {model_payload.get('failure_class')!r}"
    )


def test_auto_request_without_prompt_or_model_succeeds(tmp_path):
    """GIVEN --provider auto with only the structured (objective/instructions/
    context_files) inputs (no --prompt, no --model)
    WHEN build_request.py generates the request
    THEN it succeeds, embeds provider="auto", and passes
    validate_request_for_provider()."""
    br = load_build_request()
    rgh = load_run_gemini_headless()

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")

    output = tmp_path / "auto_request.json"
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
        provider="auto",
    )
    assert exit_code == 0, f"build_request returned {exit_code}"

    request = json.loads(output.read_text(encoding="utf-8"))
    assert request["provider"] == "auto"
    assert "prompt" not in request
    assert "model" not in request

    errors = rgh.validate_request_for_provider(request, request_path=output)
    assert errors == [], f"validate_request_for_provider returned errors: {errors}"


def test_auto_provider_guardrails_via_cli_subprocess(tmp_path):
    """GIVEN the real CLI invocation --provider auto --prompt ...
    WHEN build_request.py is run as a subprocess
    THEN it exits non-zero with failure_class=auto_prompt_not_supported."""
    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "auto_failure.json"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "build_request.py"),
            "--provider", "auto",
            "--profile", "no_tools",
            "--objective", "Summarize the build failure from context file",
            "--context-file", str(context_file),
            "--prompt", "This should not be accepted for provider=auto",
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "expected non-zero exit for --provider auto --prompt"

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["failure_class"] == "auto_prompt_not_supported"

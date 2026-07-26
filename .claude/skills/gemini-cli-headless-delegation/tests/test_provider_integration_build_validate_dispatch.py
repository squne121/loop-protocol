"""End-to-end build -> validate -> dispatch integration test for
provider="auto" (Issue #1692 AC11).

AGY/Gemini CLI subprocess calls are monkeypatched with test doubles
(``_run_agy`` / ``_run_gemini``-level functions); no real CLI subprocess or
network call is ever made.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import types
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, filename: str) -> types.ModuleType:
    path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def load_build_request() -> types.ModuleType:
    return _load("build_request", "build_request.py")


def load_run_gemini_headless() -> types.ModuleType:
    return _load("run_gemini_headless", "run_gemini_headless.py")


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=["agy"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_auto_build_validate_dispatch_pipeline_agy_first(tmp_path, monkeypatch):
    """GIVEN a provider="auto" request built by build_request.py
    WHEN it is validated with validate_request_for_provider() and then
    dispatched with provider_auto_dispatch() (AGY CLI subprocess call
    replaced with a test double via _run_agy)
    THEN the full pipeline completes successfully via the AGY candidate
    (attempted first, per PROVIDER_AUTO_RUNTIME_ORDER), and the Gemini CLI
    is never invoked (no provider="gemini" entry in provider_attempts)."""
    br = load_build_request()
    rgh = load_run_gemini_headless()
    monkeypatch.setattr(br, "_load_run_gemini_headless_module", lambda: rgh)

    # --- Step 1: build_request.py --provider auto ... ---
    context_file = tmp_path / "context.md"
    context_file.write_text("build failure log excerpt", encoding="utf-8")

    output = tmp_path / "auto_request.json"
    build_exit_code = br.build_request(
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
    assert build_exit_code == 0, "build_request.py failed to build a provider=auto request"

    request = json.loads(output.read_text(encoding="utf-8"))
    assert request["provider"] == "auto"
    assert "prompt" not in request
    assert "model" not in request

    # --- Step 2: validate_request_for_provider() (build/validate-only stage) ---
    validation_errors = rgh.validate_request_for_provider(request, request_path=output)
    assert validation_errors == [], f"provider=auto request failed validation: {validation_errors}"

    # --- Step 3: provider_auto_dispatch() with AGY CLI replaced by a test double ---
    agy_completed = _make_completed(0, stdout="AGY_INTEGRATION_TEST_RESPONSE")

    def fail_if_gemini_cli_invoked(*args, **kwargs):
        raise AssertionError(
            "Gemini CLI (_run_gemini) must not be invoked: AGY (attempted first, per "
            "PROVIDER_AUTO_RUNTIME_ORDER) already succeeded"
        )

    with patch.object(rgh, "_run_agy", return_value=agy_completed), patch.object(
        rgh, "_run_gemini", side_effect=fail_if_gemini_cli_invoked
    ):
        result = rgh.provider_auto_dispatch(request, request_path=output)

    assert result["ok"] is True, f"expected the auto-dispatch pipeline to succeed via agy, got: {result}"
    assert result["selected_provider"] == "agy"
    assert result["response_text"] == "AGY_INTEGRATION_TEST_RESPONSE"
    assert [attempt["provider"] for attempt in result["provider_attempts"]] == ["agy"]


def test_auto_build_validate_dispatch_pipeline_falls_back_to_gemini(tmp_path, monkeypatch):
    """GIVEN a provider="auto" request built by build_request.py
    WHEN the AGY candidate fails with a fallback-safe (quota) failure_class
    (AGY CLI test double returns a quota-exhausted stderr) and the Gemini
    candidate (CLI test double via _run_gemini) succeeds
    THEN the pipeline falls back to Gemini and reports selected_provider="gemini",
    with both attempts recorded in provider_attempts."""
    br = load_build_request()
    rgh = load_run_gemini_headless()
    monkeypatch.setattr(br, "_load_run_gemini_headless_module", lambda: rgh)

    context_file = tmp_path / "context.md"
    context_file.write_text("build failure log excerpt", encoding="utf-8")

    output = tmp_path / "auto_request.json"
    build_exit_code = br.build_request(
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
    assert build_exit_code == 0

    request = json.loads(output.read_text(encoding="utf-8"))
    validation_errors = rgh.validate_request_for_provider(request, request_path=output)
    assert validation_errors == []

    agy_quota_completed = _make_completed(1, stdout="", stderr="HTTP 429: RESOURCE_EXHAUSTED")
    gemini_success_completed = subprocess.CompletedProcess(
        args=["gemini"],
        returncode=0,
        stdout=json.dumps({"response": "GEMINI_INTEGRATION_TEST_RESPONSE"}),
        stderr="",
    )

    calls: list[str] = []
    real_run_delegation = rgh.run_delegation

    def spying_run_delegation(req, request_path=None, _routing=None):
        calls.append(req["provider"])
        return real_run_delegation(req, request_path=request_path, _routing=_routing)

    with patch.object(rgh, "_run_agy", return_value=agy_quota_completed), patch.object(
        rgh, "run_delegation", side_effect=spying_run_delegation
    ), patch.object(rgh, "_run_gemini", return_value=gemini_success_completed):
        result = rgh.provider_auto_dispatch(request, request_path=output)

    assert calls == ["agy", "gemini"]
    assert result["ok"] is True
    assert result["selected_provider"] == "gemini"
    assert result["response_text"] == "GEMINI_INTEGRATION_TEST_RESPONSE"
    assert result["provider_attempts"][0]["provider"] == "agy"
    assert result["provider_attempts"][0]["failure_class"] == "agy_rate_limited"
    assert result["provider_attempts"][0]["retryable_for_provider_fallback"] is True
    assert result["provider_attempts"][1]["provider"] == "gemini"

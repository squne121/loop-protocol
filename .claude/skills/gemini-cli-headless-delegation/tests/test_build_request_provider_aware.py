"""Tests for build_request.py provider-aware behaviour (Issue #1692).

AC coverage:
  AC1: --provider gemini --role ... --profile ... --objective ... embeds
       provider/role in the generated request; legacy (provider-unspecified)
       invocation request shape does not regress.
  AC2: --model and --role together are builder-level fail-closed with a
       stable failure_class.
"""
from __future__ import annotations

import importlib.util
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


# ---------------------------------------------------------------------------
# AC1: provider/role reflected in generated request; legacy shape unaffected
# ---------------------------------------------------------------------------


def test_provider_role_reflected_in_generated_request_legacy_unaffected(tmp_path):
    """GIVEN --provider gemini --role implementation --profile no_tools --objective "..."
    WHEN build_request.py generates the request
    THEN the request JSON contains provider="gemini" and role="implementation",
    AND a legacy invocation (provider unspecified) never gains a provider/role
        field at all (no regression of the pre-#1692 request shape)."""
    br = load_build_request()
    rgh = load_run_gemini_headless()

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")

    # --- provider-aware invocation ---
    output = tmp_path / "provider_request.json"
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
        provider="gemini",
        role="implementation",
    )
    assert exit_code == 0, f"build_request returned {exit_code}"
    import json

    request = json.loads(output.read_text(encoding="utf-8"))
    assert request["provider"] == "gemini"
    assert request["role"] == "implementation"
    assert "model" not in request
    errors = rgh.validate_request_for_provider(request, request_path=output)
    assert errors == [], f"validate_request_for_provider returned errors: {errors}"

    # --- legacy invocation (provider unspecified): request shape unaffected ---
    legacy_output = tmp_path / "legacy_request.json"
    legacy_exit_code = br.build_request(
        profile="no_tools",
        objective="Summarize the build failure from context file",
        instructions=[
            "Identify the root cause from the context.",
            "List any actionable recommendations.",
        ],
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=legacy_output,
        base_dir=tmp_path,
    )
    assert legacy_exit_code == 0
    legacy_request = json.loads(legacy_output.read_text(encoding="utf-8"))
    assert "provider" not in legacy_request, (
        f"legacy (provider-unspecified) invocation must not embed a provider "
        f"field, got: {legacy_request}"
    )
    assert "role" not in legacy_request
    assert "model" not in legacy_request
    # Legacy shape is exactly the pre-#1692 delegation_request_v1 keys.
    assert set(legacy_request.keys()) == {
        "schema",
        "objective",
        "instructions",
        "tool_profile",
        "output_sections",
        "context_files",
        "timeout_sec",
    }


def test_provider_role_reflected_via_cli_subprocess(tmp_path):
    """GIVEN the real CLI invocation with --provider/--role
    WHEN build_request.py is run as a subprocess
    THEN it exits 0 and the written request contains provider/role."""
    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "request.json"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "build_request.py"),
            "--provider", "gemini",
            "--role", "implementation",
            "--profile", "no_tools",
            "--objective", "Summarize the build failure from context file",
            "--context-file", str(context_file),
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"build_request.py failed: {result.stderr}"

    import json

    request = json.loads(output.read_text(encoding="utf-8"))
    assert request["provider"] == "gemini"
    assert request["role"] == "implementation"


# ---------------------------------------------------------------------------
# AC2: --model and --role together fail-closed at the builder level
# ---------------------------------------------------------------------------


def test_model_and_role_simultaneous_specification_fails_closed(tmp_path):
    """GIVEN --model X --role Y specified together
    WHEN build_request.py is invoked
    THEN it fails closed with a stable failure_class (model_role_conflict)
    and a non-zero exit code."""
    br = load_build_request()

    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")

    output = tmp_path / "failure.json"
    exit_code = br.build_request(
        profile="no_tools",
        objective="Summarize the build failure from context file",
        instructions=None,
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=tmp_path,
        provider="gemini",
        role="implementation",
        model="gemini-3-pro-preview",
    )
    assert exit_code == 1, f"expected exit_code 1, got {exit_code}"

    import json

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert result["failure_class"] == "model_role_conflict", (
        f"expected stable failure_class='model_role_conflict', got: {result.get('failure_class')!r}"
    )
    assert result["failure_reason"]
    assert isinstance(result["next_action"]["argv"], list)


def test_model_and_role_conflict_fails_closed_via_cli_subprocess(tmp_path):
    """GIVEN the real CLI invocation with --model and --role together
    WHEN build_request.py is run as a subprocess
    THEN it exits non-zero with failure_class=model_role_conflict."""
    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")
    output = tmp_path / "failure.json"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "build_request.py"),
            "--model", "gemini-3-pro-preview",
            "--role", "implementation",
            "--profile", "no_tools",
            "--objective", "Summarize the build failure from context file",
            "--context-file", str(context_file),
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "expected non-zero exit for --model + --role conflict"

    import json

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["failure_class"] == "model_role_conflict"

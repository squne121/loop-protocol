"""Tests for provider=agy + tool_profile=local_asset_research request
generation and validation without Gemini-only fields (Issue #1692 AC12).

Background: _validate_agy_local_asset_request() used to delegate its shared
envelope/profile checks to the Gemini-only validate_request(), which
requires objective/instructions/output_sections -- fields the AGY
prompt-first request shape produced by build_request.py's
_build_agy_request() never has. That made every provider=agy +
tool_profile=local_asset_research request fail validation unconditionally.
Issue #1692 AC1-AC8 never exercised this combination, so the gap went
undetected until this Issue's re-scope.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

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


def test_agy_local_asset_research_builder_request_passes_validation(tmp_path, monkeypatch):
    """GIVEN build_request.py --provider agy --profile local_asset_research
         --prompt "..." --context-file <path>
    WHEN the generated request is validated with _validate_agy_request() and
         _validate_agy_local_asset_request()
    THEN both pass, and the generated request never carries objective /
    instructions / output_sections (Gemini-only fields the AGY prompt-first
    contract does not require)."""
    br = load_build_request()
    rgh = load_run_gemini_headless()

    # build_request.py loads its own fresh run_gemini_headless module
    # instance per call (_load_run_gemini_headless_module()); pin it to our
    # already-loaded `rgh` instance so the monkeypatches below (repo root /
    # settings) apply to the exact module build_request.py validates against.
    monkeypatch.setattr(br, "_load_run_gemini_headless_module", lambda: rgh)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    context_file = repo_root / "context.md"
    context_file.write_text("local asset content for evidence bounds", encoding="utf-8")

    monkeypatch.setattr(rgh, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])

    output = tmp_path / "agy_local_asset_request.json"
    exit_code = br.build_request(
        profile="local_asset_research",
        objective=None,
        instructions=None,
        context_files=[str(context_file)],
        gh_pr=None,
        gh_issue=None,
        output=output,
        base_dir=repo_root,
        provider="agy",
        prompt="Summarize the evidence in the attached context file.",
    )
    assert exit_code == 0, f"build_request returned {exit_code}"

    request = json.loads(output.read_text(encoding="utf-8"))
    assert request["provider"] == "agy"
    assert request["tool_profile"] == "local_asset_research"
    assert request["prompt"] == "Summarize the evidence in the attached context file."
    assert "model" not in request

    for gemini_only_field in ("objective", "instructions", "output_sections"):
        assert gemini_only_field not in request, (
            f"AGY prompt-first request must not carry Gemini-only field {gemini_only_field!r}: {request}"
        )

    # AC12: the two AGY validators, called exactly as validate_request_for_provider()
    # and _run_delegation_core()'s own provider=="agy" branch call them, both pass.
    agy_errors = rgh._validate_agy_request(request)
    assert agy_errors == [], f"_validate_agy_request rejected the AGY prompt-first request: {agy_errors}"

    local_asset_errors = rgh._validate_agy_local_asset_request(request, request_path=output)
    assert local_asset_errors == [], (
        f"_validate_agy_local_asset_request rejected the AGY prompt-first request: {local_asset_errors}"
    )

    # And the single shared entrypoint (build_request.py's own validation
    # path, and run_gemini_headless.py --validate-only) agrees.
    combined_errors = rgh.validate_request_for_provider(request, request_path=output)
    assert combined_errors == [], f"validate_request_for_provider rejected the request: {combined_errors}"


def test_agy_local_asset_research_missing_context_file_still_fails_closed(tmp_path, monkeypatch):
    """GIVEN a provider=agy + tool_profile=local_asset_research request with
    NO context_files
    WHEN validated
    THEN it is still rejected (AC12 relaxes only the Gemini-only field
    requirement, not the local_asset_research-specific context file
    requirement)."""
    rgh = load_run_gemini_headless()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr(rgh, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])

    request = {
        "schema": "delegation_request_v1",
        "provider": "agy",
        "tool_profile": "local_asset_research",
        "prompt": "Summarize the evidence.",
    }
    errors = rgh.validate_request_for_provider(request)
    assert errors, "expected local_asset_research context-file requirement to still fail closed"
    assert any("context file" in e for e in errors)


def test_agy_local_asset_research_rejects_context_file_outside_repo(tmp_path, monkeypatch):
    """GIVEN a provider=agy + tool_profile=local_asset_research request whose
    context file resolves outside the repository root
    WHEN validated
    THEN it is rejected (AC12 does not weaken the existing repo-boundary
    check, only the Gemini-only field requirement)."""
    rgh = load_run_gemini_headless()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("outside content", encoding="utf-8")

    monkeypatch.setattr(rgh, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])

    request = {
        "schema": "delegation_request_v1",
        "provider": "agy",
        "tool_profile": "local_asset_research",
        "prompt": "Summarize the evidence.",
        "context_files": [str(outside_file)],
    }
    errors = rgh.validate_request_for_provider(request)
    assert errors, "expected an outside-repo context file to be rejected"
    assert any("inside repository" in e for e in errors)


def test_agy_local_asset_research_forbids_post_to_issue_url(tmp_path, monkeypatch):
    """GIVEN a provider=agy + tool_profile=local_asset_research request with
    post_to_issue_url set
    WHEN validated
    THEN it is rejected (existing post_to_issue_url ban, unaffected by the
    AC12 refactor)."""
    rgh = load_run_gemini_headless()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    context_file = repo_root / "context.md"
    context_file.write_text("content", encoding="utf-8")
    monkeypatch.setattr(rgh, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])

    request = {
        "schema": "delegation_request_v1",
        "provider": "agy",
        "tool_profile": "local_asset_research",
        "prompt": "Summarize the evidence.",
        "context_files": [str(context_file)],
        "post_to_issue_url": "https://github.com/o/r/issues/1",
    }
    errors = rgh.validate_request_for_provider(request)
    assert errors, "expected post_to_issue_url to be rejected for local_asset_research"
    assert any("post_to_issue_url" in e for e in errors)

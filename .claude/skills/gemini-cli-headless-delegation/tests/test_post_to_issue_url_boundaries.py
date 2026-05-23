"""Tests for post_to_issue_url boundary enforcement in validate_request.

AC15: github_research + post_to_issue_url is rejected by validation.

Coverage:
  - github_research + post_to_issue_url -> validation error
  - local_asset_research + post_to_issue_url -> validation error
  - proposal_only + post_to_issue_url -> validation error
  - no_tools + post_to_issue_url -> allowed (no validation error from profile rules)
  - grounded_research + post_to_issue_url -> allowed (no validation error from profile rules)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load_run_gemini_headless():
    path = _SCRIPTS_DIR / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_request(profile: str, post_to_issue_url: str | None, context_file: Path) -> dict:
    r = {
        "schema": "delegation_request_v1",
        "objective": "Investigate the PR history for regressions",
        "instructions": [
            "List the relevant PRs with evidence.",
            "Identify any regression patterns.",
        ],
        "tool_profile": profile,
        "output_sections": ["Summary", "Findings"],
        "context_files": [str(context_file)],
        "timeout_sec": 300,
    }
    if post_to_issue_url is not None:
        r["post_to_issue_url"] = post_to_issue_url
    return r


# ---------------------------------------------------------------------------
# AC15: github_research + post_to_issue_url -> rejected
# ---------------------------------------------------------------------------


def test_github_research_with_post_to_issue_url_is_rejected(tmp_path, monkeypatch):
    """GIVEN a github_research request with post_to_issue_url
    WHEN validate_request is called
    THEN a validation error is returned (github_research forbids post_to_issue_url)."""
    module = load_run_gemini_headless()
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)

    context_file = tmp_path / "context.md"
    context_file.write_text("context", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    request = _make_request(
        profile="github_research",
        post_to_issue_url="https://github.com/owner/repo/issues/1",
        context_file=context_file,
    )
    errors = module.validate_request(request, request_path=context_file)
    assert any("github_research forbids post_to_issue_url" in e for e in errors), (
        f"Expected github_research forbids post_to_issue_url in errors; got: {errors}"
    )


# ---------------------------------------------------------------------------
# local_asset_research + post_to_issue_url -> rejected
# ---------------------------------------------------------------------------


def test_local_asset_research_with_post_to_issue_url_is_rejected(tmp_path, monkeypatch):
    """GIVEN a local_asset_research request with post_to_issue_url
    WHEN validate_request is called
    THEN a validation error is returned (local_asset_research forbids post_to_issue_url)."""
    module = load_run_gemini_headless()
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)

    context_file = tmp_path / "context.md"
    context_file.write_text("context", encoding="utf-8")

    request = _make_request(
        profile="local_asset_research",
        post_to_issue_url="https://github.com/owner/repo/issues/1",
        context_file=context_file,
    )
    errors = module.validate_request(request, request_path=context_file)
    assert any("local_asset_research forbids post_to_issue_url" in e for e in errors), (
        f"Expected local_asset_research forbids post_to_issue_url in errors; got: {errors}"
    )


# ---------------------------------------------------------------------------
# proposal_only + post_to_issue_url -> rejected
# ---------------------------------------------------------------------------


def test_proposal_only_with_post_to_issue_url_is_rejected(tmp_path, monkeypatch):
    """GIVEN a proposal_only request with post_to_issue_url
    WHEN validate_request is called
    THEN a validation error is returned."""
    module = load_run_gemini_headless()
    monkeypatch.chdir(tmp_path)

    context_file = tmp_path / "context.md"
    context_file.write_text("context", encoding="utf-8")

    request = {
        "schema": "delegation_request_v1",
        "objective": "Draft a proposal for the implementation plan",
        "instructions": [
            "Return a proposal text only; do not execute any commands.",
            "Provide an implementation_draft with the key steps.",
        ],
        "tool_profile": "proposal_only",
        "output_sections": ["implementation_draft"],
        "context_files": [str(context_file)],
        "post_to_issue_url": "https://github.com/owner/repo/issues/1",
        "timeout_sec": 300,
    }
    errors = module.validate_request(request, request_path=context_file)
    assert any("post_to_issue_url" in e for e in errors), (
        f"Expected post_to_issue_url error for proposal_only; got: {errors}"
    )


# ---------------------------------------------------------------------------
# no_tools + post_to_issue_url -> allowed from profile perspective
# ---------------------------------------------------------------------------


def test_no_tools_with_post_to_issue_url_is_allowed_by_profile(tmp_path, monkeypatch):
    """GIVEN a no_tools request with post_to_issue_url
    WHEN validate_request is called
    THEN no post_to_issue_url-specific validation error is returned for this profile."""
    module = load_run_gemini_headless()
    monkeypatch.chdir(tmp_path)

    context_file = tmp_path / "context.md"
    context_file.write_text("context", encoding="utf-8")

    request = _make_request(
        profile="no_tools",
        post_to_issue_url="https://github.com/owner/repo/issues/1",
        context_file=context_file,
    )
    errors = module.validate_request(request, request_path=context_file)
    # no_tools allows post_to_issue_url — no profile-specific error
    profile_errors = [e for e in errors if "post_to_issue_url" in e and "forbids" in e]
    assert profile_errors == [], (
        f"no_tools should not forbid post_to_issue_url; got profile errors: {profile_errors}"
    )


# ---------------------------------------------------------------------------
# grounded_research + post_to_issue_url -> allowed from profile perspective
# ---------------------------------------------------------------------------


def test_grounded_research_with_post_to_issue_url_is_allowed_by_profile(tmp_path, monkeypatch):
    """GIVEN a grounded_research request with post_to_issue_url
    WHEN validate_request is called
    THEN no post_to_issue_url-specific validation error is returned for this profile."""
    module = load_run_gemini_headless()
    monkeypatch.chdir(tmp_path)

    context_file = tmp_path / "context.md"
    context_file.write_text("context", encoding="utf-8")

    request = _make_request(
        profile="grounded_research",
        post_to_issue_url="https://github.com/owner/repo/issues/1",
        context_file=context_file,
    )
    request["timeout_sec"] = 300
    errors = module.validate_request(request, request_path=context_file)
    profile_errors = [e for e in errors if "post_to_issue_url" in e and "forbids" in e]
    assert profile_errors == [], (
        f"grounded_research should not forbid post_to_issue_url; got profile errors: {profile_errors}"
    )

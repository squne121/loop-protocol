"""Tests for post_to_issue_url boundary enforcement in validate_request.

AC15: github_research + post_to_issue_url is rejected by validation.
B6: post_to_issue_url URL format validation.

Coverage:
  - github_research + post_to_issue_url -> validation error
  - local_asset_research + post_to_issue_url -> validation error
  - proposal_only + post_to_issue_url -> validation error
  - no_tools + post_to_issue_url -> allowed (no validation error from profile rules)
  - grounded_research + post_to_issue_url -> allowed (no validation error from profile rules)
  B6: URL format validation:
    - valid: https://github.com/<owner>/<repo>/issues/<number>
    - invalid: pulls/<number> path
    - invalid: non-github.com host
    - invalid: http:// (not https)
    - invalid: extra path segments after issue number
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


# ---------------------------------------------------------------------------
# B6: URL format validation for post_to_issue_url
# ---------------------------------------------------------------------------


def test_validate_post_to_issue_url_valid_format(tmp_path, monkeypatch):
    """GIVEN a correctly formatted https://github.com/.../issues/<n> URL
    WHEN _validate_post_to_issue_url is called
    THEN no errors are returned."""
    module = load_run_gemini_headless()
    valid_urls = [
        "https://github.com/owner/repo/issues/1",
        "https://github.com/squne121/loop-protocol/issues/313",
        "https://github.com/my-org/my-repo/issues/99999",
        "https://github.com/org.name/repo.name/issues/42",
    ]
    for url in valid_urls:
        errors = module._validate_post_to_issue_url(url)
        assert errors == [], f"Expected no errors for valid URL {url!r}, got: {errors}"


def test_validate_post_to_issue_url_rejects_pulls(tmp_path):
    """GIVEN a URL using /pulls/ instead of /issues/
    WHEN _validate_post_to_issue_url is called
    THEN an error is returned (pulls/<number> is explicitly forbidden)."""
    module = load_run_gemini_headless()
    url = "https://github.com/owner/repo/pulls/321"
    errors = module._validate_post_to_issue_url(url)
    assert errors, f"Expected error for pulls URL: {url!r}"
    assert any("issues" in e or "pulls" in e or "must match" in e for e in errors), (
        f"Expected descriptive error about pulls being forbidden; got: {errors}"
    )


def test_validate_post_to_issue_url_rejects_non_github_host(tmp_path):
    """GIVEN a URL with a non-github.com host
    WHEN _validate_post_to_issue_url is called
    THEN an error is returned (host spoof prevention)."""
    module = load_run_gemini_headless()
    spoof_urls = [
        "https://github.example.com/owner/repo/issues/1",
        "https://notgithub.com/owner/repo/issues/1",
        "https://github.com.evil.com/owner/repo/issues/1",
    ]
    for url in spoof_urls:
        errors = module._validate_post_to_issue_url(url)
        assert errors, f"Expected error for non-github.com host URL: {url!r}"


def test_validate_post_to_issue_url_rejects_http(tmp_path):
    """GIVEN a URL using http:// instead of https://
    WHEN _validate_post_to_issue_url is called
    THEN an error is returned."""
    module = load_run_gemini_headless()
    url = "http://github.com/owner/repo/issues/1"
    errors = module._validate_post_to_issue_url(url)
    assert errors, f"Expected error for http:// URL: {url!r}"


def test_validate_post_to_issue_url_rejects_extra_path_segments(tmp_path):
    """GIVEN a URL with extra path segments after the issue number
    WHEN _validate_post_to_issue_url is called
    THEN an error is returned."""
    module = load_run_gemini_headless()
    url = "https://github.com/owner/repo/issues/1/comments"
    errors = module._validate_post_to_issue_url(url)
    assert errors, f"Expected error for URL with extra path segments: {url!r}"


def test_validate_post_to_issue_url_via_validate_request_no_tools(tmp_path, monkeypatch):
    """GIVEN a no_tools request with an invalid post_to_issue_url (pulls URL)
    WHEN validate_request is called
    THEN a URL format validation error is returned (B6 enforced globally)."""
    module = load_run_gemini_headless()
    monkeypatch.chdir(tmp_path)

    context_file = tmp_path / "context.md"
    context_file.write_text("context", encoding="utf-8")

    request = _make_request(
        profile="no_tools",
        post_to_issue_url="https://github.com/owner/repo/pulls/321",
        context_file=context_file,
    )
    errors = module.validate_request(request, request_path=context_file)
    url_errors = [e for e in errors if "must match" in e or "pulls" in e or "issues" in e]
    assert url_errors, (
        f"Expected URL format error for pulls URL in no_tools; got errors: {errors}"
    )


def test_validate_post_to_issue_url_via_validate_request_grounded_research_valid(tmp_path, monkeypatch):
    """GIVEN a grounded_research request with a valid post_to_issue_url
    WHEN validate_request is called
    THEN no URL format error is returned."""
    module = load_run_gemini_headless()
    monkeypatch.chdir(tmp_path)

    context_file = tmp_path / "context.md"
    context_file.write_text("context", encoding="utf-8")

    request = _make_request(
        profile="grounded_research",
        post_to_issue_url="https://github.com/owner/repo/issues/42",
        context_file=context_file,
    )
    request["timeout_sec"] = 300
    errors = module.validate_request(request, request_path=context_file)
    url_format_errors = [
        e for e in errors
        if "must match" in e or "github.com" in e.lower() and "post_to_issue_url" in e
    ]
    assert url_format_errors == [], (
        f"Expected no URL format errors for valid post_to_issue_url; got: {url_format_errors}"
    )

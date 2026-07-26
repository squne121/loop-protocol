"""Tests for the Issue #1692 (human decision, 2026-07-26, PR #1798 comment)
AGY-first provider="auto" runtime policy.

AC coverage:
  AC3: run_gemini_headless.PROVIDER_AUTO_RUNTIME_ORDER == ("agy", "gemini").
  AC4: an AGY candidate that succeeds prevents any Gemini provider attempt
       (provider_attempts never contains a provider="gemini" entry).
  AC5: an AGY candidate that fails with a fallback-safe (quota/capacity)
       failure_class triggers a second, Gemini attempt.
  AC6: an AGY candidate that fails with a request-validation / permission /
       evidence failure does NOT fall back to Gemini.
  AC10: validate_request_for_provider() fails closed, at build/validate-only
        time, for provider="auto" + tool_profile not in
        PROVIDER_AUTO_ELIGIBLE_PROFILES (grounded_research /
        local_asset_research / github_research).
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


def _result(ok: bool, failure_class: str | None = None) -> dict:
    return {
        "schema": "delegation_result/v1",
        "ok": ok,
        "failure_class": failure_class,
        "model_downgrades": [],
        "response_text": "hi" if ok else None,
    }


BASE_AUTO_REQUEST = {
    "schema": "delegation_request_v1",
    "objective": "Summarize the build failure from context",
    "instructions": [
        "Identify the root cause.",
        "List actionable recommendations.",
    ],
    "tool_profile": "no_tools",
    "output_sections": ["Summary"],
    "context_files": [],
}


# ---------------------------------------------------------------------------
# AC3
# ---------------------------------------------------------------------------


def test_provider_auto_runtime_order_is_agy_then_gemini() -> None:
    """GIVEN run_gemini_headless.PROVIDER_AUTO_RUNTIME_ORDER
    WHEN inspected
    THEN agy is the first candidate and gemini is the second (Issue #1692
    human decision: Antigravity CLI is now the first provider)."""
    assert rgh.PROVIDER_AUTO_RUNTIME_ORDER == ("agy", "gemini")


# ---------------------------------------------------------------------------
# AC4
# ---------------------------------------------------------------------------


def test_agy_success_prevents_gemini_invocation() -> None:
    """GIVEN a provider="auto" request
    WHEN the AGY candidate (attempted first) succeeds
    THEN no gemini attempt is made at all: provider_attempts contains
    exactly one entry (provider="agy") and no provider="gemini" entry."""
    calls: list[str] = []

    def fake_run_delegation(request, request_path=None, _routing=None):
        calls.append(request["provider"])
        return _result(True)

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(dict(BASE_AUTO_REQUEST))

    assert calls == ["agy"]
    assert result["ok"] is True
    assert result["selected_provider"] == "agy"
    assert [attempt["provider"] for attempt in result["provider_attempts"]] == ["agy"]
    assert all(attempt["provider"] != "gemini" for attempt in result["provider_attempts"])


# ---------------------------------------------------------------------------
# AC5
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_class",
    sorted(rgh.PROVIDER_AUTO_RETRYABLE_FAILURE_CLASSES["agy"]),
)
def test_agy_fallback_safe_failure_triggers_gemini_second_attempt(failure_class: str) -> None:
    """GIVEN a provider="auto" request
    WHEN the AGY candidate fails with a fallback-safe (quota/capacity)
    failure_class (a member of PROVIDER_AUTO_RETRYABLE_FAILURE_CLASSES["agy"])
    THEN a second attempt is made against gemini, recorded in
    provider_attempts[1]."""
    calls: list[str] = []

    def fake_run_delegation(request, request_path=None, _routing=None):
        calls.append(request["provider"])
        if request["provider"] == "agy":
            return _result(False, failure_class=failure_class)
        return _result(True)

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(dict(BASE_AUTO_REQUEST))

    assert calls == ["agy", "gemini"]
    assert result["ok"] is True
    assert result["selected_provider"] == "gemini"
    assert len(result["provider_attempts"]) == 2
    assert result["provider_attempts"][0]["provider"] == "agy"
    assert result["provider_attempts"][0]["failure_class"] == failure_class
    assert result["provider_attempts"][0]["retryable_for_provider_fallback"] is True
    assert result["provider_attempts"][1]["provider"] == "gemini"


# ---------------------------------------------------------------------------
# AC6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_class",
    [
        "agy_empty_prompt",
        "agy_auth_required",
        "agy_permission_denied",
        "unsupported_provider_profile",
        "local_asset_research_targeted_evidence_unmet",
    ],
)
def test_agy_non_retryable_failure_stops_without_gemini_fallback(failure_class: str) -> None:
    """GIVEN a provider="auto" request
    WHEN the AGY candidate fails with a request-validation / permission /
    evidence failure_class (NOT a member of
    PROVIDER_AUTO_RETRYABLE_FAILURE_CLASSES["agy"])
    THEN no gemini attempt is made -- fallback does not paper over AGY
    validation/permission/evidence failures."""
    calls: list[str] = []

    def fake_run_delegation(request, request_path=None, _routing=None):
        calls.append(request["provider"])
        return _result(False, failure_class=failure_class)

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(dict(BASE_AUTO_REQUEST))

    assert calls == ["agy"]
    assert result["ok"] is False
    assert result["selected_provider"] == "agy"
    assert len(result["provider_attempts"]) == 1
    assert result["provider_attempts"][0]["failure_class"] == failure_class
    assert result["provider_attempts"][0]["retryable_for_provider_fallback"] is False
    assert result["fallback_reason"] == f"stop_if:non_retryable_failure_class:{failure_class}"


# ---------------------------------------------------------------------------
# AC10
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_profile",
    ["grounded_research", "local_asset_research", "github_research"],
)
def test_auto_unsupported_profile_fails_closed_before_dispatch(tool_profile: str) -> None:
    """GIVEN a provider="auto" request whose tool_profile is NOT one of
    PROVIDER_AUTO_ELIGIBLE_PROFILES ({"no_tools", "proposal_only"})
    WHEN validate_request_for_provider() is called (build/validate-only
    stage, BEFORE provider_auto_dispatch() ever runs)
    THEN it returns a non-empty, fail-closed error list mentioning
    provider_profile_unsupported -- the same failure_class the runtime
    dispatcher itself would use, but caught earlier."""
    request = dict(BASE_AUTO_REQUEST, provider="auto", tool_profile=tool_profile)
    errors = rgh.validate_request_for_provider(request)
    assert errors, f"expected fail-closed errors for provider=auto + tool_profile={tool_profile!r}"
    assert any("provider_profile_unsupported" in e for e in errors)

    # Never reaches provider_auto_dispatch()'s own runtime attempt loop --
    # this is a static, no-side-effect claim: run_delegation is never called
    # when validate_request_for_provider() already rejected the request.
    with patch.object(rgh, "run_delegation") as mock_run_delegation:
        # validate_request_for_provider() itself never calls run_delegation;
        # a caller (e.g. build_request.py) is expected to check errors
        # before ever reaching dispatch.
        rgh.validate_request_for_provider(request)
        mock_run_delegation.assert_not_called()


@pytest.mark.parametrize("tool_profile", ["no_tools", "proposal_only"])
def test_auto_eligible_profile_passes_validate_request_for_provider(tool_profile: str) -> None:
    """GIVEN a provider="auto" request whose tool_profile IS in
    PROVIDER_AUTO_ELIGIBLE_PROFILES
    WHEN validate_request_for_provider() is called
    THEN it does NOT fail closed on provider_profile_unsupported (it may
    still validate the rest of the structured Gemini-shaped contract)."""
    request = dict(BASE_AUTO_REQUEST, provider="auto", tool_profile=tool_profile)
    errors = rgh.validate_request_for_provider(request)
    assert not any("provider_profile_unsupported" in e for e in errors), errors

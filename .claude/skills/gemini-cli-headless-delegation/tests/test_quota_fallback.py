"""Tests for provider_auto_dispatch() / provider=auto fallback policy
(Issue #1270: provider_auto_policy_v1).

Covers AC0/AC2/AC3/AC4/AC5/AC6/AC7:
  - provider="auto" reaching runtime dispatch without unknown_provider
  - Gemini model_chain_exhausted -> AGY fallback
  - non-retryable failures (auth/permission/unsupported profile/post_to_issue_url)
    stop fallback immediately
  - retry_budget YAML validation (type / unknown key fail-closed)
  - AGY quota/capacity/auth/permission stdout+stderr classification
  - model_chain_exhausted result carries a top-level failure_class
  - AC7 (#1274): _normalize_agy_result warnings[0] leading token always
    matches failure_class
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (hermetic, mirrors test_agy_provider.py convention)
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=["agy"], returncode=returncode, stdout=stdout, stderr=stderr)


def _result(ok: bool, failure_class: str | None = None, model_downgrades=None) -> dict:
    return {
        "schema": "delegation_result/v1",
        "ok": ok,
        "failure_class": failure_class,
        "model_downgrades": model_downgrades or [],
        "response_text": "hi" if ok else None,
    }


BASE_REQUEST = {"tool_profile": "no_tools", "prompt": "hello"}


# ---------------------------------------------------------------------------
# provider="auto" runtime wiring (AC3)
# ---------------------------------------------------------------------------


def test_provider_auto_is_supported_not_unknown_provider() -> None:
    """provider="auto" must not fall into the unknown_provider fail path.

    run_delegation() itself must special-case provider="auto" and delegate to
    provider_auto_dispatch() BEFORE the SUPPORTED_PROVIDERS unknown-provider
    check (which only knows about "gemini" / "agy" candidates, never "auto").
    """
    real_run_delegation = rgh.run_delegation

    def fake_run_delegation(request, request_path=None, _routing=None):
        if request.get("provider") == "auto":
            return real_run_delegation(request, request_path=request_path, _routing=_routing)
        return _result(False, failure_class="gh_auth_required")

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.run_delegation(dict(BASE_REQUEST, provider="auto"))

    assert result["failure_class"] != "unknown_provider"
    assert result.get("selected_provider") == "gemini"


# ---------------------------------------------------------------------------
# Gemini quota -> AGY fallback (AC3/AC4)
# ---------------------------------------------------------------------------


def test_gemini_model_chain_exhausted_falls_back_to_agy_success() -> None:
    calls: list[str] = []

    def fake_run_delegation(request, request_path=None, _routing=None):
        calls.append(request["provider"])
        if request["provider"] == "gemini":
            return _result(False, failure_class="model_chain_exhausted")
        return _result(True)

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(BASE_REQUEST)

    assert calls == ["gemini", "agy"]
    assert result["ok"] is True
    assert result["selected_provider"] == "agy"
    assert len(result["provider_attempts"]) == 2
    assert result["provider_attempts"][0]["provider"] == "gemini"
    assert result["provider_attempts"][0]["failure_class"] == "model_chain_exhausted"
    assert result["fallback_policy_version"] == "v1"
    assert "fallback_reason" in result
    assert "attempts_by_model" in result


def test_gemini_success_on_first_try_no_fallback() -> None:
    def fake_run_delegation(request, request_path=None, _routing=None):
        assert request["provider"] == "gemini"
        return _result(True)

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(BASE_REQUEST)

    assert result["ok"] is True
    assert result["selected_provider"] == "gemini"
    assert result["fallback_reason"] is None
    assert len(result["provider_attempts"]) == 1


def test_both_providers_exhausted_reports_provider_fallback_exhausted() -> None:
    def fake_run_delegation(request, request_path=None, _routing=None):
        if request["provider"] == "gemini":
            return _result(False, failure_class="model_chain_exhausted")
        return _result(False, failure_class="agy_rate_limited")

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(BASE_REQUEST)

    assert result["ok"] is False
    assert result["selected_provider"] == "agy"
    assert result["fallback_reason"] == "provider_fallback_exhausted"


# ---------------------------------------------------------------------------
# Non-retryable stop conditions (AC5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_class",
    ["gh_auth_required", "request_schema_invalid", "github_research_command_denied", None],
)
def test_non_retryable_gemini_failure_does_not_fall_back(failure_class: str | None) -> None:
    calls: list[str] = []

    def fake_run_delegation(request, request_path=None, _routing=None):
        calls.append(request["provider"])
        return _result(False, failure_class=failure_class)

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(BASE_REQUEST)

    assert calls == ["gemini"]
    assert result["selected_provider"] == "gemini"


def test_post_to_issue_url_stops_fallback_even_on_failure() -> None:
    calls: list[str] = []
    request = dict(BASE_REQUEST, post_to_issue_url="https://github.com/o/r/issues/1")

    def fake_run_delegation(req, request_path=None, _routing=None):
        calls.append(req["provider"])
        return _result(False, failure_class="model_chain_exhausted")

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(request)

    assert calls == ["gemini"]
    assert result["fallback_reason"] == "stop_if:request_has_post_to_issue_url"


def test_unsupported_profile_makes_no_provider_attempt() -> None:
    calls: list[str] = []
    request = {"tool_profile": "grounded_research", "prompt": "hi"}

    def fake_run_delegation(req, request_path=None, _routing=None):
        calls.append(req["provider"])
        return _result(True)

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(request)

    assert calls == []
    assert result["failure_class"] == "provider_profile_unsupported"
    assert result["provider_attempts"] == []


# ---------------------------------------------------------------------------
# AGY quota/capacity/auth/permission classification (AC1/AC6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr_text", "expected_class"),
    [
        ("HTTP 429: RESOURCE_EXHAUSTED", "agy_rate_limited"),
        ("MODEL_CAPACITY_EXHAUSTED: model overloaded", "agy_capacity_exhausted"),
        ("Individual quota reached for grounding tool", "agy_web_grounding_quota_exhausted"),
        ("please sign in to continue", "agy_auth_required"),
        ("permission denied: 403 forbidden", "agy_permission_denied"),
        ("some unrelated agy error", "agy_exit_nonzero"),
    ],
)
def test_classify_agy_failure_stderr(stderr_text: str, expected_class: str) -> None:
    assert rgh._classify_agy_failure(1, "", stderr_text) == expected_class


def test_classify_agy_failure_checks_stdout_too() -> None:
    """AC1/AC6: classifier must inspect stdout, not only stderr."""
    assert rgh._classify_agy_failure(1, "RESOURCE_EXHAUSTED in response body", "") == "agy_rate_limited"


def test_agy_quota_stderr_wired_into_normalize_agy_result() -> None:
    """AC6: _normalize_agy_result no longer collapses quota stderr into the
    generic agy_exit_nonzero class."""
    completed = _make_completed(1, stdout="", stderr="HTTP 429: RESOURCE_EXHAUSTED")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["failure_class"] == "agy_rate_limited"
    assert result["ok"] is False


def test_agy_generic_nonzero_exit_regression_unchanged() -> None:
    """Regression: a plain non-quota AGY failure remains agy_exit_nonzero."""
    completed = _make_completed(1, stdout="", stderr="agy error message")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["failure_class"] == "agy_exit_nonzero"


# ---------------------------------------------------------------------------
# AC7 (#1274): warnings[0] leading token consistency
# ---------------------------------------------------------------------------


def test_warning_failure_class_leading_token_consistency() -> None:
    """AC7/#1274: warnings[0] must start with the same token as failure_class
    in both the CI and non-CI empty-stdout branches, and in the generic
    non-zero-exit branch."""
    with patch.dict(os.environ, {"CI": ""}, clear=False):
        completed = _make_completed(0, stdout="")
        result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["failure_class"] == "agy_empty_stdout"
    assert result["warnings"][0].startswith(result["failure_class"])

    with patch.dict(os.environ, {"CI": "1"}, clear=False):
        completed = _make_completed(0, stdout="")
        result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["failure_class"] == "agy_output_missing"
    assert result["warnings"][0].startswith(result["failure_class"])

    completed = _make_completed(1, stdout="", stderr="agy error message")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["warnings"][0].startswith(result["failure_class"])

    completed = _make_completed(1, stdout="", stderr="HTTP 429: RESOURCE_EXHAUSTED")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["warnings"][0].startswith(result["failure_class"])


# ---------------------------------------------------------------------------
# retry_budget YAML validation (AC2)
# ---------------------------------------------------------------------------


def _write_yaml(text: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
        return Path(f.name)


def test_retry_budget_valid_config_loads() -> None:
    path = _write_yaml(
        "default_chain:\n"
        "  - gemini-3-flash-preview\n"
        "providers:\n"
        "  gemini:\n"
        "    retry_budget:\n"
        "      same_model_attempts: 3\n"
        "      initial_backoff_seconds: 1\n"
        "      max_backoff_seconds: 4\n"
        "      retryable_failure_classes:\n"
        "        - quota_or_rate_limited\n"
    )
    routing = rgh.load_model_routing(path)
    budget = rgh.get_retry_budget(routing, "gemini")
    assert budget["same_model_attempts"] == 3
    assert budget["retryable_failure_classes"] == ["quota_or_rate_limited"]


def test_retry_budget_unknown_key_fails_closed() -> None:
    path = _write_yaml(
        "default_chain:\n"
        "  - gemini-3-flash-preview\n"
        "providers:\n"
        "  gemini:\n"
        "    retry_budget:\n"
        "      unknown_field: 1\n"
    )
    with pytest.raises(ValueError, match="unknown key"):
        rgh.load_model_routing(path)


def test_retry_budget_wrong_type_fails_closed() -> None:
    path = _write_yaml(
        "default_chain:\n"
        "  - gemini-3-flash-preview\n"
        "providers:\n"
        "  gemini:\n"
        "    retry_budget:\n"
        "      same_model_attempts: not_a_number\n"
    )
    with pytest.raises(ValueError, match="retry_budget"):
        rgh.load_model_routing(path)


def test_retry_budget_missing_providers_key_uses_defaults() -> None:
    path = _write_yaml("default_chain:\n  - gemini-3-flash-preview\n")
    routing = rgh.load_model_routing(path)
    budget = rgh.get_retry_budget(routing, "gemini")
    assert budget == rgh.DEFAULT_RETRY_BUDGET


def test_model_routing_default_config_file_has_provider_auto_policy_v1() -> None:
    """AC0: config/model_routing.yaml documents provider_auto_policy_v1."""
    routing = rgh.load_model_routing()
    assert "provider_auto_policy_v1" in routing
    policy = routing["provider_auto_policy_v1"]
    assert policy["runtime_order"] == ["gemini", "agy"]
    assert policy["setup_check_order"] == ["agy", "gemini"]
    assert set(policy["eligible_profiles"]) == {"no_tools", "proposal_only"}


def test_model_routing_default_config_file_has_retry_budgets_for_both_providers() -> None:
    """AC2: config/model_routing.yaml declares retry_budget for gemini and agy."""
    routing = rgh.load_model_routing()
    assert set(routing.get("providers", {})) >= {"gemini", "agy"}
    gemini_budget = rgh.get_retry_budget(routing, "gemini")
    agy_budget = rgh.get_retry_budget(routing, "agy")
    assert gemini_budget["same_model_attempts"] >= 1
    assert agy_budget["same_model_attempts"] >= 1


# ---------------------------------------------------------------------------
# model_chain_exhausted top-level failure_class (AC4, review comment #3)
# ---------------------------------------------------------------------------


def _minimal_delegation_request(tmp_path: Path, **overrides) -> dict:
    """Mirrors test_model_routing.py's _make_minimal_request (kept local to
    avoid a cross-test-file import dependency)."""
    ctx = tmp_path / "ctx.md"
    ctx.write_text("context", encoding="utf-8")
    base: dict = {
        "schema": "delegation_request_v1",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": [str(ctx)],
    }
    base.update(overrides)
    return base


def test_model_chain_exhausted_sets_top_level_failure_class(tmp_path: Path) -> None:
    """AC4: model_chain_exhausted must be surfaced as a top-level
    failure_class, not only as reason_code (previously missing)."""
    request = _minimal_delegation_request(tmp_path)
    routing = {"default_chain": ["model-a", "model-b"], "roles": {}}

    def fake_run_gemini(command, timeout_sec, prompt=None, cwd=None):
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="HTTP 429: RESOURCE_EXHAUSTED"
        )

    with patch.object(rgh, "_run_gemini", side_effect=fake_run_gemini):
        result = rgh.run_delegation(request, _routing=routing)

    assert result["ok"] is False
    assert result.get("reason_code") == "model_chain_exhausted"
    assert result.get("failure_class") == "model_chain_exhausted"

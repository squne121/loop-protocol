"""Tests for model routing: chain resolution, quota-downgrade, config override."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# resolve_model_chain tests
# ---------------------------------------------------------------------------


class TestResolveModelChain:
    """Tests for resolve_model_chain: chain resolution logic."""

    def test_explicit_model_returns_single_chain_no_downgrade(self):
        m = load_module()
        request = {"model": "gemini-custom-v9"}
        chain, err = m.resolve_model_chain(request)
        assert err is None
        assert chain == ["gemini-custom-v9"]

    def test_known_role_returns_role_chain(self):
        m = load_module()
        request = {"role": "web_research"}
        chain, err = m.resolve_model_chain(request, m.DEFAULT_MODEL_ROUTING)
        assert err is None
        assert chain == m.DEFAULT_MODEL_ROUTING["roles"]["web_research"]["model_chain"]

    def test_known_role_implementation(self):
        m = load_module()
        request = {"role": "implementation"}
        chain, err = m.resolve_model_chain(request, m.DEFAULT_MODEL_ROUTING)
        assert err is None
        assert chain[0] == "gemini-3-pro-preview"
        assert len(chain) >= 2

    def test_unknown_role_fail_closed(self):
        m = load_module()
        request = {"role": "nonexistent_role_xyz"}
        chain, err = m.resolve_model_chain(request, m.DEFAULT_MODEL_ROUTING)
        assert chain == []
        assert err is not None
        assert "unknown_role" in err
        assert "nonexistent_role_xyz" in err

    def test_no_model_no_role_uses_default_chain(self):
        m = load_module()
        request = {}
        chain, err = m.resolve_model_chain(request, m.DEFAULT_MODEL_ROUTING)
        assert err is None
        assert chain == m.DEFAULT_MODEL_ROUTING["default_chain"]

    def test_explicit_model_overrides_role(self):
        """When both model and role are set, explicit model wins (no downgrade)."""
        m = load_module()
        request = {"model": "gemini-explicit", "role": "implementation"}
        chain, err = m.resolve_model_chain(request, m.DEFAULT_MODEL_ROUTING)
        assert err is None
        assert chain == ["gemini-explicit"]


# ---------------------------------------------------------------------------
# load_model_routing tests
# ---------------------------------------------------------------------------


class TestLoadModelRouting:
    """Tests for config file loading and merging."""

    def test_no_config_file_returns_defaults(self, tmp_path):
        m = load_module()
        nonexistent = tmp_path / "does_not_exist.yaml"
        routing = m.load_model_routing(config_path=nonexistent)
        assert routing["default_chain"] == m.DEFAULT_MODEL_ROUTING["default_chain"]
        assert set(routing["roles"].keys()) == set(m.DEFAULT_MODEL_ROUTING["roles"].keys())

    def test_config_override_merges_into_defaults(self, tmp_path):
        m = load_module()
        yaml_content = """
default_chain:
  - override-model-a
  - override-model-b
roles:
  web_research:
    model_chain:
      - override-model-a
"""
        config_file = tmp_path / "model_routing.yaml"
        config_file.write_text(yaml_content, encoding="utf-8")

        routing = m.load_model_routing(config_path=config_file)

        # override applied
        assert routing["default_chain"] == ["override-model-a", "override-model-b"]
        assert routing["roles"]["web_research"]["model_chain"] == ["override-model-a"]
        # non-overridden roles still present from defaults
        assert "implementation" in routing["roles"]

    def test_invalid_yaml_raises_value_error(self, tmp_path):
        m = load_module()
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("key: [unclosed", encoding="utf-8")

        with pytest.raises(ValueError, match="invalid YAML"):
            m.load_model_routing(config_path=config_file)

    def test_empty_default_chain_raises_value_error(self, tmp_path):
        m = load_module()
        yaml_content = "default_chain: []\n"
        config_file = tmp_path / "model_routing.yaml"
        config_file.write_text(yaml_content, encoding="utf-8")

        with pytest.raises(ValueError, match="non-empty list"):
            m.load_model_routing(config_path=config_file)

    def test_empty_role_chain_raises_value_error(self, tmp_path):
        m = load_module()
        yaml_content = """
roles:
  web_research:
    model_chain: []
"""
        config_file = tmp_path / "model_routing.yaml"
        config_file.write_text(yaml_content, encoding="utf-8")

        with pytest.raises(ValueError, match="non-empty list"):
            m.load_model_routing(config_path=config_file)

    def test_non_mapping_yaml_raises_value_error(self, tmp_path):
        m = load_module()
        config_file = tmp_path / "model_routing.yaml"
        config_file.write_text("- item1\n- item2\n", encoding="utf-8")

        with pytest.raises(ValueError, match="expected a YAML mapping"):
            m.load_model_routing(config_path=config_file)

    def test_empty_yaml_file_returns_defaults(self, tmp_path):
        """Empty YAML file should not raise; defaults are returned as-is."""
        m = load_module()
        config_file = tmp_path / "model_routing.yaml"
        config_file.write_text("", encoding="utf-8")

        routing = m.load_model_routing(config_path=config_file)
        assert routing["default_chain"] == m.DEFAULT_MODEL_ROUTING["default_chain"]


# ---------------------------------------------------------------------------
# run_delegation: model downgrade tests
# ---------------------------------------------------------------------------


def _make_minimal_request(tmp_path: Path, **overrides) -> dict[str, Any]:
    """Build a minimal valid delegation_request_v1 for testing."""
    ctx = tmp_path / "ctx.md"
    ctx.write_text("context", encoding="utf-8")
    base: dict[str, Any] = {
        "schema": "delegation_request_v1",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": [str(ctx)],
    }
    base.update(overrides)
    return base


def _quota_response(model: str) -> subprocess.CompletedProcess:
    """Simulate a quota-exhausted (429) subprocess response."""
    return subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr=f"HTTP 429: RESOURCE_EXHAUSTED quota exceeded for model {model}",
    )


def _success_response(response_text: str = "ok", model: str = "gemini-mock") -> subprocess.CompletedProcess:
    """Simulate a successful subprocess response with a JSON envelope."""
    payload = json.dumps({"response": response_text, "stats": {"models": {model: {}}}})
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=payload,
        stderr="",
    )


class TestRunDelegationModelDowngrade:
    """Integration tests for quota-triggered model downgrade in run_delegation."""

    def test_quota_on_first_model_downgrades_to_second_succeeds(self, tmp_path):
        m = load_module()
        request = _make_minimal_request(tmp_path)
        routing = {
            "default_chain": ["model-a", "model-b"],
            "roles": {},
        }
        call_count = 0

        def fake_run_gemini(command, timeout_sec, prompt=None, cwd=None):
            nonlocal call_count
            call_count += 1
            model_flag_index = command.index("--model") + 1
            model = command[model_flag_index]
            if model == "model-a":
                return _quota_response("model-a")
            return _success_response("result from model-b", model="model-b")

        with patch.object(m, "_run_gemini", side_effect=fake_run_gemini):
            result = m.run_delegation(request, _routing=routing)

        assert result["ok"] is True
        assert result["actual_model"] == "model-b"
        assert len(result["model_downgrades"]) == 1
        assert result["model_downgrades"][0]["from"] == "model-a"
        assert result["model_downgrades"][0]["to"] == "model-b"
        assert result["model_downgrades"][0]["reason"] == "quota_model_downgrade"
        assert result["model_chain"] == ["model-a", "model-b"]

    def test_quota_on_all_models_chain_exhausted(self, tmp_path):
        m = load_module()
        request = _make_minimal_request(tmp_path)
        routing = {
            "default_chain": ["model-a", "model-b"],
            "roles": {},
        }

        def fake_run_gemini(command, timeout_sec, prompt=None, cwd=None):
            return _quota_response("quota-for-all")

        with patch.object(m, "_run_gemini", side_effect=fake_run_gemini):
            result = m.run_delegation(request, _routing=routing)

        assert result["ok"] is False
        assert result.get("reason_code") == "model_chain_exhausted"
        assert "model_chain_exhausted" in (result.get("failure_reason") or "")
        assert result["model_chain"] == ["model-a", "model-b"]

    def test_no_downgrade_on_explicit_model_with_quota(self, tmp_path):
        """When model is explicit, quota failure does NOT trigger chain downgrade."""
        m = load_module()
        request = _make_minimal_request(tmp_path, model="explicit-model")
        routing = {
            "default_chain": ["default-a", "default-b"],
            "roles": {},
        }

        def fake_run_gemini(command, timeout_sec, prompt=None, cwd=None):
            return _quota_response("explicit-model")

        with patch.object(m, "_run_gemini", side_effect=fake_run_gemini):
            result = m.run_delegation(request, _routing=routing)

        assert result["ok"] is False
        # Single-model chain: no downgrades
        assert result["model_downgrades"] == []
        assert result["model_chain"] == ["explicit-model"]

    def test_result_contains_model_downgrades_field(self, tmp_path):
        """result always contains model_downgrades (even when empty)."""
        m = load_module()
        request = _make_minimal_request(tmp_path)
        routing = {
            "default_chain": ["model-only"],
            "roles": {},
        }

        def fake_run_gemini(command, timeout_sec, prompt=None, cwd=None):
            return _success_response("all good")

        with patch.object(m, "_run_gemini", side_effect=fake_run_gemini):
            result = m.run_delegation(request, _routing=routing)

        assert "model_downgrades" in result
        assert "model_chain" in result
        assert "actual_model" in result

    def test_unknown_role_fail_closed(self, tmp_path):
        m = load_module()
        request = _make_minimal_request(tmp_path, role="not_a_real_role")
        routing = m.DEFAULT_MODEL_ROUTING

        result = m.run_delegation(request, _routing=routing)

        assert result["ok"] is False
        assert result.get("reason_code") == "unknown_role"
        assert "unknown_role" in (result.get("failure_reason") or "")

    def test_quota_retry_within_same_model_succeeds_no_downgrade(self, tmp_path):
        """attempt 0 で quota、attempt 1（同一 model）で成功 → model_downgrades == [] かつ ok is True."""
        m = load_module()
        request = _make_minimal_request(tmp_path)
        routing = {
            "default_chain": ["model-only"],
            "roles": {},
        }
        call_count = 0

        def fake_run_gemini(command, timeout_sec, prompt=None, cwd=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _quota_response("model-only")
            return _success_response("retry succeeded")

        with patch.object(m, "_run_gemini", side_effect=fake_run_gemini):
            result = m.run_delegation(request, _routing=routing)

        assert result["ok"] is True
        assert result["model_downgrades"] == []
        assert result["model_chain"] == ["model-only"]
        # only model-only was ever tried
        assert call_count >= 2  # at least one retry on same model

    def test_role_web_research_uses_role_chain(self, tmp_path):
        m = load_module()
        request = _make_minimal_request(tmp_path, role="web_research")
        routing = {
            "default_chain": ["default-x"],
            "roles": {
                "web_research": {"model_chain": ["role-flash", "role-fallback"]},
            },
        }
        used_models: list[str] = []

        def fake_run_gemini(command, timeout_sec, prompt=None, cwd=None):
            model_flag_index = command.index("--model") + 1
            used_models.append(command[model_flag_index])
            return _success_response("done")

        with patch.object(m, "_run_gemini", side_effect=fake_run_gemini):
            result = m.run_delegation(request, _routing=routing)

        assert result["ok"] is True
        assert used_models[0] == "role-flash"
        assert result["model_chain"] == ["role-flash", "role-fallback"]


# ---------------------------------------------------------------------------
# DEFAULT_MODEL alignment
# ---------------------------------------------------------------------------


class TestDefaultModelAlignment:
    def test_default_model_matches_default_chain_head(self):
        m = load_module()
        assert m.DEFAULT_MODEL == m.DEFAULT_MODEL_ROUTING["default_chain"][0], (
            "DEFAULT_MODEL must match DEFAULT_MODEL_ROUTING['default_chain'][0]"
        )

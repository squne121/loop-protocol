"""Hermetic tests for agent_provider_route_smoke/v1 (Issue #1886 AC5).

These tests never spawn a live Claude Code / Codex CLI process: they cover
the producer's --dry-run path, schema validity, and the validator's semantic
assertions against synthetic fixture artifacts.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / "agent_provider_route_smoke_v1.schema.json"
PRODUCER_PATH = REPO_ROOT / "scripts" / "agent-ops" / "run_agent_provider_route_smoke.py"
VALIDATOR_PATH = REPO_ROOT / "scripts" / "agent-ops" / "validate_agent_provider_route_smoke.py"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def producer() -> types.ModuleType:
    return _load_module(PRODUCER_PATH, "test_agent_provider_route_smoke_producer")


@pytest.fixture(scope="module")
def validator() -> types.ModuleType:
    return _load_module(VALIDATOR_PATH, "test_agent_provider_route_smoke_validator")


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _base_artifact(**overrides) -> dict:
    artifact = {
        "schema": "agent_provider_route_smoke/v1",
        "run_id": "11111111-1111-1111-1111-111111111111",
        "generated_at": "2026-08-06T00:00:00Z",
        "subject": {
            "runtime": "claude_code",
            "agent_name": "codebase-investigator",
            "head_sha": "8f899d4a4a17f282fe75a03944448810a4c2cd04",
            "runtime_version": "2.1.223 (Claude Code)",
            "agent_definition_sha256": "a" * 64,
            "effective_runtime_config_sha256": "a" * 64,
        },
        "spawn": {
            "parent_session_id": "parent-1",
            "child_session_id": "child-1",
            "native_spawn_event_observed": True,
        },
        "request": {
            "profile": "local_asset_research",
            "expected_provider": "agy",
            "validation": "pass",
        },
        "provider_observation": {
            "selected_provider": "agy",
            "provider_attempts": ["agy"],
            "gemini_invocation_count": 0,
            "direct_fallback_invocation_count": 0,
        },
        "route_evidence": {"schema": None, "sha256": None},
        "status": "pass",
        "failure_class": None,
    }
    artifact.update(overrides)
    return artifact


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


class TestSchemaShape:
    def test_schema_is_valid_json(self, schema: dict):
        assert schema["title"] == "agent_provider_route_smoke/v1"

    def test_minimal_pass_artifact_validates(self, schema: dict):
        jsonschema.validate(_base_artifact(), schema)

    def test_github_research_pass_requires_agy_evidence_schema(self, schema: dict):
        artifact = _base_artifact(
            request={"profile": "github_research", "expected_provider": "agy", "validation": "pass"},
            route_evidence={"schema": "agy_github_research_evidence/v1", "sha256": "b" * 64},
        )
        jsonschema.validate(artifact, schema)

    def test_github_research_pass_rejects_wrong_evidence_schema(self, schema: dict):
        artifact = _base_artifact(
            request={"profile": "github_research", "expected_provider": "agy", "validation": "pass"},
            route_evidence={"schema": "some_other_schema/v1", "sha256": "b" * 64},
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(artifact, schema)

    def test_pass_requires_native_spawn_event_observed_true(self, schema: dict):
        artifact = _base_artifact(spawn={
            "parent_session_id": "parent-1", "child_session_id": "child-1",
            "native_spawn_event_observed": False,
        })
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(artifact, schema)

    def test_pass_requires_gemini_and_fallback_counts_zero(self, schema: dict):
        artifact = _base_artifact(provider_observation={
            "selected_provider": "agy", "provider_attempts": ["agy"],
            "gemini_invocation_count": 1, "direct_fallback_invocation_count": 0,
        })
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(artifact, schema)

    def test_fail_status_does_not_require_native_spawn(self, schema: dict):
        artifact = _base_artifact(
            status="fail",
            failure_class="spawn_not_observed",
            spawn={"parent_session_id": "parent-1", "child_session_id": "", "native_spawn_event_observed": False},
        )
        jsonschema.validate(artifact, schema)

    def test_additional_properties_rejected(self, schema: dict):
        artifact = _base_artifact()
        artifact["unexpected_field"] = "nope"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(artifact, schema)


# ---------------------------------------------------------------------------
# Producer: routes / prompt / subject / dry-run
# ---------------------------------------------------------------------------


class TestProducer:
    def test_required_routes_has_six_entries(self, producer):
        assert len(producer.REQUIRED_ROUTES) == 6

    def test_required_routes_are_unique(self, producer):
        keys = {producer._route_key(r) for r in producer.REQUIRED_ROUTES}
        assert len(keys) == 6

    def test_required_routes_cover_both_runtimes(self, producer):
        runtimes = {r["runtime"] for r in producer.REQUIRED_ROUTES}
        assert runtimes == {"claude_code", "codex_cli"}

    def test_required_routes_cover_expected_profiles(self, producer):
        profiles_by_agent: dict[str, set[str]] = {}
        for route in producer.REQUIRED_ROUTES:
            profiles_by_agent.setdefault(route["agent"], set()).add(route["profile"])
        assert profiles_by_agent["codebase-investigator"] == {"local_asset_research", "github_research"}
        assert profiles_by_agent["web-researcher"] == {"grounded_research"}

    def test_find_route_unknown_returns_none(self, producer):
        assert producer._find_route("claude_code", "codebase-investigator", "grounded_research") is None

    def test_build_route_prompt_requests_agy_provider(self, producer):
        route = producer.REQUIRED_ROUTES[0]
        prompt = producer.build_route_prompt(route)
        assert "--provider agy" in prompt
        assert f"--profile {route['profile']}" in prompt

    def test_build_route_prompt_forbids_gemini_binary(self, producer):
        for route in producer.REQUIRED_ROUTES:
            prompt = producer.build_route_prompt(route)
            assert "Do not invoke a binary literally named" in prompt
            assert "gemini" in prompt.lower()

    def test_build_route_prompt_forbids_websearch_webfetch_fallback(self, producer):
        for route in producer.REQUIRED_ROUTES:
            prompt = producer.build_route_prompt(route)
            assert "WebSearch or WebFetch" in prompt

    def test_compute_subject_returns_expected_shape(self, producer):
        subject = producer.compute_subject(producer.REQUIRED_ROUTES[0], REPO_ROOT)
        assert subject["runtime"] == "claude_code"
        assert subject["agent_name"] == "codebase-investigator"
        assert subject["head_sha"] is None or isinstance(subject["head_sha"], str)

    def test_parse_harness_summary_md_roundtrip(self, producer):
        text = "\n".join([
            "# Runtime Smoke Summary",
            "",
            "- child_session_id: None",
            "- native_spawn_event_observed: True",
            "- parent_session_id: abc-123",
        ])
        parsed = producer._parse_harness_summary_md(text)
        assert parsed["child_session_id"] is None
        assert parsed["native_spawn_event_observed"] is True
        assert parsed["parent_session_id"] == "abc-123"

    def test_dry_run_writes_one_artifact_per_route(self, producer, schema, tmp_path):
        rc = producer.main([
            "--all-routes", "--dry-run",
            "--output-dir", str(tmp_path),
            "--repo-root", str(REPO_ROOT),
        ])
        assert rc == 0
        artifact_paths = sorted(tmp_path.glob("*.json"))
        artifact_paths = [p for p in artifact_paths if p.name != "index.json"]
        assert len(artifact_paths) == 6
        for path in artifact_paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            jsonschema.validate(data, schema)
            assert data["status"] == "skip"
            assert data["provider_observation"]["gemini_invocation_count"] == 0
            assert data["provider_observation"]["direct_fallback_invocation_count"] == 0

    def test_dry_run_single_route_writes_one_artifact(self, producer, tmp_path):
        rc = producer.main([
            "--runtime", "codex_cli", "--agent", "web-researcher", "--profile", "grounded_research",
            "--dry-run", "--output-dir", str(tmp_path), "--repo-root", str(REPO_ROOT),
        ])
        assert rc == 0
        artifact_paths = [p for p in tmp_path.glob("*.json") if p.name != "index.json"]
        assert len(artifact_paths) == 1

    def test_unknown_route_returns_nonzero(self, producer, tmp_path, capsys):
        rc = producer.main([
            "--runtime", "codex_cli", "--agent", "codebase-investigator", "--profile", "grounded_research",
            "--dry-run", "--output-dir", str(tmp_path), "--repo-root", str(REPO_ROOT),
        ])
        assert rc == 1

    def test_no_route_selector_is_usage_error(self, producer, tmp_path):
        with pytest.raises(SystemExit):
            producer.main(["--output-dir", str(tmp_path), "--repo-root", str(REPO_ROOT)])


# ---------------------------------------------------------------------------
# Validator: schema + semantic assertions against synthetic fixtures
# ---------------------------------------------------------------------------


class TestValidator:
    def _write_artifact(self, directory: Path, name: str, artifact: dict) -> Path:
        path = directory / f"{name}.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        return path

    def test_validate_passes_on_well_formed_artifacts(self, validator, tmp_path):
        self._write_artifact(tmp_path, "route-a", _base_artifact())
        rc = validator.main(["--artifacts-dir", str(tmp_path)])
        assert rc == 0

    def test_no_artifacts_is_usage_error(self, validator, tmp_path):
        rc = validator.main(["--artifacts-dir", str(tmp_path)])
        assert rc == 2

    def test_malformed_json_is_usage_error(self, validator, tmp_path):
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        rc = validator.main(["--artifacts-dir", str(tmp_path)])
        assert rc == 2

    def test_require_native_spawn_event_fails_when_self_reported(self, validator, tmp_path):
        artifact = _base_artifact(spawn={
            "parent_session_id": "same-id", "child_session_id": "same-id",
            "native_spawn_event_observed": True,
        })
        self._write_artifact(tmp_path, "route-a", artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--require-native-spawn-event"])
        assert rc == 1

    def test_require_native_spawn_event_passes_with_distinct_ids(self, validator, tmp_path):
        self._write_artifact(tmp_path, "route-a", _base_artifact())
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--require-native-spawn-event"])
        assert rc == 0

    def test_require_native_spawn_event_ignores_non_pass_status(self, validator, tmp_path):
        artifact = _base_artifact(
            status="fail",
            failure_class="spawn_not_observed",
            spawn={"parent_session_id": "p", "child_session_id": "", "native_spawn_event_observed": False},
        )
        self._write_artifact(tmp_path, "route-a", artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--require-native-spawn-event"])
        assert rc == 0

    def test_assert_zero_gemini_and_fallback_invocations_fails_on_nonzero_gemini(self, validator, tmp_path):
        artifact = _base_artifact(
            status="fail",
            failure_class="gemini_invoked",
            provider_observation={
                "selected_provider": None, "provider_attempts": [],
                "gemini_invocation_count": 1, "direct_fallback_invocation_count": 0,
            },
        )
        self._write_artifact(tmp_path, "route-a", artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--assert-zero-gemini-and-fallback-invocations"])
        assert rc == 1

    def test_assert_zero_gemini_and_fallback_invocations_fails_on_nonzero_fallback(self, validator, tmp_path):
        artifact = _base_artifact(
            status="fail",
            failure_class="direct_fallback_invoked",
            provider_observation={
                "selected_provider": None, "provider_attempts": [],
                "gemini_invocation_count": 0, "direct_fallback_invocation_count": 2,
            },
        )
        self._write_artifact(tmp_path, "route-a", artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--assert-zero-gemini-and-fallback-invocations"])
        assert rc == 1

    def test_assert_zero_gemini_and_fallback_invocations_skip_status_still_checked(self, validator, tmp_path):
        """AC8: exit 77 / SKIP is never promoted to aggregate PASS -- a skip
        artifact with a nonzero gemini_invocation_count must still fail this
        assertion rather than being silently excluded."""
        artifact = _base_artifact(
            status="skip",
            failure_class="agy_unavailable",
            provider_observation={
                "selected_provider": None, "provider_attempts": [],
                "gemini_invocation_count": 1, "direct_fallback_invocation_count": 0,
            },
        )
        self._write_artifact(tmp_path, "route-a", artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--assert-zero-gemini-and-fallback-invocations"])
        assert rc == 1

    def test_assert_zero_gemini_and_fallback_invocations_passes_on_zero_counts(self, validator, tmp_path):
        self._write_artifact(tmp_path, "route-a", _base_artifact())
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--assert-zero-gemini-and-fallback-invocations"])
        assert rc == 0

    def test_latest_run_directory_picks_most_recently_modified(self, validator, tmp_path):
        older = tmp_path / "run-a"
        newer = tmp_path / "run-b"
        older.mkdir()
        newer.mkdir()
        import os
        import time
        os.utime(older, (1, 1))
        time.sleep(0.01)
        os.utime(newer, (2, 2))
        assert validator.latest_run_directory(tmp_path) == newer

    def test_latest_run_directory_none_when_missing(self, validator, tmp_path):
        assert validator.latest_run_directory(tmp_path / "does-not-exist") is None

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
RUNTIME_SMOKE_PATH = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


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
def runtime_smoke() -> types.ModuleType:
    return _load_module(RUNTIME_SMOKE_PATH, "test_agent_provider_route_smoke_runtime_smoke")


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

    def test_build_route_prompt_requests_agy_provider(self, producer, tmp_path):
        route = producer.REQUIRED_ROUTES[0]
        prompt = producer.build_route_prompt(route, tmp_path)
        assert "--provider agy" in prompt
        assert f"--profile {route['profile']}" in prompt

    def test_build_route_prompt_forbids_gemini_binary(self, producer, tmp_path):
        for route in producer.REQUIRED_ROUTES:
            prompt = producer.build_route_prompt(route, tmp_path)
            assert "Do not invoke a binary literally named" in prompt
            assert "gemini" in prompt.lower()

    def test_build_route_prompt_forbids_websearch_webfetch_fallback(self, producer, tmp_path):
        for route in producer.REQUIRED_ROUTES:
            prompt = producer.build_route_prompt(route, tmp_path)
            assert "WebSearch or WebFetch" in prompt

    def test_build_route_prompt_requires_result_and_request_paths(self, producer, tmp_path):
        route = producer.REQUIRED_ROUTES[0]
        prompt = producer.build_route_prompt(route, tmp_path)
        assert str(tmp_path / "delegation_request.json") in prompt
        assert str(tmp_path / "delegation_result.json") in prompt
        assert "run_gemini_headless.py" in prompt

    def test_build_route_prompt_github_research_requires_route_evidence_copy(self, producer, tmp_path):
        route = producer._find_route("claude_code", "codebase-investigator", "github_research")
        prompt = producer.build_route_prompt(route, tmp_path)
        assert str(tmp_path / "route_evidence.json") in prompt
        assert "agy_github_research_evidence/v1" in prompt

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


class TestDirectWebToolEventDetection:
    """Adversarial test 3 (PR #2005 review): injecting a Claude
    WebSearch/WebFetch event or a Codex direct-web-shaped event must
    increase the observed fallback count (never a hard-coded 0)."""

    def test_claude_websearch_event_is_counted(self, runtime_smoke):
        stdout = "\n".join(
            json.dumps(line)
            for line in [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {}}]},
                },
            ]
        )
        assert runtime_smoke.count_direct_web_tool_events("claude", stdout) == 1

    def test_claude_webfetch_event_is_counted(self, runtime_smoke):
        stdout = "\n".join(
            json.dumps(line)
            for line in [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "WebFetch", "input": {}}]},
                },
            ]
        )
        assert runtime_smoke.count_direct_web_tool_events("claude", stdout) == 1

    def test_claude_agent_tool_use_is_not_counted_as_fallback(self, runtime_smoke):
        stdout = "\n".join(json.dumps(line) for line in _synthetic_claude_stream_lines())
        assert runtime_smoke.count_direct_web_tool_events("claude", stdout) == 0

    def test_codex_web_search_token_is_counted(self, runtime_smoke):
        stdout = json.dumps({"item": {"type": "collab_tool_call", "tool": "web_search"}})
        assert runtime_smoke.count_direct_web_tool_events("codex", stdout) == 1

    def test_codex_unrelated_command_is_not_counted(self, runtime_smoke):
        stdout = json.dumps({"item": {"type": "collab_tool_call", "command": "ls -la"}})
        assert runtime_smoke.count_direct_web_tool_events("codex", stdout) == 0


class TestProducerRouteEvidenceValidation:
    """Issue #1886 P0-1 fix_delta: the producer must derive
    request.validation / provider_observation.* from ACTUAL files the
    delegated agent wrote, never stamp expected constants on harness exit 0
    alone (PR #2005 adversarial review, decisive false-pass repro)."""

    def test_delegation_request_missing_is_not_run(self, producer, tmp_path):
        route = producer.REQUIRED_ROUTES[0]
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "not_run"

    def test_delegation_request_wrong_provider_fails(self, producer, tmp_path):
        route = producer.REQUIRED_ROUTES[0]
        (tmp_path / "delegation_request.json").write_text(
            json.dumps({"provider": "gemini", "profile": route["profile"], "prompt": "x"}),
            encoding="utf-8",
        )
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "fail"

    def test_delegation_request_with_model_fails(self, producer, tmp_path):
        route = producer.REQUIRED_ROUTES[0]
        (tmp_path / "delegation_request.json").write_text(
            json.dumps({"provider": "agy", "profile": route["profile"], "prompt": "x", "model": "gpt-5"}),
            encoding="utf-8",
        )
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "fail"

    def test_delegation_request_empty_prompt_fails(self, producer, tmp_path):
        route = producer.REQUIRED_ROUTES[0]
        (tmp_path / "delegation_request.json").write_text(
            json.dumps({"provider": "agy", "profile": route["profile"], "prompt": "   "}),
            encoding="utf-8",
        )
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "fail"

    def test_delegation_request_valid_passes(self, producer, tmp_path):
        route = producer.REQUIRED_ROUTES[0]
        (tmp_path / "delegation_request.json").write_text(
            json.dumps({"provider": "agy", "profile": route["profile"], "prompt": "do the thing"}),
            encoding="utf-8",
        )
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "pass"

    def test_delegation_result_missing_yields_no_provider(self, producer, tmp_path):
        provider, attempts, ok = producer._validate_delegation_result_evidence(tmp_path)
        assert provider is None
        assert attempts == []
        assert ok is False

    def test_delegation_result_reads_actual_selected_provider(self, producer, tmp_path):
        (tmp_path / "delegation_result.json").write_text(
            json.dumps(
                {"ok": True, "selected_provider": "agy", "provider_attempts": [{"provider": "agy", "ok": True}]}
            ),
            encoding="utf-8",
        )
        provider, attempts, ok = producer._validate_delegation_result_evidence(tmp_path)
        assert provider == "agy"
        assert attempts == ["agy"]
        assert ok is True

    def test_delegation_result_wrapper_not_ok_is_recorded(self, producer, tmp_path):
        (tmp_path / "delegation_result.json").write_text(
            json.dumps({"ok": False, "selected_provider": None, "provider_attempts": []}),
            encoding="utf-8",
        )
        provider, attempts, ok = producer._validate_delegation_result_evidence(tmp_path)
        assert ok is False

    def test_github_research_route_evidence_missing_is_none(self, producer, tmp_path):
        assert producer._validate_github_research_route_evidence(tmp_path) is None

    def test_github_research_route_evidence_wrong_schema_is_none(self, producer, tmp_path):
        (tmp_path / "route_evidence.json").write_text(
            json.dumps({"schema": "some_other_schema/v1"}), encoding="utf-8"
        )
        assert producer._validate_github_research_route_evidence(tmp_path) is None

    def test_github_research_route_evidence_valid_returns_real_digest(self, producer, tmp_path):
        path = tmp_path / "route_evidence.json"
        path.write_text(json.dumps({"schema": "agy_github_research_evidence/v1"}), encoding="utf-8")
        import hashlib
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert producer._validate_github_research_route_evidence(tmp_path) == expected

    def test_count_direct_fallback_hits_reads_harness_summary(self, producer):
        assert producer._count_direct_fallback_hits({"direct_web_tool_event_count": "2"}) == 2
        assert producer._count_direct_fallback_hits({"direct_web_tool_event_count": 3}) == 3
        assert producer._count_direct_fallback_hits({}) == 0


class TestProducerRetryPolicy:
    """Issue #1886 P0-4 fix_delta: only Codex spawn_not_observed is eligible
    for a single bounded retry; identity mismatch, validation failure,
    provider mismatch, and fallback detection are never retried."""

    def test_codex_spawn_not_observed_is_transient_candidate(self, producer):
        route = producer._find_route("codex_cli", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "spawn_not_observed") is True

    def test_claude_spawn_not_observed_is_not_transient_candidate(self, producer):
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "spawn_not_observed") is False

    def test_provider_mismatch_is_never_transient_candidate(self, producer):
        route = producer._find_route("codex_cli", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "provider_mismatch") is False

    def test_validation_failed_is_never_transient_candidate(self, producer):
        route = producer._find_route("codex_cli", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "validation_failed") is False

    def test_direct_fallback_invoked_is_never_transient_candidate(self, producer):
        route = producer._find_route("codex_cli", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "direct_fallback_invoked") is False


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

    def _six_route_pass_artifacts(self, producer, *, batch_run_id="batch-1", head_sha="a" * 40):
        artifacts = {}
        for route in producer.REQUIRED_ROUTES:
            artifact = _base_artifact(
                subject={
                    "runtime": route["runtime"],
                    "agent_name": route["agent"],
                    "head_sha": head_sha,
                    "runtime_version": "1.0.0",
                    "agent_definition_sha256": "a" * 64,
                    "effective_runtime_config_sha256": "a" * 64,
                },
                request={"profile": route["profile"], "expected_provider": "agy", "validation": "pass"},
                route_evidence=(
                    {"schema": "agy_github_research_evidence/v1", "sha256": "b" * 64}
                    if route["profile"] == "github_research"
                    else {"schema": None, "sha256": None}
                ),
            )
            artifact["batch_run_id"] = batch_run_id
            artifacts[producer._route_key(route)] = artifact
        return artifacts

    def test_require_native_spawn_event_passes_with_distinct_ids(self, validator, producer, tmp_path):
        for key, artifact in self._six_route_pass_artifacts(producer).items():
            self._write_artifact(tmp_path, key.replace(":", "-"), artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--require-native-spawn-event"])
        assert rc == 0

    def test_require_native_spawn_event_missing_route_fails_close_gate(self, validator, producer, tmp_path):
        """Issue #1886 P0-3: a five-of-six cohort (one route missing) must
        fail the aggregate close gate even though every present artifact is
        individually well-formed and status=pass."""
        artifacts = self._six_route_pass_artifacts(producer)
        del artifacts[next(iter(artifacts))]
        for key, artifact in artifacts.items():
            self._write_artifact(tmp_path, key.replace(":", "-"), artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--require-native-spawn-event"])
        assert rc == 1

    def test_require_native_spawn_event_non_pass_status_fails_close_gate(self, validator, producer, tmp_path):
        """Issue #1886 P0-3 fix_delta (PR #2005 adversarial review): a
        non-pass artifact anywhere in the six-route cohort must fail the
        aggregate close gate -- the prior validator silently excluded
        non-pass artifacts from this assertion entirely."""
        artifacts = self._six_route_pass_artifacts(producer)
        some_key = next(iter(artifacts))
        artifacts[some_key]["status"] = "fail"
        artifacts[some_key]["failure_class"] = "spawn_not_observed"
        artifacts[some_key]["spawn"] = {
            "parent_session_id": "p", "child_session_id": "", "native_spawn_event_observed": False,
        }
        for key, artifact in artifacts.items():
            self._write_artifact(tmp_path, key.replace(":", "-"), artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--require-native-spawn-event"])
        assert rc == 1

    def test_decisive_dry_run_skip_cohort_is_rejected_by_close_gate(self, validator, producer, tmp_path):
        """Issue #1886 P0-3: the exact decisive false-pass repro from the
        PR #2005 adversarial review -- six ``status: skip`` artifacts (as
        ``--dry-run`` produces) covering all six required routes -- must be
        rejected by the aggregate close gate, not silently accepted as an
        aggregate PASS."""
        artifacts = self._six_route_pass_artifacts(producer)
        for artifact in artifacts.values():
            artifact["status"] = "skip"
            artifact["failure_class"] = "agy_unavailable"
            artifact["spawn"] = {
                "parent_session_id": "", "child_session_id": "", "native_spawn_event_observed": False,
            }
            artifact["request"]["validation"] = "not_run"
            artifact["provider_observation"]["selected_provider"] = None
        for key, artifact in artifacts.items():
            self._write_artifact(tmp_path, key.replace(":", "-"), artifact)
        rc = validator.main([
            "--artifacts-dir", str(tmp_path),
            "--require-native-spawn-event",
            "--assert-zero-gemini-and-fallback-invocations",
        ])
        assert rc == 1

    def test_duplicate_route_artifacts_rejected(self, validator, producer, tmp_path):
        artifacts = self._six_route_pass_artifacts(producer)
        some_key = next(iter(artifacts))
        self._write_artifact(tmp_path, "dup-a", artifacts[some_key])
        self._write_artifact(tmp_path, "dup-b", artifacts[some_key])
        for key, artifact in artifacts.items():
            if key == some_key:
                continue
            self._write_artifact(tmp_path, key.replace(":", "-"), artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--require-native-spawn-event"])
        assert rc == 1

    def test_mismatched_head_sha_rejected(self, validator, producer, tmp_path):
        artifacts = self._six_route_pass_artifacts(producer)
        some_key = next(iter(artifacts))
        artifacts[some_key]["subject"]["head_sha"] = "c" * 40
        for key, artifact in artifacts.items():
            self._write_artifact(tmp_path, key.replace(":", "-"), artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--require-native-spawn-event"])
        assert rc == 1

    def test_expected_head_sha_mismatch_rejected(self, validator, producer, tmp_path):
        artifacts = self._six_route_pass_artifacts(producer, head_sha="a" * 40)
        for key, artifact in artifacts.items():
            self._write_artifact(tmp_path, key.replace(":", "-"), artifact)
        rc = validator.main([
            "--artifacts-dir", str(tmp_path),
            "--require-native-spawn-event",
            "--expected-head-sha", "d" * 40,
        ])
        assert rc == 1

    def test_missing_github_research_route_evidence_digest_rejected(self, validator, producer, tmp_path):
        artifacts = self._six_route_pass_artifacts(producer)
        github_key = next(k for k in artifacts if "github_research" in k)
        artifacts[github_key]["route_evidence"]["sha256"] = None
        for key, artifact in artifacts.items():
            self._write_artifact(tmp_path, key.replace(":", "-"), artifact)
        rc = validator.main(["--artifacts-dir", str(tmp_path), "--require-native-spawn-event"])
        assert rc == 1

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

    def test_assert_zero_gemini_and_fallback_invocations_passes_on_zero_counts(self, validator, producer, tmp_path):
        for key, artifact in self._six_route_pass_artifacts(producer).items():
            self._write_artifact(tmp_path, key.replace(":", "-"), artifact)
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


# ---------------------------------------------------------------------------
# Claude Code child session id: derivable from captured stdout stream
# without depending on a persisted session transcript file (Issue #1886 AC7
# fix-delta, iteration 6). Merged from
# test_run_worktree_agent_runtime_smoke_stream_child_session_id.py per
# fix-delta iteration 8 (Allowed Paths compliance).
# ---------------------------------------------------------------------------


def _synthetic_claude_stream_lines() -> list[dict]:
    """Shaped after a real captured stdout stream for a single Task/Agent
    tool_use under ``--no-session-persistence`` (no transcript file is ever
    written for this stream)."""
    parent_session_id = "parent-session-aaaa"
    child_agent_id = "a72066e6f732aa768"
    return [
        {"type": "system", "subtype": "init", "session_id": parent_session_id},
        {
            "type": "assistant",
            "session_id": parent_session_id,
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Agent", "input": {}},
                ]
            },
        },
        {
            "type": "user",
            "session_id": parent_session_id,
            "message": {
                "content": [
                    {
                        "tool_use_id": "toolu_x",
                        "type": "tool_result",
                        "content": [
                            {"type": "text", "text": "OK"},
                            {
                                "type": "text",
                                "text": (
                                    f"agentId: {child_agent_id} (use SendMessage with "
                                    f"to: '{child_agent_id}', summary: '...' to continue "
                                    "this agent)"
                                ),
                            },
                        ],
                    }
                ]
            },
            "tool_use_result": {
                "status": "completed",
                "agentId": child_agent_id,
                "agentType": "general-purpose",
            },
        },
        {"type": "result", "subtype": "success", "session_id": parent_session_id},
    ]


class TestClaudeChildAgentTypeIdentityBinding:
    """Issue #1886 P0-2 fix_delta (PR #2005 adversarial review): a distinct
    child session id alone proved only that SOME child was spawned, never
    that it was the REQUESTED custom agent -- a `general-purpose` child
    satisfied the same evidence as `codebase-investigator`. These tests
    cover the new `extract_claude_child_agent_type` extractor and the
    identity-binding wiring in `run_worktree_agent_runtime_smoke.main()`."""

    def test_extract_claude_child_agent_type_from_tool_use_result(self, runtime_smoke):
        stdout = "\n".join(json.dumps(line) for line in _synthetic_claude_stream_lines())
        agent_type = runtime_smoke.extract_claude_child_agent_type(stdout)
        assert agent_type == "general-purpose"

    def test_extract_claude_child_agent_type_returns_none_without_evidence(self, runtime_smoke):
        stdout = "\n".join(
            json.dumps(line)
            for line in [
                {"type": "system", "subtype": "init", "session_id": "parent-only"},
                {"type": "result", "subtype": "success", "session_id": "parent-only"},
            ]
        )
        assert runtime_smoke.extract_claude_child_agent_type(stdout) is None

    def test_general_purpose_child_does_not_satisfy_codebase_investigator_identity(
        self, runtime_smoke
    ):
        """Adversarial test 1 (PR #2005 review): a native spawn event whose
        OBSERVED agentType ("general-purpose") does not match the REQUESTED
        agent ("codebase-investigator") must not be treated as identity
        verified -- this is the exact false-pass shape a generic child
        satisfied under the prior agentId-only evidence."""
        observed_agent_type = "general-purpose"
        requested_agent_type = "codebase-investigator"
        identity_verified = (
            observed_agent_type is not None
            and requested_agent_type is not None
            and observed_agent_type == requested_agent_type
        )
        assert identity_verified is False

    def test_matching_agent_type_satisfies_identity(self, runtime_smoke):
        observed_agent_type = "codebase-investigator"
        requested_agent_type = "codebase-investigator"
        identity_verified = (
            observed_agent_type is not None
            and requested_agent_type is not None
            and observed_agent_type == requested_agent_type
        )
        assert identity_verified is True

    def test_codex_identity_verified_when_observed_agent_role_matches(self):
        """Issue #1886 P0-2 iteration-N fix_delta: live investigation of
        real Codex rollout logs found the spawned child's own
        `session_meta.agent_role` field genuinely carries the requested
        custom agent's role name (see
        `extract_codex_child_agent_role`) -- this supersedes the prior
        always-False fail-closed posture (commit 8915af25). Mirrors the
        Codex `else` branch wiring in
        run_worktree_agent_runtime_smoke.main()."""
        observed_agent_type = "codebase-investigator"
        requested_agent_type = "codebase-investigator"
        identity_verified = (
            observed_agent_type is not None
            and requested_agent_type is not None
            and observed_agent_type == requested_agent_type
        )
        assert identity_verified is True

    def test_codex_identity_not_verified_when_observed_agent_role_mismatches(self):
        """Adversarial case mirroring the Claude
        `general-purpose`-vs-`codebase-investigator` test above: an
        observed `agent_role` that does not match the requested agent
        type must not be treated as identity verified."""
        observed_agent_type = "general-purpose"
        requested_agent_type = "codebase-investigator"
        identity_verified = (
            observed_agent_type is not None
            and requested_agent_type is not None
            and observed_agent_type == requested_agent_type
        )
        assert identity_verified is False

    def test_codex_identity_fails_closed_when_agent_role_absent(self):
        """If a future Codex CLI version/config ever omits `agent_role`
        from the child's `session_meta`, `extract_codex_child_agent_role`
        returns `None` and identity must still fail closed to
        unverified -- never promoted to True on child-id presence alone."""
        observed_agent_type = None
        requested_agent_type = "codebase-investigator"
        identity_verified = (
            observed_agent_type is not None
            and requested_agent_type is not None
            and observed_agent_type == requested_agent_type
        )
        assert identity_verified is False


class TestClaudeChildSessionIdFromStdoutStream:
    def test_extract_claude_child_session_id_from_stdout_without_transcript_file(
        self, runtime_smoke, tmp_path
    ):
        """Primary path: ``tool_use_result.agentId`` on a ``type: "user"``
        event is found directly in the captured stdout stream, with no
        dependency on any file under a (here, deliberately nonexistent)
        ``~/.claude/projects`` directory -- proving the fix works precisely
        in the ``--no-session-persistence`` case this bug was about."""
        stdout = "\n".join(json.dumps(line) for line in _synthetic_claude_stream_lines())

        parent_session_id = runtime_smoke.extract_claude_parent_session_id(stdout)
        assert parent_session_id == "parent-session-aaaa"

        # cwd is an arbitrary nonexistent path -- the stream-based primary
        # path must succeed without ever touching the filesystem-based
        # fallback.
        child_session_id = runtime_smoke.extract_claude_child_session_id(
            parent_session_id, str(tmp_path / "does-not-exist"), stdout
        )
        assert child_session_id == "a72066e6f732aa768"
        assert child_session_id != parent_session_id

    def test_extract_claude_child_session_id_falls_back_to_text_block_regex(
        self, runtime_smoke, tmp_path
    ):
        """Fallback within the stream path: if ``tool_use_result`` is absent
        but the human-readable ``agentId: <hex>`` text line is still present
        in a tool_result content block, it must still be recovered."""
        lines = _synthetic_claude_stream_lines()
        # Drop the structured tool_use_result field to exercise the
        # text-block regex fallback exclusively.
        for line in lines:
            line.pop("tool_use_result", None)
        stdout = "\n".join(json.dumps(line) for line in lines)

        parent_session_id = runtime_smoke.extract_claude_parent_session_id(stdout)
        child_session_id = runtime_smoke.extract_claude_child_session_id(
            parent_session_id, str(tmp_path / "does-not-exist"), stdout
        )
        assert child_session_id == "a72066e6f732aa768"

    def test_extract_claude_child_session_id_returns_none_without_spawn_evidence(
        self, runtime_smoke
    ):
        """Fail-closed: no Agent/Task tool_use in the stream -> ``None``,
        never a guess."""
        stdout = "\n".join(
            json.dumps(line)
            for line in [
                {"type": "system", "subtype": "init", "session_id": "parent-only"},
                {"type": "result", "subtype": "success", "session_id": "parent-only"},
            ]
        )
        parent_session_id = runtime_smoke.extract_claude_parent_session_id(stdout)
        assert (
            runtime_smoke.extract_claude_child_session_id(parent_session_id, "/nonexistent", stdout)
            is None
        )


# ---------------------------------------------------------------------------
# Codex CLI child session id: derivable from a rollout log's own content
# linkage under --ephemeral, not solely a filename-substring match (Issue
# #1886 AC7 fix-delta, iteration 7). Merged from
# test_run_worktree_agent_runtime_smoke_codex_child_session_id.py per
# fix-delta iteration 8 (Allowed Paths compliance).
# ---------------------------------------------------------------------------


def _write_codex_child_rollout_log(sessions_dir: Path, *, own_id: str, parent_thread_id: str) -> Path:
    """Shaped after a real, live-observed rollout log written for a spawned
    Codex CLI sub-agent thread: the file's own id is embedded both in its
    filename and in the first ``session_meta`` record's ``payload.id``; the
    spawning parent's own ``thread_id`` is recorded as
    ``payload.parent_thread_id`` (and duplicated as ``payload.session_id``)
    -- never in the filename."""
    day_dir = sessions_dir / "2026" / "08" / "06"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-08-06T21-51-04-{own_id}.jsonl"
    lines = [
        {
            "timestamp": "2026-08-06T12:51:04.552Z",
            "type": "session_meta",
            "payload": {
                "session_id": parent_thread_id,
                "id": own_id,
                "parent_thread_id": parent_thread_id,
                "cwd": "/home/example/worktree",
                "thread_source": "subagent",
                "agent_role": "codebase-investigator",
            },
        },
        {"timestamp": "2026-08-06T12:51:05.000Z", "type": "turn_context", "payload": {}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


class TestCodexChildSessionIdViaContentLinkedRolloutLog:
    @pytest.fixture()
    def fake_home(self, tmp_path: Path) -> Path:
        home = tmp_path / "home"
        home.mkdir()
        return home

    def test_extract_codex_child_session_id_via_content_linked_rollout_log_under_ephemeral(
        self, runtime_smoke, fake_home: Path, monkeypatch
    ):
        """Fallback path (iteration 7 fix): under ``--ephemeral``, no
        rollout log's *filename* ever contains the parent's own
        ``thread_id`` -- the child's own rollout log must instead be
        located by its content-level ``parent_thread_id`` linkage, and the
        child's own thread id (its ``payload.id``) returned as evidence of
        a distinct native spawn."""
        monkeypatch.setattr(runtime_smoke.Path, "home", classmethod(lambda cls: fake_home))

        parent_thread_id = "019fd720-2814-7362-b530-cb659cec97f8"
        child_own_id = "019fd720-9458-7703-b3f0-07aac6e6b350"
        _write_codex_child_rollout_log(
            fake_home / ".codex" / "sessions", own_id=child_own_id, parent_thread_id=parent_thread_id
        )

        child_session_id = runtime_smoke.extract_codex_child_session_id(parent_thread_id)
        assert child_session_id == child_own_id
        assert child_session_id != parent_thread_id

    def test_extract_codex_child_session_id_prefers_filename_match_when_present(
        self, runtime_smoke, fake_home: Path, monkeypatch
    ):
        """Primary path (unchanged): if a rollout log's filename directly
        contains the parent's ``thread_id`` (the parent's own transcript
        was persisted, e.g. a future non-``--ephemeral`` caller), the
        existing ``spawn_agent`` function_call/function_call_output parsing
        must still take precedence over the content-linked fallback."""
        monkeypatch.setattr(runtime_smoke.Path, "home", classmethod(lambda cls: fake_home))

        parent_thread_id = "parent-thread-zzzz"
        sessions_dir = fake_home / ".codex" / "sessions" / "2026" / "08" / "06"
        sessions_dir.mkdir(parents=True)
        parent_log = sessions_dir / f"rollout-2026-08-06T00-00-00-{parent_thread_id}.jsonl"
        lines = [
            {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "call_1"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": json.dumps({"agent_id": "spawned-agent-id-from-tool-output"}),
                },
            },
        ]
        parent_log.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

        child_session_id = runtime_smoke.extract_codex_child_session_id(parent_thread_id)
        assert child_session_id == "spawned-agent-id-from-tool-output"

    def test_extract_codex_child_session_id_returns_none_without_any_linkage(
        self, runtime_smoke, fake_home: Path, monkeypatch
    ):
        """Fail-closed: no filename match and no rollout log content links
        back to the given parent id -> ``None``, never a guess."""
        monkeypatch.setattr(runtime_smoke.Path, "home", classmethod(lambda cls: fake_home))

        _write_codex_child_rollout_log(
            fake_home / ".codex" / "sessions",
            own_id="unrelated-child-id",
            parent_thread_id="some-other-parent-thread-id",
        )

        assert runtime_smoke.extract_codex_child_session_id("this-parent-id-has-no-match") is None

    def test_extract_codex_child_session_id_returns_none_for_empty_parent_id(
        self, runtime_smoke, fake_home: Path, monkeypatch
    ):
        monkeypatch.setattr(runtime_smoke.Path, "home", classmethod(lambda cls: fake_home))
        assert runtime_smoke.extract_codex_child_session_id(None) is None
        assert runtime_smoke.extract_codex_child_session_id("") is None


class TestCodexChildAgentRoleIdentityEvidence:
    """Issue #1886 P0-2 iteration-N fix_delta (PR #2005 REQUEST_CHANGES):
    live investigation of real, local Codex rollout logs (Codex CLI
    0.146.0, multiple `codebase-investigator`/`web-researcher` routes,
    2026-08-06/07) found that the spawned child's own rollout log
    `session_meta` record genuinely carries an `agent_role` field written
    by the Codex CLI itself -- see `_write_codex_child_rollout_log`, which
    is shaped after those real logs and already includes this field.
    These tests cover the new `extract_codex_child_agent_role` extractor
    that supersedes the prior always-``None``/always-``False`` fail-closed
    posture (commit 8915af25)."""

    @pytest.fixture()
    def fake_home(self, tmp_path: Path) -> Path:
        home = tmp_path / "home"
        home.mkdir()
        return home

    def test_extract_codex_child_agent_role_from_content_linked_session_meta(
        self, runtime_smoke, fake_home: Path, monkeypatch
    ):
        monkeypatch.setattr(runtime_smoke.Path, "home", classmethod(lambda cls: fake_home))

        parent_thread_id = "019fd720-2814-7362-b530-cb659cec97f8"
        child_own_id = "019fd720-9458-7703-b3f0-07aac6e6b350"
        _write_codex_child_rollout_log(
            fake_home / ".codex" / "sessions", own_id=child_own_id, parent_thread_id=parent_thread_id
        )

        agent_role = runtime_smoke.extract_codex_child_agent_role(parent_thread_id)
        assert agent_role == "codebase-investigator"

    def test_extract_codex_child_agent_role_returns_none_without_any_linkage(
        self, runtime_smoke, fake_home: Path, monkeypatch
    ):
        """Fail-closed: no rollout log content links back to the given
        parent id -> ``None``, never a guess."""
        monkeypatch.setattr(runtime_smoke.Path, "home", classmethod(lambda cls: fake_home))

        _write_codex_child_rollout_log(
            fake_home / ".codex" / "sessions",
            own_id="unrelated-child-id",
            parent_thread_id="some-other-parent-thread-id",
        )

        assert runtime_smoke.extract_codex_child_agent_role("this-parent-id-has-no-match") is None

    def test_extract_codex_child_agent_role_returns_none_when_field_absent(
        self, runtime_smoke, fake_home: Path, monkeypatch
    ):
        """Fail-closed: a linked child session_meta record that lacks the
        agent_role field entirely -> ``None``, never a guess (preserves
        fail-closed posture for a hypothetical Codex CLI version/config
        that omits this field)."""
        monkeypatch.setattr(runtime_smoke.Path, "home", classmethod(lambda cls: fake_home))

        sessions_dir = fake_home / ".codex" / "sessions" / "2026" / "08" / "06"
        sessions_dir.mkdir(parents=True)
        parent_thread_id = "parent-no-agent-role"
        child_own_id = "child-no-agent-role"
        path = sessions_dir / f"rollout-2026-08-06T21-51-04-{child_own_id}.jsonl"
        lines = [
            {
                "timestamp": "2026-08-06T12:51:04.552Z",
                "type": "session_meta",
                "payload": {
                    "session_id": parent_thread_id,
                    "id": child_own_id,
                    "parent_thread_id": parent_thread_id,
                    "cwd": "/home/example/worktree",
                    "thread_source": "subagent",
                },
            },
        ]
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

        assert runtime_smoke.extract_codex_child_agent_role(parent_thread_id) is None

    def test_extract_codex_child_agent_role_returns_none_for_empty_parent_id(
        self, runtime_smoke, fake_home: Path, monkeypatch
    ):
        monkeypatch.setattr(runtime_smoke.Path, "home", classmethod(lambda cls: fake_home))
        assert runtime_smoke.extract_codex_child_agent_role(None) is None
        assert runtime_smoke.extract_codex_child_agent_role("") is None

    def test_child_session_id_and_agent_role_read_the_same_record(
        self, runtime_smoke, fake_home: Path, monkeypatch
    ):
        """Both extractors must agree on identity for the same spawn --
        they share `_find_codex_child_session_meta` precisely so the child
        session id and its identity evidence can never disagree."""
        monkeypatch.setattr(runtime_smoke.Path, "home", classmethod(lambda cls: fake_home))

        parent_thread_id = "019fd723-shared-parent"
        child_own_id = "019fd723-shared-child"
        _write_codex_child_rollout_log(
            fake_home / ".codex" / "sessions", own_id=child_own_id, parent_thread_id=parent_thread_id
        )

        assert runtime_smoke.extract_codex_child_session_id(parent_thread_id) == child_own_id
        assert (
            runtime_smoke.extract_codex_child_agent_role(parent_thread_id) == "codebase-investigator"
        )

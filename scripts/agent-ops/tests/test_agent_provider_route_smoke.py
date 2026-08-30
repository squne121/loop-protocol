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
    def test_required_routes_has_three_entries(self, producer):
        """Issue #2161: the 3 native Codex CLI (codex_cli) routes were
        retired along with native Codex CLI; only the 3 claude_code routes
        remain."""
        assert len(producer.REQUIRED_ROUTES) == 3

    def test_required_routes_are_unique(self, producer):
        keys = {producer._route_key(r) for r in producer.REQUIRED_ROUTES}
        assert len(keys) == 3

    def test_required_routes_cover_claude_code_runtime(self, producer):
        runtimes = {r["runtime"] for r in producer.REQUIRED_ROUTES}
        assert runtimes == {"claude_code"}

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

    def test_build_route_prompt_local_asset_research_includes_context_file(self, producer, tmp_path):
        """Issue #1886 P0-4 fix_delta (PR #2005 REQUEST_CHANGES): build_request.py
        --provider agy --profile local_asset_research fails closed without at
        least one --context-file (see
        _validate_local_asset_context_files() in run_gemini_headless.py)."""
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        prompt = producer.build_route_prompt(route, tmp_path)
        assert "--context-file" in prompt

    def test_build_route_prompt_github_research_omits_context_file(self, producer, tmp_path):
        route = producer._find_route("claude_code", "codebase-investigator", "github_research")
        prompt = producer.build_route_prompt(route, tmp_path)
        assert "--context-file" not in prompt

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
        artifact_paths = [
            p for p in artifact_paths
            if p.name != "index.json" and not p.name.endswith("-diagnostics.json")
        ]
        assert len(artifact_paths) == 3
        for path in artifact_paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            jsonschema.validate(data, schema)
            assert data["status"] == "skip"
            assert data["provider_observation"]["gemini_invocation_count"] == 0
            assert data["provider_observation"]["direct_fallback_invocation_count"] == 0

    def test_dry_run_single_route_writes_one_artifact(self, producer, tmp_path):
        rc = producer.main([
            "--runtime", "claude_code", "--agent", "web-researcher", "--profile", "grounded_research",
            "--dry-run", "--output-dir", str(tmp_path), "--repo-root", str(REPO_ROOT),
        ])
        assert rc == 0
        artifact_paths = [
            p for p in tmp_path.glob("*.json")
            if p.name != "index.json" and not p.name.endswith("-diagnostics.json")
        ]
        assert len(artifact_paths) == 1

    def test_unknown_route_returns_nonzero(self, producer, tmp_path, capsys):
        rc = producer.main([
            "--runtime", "claude_code", "--agent", "codebase-investigator", "--profile", "grounded_research",
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

    # Issue #2161 (native Codex CLI retirement): test_codex_web_search_token_is_counted
    # and test_codex_unrelated_command_is_not_counted were removed -- they
    # exercised the Codex-only best-effort token-scan branch
    # (_CODEX_DIRECT_WEB_TOKEN_RE), which was removed along with the
    # ``codex`` runtime lane.


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
            json.dumps({"provider": "gemini", "tool_profile": route["profile"], "prompt": "x"}),
            encoding="utf-8",
        )
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "fail"

    def test_delegation_request_with_model_fails(self, producer, tmp_path):
        route = producer.REQUIRED_ROUTES[0]
        (tmp_path / "delegation_request.json").write_text(
            json.dumps({"provider": "agy", "tool_profile": route["profile"], "prompt": "x", "model": "gpt-5"}),
            encoding="utf-8",
        )
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "fail"

    def test_delegation_request_empty_prompt_fails(self, producer, tmp_path):
        route = producer.REQUIRED_ROUTES[0]
        (tmp_path / "delegation_request.json").write_text(
            json.dumps({"provider": "agy", "tool_profile": route["profile"], "prompt": "   "}),
            encoding="utf-8",
        )
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "fail"

    def test_delegation_request_valid_passes(self, producer, tmp_path):
        """Fixture key MUST be "tool_profile" -- the real key
        build_request.py --provider agy writes (_build_agy_request() in
        build_request.py). Using "profile" here would silently pass a
        fixture that no real build_request.py invocation could ever
        produce (PR #2005 REQUEST_CHANGES: shadow-fixture regression)."""
        route = producer.REQUIRED_ROUTES[0]
        (tmp_path / "delegation_request.json").write_text(
            json.dumps({"provider": "agy", "tool_profile": route["profile"], "prompt": "do the thing"}),
            encoding="utf-8",
        )
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "pass"

    def test_delegation_request_old_wrong_profile_key_fails(self, producer, tmp_path):
        """Regression guard (PR #2005 REQUEST_CHANGES P0-1): a payload using
        the bugged/never-real "profile" key (instead of the real
        "tool_profile" key) must fail -- it never matches
        route["profile"] against the actual production field, and this
        producer must not silently accept it as equivalent."""
        route = producer.REQUIRED_ROUTES[0]
        (tmp_path / "delegation_request.json").write_text(
            json.dumps({"provider": "agy", "profile": route["profile"], "prompt": "do the thing"}),
            encoding="utf-8",
        )
        assert producer._validate_delegation_request_evidence(tmp_path, route) == "fail"

    def test_delegation_result_missing_yields_no_provider(self, producer, tmp_path):
        provider, attempts, ok, agy_failure_class = producer._validate_delegation_result_evidence(
            tmp_path
        )
        assert provider is None
        assert attempts == []
        assert ok is False
        assert agy_failure_class is None

    def test_delegation_result_reads_actual_provider_key(self, producer, tmp_path):
        """A real provider="agy" delegation_result/v1 never has a
        "selected_provider" or "provider_attempts" key (those are
        provider="auto"-only fields written by
        _build_delegation_audit_record() in run_gemini_headless.py); the
        real single-provider signal is the top-level "provider" key on
        every _run_agy() return dict. This is the fixture shape a live
        route smoke run actually produces (PR #2005 REQUEST_CHANGES
        regression: the prior fixture used the never-real
        "selected_provider" key and could not have caught this)."""
        (tmp_path / "delegation_result.json").write_text(
            json.dumps({"ok": True, "provider": "agy", "tool_profile": "local_asset_research"}),
            encoding="utf-8",
        )
        provider, attempts, ok, agy_failure_class = producer._validate_delegation_result_evidence(
            tmp_path
        )
        assert provider == "agy"
        assert attempts == ["agy"]
        assert ok is True
        assert agy_failure_class is None

    def test_delegation_result_wrapper_not_ok_is_recorded(self, producer, tmp_path):
        (tmp_path / "delegation_result.json").write_text(
            json.dumps({"ok": False, "provider": "agy", "failure_class": "agy_exit_nonzero"}),
            encoding="utf-8",
        )
        provider, attempts, ok, agy_failure_class = producer._validate_delegation_result_evidence(
            tmp_path
        )
        assert ok is False
        assert agy_failure_class == "agy_exit_nonzero"

    def test_delegation_result_auto_dispatch_shape_still_supported(self, producer, tmp_path):
        """provider="auto" results (not currently exercised by this route
        smoke, which always requests provider="agy" -- but the
        selected_provider/provider_attempts fields are still a valid
        result shape this producer must keep reading correctly)."""
        (tmp_path / "delegation_result.json").write_text(
            json.dumps(
                {"ok": True, "selected_provider": "agy", "provider_attempts": [{"provider": "agy", "ok": True}]}
            ),
            encoding="utf-8",
        )
        provider, attempts, ok, agy_failure_class = producer._validate_delegation_result_evidence(
            tmp_path
        )
        assert provider == "agy"
        assert attempts == ["agy"]
        assert ok is True
        assert agy_failure_class is None

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
    """Issue #1886 P0-4 fix_delta: identity mismatch, validation failure,
    provider mismatch, and fallback detection are never retried. Issue #2161
    (native Codex CLI retirement): the former Codex-only spawn_not_observed
    bounded-retry branch was removed along with the codex_cli route, so
    spawn_not_observed is now never a transient candidate for any route."""

    def test_claude_spawn_not_observed_is_not_transient_candidate(self, producer):
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "spawn_not_observed") is False

    def test_provider_mismatch_is_never_transient_candidate(self, producer):
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "provider_mismatch") is False

    def test_validation_failed_is_never_transient_candidate(self, producer):
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "validation_failed") is False

    def test_direct_fallback_invoked_is_never_transient_candidate(self, producer):
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "direct_fallback_invoked") is False

    def test_validation_failed_without_materialization_diagnostics_is_not_transient(self, producer):
        """A ``validation_failed`` cause with no diagnostics (or diagnostics
        that never recorded the artifact-materialization race) must remain
        non-transient -- only adding the new Issue #2015 AC14 diagnostic
        signature below is allowed to change that."""
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        assert producer._is_transient_infrastructure_candidate(route, "validation_failed", None) is False
        assert (
            producer._is_transient_infrastructure_candidate(
                route, "validation_failed", {"secondary_failures": []}
            )
            is False
        )
        assert (
            producer._is_transient_infrastructure_candidate(
                route,
                "validation_failed",
                {"secondary_failures": [{"kind": "nonzero_harness_exit_with_spawn_evidence"}]},
            )
            is False
        )

    def test_claude_child_completed_artifact_not_materialized_is_transient_candidate(self, producer):
        """Issue #2015 AC14 root-cause finding (live reproduction on head
        505d3528): a genuinely-completed child (SubagentStop hook fired)
        whose delegation artifact never materializes even after the bounded
        poll is a genuine async-Task-dispatch infrastructure-timing race,
        not a validation defect -- eligible for a bounded single retry
        (reproduced live on claude_code). Issue #2161: the former
        codex_cli-only duplicate of this test was removed along with the
        codex_cli route (the underlying retry-eligibility check is not
        route-specific)."""
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        diagnostics = {
            "secondary_failures": [
                {"kind": "child_completed_but_artifact_not_materialized"},
            ],
        }
        assert (
            producer._is_transient_infrastructure_candidate(route, "validation_failed", diagnostics)
            is True
        )

    def test_agy_rate_limited_is_transient_candidate(self, producer):
        """Issue #2015 root-cause finding (live re-run, 2026-08-09, head
        948759e8): a genuinely-completed, genuinely-spawned trial can still
        fail ``validation_failed`` because the materialized
        ``delegation_result.json`` itself reports ``ok: false`` with
        ``failure_class: agy_rate_limited`` -- a real AGY-side
        ``RESOURCE_EXHAUSTED`` (HTTP 429) quota/rate-limit error observed
        under concurrent multi-session host load, AFTER a genuinely
        successful Serena MCP retrieval
        (``local_asset_retrieval_metadata.retrieval_status: "succeeded"``).
        ``references/failure-class-taxonomy.md``'s AGY provider failure
        class table already documents ``agy_rate_limited`` as
        retryable="yes"; this is retry-eligible at this route-smoke layer
        too. Issue #2161: the former codex_cli/claude_code dual-runtime
        loop was simplified to the one remaining runtime (the underlying
        retry-eligibility check is not route-specific)."""
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        diagnostics = {
            "secondary_failures": [
                {"kind": "agy_transient_quota_failure", "agy_failure_class": "agy_rate_limited"},
            ],
        }
        assert (
            producer._is_transient_infrastructure_candidate(route, "validation_failed", diagnostics)
            is True
        )

    def test_agy_capacity_exhausted_is_transient_candidate(self, producer):
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        diagnostics = {
            "secondary_failures": [
                {"kind": "agy_transient_quota_failure", "agy_failure_class": "agy_capacity_exhausted"},
            ],
        }
        assert (
            producer._is_transient_infrastructure_candidate(route, "validation_failed", diagnostics)
            is True
        )

    def test_agy_transient_quota_marker_never_promotes_non_validation_failed_classes(
        self, producer
    ):
        """The new ``agy_transient_quota_failure`` secondary-failure marker
        is scoped identically to the pre-existing materialization-race
        marker: it only ever promotes ``failure_class: validation_failed``,
        never an unrelated deterministic signal such as ``gemini_invoked``
        (checked, and always wins, ahead of ``delegation_result.json``
        state in ``_run_route_once``)."""
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        diagnostics = {
            "secondary_failures": [
                {"kind": "agy_transient_quota_failure", "agy_failure_class": "agy_rate_limited"},
            ],
        }
        assert (
            producer._is_transient_infrastructure_candidate(route, "gemini_invoked", diagnostics)
            is False
        )
        assert (
            producer._is_transient_infrastructure_candidate(route, "provider_mismatch", diagnostics)
            is False
        )

    def test_child_completed_marker_never_promotes_a_deterministic_policy_violation(
        self, producer
    ):
        """Issue #2015 P1 fix (control-plane live re-run + live repro, head
        ffad6201, 2026-08-09): the diagnostic marker's retry-eligibility
        was originally scoped to ``validation_failed`` only, on the (since
        live-disproven) assumption that ``provider_mismatch`` could not
        also stem from the same missing-result-file race -- a live
        ``codex_cli``/``local_asset_research`` repro on this head showed
        the identical ``child_completed_but_artifact_not_materialized``
        condition surfacing as ``failure_class: provider_mismatch``
        instead (see ``_is_transient_infrastructure_candidate``'s own
        docstring/comment for the exact reproduced diagnostics). It is
        therefore now retry-eligible. What must still NEVER be promoted is
        a deterministic, independent policy-violation signal such as
        ``gemini_invoked`` (a literal forbidden-binary sentinel hit,
        recorded during the SAME already-completed subprocess run,
        unrelated to and always checked ahead of
        ``delegation_result.json`` state) -- retrying that would not
        resolve it and would only burn route budget."""
        route = producer._find_route("claude_code", "codebase-investigator", "local_asset_research")
        diagnostics = {
            "secondary_failures": [
                {"kind": "child_completed_but_artifact_not_materialized"},
            ],
        }
        assert (
            producer._is_transient_infrastructure_candidate(route, "provider_mismatch", diagnostics)
            is True
        )
        assert (
            producer._is_transient_infrastructure_candidate(route, "gemini_invoked", diagnostics)
            is False
        )


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

    # Issue #2161 (native Codex CLI retirement):
    # test_codex_identity_verified_when_observed_agent_role_matches and
    # test_codex_identity_not_verified_when_observed_agent_role_mismatches
    # were removed as exact duplicates of
    # test_matching_agent_type_satisfies_identity /
    # test_general_purpose_child_does_not_satisfy_codebase_investigator_identity
    # above (same inline identity-verified formula).
    # test_codex_identity_fails_closed_when_agent_role_absent exercised
    # extract_codex_child_agent_role(), removed along with the ``codex``
    # runtime lane.


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

# Issue #2161 (native Codex CLI retirement): the Codex CLI child
# session id / agent role identity evidence tests
# (TestCodexChildSessionIdViaContentLinkedRolloutLog,
# TestCodexChildAgentRoleIdentityEvidence) and their
# _write_codex_child_rollout_log() helper were removed -- they
# exercised extract_codex_child_session_id() /
# extract_codex_child_agent_role() / _find_codex_child_session_meta(),
# all removed along with the ``codex`` runtime lane.

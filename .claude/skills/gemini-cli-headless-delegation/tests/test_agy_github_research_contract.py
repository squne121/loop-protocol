"""Contract tests for `provider=agy` + `tool_profile=github_research` (Issue #1920).

Covers AC1-AC6: canonical builder/provider parity, the single-`gh`-invocation
semantic allowlist (allow/deny, including the `agy_permission_enforcement_hook`
`ALLOWED_PROFILES` connection), repository/host binding + token isolation +
redaction-before-truncate, the bounded 8-iteration evidence schema, the
SKIP-is-not-PASS contract, and Gemini-invocation-count parity.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_SCHEMA_PATH = Path(__file__).resolve().parents[4] / "schemas" / "agy_github_research_evidence_v1.schema.json"


def _load(name: str, filename: str) -> types.ModuleType:
    path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # required for dataclasses to resolve cls.__module__
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def broker() -> types.ModuleType:
    return _load("agy_github_research_broker_under_test", "run_agy_github_research_broker.py")


@pytest.fixture()
def rgh() -> types.ModuleType:
    return _load("run_gemini_headless_under_test", "run_gemini_headless.py")


@pytest.fixture()
def build_request() -> types.ModuleType:
    return _load("build_request_under_test", "build_request.py")


@pytest.fixture()
def permission_policy() -> types.ModuleType:
    return _load("agy_permission_policy_under_test", "agy_permission_policy.py")


@pytest.fixture()
def hook() -> types.ModuleType:
    return _load("agy_permission_enforcement_hook_under_test", "agy_permission_enforcement_hook.py")


# ---------------------------------------------------------------------------
# AC1: canonical builder + provider parity
# ---------------------------------------------------------------------------


def test_ac1_builder_supports_agy_github_research_without_model(build_request, tmp_path):
    output_path = tmp_path / "request.json"
    exit_code = build_request.main(
        [
            "--provider",
            "agy",
            "--profile",
            "github_research",
            "--prompt",
            "Summarize issue 1920",
            "--output",
            str(output_path),
        ]
    )
    assert exit_code == 0
    request = json.loads(output_path.read_text())
    assert request["provider"] == "agy"
    assert request["tool_profile"] == "github_research"
    assert "model" not in request


def test_ac1_github_research_is_in_agy_supported_profiles(rgh):
    assert rgh.GITHUB_RESEARCH_PROFILE in rgh.AGY_SUPPORTED_PROFILES


def test_ac1_validate_request_for_provider_accepts_agy_github_research(rgh):
    request = {
        "schema": "delegation_request_v1",
        "provider": "agy",
        "tool_profile": "github_research",
        "prompt": "non-empty prompt",
    }
    assert rgh.validate_request_for_provider(request) == []


def test_ac1_validate_request_for_provider_rejects_explicit_model(rgh):
    request = {
        "schema": "delegation_request_v1",
        "provider": "agy",
        "tool_profile": "github_research",
        "prompt": "non-empty prompt",
        "model": "some-model",
    }
    errors = rgh.validate_request_for_provider(request)
    assert any("unsupported_provider_option" in e for e in errors)


# ---------------------------------------------------------------------------
# AC2: single gh invocation semantic allowlist + hook ALLOWED_PROFILES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["issue", "view", "1920"],
        ["issue", "list"],
        ["pr", "view", "1"],
        ["pr", "list"],
        ["pr", "diff", "1"],
        ["pr", "checks", "1"],
        ["repo", "view"],
        ["search", "issues", "1920"],
        ["search", "prs", "1920"],
        ["search", "repos", "loop-protocol"],
        ["release", "list"],
        ["api", "repos/squne121/loop-protocol"],
    ],
)
def test_ac2_allowlisted_commands_are_allowed(broker, argv):
    result = broker.validate_gh_argv(argv)
    assert result.allowed is True


@pytest.mark.parametrize(
    "argv",
    [
        ["issue", "close", "1"],
        ["issue", "create"],
        ["pr", "merge", "1"],
        ["pr", "review", "1"],
        ["repo", "delete"],
        ["auth", "login"],
        ["auth", "status"],
        ["alias", "set", "x", "y"],
        ["extension", "install", "x"],
        ["api", "graphql", "-f", "query=x"],
        ["api", "repos/x/y", "-X", "POST"],
        ["api", "repos/x/y", "--method", "DELETE"],
        ["secret", "set", "x"],
    ],
)
def test_ac2_denied_commands_are_denied_pre_execution(broker, argv):
    result = broker.validate_gh_argv(argv)
    assert result.allowed is False


def test_ac2_compound_shell_is_denied(broker):
    result = broker.validate_gh_argv(["issue", "view", "1;", "rm", "-rf", "/"])
    assert result.allowed is False
    assert result.probe_class == "compound_shell"


def test_ac2_hook_allowed_profiles_includes_github_research(hook):
    assert "github_research" in hook.ALLOWED_PROFILES


def test_ac2_github_research_has_no_native_tool_permission(permission_policy):
    assert permission_policy.PROFILE_ALLOWED_TOOLS[permission_policy.GITHUB_RESEARCH_PROFILE] == frozenset()
    assert permission_policy.PROFILE_ALLOWED_PERMISSION_RESOURCES[permission_policy.GITHUB_RESEARCH_PROFILE] == (
        frozenset()
    )


def test_ac2_github_research_is_in_permission_policy_allowed_profiles(permission_policy):
    assert permission_policy.GITHUB_RESEARCH_PROFILE in permission_policy.ALLOWED_PROFILES


# ---------------------------------------------------------------------------
# AC3: repository/host binding, token isolation, redaction-before-truncate
# ---------------------------------------------------------------------------


def test_ac3_issue_and_pr_commands_get_repo_binding_injected(broker):
    bound = broker._force_repo_binding(["issue", "view", "1"], host="github.com", repo="squne121/loop-protocol")
    assert bound[-2:] == ["--repo", "github.com/squne121/loop-protocol"]


def test_ac3_repo_view_does_not_get_repo_flag_injected(broker):
    # `gh repo view` does not accept `--repo` (positional/env-only); injecting
    # it would be rejected by gh itself with "unknown flag".
    bound = broker._force_repo_binding(["repo", "view"], host="github.com", repo="squne121/loop-protocol")
    assert "--repo" not in bound


def test_ac3_cross_repository_override_is_denied(broker):
    result = broker.validate_gh_argv(["issue", "view", "1", "--repo", "someone-else/other-repo"])
    assert result.allowed is False
    assert result.probe_class == "cross_repository"


def test_ac3_alternate_host_override_is_denied(broker):
    result = broker.validate_gh_argv(["issue", "view", "1", "--hostname", "example.com"])
    assert result.allowed is False
    # Both host- and repository-override attempts collapse into the same
    # structural "cross_repository_or_host_denied" reason at the broker
    # layer; the e2e module's own negative-probe list (see
    # run_agy_github_research_e2e._NEGATIVE_PROBES) is what carries the
    # distinct alternate_host / cross_repository probe_class labels used in
    # the evidence artifact.
    assert result.probe_class == "cross_repository"


def test_ac3_isolated_gh_config_dir_is_fresh_and_empty(broker, tmp_path):
    config_dir = broker._isolated_gh_config_dir(tmp_path)
    assert config_dir.exists()
    assert list(config_dir.iterdir()) == []


def test_ac3_env_never_carries_ambient_secret_vars(broker, monkeypatch, tmp_path):
    monkeypatch.setenv("SOME_OTHER_SECRET", "leak-me")
    env = broker._minimal_broker_env(
        gh_token="fake-token-value",
        host="github.com",
        repo="squne121/loop-protocol",
        gh_config_dir=tmp_path,
    )
    assert "SOME_OTHER_SECRET" not in env
    assert env["GH_TOKEN"] == "fake-token-value"
    assert env["GH_HOST"] == "github.com"
    assert env["GH_REPO"] == "squne121/loop-protocol"
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_CONFIG_DIR"] == str(tmp_path)


def test_ac3_redaction_before_truncate_removes_token_shape_before_cap(broker):
    secret = "ghp_" + ("a" * 40)
    text = "prefix " + secret + " suffix " + ("x" * 100)
    redacted, truncated = broker._bounded_redacted(text, cap_bytes=20)
    assert secret not in redacted
    assert "[REDACTED]" in redacted or truncated


def test_ac3_digest_never_contains_raw_token(broker):
    secret = "ghp_" + ("b" * 40)
    digest = broker._digest("token=" + secret)
    assert secret not in digest
    assert digest.startswith("sha256:")


# ---------------------------------------------------------------------------
# AC4: bounded 8-iteration evidence schema
# ---------------------------------------------------------------------------


def test_ac4_evidence_schema_file_exists_and_is_valid_json_schema():
    assert _SCHEMA_PATH.is_file()
    schema = json.loads(_SCHEMA_PATH.read_text())
    import jsonschema

    jsonschema.Draft7Validator.check_schema(schema)
    assert schema["title"] == "agy_github_research_evidence/v1"


def test_ac4_evidence_schema_fixes_the_numeric_limits_contract():
    schema = json.loads(_SCHEMA_PATH.read_text())
    limits_props = schema["properties"]["limits"]["properties"]
    assert limits_props["max_iterations"]["const"] == 8
    assert limits_props["command_timeout_seconds"]["const"] == 30
    assert limits_props["total_route_timeout_seconds"]["const"] == 180
    assert limits_props["stdout_bytes_per_command"]["const"] == 65536
    assert limits_props["stderr_bytes_per_command"]["const"] == 16384
    assert limits_props["aggregate_retained_bytes"]["const"] == 262144
    assert limits_props["max_records_per_command"]["const"] == 100
    assert limits_props["pagination"]["const"] is False


def test_ac4_e2e_module_limits_match_the_schema_constants():
    e2e = _load("run_agy_github_research_e2e_under_test_limits", "run_agy_github_research_e2e.py")
    schema = json.loads(_SCHEMA_PATH.read_text())
    limits_props = schema["properties"]["limits"]["properties"]
    for key, prop in limits_props.items():
        assert e2e.LIMITS[key] == prop["const"], key


def test_ac4_evidence_artifact_conforms_to_schema(tmp_path, monkeypatch):
    e2e = _load("run_agy_github_research_e2e_under_test_artifact", "run_agy_github_research_e2e.py")
    monkeypatch.chdir(tmp_path)
    evidence = e2e._build_evidence(
        run_id="run-1",
        status="skip",
        skip_reason="agy_cli_unavailable",
        iterations=[],
        negative_probes=[
            {"probe_class": "mutation", "denied_pre_execution": True, "reason": "denied_subcommand"},
        ],
        positive_run={
            "observed": False,
            "exit_code": None,
            "iteration_count": 0,
            "adaptive_next_command_observed": False,
        },
        agy_observed_version=None,
    )
    import jsonschema

    schema = json.loads(_SCHEMA_PATH.read_text())
    jsonschema.Draft7Validator(schema).validate(evidence)
    assert evidence["schema"] == "agy_github_research_evidence/v1"
    assert evidence["gemini_invocation_count"] == 0


def test_ac4_never_reuses_undefined_route_smoke_schema():
    schema = json.loads(_SCHEMA_PATH.read_text())
    # The schema's own canonical identity must be agy_github_research_evidence/v1,
    # never the unrelated/undefined AGENT_PROVIDER_ROUTE_SMOKE_V1 (the schema's
    # description text may still *mention* that name to document non-reuse).
    assert schema["title"] == "agy_github_research_evidence/v1"
    assert schema["properties"]["schema"]["const"] == "agy_github_research_evidence/v1"


# ---------------------------------------------------------------------------
# AC5: SKIP is not PASS; no Gemini/direct fallback
# ---------------------------------------------------------------------------


def test_ac5_missing_gh_token_produces_skip_not_pass(tmp_path, monkeypatch):
    e2e = _load("run_agy_github_research_e2e_under_test_skip", "run_agy_github_research_e2e.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e.shutil, "which", lambda _binary: "/usr/bin/agy")
    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["exit_code"] == 77
    assert result["ok"] is False
    assert result["failure_class"] == "github_research_skip"


def test_ac5_missing_agy_cli_produces_skip_not_pass(tmp_path, monkeypatch):
    e2e = _load("run_agy_github_research_e2e_under_test_skip2", "run_agy_github_research_e2e.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(e2e.shutil, "which", lambda _binary: None)
    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["exit_code"] == 77
    assert result["ok"] is False


def test_ac5_skip_artifact_status_is_skip_not_pass(tmp_path, monkeypatch):
    e2e = _load("run_agy_github_research_e2e_under_test_skip3", "run_agy_github_research_e2e.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e.shutil, "which", lambda _binary: None)
    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    artifact_path = Path(result["result_surface"]["primary_artifact"])
    evidence = json.loads(artifact_path.read_text())
    assert evidence["status"] == "skip"
    assert evidence["status"] != "pass"


# ---------------------------------------------------------------------------
# AC6: parity + isolation invariants
# ---------------------------------------------------------------------------


def test_ac6_gh_token_never_reaches_agy_subprocess_env(monkeypatch, tmp_path):
    e2e = _load("run_agy_github_research_e2e_under_test_env", "run_agy_github_research_e2e.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "must-not-leak-into-agy")
    env, _prefix, workspace = e2e._isolated_agy_env("agy")
    try:
        assert "GH_TOKEN" not in env
    finally:
        if workspace is not None:
            import shutil

            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_ac6_gemini_invocation_count_is_always_zero(tmp_path, monkeypatch):
    e2e = _load("run_agy_github_research_e2e_under_test_gemini_count", "run_agy_github_research_e2e.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e.shutil, "which", lambda _binary: None)
    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["gemini_invocation_count"] == 0


def test_ac6_broker_and_e2e_negative_probes_cover_all_five_classes():
    e2e = _load("run_agy_github_research_e2e_under_test_probes", "run_agy_github_research_e2e.py")
    classes = {probe_class for probe_class, _argv in e2e._NEGATIVE_PROBES}
    assert classes == {"mutation", "cross_repository", "alternate_host", "compound_shell", "credential_display"}
    for _probe_class, argv in e2e._NEGATIVE_PROBES:
        assert argv, "every negative probe must carry a non-empty argv"

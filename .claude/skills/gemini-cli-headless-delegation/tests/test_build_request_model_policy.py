"""Tests for build_request.py's `model-policy` subcommand (Issue #1269).

AC coverage:
  AC1: legacy `--profile`/`--objective` invocation is unaffected by the new
       `model-policy` subcommand.
  AC2: `model-policy --help` lists --provider/--role/--profile and does not
       require --objective/--context-file/--output.
  AC3: `model-policy` calls run_gemini_headless.resolve_model_chain()
       directly; stdout resolved_chain matches its return value exactly.
  AC4: `--provider agy` always reports actual_model=null;
       "agy-default" is surfaced only via legacy_compatibility_label.
  AC5: `--provider auto` requires --profile and reports
       provider_candidates/runtime_order/profile_eligible/consumer_constraints.
  AC6: stdout is a single delegation_model_policy/v1 JSON object with no
       filesystem side effects.
  AC7: unknown role / invalid model_routing config / missing --profile for
       provider=auto all return a stable failure_class and non-zero exit.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_BUILD_REQUEST_PATH = _SCRIPTS_DIR / "build_request.py"


# ---------------------------------------------------------------------------
# Helpers: module loaders (mirrors test_build_request.py convention)
# ---------------------------------------------------------------------------


def load_build_request():
    spec = importlib.util.spec_from_file_location("build_request", _BUILD_REQUEST_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_run_gemini_headless():
    path = _SCRIPTS_DIR / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# AC1: legacy invocation unaffected
# ---------------------------------------------------------------------------


def test_legacy_profile_objective_invocation_unaffected_by_model_policy_subcommand(tmp_path):
    """GIVEN the legacy --profile/--objective CLI invocation
    WHEN build_request.py is invoked exactly as before model-policy existed
    THEN argv parsing, stdout request JSON, and exit code all remain unchanged."""
    context_file = tmp_path / "context.md"
    context_file.write_text("test context", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_BUILD_REQUEST_PATH),
            "--profile",
            "github_research",
            "--objective",
            "Investigate the latest PR for regression issues via gh pr list",
            "--context-file",
            str(context_file),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"legacy invocation failed: {result.stderr}"

    payload = json.loads(result.stdout)
    assert payload["schema"] == "delegation_request_v1"
    assert payload["tool_profile"] == "github_research"
    assert payload["objective"] == "Investigate the latest PR for regression issues via gh pr list"
    assert payload["context_files"] == [str(context_file)]
    # model-policy must not leak into the legacy request schema.
    assert "model-policy" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# AC2: --help lists provider/role/profile only
# ---------------------------------------------------------------------------


def test_model_policy_subcommand_help_lists_provider_role_profile():
    """GIVEN build_request.py model-policy --help
    WHEN invoked
    THEN it exits 0 and lists --provider/--role/--profile but not
    --objective/--context-file/--output."""
    result = subprocess.run(
        [sys.executable, str(_BUILD_REQUEST_PATH), "model-policy", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"model-policy --help returned {result.returncode}: {result.stderr}"
    assert "--provider" in result.stdout
    assert "--role" in result.stdout
    assert "--profile" in result.stdout
    assert "--objective" not in result.stdout
    assert "--context-file" not in result.stdout
    assert "--output" not in result.stdout


# ---------------------------------------------------------------------------
# AC3: gemini+role uses resolve_model_chain() directly
# ---------------------------------------------------------------------------


def test_model_policy_gemini_role_uses_resolve_model_chain_directly():
    """GIVEN --provider gemini --role implementation
    WHEN model-policy resolves the chain
    THEN stdout resolved_chain equals resolve_model_chain()'s own return
    value exactly (no reimplementation of routing/precedence)."""
    br = load_build_request()
    rgh = load_run_gemini_headless()

    payload, exit_code = br.build_model_policy(provider="gemini", role="implementation", profile=None)
    assert exit_code == 0
    assert payload["ok"] is True

    routing = rgh.load_model_routing()
    expected_chain, error = rgh.resolve_model_chain({"role": "implementation"}, routing)
    assert error is None
    assert payload["resolved_chain"] == expected_chain
    assert payload["actual_model"] is None
    assert payload["resolver_source"] == "run_gemini_headless.resolve_model_chain"


# ---------------------------------------------------------------------------
# AC4: agy actual_model is always null, never "agy-default"
# ---------------------------------------------------------------------------


def test_model_policy_agy_actual_model_is_null_not_agy_default():
    """GIVEN --provider agy
    WHEN model-policy inspects it
    THEN actual_model is always null, "agy-default" only appears as
    legacy_compatibility_label, and wrapper/upstream capability are
    reported as separate objects."""
    br = load_build_request()

    payload, exit_code = br.build_model_policy(provider="agy", role=None, profile=None)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["actual_model"] is None
    assert payload["legacy_compatibility_label"] == "agy-default"
    # actual_model must never carry the "agy-default" observed-value sentinel.
    assert payload["actual_model"] != "agy-default"

    assert "wrapper_capability" in payload
    assert "upstream_capability" in payload
    assert payload["wrapper_capability"]["explicit_model_selection"] is False
    assert payload["wrapper_capability"]["role_based_model_chain"] is False
    assert payload["upstream_capability"]["probed"] is False


# ---------------------------------------------------------------------------
# AC5: auto requires --profile and reports consumer_constraints
# ---------------------------------------------------------------------------


def test_model_policy_auto_requires_profile_and_reports_consumer_constraints():
    """GIVEN --provider auto
    WHEN --profile is omitted THEN it fails closed with a stable failure_class.
    WHEN --profile is provided THEN provider_candidates/runtime_order/
    profile_eligible/consumer_constraints are all reported."""
    br = load_build_request()
    rgh = load_run_gemini_headless()

    missing_profile_payload, missing_profile_exit = br.build_model_policy(
        provider="auto", role=None, profile=None
    )
    assert missing_profile_exit != 0
    assert missing_profile_payload["ok"] is False
    assert missing_profile_payload["failure_class"] == "profile_required_for_auto"

    payload, exit_code = br.build_model_policy(provider="auto", role=None, profile="no_tools")
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["runtime_order"] == list(rgh.PROVIDER_AUTO_RUNTIME_ORDER)
    assert payload["profile_eligible"] is True

    providers_seen = {candidate["provider"] for candidate in payload["provider_candidates"]}
    assert providers_seen == set(rgh.PROVIDER_AUTO_RUNTIME_ORDER)

    agy_candidate = next(c for c in payload["provider_candidates"] if c["provider"] == "agy")
    assert agy_candidate["actual_model"] is None
    assert agy_candidate["legacy_compatibility_label"] == "agy-default"

    constraints = payload["consumer_constraints"]
    assert constraints["fan_out"] == {
        "supported": False,
        "reason_code": rgh.PROVIDER_AUTO_FAN_OUT_UNSUPPORTED_REASON_CODE,
    }
    assert constraints["agy_fallback_requires_prompt"] is True
    assert constraints["explicit_model_survives_fallback"] is False

    # a profile outside PROVIDER_AUTO_ELIGIBLE_PROFILES must be reported as
    # ineligible without crashing the inspector (still ok=true / read-only).
    ineligible_payload, ineligible_exit = br.build_model_policy(
        provider="auto", role=None, profile="grounded_research"
    )
    assert ineligible_exit == 0
    assert ineligible_payload["profile_eligible"] is False


# ---------------------------------------------------------------------------
# AC6: stdout-only JSON, no filesystem side effects
# ---------------------------------------------------------------------------


def test_model_policy_stdout_is_delegation_model_policy_v1_json_no_file_side_effect(tmp_path):
    """GIVEN model-policy is invoked as a subprocess CLI
    WHEN it runs
    THEN stdout is exactly one delegation_model_policy/v1 JSON object and no
    new files are created anywhere under tmp_path (cwd)."""
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    result = subprocess.run(
        [
            sys.executable,
            str(_BUILD_REQUEST_PATH),
            "model-policy",
            "--provider",
            "gemini",
            "--role",
            "implementation",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, f"model-policy invocation failed: {result.stderr}"

    payload = json.loads(result.stdout)
    assert payload["schema"] == "delegation_model_policy/v1"

    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert before == after, f"model-policy must not create files: before={before} after={after}"


# ---------------------------------------------------------------------------
# AC7: unknown role / invalid config return stable failure_class + exit code
# ---------------------------------------------------------------------------


def test_model_policy_unknown_role_and_invalid_config_return_stable_failure_class(tmp_path):
    """GIVEN an unknown role or a malformed model_routing.yaml
    WHEN model-policy resolves the request
    THEN both fail closed with a stable failure_class and non-zero exit code."""
    br = load_build_request()

    unknown_role_payload, unknown_role_exit = br.build_model_policy(
        provider="gemini", role="no_such_role", profile=None
    )
    assert unknown_role_exit != 0
    assert unknown_role_payload["ok"] is False
    assert unknown_role_payload["failure_class"] == "unknown_role"

    bad_config = tmp_path / "model_routing.yaml"
    bad_config.write_text("default_chain: {not_a_list: true}\n", encoding="utf-8")

    invalid_config_payload, invalid_config_exit = br.build_model_policy(
        provider="gemini", role=None, profile=None, config_path=bad_config
    )
    assert invalid_config_exit != 0
    assert invalid_config_payload["ok"] is False
    assert invalid_config_payload["failure_class"] == "config_invalid"
    assert invalid_config_payload["reason_code"] == "routing_config_invalid"

    # failure_class values must be distinct and stable across the two cases.
    assert unknown_role_payload["failure_class"] != invalid_config_payload["failure_class"]


# ---------------------------------------------------------------------------
# Issue #1695 PR review Blocker 1: no bytecode cache written to the runtime
# source directory.
# ---------------------------------------------------------------------------


def test_model_policy_does_not_create_bytecode_cache_in_runtime_source_dir(monkeypatch):
    """GIVEN model-policy dynamically loads run_gemini_headless.py
    WHEN it runs, even with PYTHONDONTWRITEBYTECODE unset
    THEN no __pycache__ (or any other) file is created under scripts/ --
    the "no-side-effect inspector" claim must hold against the repo-tracked
    runtime source directory, not just against a scratch cwd (AC6 covers
    cwd; this covers scripts/)."""
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    br = load_build_request()

    before = sorted(p.relative_to(_SCRIPTS_DIR) for p in _SCRIPTS_DIR.rglob("*"))
    payload, exit_code = br.build_model_policy(provider="gemini", role="implementation", profile=None)
    after = sorted(p.relative_to(_SCRIPTS_DIR) for p in _SCRIPTS_DIR.rglob("*"))

    assert exit_code == 0
    assert payload["ok"] is True
    assert before == after, (
        "model-policy must not write bytecode cache (or any other file) into "
        f"scripts/: before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# Issue #1695 PR review Blocker 2: control-flow order matches runtime dispatch.
# ---------------------------------------------------------------------------


def test_model_policy_agy_does_not_load_broken_gemini_routing(tmp_path):
    """GIVEN a broken model_routing.yaml
    WHEN --provider agy is inspected
    THEN it still succeeds -- AGY never loads routing config at runtime
    (_run_delegation_core() dispatches provider="agy" entirely before
    Gemini validation/routing), so the inspector must not either."""
    br = load_build_request()

    bad_config = tmp_path / "model_routing.yaml"
    bad_config.write_text("default_chain: {not_a_list: true}\n", encoding="utf-8")

    payload, exit_code = br.build_model_policy(
        provider="agy", role=None, profile=None, config_path=bad_config
    )
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["failure_class"] is None
    assert payload["actual_model"] is None
    assert payload["legacy_compatibility_label"] == "agy-default"


def test_model_policy_auto_ineligible_profile_short_circuits_routing(tmp_path):
    """GIVEN --provider auto with a tool_profile outside
    PROVIDER_AUTO_ELIGIBLE_PROFILES, and a broken model_routing.yaml
    WHEN model-policy inspects it
    THEN it still succeeds with profile_eligible=False -- matching
    provider_auto_dispatch()'s own stop-condition, which makes no provider
    attempt (and therefore never loads routing) for an ineligible profile."""
    br = load_build_request()

    bad_config = tmp_path / "model_routing.yaml"
    bad_config.write_text("default_chain: {not_a_list: true}\n", encoding="utf-8")

    payload, exit_code = br.build_model_policy(
        provider="auto", role=None, profile="grounded_research", config_path=bad_config
    )
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["failure_class"] is None
    assert payload["profile_eligible"] is False
    assert payload["provider_candidates"] is None
    assert payload["consumer_constraints"] is None


def test_model_policy_auto_ineligible_profile_short_circuits_unknown_role(tmp_path):
    """GIVEN --provider auto with a tool_profile outside
    PROVIDER_AUTO_ELIGIBLE_PROFILES, and an unknown --role
    WHEN model-policy inspects it
    THEN it still succeeds with profile_eligible=False -- the unknown role
    is never resolved because no provider attempt is made at all for an
    ineligible profile (matching provider_auto_dispatch())."""
    br = load_build_request()

    payload, exit_code = br.build_model_policy(
        provider="auto", role="no_such_role", profile="grounded_research"
    )
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["failure_class"] is None
    assert payload["profile_eligible"] is False


# ---------------------------------------------------------------------------
# Issue #1695 PR review Blocker 3: closed-schema validation (exact keys, no
# unknown fields, discriminated variants of delegation_model_policy/v1).
# ---------------------------------------------------------------------------

_BASE_KEYS = {"schema", "provider", "role", "profile", "ok", "failure_class", "failure_reason"}
_GEMINI_SUCCESS_KEYS = _BASE_KEYS | {"resolved_chain", "actual_model", "resolver_source"}
_RESOLVE_FAILURE_KEYS = _BASE_KEYS
_CONFIG_INVALID_KEYS = _BASE_KEYS | {"reason_code"}
_AGY_KEYS = _BASE_KEYS | {
    "resolved_chain",
    "configured_chain",
    "actual_model",
    "legacy_compatibility_label",
    "wrapper_capability",
    "upstream_capability",
    "readiness_checked",
    "credentials_checked",
    "provider_available",
}
_AGY_KEYS_WITH_ROLE = _AGY_KEYS | {"role_applied", "role_note"}
_AUTO_EARLY_FAILURE_KEYS = _BASE_KEYS
_AUTO_INELIGIBLE_KEYS = _BASE_KEYS | {
    "runtime_order",
    "profile_eligible",
    "provider_candidates",
    "consumer_constraints",
}
_AUTO_ELIGIBLE_SUCCESS_KEYS = _AUTO_INELIGIBLE_KEYS


def _assert_exact_keys(payload: dict, expected_keys: set) -> None:
    actual_keys = set(payload.keys())
    assert actual_keys == expected_keys, (
        f"unexpected key set: extra={actual_keys - expected_keys}, "
        f"missing={expected_keys - actual_keys}"
    )


def test_model_policy_closed_schema_rejects_unknown_fields(tmp_path):
    """GIVEN each delegation_model_policy/v1 payload variant
    WHEN its key set is compared against the variant's exact closed schema
    THEN no unknown/extra keys are present and no expected key is missing."""
    br = load_build_request()

    gemini_ok, _ = br.build_model_policy(provider="gemini", role="implementation", profile=None)
    _assert_exact_keys(gemini_ok, _GEMINI_SUCCESS_KEYS)

    gemini_unknown_role, _ = br.build_model_policy(provider="gemini", role="no_such_role", profile=None)
    _assert_exact_keys(gemini_unknown_role, _RESOLVE_FAILURE_KEYS)

    bad_config = tmp_path / "model_routing.yaml"
    bad_config.write_text("default_chain: {not_a_list: true}\n", encoding="utf-8")
    config_invalid, _ = br.build_model_policy(
        provider="gemini", role=None, profile=None, config_path=bad_config
    )
    _assert_exact_keys(config_invalid, _CONFIG_INVALID_KEYS)

    agy_no_role, _ = br.build_model_policy(provider="agy", role=None, profile=None)
    _assert_exact_keys(agy_no_role, _AGY_KEYS)

    agy_with_role, _ = br.build_model_policy(provider="agy", role="implementation", profile=None)
    _assert_exact_keys(agy_with_role, _AGY_KEYS_WITH_ROLE)

    auto_missing_profile, _ = br.build_model_policy(provider="auto", role=None, profile=None)
    _assert_exact_keys(auto_missing_profile, _AUTO_EARLY_FAILURE_KEYS)

    auto_ineligible, _ = br.build_model_policy(provider="auto", role=None, profile="grounded_research")
    _assert_exact_keys(auto_ineligible, _AUTO_INELIGIBLE_KEYS)

    auto_eligible, _ = br.build_model_policy(provider="auto", role=None, profile="no_tools")
    _assert_exact_keys(auto_eligible, _AUTO_ELIGIBLE_SUCCESS_KEYS)
    for candidate in auto_eligible["provider_candidates"]:
        if candidate["provider"] == "gemini":
            assert set(candidate.keys()) == {"provider", "resolved_chain", "actual_model"}
        elif candidate["provider"] == "agy":
            assert set(candidate.keys()) == _AGY_KEYS - _BASE_KEYS | {"provider"}

    invalid_provider, _ = br.build_model_policy(provider="not_a_real_provider", role=None, profile=None)
    _assert_exact_keys(invalid_provider, _BASE_KEYS)


# ---------------------------------------------------------------------------
# Major 1: argparse usage errors stay inside the JSON contract.
# ---------------------------------------------------------------------------


def test_model_policy_cli_invalid_provider_returns_json_failure_not_argparse_usage_text():
    """GIVEN build_request.py model-policy --provider <invalid>
    WHEN argparse would normally print usage text to stderr and exit 2
    THEN stdout instead carries a delegation_model_policy/v1 JSON failure
    payload and the process exits 1."""
    result = subprocess.run(
        [sys.executable, str(_BUILD_REQUEST_PATH), "model-policy", "--provider", "not-a-real-provider"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, f"expected exit 1, got {result.returncode}: {result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["schema"] == "delegation_model_policy/v1"
    assert payload["ok"] is False
    assert payload["failure_class"] == "invalid_cli_arguments"


def test_model_policy_cli_missing_required_provider_returns_json_failure():
    """GIVEN build_request.py model-policy invoked without --provider
    WHEN the required argument is missing
    THEN stdout still carries a JSON failure payload (not argparse usage
    text) and the process exits 1."""
    result = subprocess.run(
        [sys.executable, str(_BUILD_REQUEST_PATH), "model-policy"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, f"expected exit 1, got {result.returncode}: {result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["schema"] == "delegation_model_policy/v1"
    assert payload["ok"] is False
    assert payload["failure_class"] == "invalid_cli_arguments"


# ---------------------------------------------------------------------------
# Major 2: fan_out reason_code is imported from run_gemini_headless.py, not
# a hardcoded literal duplicated in build_request.py.
# ---------------------------------------------------------------------------


def test_model_policy_auto_fan_out_reason_code_matches_runtime_constant():
    """GIVEN --provider auto with an eligible profile
    WHEN consumer_constraints.fan_out is inspected
    THEN its reason_code is read from run_gemini_headless.py's own exported
    constant, not a separately hand-written literal in build_request.py."""
    br = load_build_request()
    rgh = load_run_gemini_headless()

    payload, exit_code = br.build_model_policy(provider="auto", role=None, profile="proposal_only")
    assert exit_code == 0
    fan_out = payload["consumer_constraints"]["fan_out"]
    assert fan_out["supported"] is False
    assert fan_out["reason_code"] == rgh.PROVIDER_AUTO_FAN_OUT_UNSUPPORTED_REASON_CODE
    assert isinstance(fan_out["reason_code"], str) and fan_out["reason_code"]


# ---------------------------------------------------------------------------
# Major 3: AGY upstream_capability / readiness fields.
# ---------------------------------------------------------------------------


def test_model_policy_agy_upstream_capability_reports_readiness_and_version_fields():
    """GIVEN --provider agy
    WHEN model-policy inspects it
    THEN upstream_capability distinguishes "wrapper does not document
    upstream explicit-model-selection support" from "we never probed the
    installed CLI version", and top-level readiness/credentials/
    provider_available fields make explicit that no live probe occurred."""
    br = load_build_request()

    payload, exit_code = br.build_model_policy(provider="agy", role=None, profile=None)
    assert exit_code == 0

    upstream = payload["upstream_capability"]
    assert upstream["probed"] is False
    assert upstream["documented_explicit_model_selection"] is False
    assert upstream["installed_version"] is None
    assert upstream["installed_version_probed"] is False

    assert payload["configured_chain"] is None
    assert payload["readiness_checked"] is False
    assert payload["credentials_checked"] is False
    assert payload["provider_available"] is None


# ---------------------------------------------------------------------------
# Minor: invalid provider via the Python API (not just the CLI) fails closed.
# ---------------------------------------------------------------------------


def test_model_policy_invalid_provider_via_python_api_fails_closed():
    """GIVEN build_model_policy() is called directly with a provider value
    outside MODEL_POLICY_PROVIDERS (bypassing argparse's `choices`)
    WHEN it runs
    THEN it fails closed with a stable failure_class instead of falling
    through to the auto-dispatch branch."""
    br = load_build_request()

    payload, exit_code = br.build_model_policy(provider="not_a_real_provider", role=None, profile=None)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["failure_class"] == "invalid_provider"
    assert payload["provider"] == "not_a_real_provider"

"""Issue #1979 AC6: paired artifact schema fields (digests, identity, binding)."""
# ruff: noqa: E501

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

RUNNER = Path(__file__).parents[1] / "scripts" / "run_agy_permission_boundary_e2e.py"
SPEC = importlib.util.spec_from_file_location("agy_permission_boundary_runner_schema_test", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_schema_still_declares_additional_properties_false_everywhere() -> None:
    schema = json.loads(MODULE.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    for name in ("runner", "artifact", "matrix", "diagnostic_ledger", "fallback", "failure_taxonomy", "cleanup", "secret_scan", "capability_gate", "mcp", "pairing"):
        assert schema["properties"][name]["additionalProperties"] is False
    assert schema["properties"]["runner"]["properties"]["binary_identity"]["additionalProperties"] is False


def test_unavailable_artifact_carries_the_new_fields_and_is_schema_valid() -> None:
    artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="grounded_research")
    schema = json.loads(MODULE.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(artifact)) == []

    assert artifact["capability_gate"]["bootstrap_predicate"] == "pre_invocation_ephemeral_message_injection"
    assert artifact["capability_gate"]["status"] in {"supported", "unsupported", "unavailable", "inconclusive", "evidence_invalid"}
    assert artifact["mcp"] == {
        "status": "unsupported_by_design",
        "completion_blocker": False,
        "reason": artifact["mcp"]["reason"],
    }
    assert artifact["matrix"]["tool_inventory_digest"].startswith("sha256:")
    identity = artifact["runner"]["binary_identity"]
    assert set(identity) == {"realpath", "sha256", "size", "mtime_ns", "platform", "arch"}
    assert artifact["pairing"]["role"] == "allow"
    assert artifact["pairing"]["counterpart_role"] == "deny"
    assert artifact["pairing"]["bound"] is False
    assert artifact["pairing"]["counterpart_digest"] is None
    assert artifact["cleanup"]["process_group_isolated"] is True
    assert artifact["cleanup"]["descendant_processes_absent"] is True


def test_tool_inventory_digest_is_deterministic_and_bound_to_attempt_specs() -> None:
    first = MODULE._tool_inventory_digest()
    second = MODULE._tool_inventory_digest()
    assert first == second
    names = sorted(tool_name for tool_name, _ in MODULE.ATTEMPT_SPECS.values())
    assert first == MODULE._sha256(MODULE._canonical_json(names))


def test_pairing_binds_allow_and_deny_artifacts_when_siblings_exist(tmp_path: Path) -> None:
    allow_dir = tmp_path / "allow"
    deny_dir = tmp_path / "deny"
    allow_dir.mkdir()
    deny_dir.mkdir()

    deny_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="no_tools", artifact_dir=deny_dir)
    MODULE._write_artifact(deny_dir, deny_artifact)

    allow_binding = MODULE._pairing_binding("grounded_research", allow_dir)
    assert allow_binding["role"] == "allow"
    assert allow_binding["counterpart_role"] == "deny"
    assert allow_binding["bound"] is True
    assert allow_binding["counterpart_digest"] == deny_artifact["artifact"]["digest"]


def test_pairing_is_unbound_when_counterpart_directory_is_absent(tmp_path: Path) -> None:
    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()
    binding = MODULE._pairing_binding("grounded_research", allow_dir)
    assert binding["bound"] is False
    assert binding["counterpart_digest"] is None


def test_binary_identity_none_for_missing_executable() -> None:
    identity = MODULE._binary_identity(None)
    assert identity["realpath"] is None
    assert identity["sha256"] is None
    assert identity["size"] is None
    assert identity["mtime_ns"] is None
    assert isinstance(identity["platform"], str)
    assert isinstance(identity["arch"], str)


@pytest.mark.parametrize("profile,expected_role", [("no_tools", "deny"), ("proposal_only", "n/a"), ("local_asset_research", "n/a")])
def test_pairing_role_by_profile(tmp_path: Path, profile: str, expected_role: str) -> None:
    binding = MODULE._pairing_binding(profile, tmp_path)
    assert binding["role"] == expected_role


# Issue #1979 fix_delta blocker_6: `agy_permission_boundary_e2e/v1` stays at
# its v1 id, but `capability_gate`/`mcp`/`pairing`/`binary_identity`/
# `tool_inventory_digest`/the two new `cleanup` fields are additive and
# optional -- a pre-#1979 (base `main`) shaped artifact must still validate.
_OLD_V1_SHAPE_ARTIFACT: dict = {
    "schema": "agy_permission_boundary_e2e/v1",
    "generated_at": "2026-01-01T00:00:00+00:00",
    "runner": {
        "identity": "run_agy_permission_boundary_e2e",
        "exit_code": 77,
        "actual_agy_executed": False,
        "identity_verified": False,
        "executable_ref": "unavailable",
        "executable_version": "unavailable",
        "binary_digest": "sha256:" + "0" * 64,
        "child_returncode": None,
        "artifact_digest": "sha256:" + "1" * 64,
    },
    "artifact": {"digest": "sha256:" + "1" * 64},
    "matrix": {"profile": "no_tools", "capabilities": ["command", "write", "read", "network"]},
    "attempts": [
        {
            "correlation": {
                "run_id": "unavailable",
                "conversation_id": "unavailable",
                "step_index": index,
                "tool_name": tool_name,
                "args_digest": "sha256:" + "0" * 64,
                "profile": "no_tools",
                "capability": capability,
                "canary_id": "unavailable",
            },
            "expectation": "deny",
            "predicates": {
                "deterministic_attempt_present": False,
                "pre_tool_use_present": False,
                "decision_matches_expectation": False,
                "post_tool_use_matches_expectation": False,
                "side_effect_matches_expectation": False,
                "same_attempt_correlation": False,
                "logger_failure_absent": False,
            },
        }
        for index, (capability, tool_name) in enumerate(
            [("command", "run_command"), ("write", "write_to_file"), ("read", "view_file"), ("network", "read_url_content")]
        )
    ],
    "diagnostic_ledger": {
        "pre_invocation_hook_started": False,
        "pre_invocation_context_accepted": False,
        "injected_step_count": 0,
        "enforcement_event_count": 0,
        "pre_tool_use_event_count": 0,
        "post_tool_use_event_count": 0,
        "raw_payload_persisted": False,
    },
    "fallback": {"used": False},
    "failure_taxonomy": {"class": "agy_permission_boundary_unavailable", "completion": False, "retry": "restore_runtime"},
    "cleanup": {"temporary_processes_removed": True, "loopback_servers_stopped": True},
    "secret_scan": {"clean": True},
}


def test_pre_1979_v1_shaped_artifact_without_new_fields_remains_schema_valid() -> None:
    """Issue #1979 fix_delta blocker_6: old-shape (pre-capability-gate) v1
    artifacts, lacking `capability_gate`/`mcp`/`pairing`/`binary_identity`/
    `tool_inventory_digest`/the two new `cleanup` fields entirely, must still
    validate against the schema this PR ships -- this is the documented
    compatibility stance (keep v1 id, make the additive fields optional)
    chosen instead of a v2 id bump.
    """
    schema = json.loads(MODULE.SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(_OLD_V1_SHAPE_ARTIFACT))
    assert errors == [], [error.message for error in errors]


def test_producer_still_always_emits_the_additive_fields() -> None:
    """The optional-field compatibility stance is schema-level only -- this
    runner's own producer never regresses to omitting the new evidence.
    """
    artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="no_tools")
    for key in ("capability_gate", "mcp", "pairing", "attempt_method", "prompt_compliance"):
        assert key in artifact
    assert "binary_identity" in artifact["runner"]
    assert "tool_inventory_digest" in artifact["matrix"]
    assert "process_group_isolated" in artifact["cleanup"]
    assert "descendant_processes_absent" in artifact["cleanup"]
    assert artifact["attempt_method"] == "ephemeral_message_prompt"
    assert artifact["prompt_compliance"] == {}


# Issue #1979 fix_delta blocker_4: aggregate manifest is a non-self-referential
# allow/deny binding computed only after both individual artifacts finalize.


def test_build_and_validate_aggregate_manifest_round_trips(tmp_path: Path) -> None:
    allow_dir, deny_dir = tmp_path / "allow", tmp_path / "deny"
    allow_dir.mkdir()
    deny_dir.mkdir()
    allow_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="grounded_research", artifact_dir=allow_dir)
    deny_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="no_tools", artifact_dir=deny_dir)
    manifest = MODULE.build_aggregate_manifest(allow_artifact, deny_artifact)
    assert manifest["schema"] == MODULE.AGGREGATE_SCHEMA
    valid, reason = MODULE.validate_aggregate_manifest(manifest, allow_artifact, deny_artifact)
    assert (valid, reason) == (True, "valid")


def test_validate_aggregate_manifest_rejects_digest_tamper(tmp_path: Path) -> None:
    allow_dir, deny_dir = tmp_path / "allow", tmp_path / "deny"
    allow_dir.mkdir()
    deny_dir.mkdir()
    allow_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="grounded_research", artifact_dir=allow_dir)
    deny_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="no_tools", artifact_dir=deny_dir)
    manifest = MODULE.build_aggregate_manifest(allow_artifact, deny_artifact)
    manifest["allow"]["artifact_digest"] = "sha256:" + "9" * 64
    valid, reason = MODULE.validate_aggregate_manifest(manifest, allow_artifact, deny_artifact)
    assert valid is False
    assert reason == "aggregate_allow_digest_mismatch"


def test_validate_aggregate_manifest_requires_both_sides_supported_for_pass() -> None:
    allow_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="grounded_research")
    deny_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="no_tools")
    allow_artifact["runner"]["exit_code"] = MODULE.EXIT_PASS
    # Recompute the digest after the mutation above so the rehash check does
    # not mask the `aggregate_pass_requires_both_sides` invariant this test
    # targets -- this fixture never claims a genuinely schema-valid PASS
    # artifact (that requires the full pass-invariant set), only that the
    # aggregate validator's pass-symmetry check fires independently.
    allow_artifact["artifact"]["digest"] = MODULE._artifact_digest(allow_artifact)
    allow_artifact["runner"]["artifact_digest"] = allow_artifact["artifact"]["digest"]
    manifest = MODULE.build_aggregate_manifest(allow_artifact, deny_artifact)
    valid, reason = MODULE.validate_aggregate_manifest(manifest, allow_artifact, deny_artifact)
    assert valid is False
    assert reason == "aggregate_pass_requires_both_sides"


def test_maybe_write_aggregate_manifest_is_best_effort_when_counterpart_absent(tmp_path: Path) -> None:
    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()
    allow_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="grounded_research", artifact_dir=allow_dir)
    MODULE._maybe_write_aggregate_manifest(allow_dir, allow_artifact)
    assert not (tmp_path / "aggregate" / "manifest.json").exists()


def test_maybe_write_aggregate_manifest_writes_once_both_sides_exist(tmp_path: Path) -> None:
    allow_dir, deny_dir = tmp_path / "allow", tmp_path / "deny"
    allow_dir.mkdir()
    deny_dir.mkdir()
    allow_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="grounded_research", artifact_dir=allow_dir)
    deny_artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="no_tools", artifact_dir=deny_dir)
    MODULE._write_artifact(deny_dir, deny_artifact)
    MODULE._maybe_write_aggregate_manifest(allow_dir, allow_artifact)
    manifest_path = tmp_path / "aggregate" / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    valid, reason = MODULE.validate_aggregate_manifest(manifest, allow_artifact, deny_artifact)
    assert (valid, reason) == (True, "valid")

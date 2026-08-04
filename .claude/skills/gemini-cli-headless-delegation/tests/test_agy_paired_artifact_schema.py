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

    assert artifact["capability_gate"]["bootstrap_predicate"] == "pre_invocation_injected_tool_call"
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

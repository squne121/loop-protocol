"""Regression coverage for canonical preflight runtime/schema recovery (#1037)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import jsonschema

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as wrapper  # noqa: E402
import validate_issue_execution_decision as decision_validator  # noqa: E402


def _snapshot() -> dict:
    return {"schema_version": "raw_issue_snapshot/v1", "fetched_at": "2026-07-26T00:00:00+00:00", "issue_number": 1037, "repo": "squne121/loop-protocol", "issue": {"number": 1037, "body": "test", "title": "test", "labels": []}, "comments": []}


def _planner_input() -> dict:
    return {"schema_version": "refinement_loop_planner_input/v1", "issue": {"number": 1037}}


def test_issue_execution_decision_schema_id_is_absolute_and_portable():
    schema = json.loads((SCHEMAS_DIR / "issue_execution_decision_v1.schema.json").read_text(encoding="utf-8"))
    assert schema["$id"].startswith("urn:")
    assert "#" not in schema["$id"]
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema)


def test_validate_schema_never_raises_for_reference_resolution_failures():
    broken_schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:loop-protocol:broken-validator-test:v1", "$ref": "urn:loop-protocol:missing-resource:v1"}
    with mock.patch.object(decision_validator, "_load_schema", return_value=broken_schema):
        violations = decision_validator.validate_schema({"anything": "value"})
    assert len(violations) == 1
    assert violations[0].startswith("schema_validation_error:")


def test_planner_exit_3_emits_schema_valid_fallback_result(tmp_path, capsys):
    fixture = {"schema_version": "refinement_preflight_input/v1", "issue_number": 1037, "repo": "squne121/loop-protocol", "now": "2026-07-26T00:00:00+00:00", "issue": {"number": 1037, "body": "test", "title": "test", "labels": []}, "comments": [], "anchor_comment_urls": []}
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    with (mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path), mock.patch.object(wrapper, "_invoke_planner", return_value=(None, 3, "internal", ""))):
        result, exit_code = wrapper.run_preflight(issue_number=1037, repo="squne121/loop-protocol", anchor_comment_urls=[], fixture_path=fixture_path)
    assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE
    assert result["planner_fail_closed"] is True
    assert result["rewrite_constraints"]["schema_version"] == "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1"
    assert wrapper._validate_result_artifact(result) == []
    assert "STATUS: environment_failure" in capsys.readouterr().out


def test_failure_artifact_is_readback_schema_valid_before_reporting(tmp_path):
    result, _ = wrapper._emit_failure_result(repo_root=tmp_path, issue_number=1037, repo="squne121/loop-protocol", status="environment_failure", next_action="fix_environment", blockers=[wrapper.BLOCKER_PLANNER_INTERNAL_ERROR], planner_exit_code=3, planner_fail_closed=True, planner_fail_closed_reason_codes=[wrapper.BLOCKER_PLANNER_INTERNAL_ERROR], required_sections=[], required_contract_keys=[], rewrite_constraints=wrapper._build_safe_rewrite_constraints([], []), planner_input=_planner_input(), raw_snapshot=_snapshot())
    artifact = Path(result["artifacts"]["refinement_preflight_result_v1"])
    readback = json.loads(artifact.read_text(encoding="utf-8"))
    assert readback == result
    assert wrapper._validate_result_artifact(readback) == []


def test_preflight_provenance_records_interpreter_and_dependency_versions(tmp_path):
    provenance = wrapper.build_provenance(repo="squne121/loop-protocol", issue_number=1037, anchor_comment_url="", planner_input=_planner_input(), raw_snapshot=_snapshot(), wrapper_exit_code=3, wrapper_status="environment_failure", blockers=[wrapper.BLOCKER_PLANNER_INTERNAL_ERROR], stderr="bounded error", repo_root=tmp_path)
    assert provenance["python_executable"] == sys.executable
    assert isinstance(provenance["python_version"], str)
    assert set(provenance["dependency_versions"]) == {"jsonschema", "referencing"}
    assert all(isinstance(version, str) and version for version in provenance["dependency_versions"].values())

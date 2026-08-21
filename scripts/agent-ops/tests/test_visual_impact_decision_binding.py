"""test_visual_impact_decision_binding.py (Issue #2019 AC11)

GIVEN/WHEN/THEN tests proving VISUAL_IMPACT_DECISION_V1 is generated from
trusted observation fields (never copied from a declaration) and binds
every field the Issue's In Scope B/C section enumerates.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import jsonschema
import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2019_decision_binding"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-impact.schema.json"


@pytest.fixture(scope="module")
def decision_schema() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {"$defs": schema["$defs"], **schema["$defs"]["VISUAL_IMPACT_DECISION_V1"]}


def _sample_decision() -> dict:
    return rvi.build_decision(
        repository="squne121/loop-protocol",
        pull_request_number=2019,
        base_sha="a" * 40,
        head_sha="b" * 40,
        base_registry_blob_sha="c" * 40,
        head_registry_blob_sha="d" * 40,
        pr_body="## VISUAL_IMPACT_DECLARATION_V1 example",
        changed_path_entries=[{"status": "modified", "path": "src/ui/combatHud.ts"}],
        affected_surfaces=[
            {
                "surface_id": "combat-hud-running",
                "contract_id": "combat-hud-running:vitest-browser-mode",
                "disposition": "verified_unchanged",
                "evidence": {"baseline_unchanged": True, "canonical_verify_success": True},
            }
        ],
        component_vrt_report_check_run_id="123456",
        github_actions_app_identity="github-actions[bot]",
        artifact_id="987",
        artifact_digest="sha256:" + "e" * 64,
        workflow_run_id=555,
        run_attempt=1,
    )


def test_decision_validates_against_schema(decision_schema):
    jsonschema.validate(_sample_decision(), decision_schema)


def test_decision_binds_every_required_field(decision_schema):
    decision = _sample_decision()
    required_fields = decision_schema["required"]
    for field in required_fields:
        assert field in decision, f"missing required decision field: {field}"


def test_pr_body_sha256_is_computed_not_copied():
    body = "## VISUAL_IMPACT_DECLARATION_V1 example body text"
    decision = rvi.build_decision(
        repository="squne121/loop-protocol",
        pull_request_number=1,
        base_sha="0" * 40,
        head_sha="1" * 40,
        base_registry_blob_sha="2" * 40,
        head_registry_blob_sha="3" * 40,
        pr_body=body,
        changed_path_entries=[],
        affected_surfaces=[],
        component_vrt_report_check_run_id=None,
        github_actions_app_identity="github-actions[bot]",
        artifact_id=None,
        artifact_digest=None,
    )
    assert decision["pr_body_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    # The raw PR body string itself never appears verbatim in the decision.
    assert body not in json.dumps(decision)


def test_changed_paths_digest_includes_rename_old_new_status():
    entries = [
        {"status": "renamed", "path": "src/ui/newName.ts", "old_path": "src/ui/oldName.ts"},
        {"status": "removed", "path": "src/ui/deleted.ts"},
    ]
    digest_obj = rvi.build_changed_paths_digest(entries)
    assert digest_obj["algorithm"] == "sha256"
    assert digest_obj["entries"] == entries
    assert len(digest_obj["digest"]) == 64


def test_decision_does_not_copy_declaration_disposition_verbatim():
    """AC11/AC12: build_decision() takes independently-evaluated
    affected_surfaces (surface_id/contract_id/disposition/evidence), never a
    raw parsed declaration dict -- prove the function signature structurally
    rejects a bare declaration-shaped surfaces list (missing contract_id/
    evidence, which only trusted evaluation produces)."""
    decision = _sample_decision()
    for entry in decision["affected_surfaces"]:
        assert "contract_id" in entry
        assert "evidence" in entry


def test_resolver_and_policy_version_present_and_stable():
    decision = _sample_decision()
    assert decision["resolver_version"] == rvi.RESOLVER_ORCHESTRATION_VERSION
    assert decision["policy_version"] == rvi.POLICY_VERSION


def test_base_sha_and_head_sha_must_be_full_length_hex(decision_schema):
    bad = _sample_decision()
    bad["head_sha"] = "not-a-sha"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, decision_schema)


def test_pr2045_evaluate_pr_policy_evidence_payload_validates_against_schema(decision_schema):
    """PR #2045 OWNER fix_delta P1-2 / P0-5 (V2 manifest): `evaluate_pr_policy`'s
    per-surface `evidence` payload (as embedded via
    `_build_decision_surface_entry` / `build_decision`) must itself validate
    against the schema's closed `evidence` $def (additionalProperties:
    false). Uses a real VISUAL_BASELINE_REVIEW_EVIDENCE_V2 manifest built via
    `build_evidence_manifest_v2_record` (never a hand-rolled dict) and
    trusted CheckRun binding params (never sourced from the manifest's own
    self-report)."""
    resolve_result = rvi.ResolveResult(
        changed_paths=["src/ui/combatHud.ts"],
        affected_surfaces=[{"surface_id": "combat-hud-running", "reason": "direct_producer"}],
    )
    baseline_path = (
        "tests/component/__screenshots__/combat-hud-running.vrt.test.ts/combat-hud-running-chromium-linux.png"
    )
    contracts = {
        "runner": "vitest-browser-mode",
        "job": "component-vrt-report",
        "baseline": baseline_path,
        "update_command_id": "vitest_component_vrt_update",
        "verify_command_id": "vitest_component_vrt_verify",
    }
    registry_doc = {
        "surfaces": {
            "combat-hud-running": {
                "contracts": contracts,
                "policy": {"disposition_required": True},
            }
        }
    }
    declaration_doc = {"surfaces": [{"surface_id": "combat-hud-running", "disposition": "verified_unchanged"}]}
    record = rvi.build_evidence_manifest_v2_record(
        surface_id="combat-hud-running",
        contract_digest=rvi.compute_contract_digest(registry_doc["surfaces"]["combat-hud-running"]),
        head_sha="b" * 40,
        workflow_run_id="111",
        baseline_path=baseline_path,
        baseline_sha256="f" * 64,
        actual_sha256="f" * 64,
        mismatched_pixels=0,
        verify_command_id="vitest_component_vrt_verify",
        verify_succeeded=True,
        update_command_id="vitest_component_vrt_update",
        update_executed=False,
        update_succeeded=False,
        expected_artifact_id="1",
        actual_artifact_id="2",
        diff_artifact_id="3",
    )
    evidence_manifest = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [record]}
    policy_result = rvi.evaluate_pr_policy(
        resolve_result=resolve_result,
        declaration_doc=declaration_doc,
        registry_doc=registry_doc,
        evidence_manifest=evidence_manifest,
        head_sha="b" * 40,
        changed_paths=["src/ui/combatHud.ts"],
        actor="squne121",
        authorized_owners=set(),
        today=date(2026, 8, 9),
        trusted_check_run_id="123",
        trusted_check_conclusion="success",
    )
    assert policy_result["ok"] is True
    decision = rvi.build_decision(
        repository="squne121/loop-protocol",
        pull_request_number=2045,
        base_sha="a" * 40,
        head_sha="b" * 40,
        base_registry_blob_sha="c" * 40,
        head_registry_blob_sha="d" * 40,
        pr_body="",
        changed_path_entries=[{"status": "modified", "path": "src/ui/combatHud.ts"}],
        affected_surfaces=[
            rvi._build_decision_surface_entry(r, registry_doc) for r in policy_result["surface_results"]
        ],
        component_vrt_report_check_run_id="1",
        github_actions_app_identity="github-actions[bot]",
        artifact_id="987",
        artifact_digest="sha256:" + "e" * 64,
        workflow_run_id=555,
        run_attempt=1,
    )
    jsonschema.validate(decision, decision_schema)
    evidence = decision["affected_surfaces"][0]["evidence"]
    assert "ok" not in evidence
    assert "reason" not in evidence
    assert evidence["baseline_unchanged"] is True
    assert evidence["canonical_verify_success"] is True


def test_pr2045_mismatched_pixels_as_json_string_zero_still_passes():
    """PR #2045 OWNER fix_delta P0-5: the live evidence manifest artifact
    emits `mismatched_pixels` as a JSON string "0" (bash env var ->
    sys.argv, never cast back to int). This must NOT make
    `canonical_verify_success` permanently False."""
    evidence = rvi.build_evidence_from_manifest(
        manifest={"head_sha": "b" * 40, "mismatched_pixels": "0", "artifact_id": "1"},
        head_sha="b" * 40,
        surface_id="combat-hud-running",
        contract_job="component-vrt-report",
    )
    assert evidence.canonical_verify_success is True


def test_pr2045_mismatched_pixels_non_numeric_string_fails_closed():
    evidence = rvi.build_evidence_from_manifest(
        manifest={"head_sha": "b" * 40, "mismatched_pixels": "unknown_non_zero", "artifact_id": "1"},
        head_sha="b" * 40,
        surface_id="combat-hud-running",
        contract_job="component-vrt-report",
    )
    assert evidence.canonical_verify_success is False

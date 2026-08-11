"""Issue #2019 AC30-AC37 (P0-6 trust boundary, Scope Delta 2026-08-09).

Tests the `--mode verify-trusted-artifact` CLI surface / `verify_trusted_artifact()`
of `resolve_visual_impact.py` (invoked by the `workflow_run`-triggered
`.github/workflows/visual-impact-trusted-consumer.yml`) and statically
verifies that workflow file never checks out or executes candidate PR head
code (AC36).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2019_trusted_consumer"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

REPO_ROOT = Path(__file__).resolve().parents[3]
VISUAL_IMPACT_SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-impact.schema.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "visual-impact-trusted-consumer.yml"

EXPECTED_REPOSITORY = "squne121/loop-protocol"
EXPECTED_PR_NUMBER = 2019
EXPECTED_HEAD_SHA = "b" * 40
SURFACE_ID = "combat-hud-running"


def _sample_manifest_record() -> dict:
    return rvi.build_evidence_manifest_v2_record(
        surface_id=SURFACE_ID,
        contract_digest="c" * 64,
        head_sha=EXPECTED_HEAD_SHA,
        workflow_run_id="123",
        check_run_id="456",
        check_suite_id="789",
        github_app_id="15368",
        github_app_slug="github-actions",
        check_conclusion="success",
        baseline_path="tests/component/__screenshots__/combat-hud-running.png",
        baseline_sha256="d" * 64,
        actual_sha256="d" * 64,
        mismatched_pixels=0,
        verify_command_id="vitest_component_vrt_verify",
        verify_succeeded=True,
        update_command_id=None,
        update_executed=False,
        update_succeeded=None,
        expected_artifact_id="art-1",
        actual_artifact_id="art-1",
        diff_artifact_id="art-1",
    )


def _sample_manifest() -> dict:
    return {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [_sample_manifest_record()]}


def _sample_decision(*, head_sha: str = EXPECTED_HEAD_SHA, evidence_manifest_digest: str | None) -> dict:
    affected_surfaces = [
        {
            "surface_id": SURFACE_ID,
            "contract_id": "vrt-component",
            "disposition": "verified_unchanged",
            "evidence": ({"evidence_manifest_digest": evidence_manifest_digest} if evidence_manifest_digest else {}),
        }
    ]
    return rvi.build_decision(
        repository=EXPECTED_REPOSITORY,
        pull_request_number=EXPECTED_PR_NUMBER,
        base_sha="a" * 40,
        head_sha=head_sha,
        base_registry_blob_sha="c" * 40,
        head_registry_blob_sha="d" * 40,
        pr_body="dummy",
        changed_path_entries=[{"status": "modified", "path": "src/ui/combatHud.ts"}],
        affected_surfaces=affected_surfaces,
        component_vrt_report_check_run_id="456",
        github_actions_app_identity="github-actions[bot]",
        artifact_id="art-decision-1",
        artifact_digest="e" * 64,
    )


def _verify(
    decision: dict | None,
    manifest: dict | None,
    *,
    expected_head_sha: str = EXPECTED_HEAD_SHA,
    trusted_rederivation: "rvi.TrustedRederivation | None" = None,
):
    decision_raw = None if decision is None else json.dumps(decision).encode("utf-8")
    manifest_raw = None if manifest is None else json.dumps(manifest).encode("utf-8")
    return rvi.verify_trusted_artifact(
        decision_raw=decision_raw,
        evidence_manifest_raw=manifest_raw,
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=expected_head_sha,
        expected_repository=EXPECTED_REPOSITORY,
        expected_pr_number=EXPECTED_PR_NUMBER,
        trusted_rederivation=trusted_rederivation,
    )


def test_valid_artifact_passes():
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)
    verdict = _verify(decision, manifest)
    assert verdict.ok is True
    assert verdict.reason_codes == []


def test_schema_violation_additional_property_rejected():
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)
    decision["unexpected_extra_field"] = "not allowed by additionalProperties:false"
    verdict = _verify(decision, manifest)
    assert verdict.ok is False
    assert any("decision_schema_invalid" in code for code in verdict.reason_codes)


def test_schema_violation_missing_required_field_rejected():
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)
    del decision["artifact_digest"]
    verdict = _verify(decision, manifest)
    assert verdict.ok is False
    assert any("decision_schema_invalid" in code for code in verdict.reason_codes)


def test_digest_tamper_evidence_manifest_record_rejected():
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)
    # Tamper with the manifest record AFTER computing its digest -- the
    # record no longer self-verifies (manifest_sha256 was computed over the
    # ORIGINAL mismatched_pixels value).
    tampered_manifest = copy.deepcopy(manifest)
    tampered_manifest["surfaces"][0]["mismatched_pixels"] = 999
    verdict = _verify(decision, tampered_manifest)
    assert verdict.ok is False
    assert any("evidence_manifest_digest_tamper" in code for code in verdict.reason_codes)


def test_digest_tamper_decision_claim_does_not_match_manifest_record():
    manifest = _sample_manifest()
    decision = _sample_decision(evidence_manifest_digest="f" * 64)  # fabricated, self-consistent-looking digest
    verdict = _verify(decision, manifest)
    assert verdict.ok is False
    assert any("evidence_manifest_digest_mismatch" in code for code in verdict.reason_codes)


def test_forced_success_producer_still_fails_independent_verification():
    # Simulates a candidate PR that forced the `visual-impact-policy`
    # producer job step to `exit 0` / report success without the evidence
    # manifest ever actually being produced/uploaded with matching content:
    # the decision CLAIMS a digest, but no evidence manifest artifact
    # backs it (producer job reported "success" regardless).
    decision = _sample_decision(evidence_manifest_digest="a" * 64)
    verdict = _verify(decision, manifest=None)
    assert verdict.ok is False
    assert any("evidence_manifest_missing" in code for code in verdict.reason_codes)


def test_missing_artifact_never_silently_passes():
    verdict = _verify(decision=None, manifest=_sample_manifest())
    assert verdict.ok is False
    assert verdict.reason_codes == ["missing_decision_artifact"]


def test_stale_head_rejected():
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    stale_decision = _sample_decision(head_sha="c" * 40, evidence_manifest_digest=digest)
    # `expected_head_sha` simulates the LIVE current PR head fetched by the
    # trusted consumer workflow at run time, which has since moved past the
    # workflow_run event's (stale) head_sha baked into the decision.
    verdict = _verify(stale_decision, manifest, expected_head_sha=EXPECTED_HEAD_SHA)
    assert verdict.ok is False
    assert "head_sha_mismatch" in verdict.reason_codes


def test_repository_mismatch_rejected():
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)
    decision["repository"] = "someone-else/other-repo"
    verdict = _verify(decision, manifest)
    assert verdict.ok is False
    assert "repository_mismatch" in verdict.reason_codes


def test_pull_request_number_mismatch_rejected():
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)
    decision["pull_request_number"] = 4242
    verdict = _verify(decision, manifest)
    assert verdict.ok is False
    assert "pull_request_number_mismatch" in verdict.reason_codes


def test_oversized_decision_artifact_rejected():
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)
    raw = json.dumps(decision).encode("utf-8")
    verdict = rvi.verify_trusted_artifact(
        decision_raw=raw,
        evidence_manifest_raw=None,
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=EXPECTED_HEAD_SHA,
        expected_repository=EXPECTED_REPOSITORY,
        expected_pr_number=EXPECTED_PR_NUMBER,
        max_decision_bytes=len(raw) - 1,
    )
    assert verdict.ok is False
    assert verdict.reason_codes == ["decision_artifact_too_large"]


def test_malformed_json_rejected():
    verdict = rvi.verify_trusted_artifact(
        decision_raw=b"{not valid json",
        evidence_manifest_raw=None,
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=EXPECTED_HEAD_SHA,
        expected_repository=EXPECTED_REPOSITORY,
        expected_pr_number=EXPECTED_PR_NUMBER,
    )
    assert verdict.ok is False
    assert any(code.startswith("decision_not_json") for code in verdict.reason_codes)


# --- Issue #2091 AC1-AC5: trusted-side re-derivation adversarial fixtures ---


def _registry_doc(*, producer_paths: list[str], coverage_roots: list[str] | None = None) -> dict:
    return {
        "surfaces": {SURFACE_ID: {"producers": {"modules": producer_paths}}},
        "coverage_roots": coverage_roots or [],
    }


def _forged_decision(*, changed_path_entries: list[dict], affected_surfaces: list[dict]) -> dict:
    """A decision shaped exactly like Forgery 1 in Issue #2090/#2091:
    `component_vrt_report_check_run_id`/`artifact_id`/`artifact_digest` all
    `null`, no evidence trail at all -- the producer claims "nothing to
    verify"."""
    return rvi.build_decision(
        repository=EXPECTED_REPOSITORY,
        pull_request_number=EXPECTED_PR_NUMBER,
        base_sha="a" * 40,
        head_sha=EXPECTED_HEAD_SHA,
        base_registry_blob_sha="c" * 40,
        head_registry_blob_sha="d" * 40,
        pr_body="dummy",
        changed_path_entries=changed_path_entries,
        affected_surfaces=affected_surfaces,
        component_vrt_report_check_run_id=None,
        github_actions_app_identity="github-actions[bot]",
        artifact_id=None,
        artifact_digest=None,
    )


def test_affected_surfaces_empty_forgery_rejected():
    """Forgery 1: producer self-reports `affected_surfaces: []` even though
    a changed path is a REAL registered producer for a surface. Trusted-side
    re-derivation (base/head registry + independently-obtained changed
    paths) must catch this regardless of the empty evidence trail."""
    changed_entries = [{"status": "modified", "path": "src/ui/combatHud.ts"}]
    registry_doc = _registry_doc(producer_paths=["src/ui/combatHud.ts"])
    decision = _forged_decision(changed_path_entries=changed_entries, affected_surfaces=[])
    trusted = rvi.TrustedRederivation(
        changed_path_entries=changed_entries,
        base_registry_doc=registry_doc,
        head_registry_doc=registry_doc,
    )
    verdict = _verify(decision, manifest=None, trusted_rederivation=trusted)
    assert verdict.ok is False
    assert any(code.startswith("affected_surfaces_undercount") for code in verdict.reason_codes)


def test_evidence_empty_coherent_forgery_rejected():
    """Forgery 2: the producer's `affected_surfaces` surface_id is the
    CORRECT one, but its `evidence` object is an empty `{}` -- no digest
    claim at all. Must be rejected even with no `trusted_rederivation`
    supplied (pure schema/logic check, AC3)."""
    manifest = _sample_manifest()
    decision = _sample_decision(evidence_manifest_digest=None)
    assert decision["affected_surfaces"][0]["evidence"] == {}
    verdict = _verify(decision, manifest)
    assert verdict.ok is False
    assert any(code.startswith("evidence_digest_claim_missing") for code in verdict.reason_codes)


def test_missing_ref_object_fail_closed():
    """AC4: a ref whose commit object was never fetched locally (shallow
    checkout) must raise `MissingRefObjectError` -- never silently degrade
    to a synthetic empty registry the way a genuinely-missing FILE does."""
    all_zero_sha = "0" * 40
    with pytest.raises(rvi.MissingRefObjectError):
        rvi.load_registry_text(
            REPO_ROOT / "docs" / "dev" / "visual-surfaces.yml",
            all_zero_sha,
            REPO_ROOT,
        )


def test_missing_ref_object_never_silently_empty_registry_in_resolve():
    """AC4 integration: `resolve()` must fail closed (record an error, never
    silently proceed with an empty head registry) when `head_ref` names a
    commit object that cannot be resolved locally."""
    result = rvi.resolve(changed_paths=["docs/dev/visual-surfaces.yml"], head_ref="0" * 40)
    assert any("head registry invalid" in e for e in result.errors)
    assert result.affected_surfaces == []


def test_no_visual_impact_empty_surfaces_pass():
    """AC5 regression: a genuinely no-visual-impact PR (changed paths match
    no producer/contract/global-invalidator/coverage-root) legitimately
    reports `affected_surfaces: []` and PASSES -- trusted-side re-derivation
    must never turn a real empty set into a false rejection."""
    changed_entries = [{"status": "modified", "path": "docs/README.md"}]
    registry_doc = _registry_doc(producer_paths=["src/ui/combatHud.ts"], coverage_roots=["src/ui/**"])
    decision = _forged_decision(changed_path_entries=changed_entries, affected_surfaces=[])
    trusted = rvi.TrustedRederivation(
        changed_path_entries=changed_entries,
        base_registry_doc=registry_doc,
        head_registry_doc=registry_doc,
    )
    verdict = _verify(decision, manifest=None, trusted_rederivation=trusted)
    assert verdict.ok is True
    assert verdict.reason_codes == []


def test_stale_base_rejected():
    """A `base_sha` in the decision that no longer matches the PR's actual
    LIVE base (independently fetched by the trusted consumer) is rejected
    -- distinct from the existing stale-HEAD check."""
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)  # decision["base_sha"] == "a" * 40
    trusted = rvi.TrustedRederivation(expected_base_sha="f" * 40)
    verdict = _verify(decision, manifest, trusted_rederivation=trusted)
    assert verdict.ok is False
    assert "base_sha_mismatch" in verdict.reason_codes


def test_incomplete_changed_paths_rejected():
    """An incomplete changed-path set (e.g. a paginated files API hit its
    page-size cap, or a >3000-file PR) can never be trusted to prove the
    producer's `affected_surfaces` claim -- fail closed unconditionally."""
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)
    trusted = rvi.TrustedRederivation(changed_paths_complete=False)
    verdict = _verify(decision, manifest, trusted_rederivation=trusted)
    assert verdict.ok is False
    assert "changed_paths_incomplete_unknown" in verdict.reason_codes


def test_policy_version_mismatch_rejected():
    manifest = _sample_manifest()
    digest = manifest["surfaces"][0]["manifest_sha256"]
    decision = _sample_decision(evidence_manifest_digest=digest)
    decision["policy_version"] = "999"
    verdict = _verify(decision, manifest)
    assert verdict.ok is False
    assert "policy_version_mismatch" in verdict.reason_codes


def test_registry_mapping_deletion_detected_by_trusted_minimum():
    """AC1: a head-side registry that silently drops a producer mapping
    present in base is still caught by `resolve_trusted_minimum()` even
    though the producer's own head registry no longer lists it."""
    base_doc = _registry_doc(producer_paths=["src/ui/combatHud.ts"])
    head_doc = _registry_doc(producer_paths=[])  # mapping deleted head-side
    affected, _unmapped = rvi.resolve_trusted_minimum(["src/ui/combatHud.ts"], base_doc, head_doc)
    assert affected.get(SURFACE_ID) in {"mapping_deleted", "direct_producer"}


# --- AC30/AC36: static workflow-definition verification ---------------------


@pytest.fixture(scope="module")
def workflow_doc() -> dict:
    assert WORKFLOW_PATH.exists(), f"{WORKFLOW_PATH} must exist (AC30)"
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_workflow_triggers_on_workflow_run(workflow_doc: dict):
    on_block = workflow_doc.get("on") or workflow_doc.get(True)  # PyYAML parses bare `on:` as boolean True key
    assert on_block is not None, "workflow must declare an `on:` trigger block"
    assert "workflow_run" in on_block


def test_workflow_check_name_present():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "visual-impact-policy-trusted" in text


def test_no_pr_head_execution_no_checkout_ref_override(workflow_doc: dict):
    """AC30/AC36: no `actions/checkout` step in this workflow may pin `ref:`
    to anything PR-head-derived (that would defeat the entire trust
    boundary this workflow exists to provide)."""
    forbidden_ref_fragments = (
        "pull_request.head",
        "workflow_run.head_sha",
        "github.head_ref",
    )
    for job in (workflow_doc.get("jobs") or {}).values():
        for step in job.get("steps", []) or []:
            uses = step.get("uses", "")
            if isinstance(uses, str) and uses.startswith("actions/checkout"):
                ref = (step.get("with") or {}).get("ref", "")
                for fragment in forbidden_ref_fragments:
                    assert fragment not in str(ref), (
                        f"actions/checkout step must not pin ref to PR-head-derived "
                        f"expression, found: {ref!r}"
                    )


def test_no_pr_head_execution_no_package_scripts(workflow_doc: dict):
    """AC30/AC36: this workflow must never run PR-head `package.json`
    scripts / pnpm|npm|npx invocations / local composite actions outside
    the fixed base-locked setup actions."""
    forbidden_run_fragments = ("pnpm ", "npm ", "npx ", "pnpm\n", "npm\n")
    allowed_local_actions = {"./.github/actions/setup-python-uv"}
    for job in (workflow_doc.get("jobs") or {}).values():
        for step in job.get("steps", []) or []:
            uses = step.get("uses")
            if isinstance(uses, str) and uses.startswith("./"):
                assert uses in allowed_local_actions, f"unexpected local composite action: {uses}"
            run = step.get("run")
            if isinstance(run, str):
                for fragment in forbidden_run_fragments:
                    msg = f"step must not execute PR-head package scripts: {fragment!r} in run block"
                    assert fragment not in run, msg


def test_job_permissions_are_minimal(workflow_doc: dict):
    jobs = workflow_doc.get("jobs") or {}
    for job in jobs.values():
        permissions = job.get("permissions") or {}
        assert permissions.get("contents") == "read"
        for scope, level in permissions.items():
            if scope != "checks":
                assert level == "read", f"unexpected write permission granted: {scope}={level}"

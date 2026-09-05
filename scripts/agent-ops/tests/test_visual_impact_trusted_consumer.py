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
import io
import json
import os
import re
import subprocess
import sys
import zipfile
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

_FAKE_GH_TEMPLATE = '#!/usr/bin/env python3\nimport json\nimport sys\n\nRESPONSES = {responses_literal!r}\nARGV_TEXT = " ".join(sys.argv[1:])\nfor needle, response in RESPONSES.items():\n    if needle in ARGV_TEXT:\n        sys.stdout.write(response["status_line"] + "\\r\\n\\r\\n" + response["body"])\n        sys.exit(0 if response.get("exit_zero", True) else 1)\nsys.stderr.write("unexpected gh invocation: " + ARGV_TEXT + "\\n")\nsys.exit(1)\n'  # noqa: E501 -- generated fake-gh script source

EXPECTED_REPOSITORY = "squne121/loop-protocol"
EXPECTED_PR_NUMBER = 2019
EXPECTED_HEAD_SHA = "b" * 40
SURFACE_ID = "combat-hud-running"
# Issue #2230 AC2: fixed workflow_run_id/run_attempt used by every
# build_decision() call in this file so schema validation always passes;
# only tests that supply a matching authenticated
# component_vrt_checkrun_provenance additionally need these to line up.
EXPECTED_WORKFLOW_RUN_ID = 4242
EXPECTED_RUN_ATTEMPT = 2


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
        workflow_run_id=EXPECTED_WORKFLOW_RUN_ID,
        run_attempt=EXPECTED_RUN_ATTEMPT,
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
        workflow_run_id=EXPECTED_WORKFLOW_RUN_ID,
        run_attempt=EXPECTED_RUN_ATTEMPT,
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


def _no_impact_trusted_rederivation(changed_entries: list[dict], registry_doc: dict) -> "rvi.TrustedRederivation":
    return rvi.TrustedRederivation(
        changed_path_entries=changed_entries,
        base_registry_doc=registry_doc,
        head_registry_doc=registry_doc,
        component_vrt_checkrun_provenance=rvi.ComponentVrtCheckrunProvenanceResult(
            ok=True,
            reason_codes=[],
            check_run_id=555,
            workflow_run_id=EXPECTED_WORKFLOW_RUN_ID,
            run_attempt=EXPECTED_RUN_ATTEMPT,
            app_id=rvi.GITHUB_ACTIONS_APP_ID,
            app_slug=rvi.GITHUB_ACTIONS_APP_SLUG,
        ),
    )


def test_no_visual_impact_empty_surfaces_missing_evidence_manifest_fails_closed():
    """Issue #2230 fix_delta P1-5 (human reviewer): a genuinely
    no-visual-impact PR (`affected_surfaces: []`) with a COMPLETELY MISSING
    evidence-manifest artifact (`evidence_manifest_raw=None`) must now fail
    closed -- AC2 requires BOTH the decision AND evidence-manifest artifacts
    to be present/acquired, unconditionally, never only when a surface's
    decision entry happens to claim an `evidence_manifest_digest`. This
    inverts the PREVIOUS (buggy) expectation of this test, which asserted
    `ok=True` for a totally-missing evidence-manifest artifact just because
    `affected_surfaces` was empty and the per-surface digest-claim loop
    therefore never executed."""
    changed_entries = [{"status": "modified", "path": "docs/README.md"}]
    registry_doc = _registry_doc(producer_paths=["src/ui/combatHud.ts"], coverage_roots=["src/ui/**"])
    decision = _forged_decision(changed_path_entries=changed_entries, affected_surfaces=[])
    # PR #2229 review fix_delta P1-1: `verify_trusted_artifact()` now
    # cross-checks the decision's self-reported
    # `component_vrt_report_check_run_id`/`github_actions_app_identity`
    # against the AUTHENTICATED provenance identity when that provenance
    # succeeded -- give this test a realistic matching pair instead of the
    # `_forged_decision()` default `null` check-run id (a real CI run
    # always populates this field, no-visual-impact or not).
    decision["component_vrt_report_check_run_id"] = "555"
    trusted = _no_impact_trusted_rederivation(changed_entries, registry_doc)
    verdict = _verify(decision, manifest=None, trusted_rederivation=trusted)
    assert verdict.ok is False
    assert "producer_artifact_acquisition_failed:evidence_manifest_missing" in verdict.reason_codes


def test_no_visual_impact_empty_surfaces_present_empty_manifest_still_passes():
    """Companion positive regression: the same no-visual-impact decision,
    but the evidence-manifest artifact IS actually present (schema-valid,
    genuinely empty `surfaces: []`, never merely `None`/missing) -- this
    must still PASS. A real, present, empty manifest is fine; a MISSING
    manifest artifact is not (this is the distinction the fix draws)."""
    changed_entries = [{"status": "modified", "path": "docs/README.md"}]
    registry_doc = _registry_doc(producer_paths=["src/ui/combatHud.ts"], coverage_roots=["src/ui/**"])
    decision = _forged_decision(changed_path_entries=changed_entries, affected_surfaces=[])
    decision["component_vrt_report_check_run_id"] = "555"
    trusted = _no_impact_trusted_rederivation(changed_entries, registry_doc)
    empty_manifest = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": []}
    verdict = _verify(decision, manifest=empty_manifest, trusted_rederivation=trusted)
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


def test_trusted_fetch_uses_checkout_managed_credentials(workflow_doc: dict):
    """AC1/AC5: use checkout's supported authenticated Git path, never echo a token."""
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
    trusted = next(step for step in steps if step.get("id") == "trusted")

    assert checkout.get("with", {}).get("persist-credentials") is True
    assert trusted.get("continue-on-error") is True
    assert "git fetch --no-tags --depth=1 origin" in trusted["run"]
    assert "http.extraheader" not in trusted["run"]
    assert "GH_TOKEN" not in trusted["run"]


def test_checkout_is_pinned_to_the_v6_0_3_immutable_revision(workflow_doc: dict):
    """GIVEN the privileged consumer, WHEN checkout runs, THEN its action ref is immutable."""
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))

    assert checkout["uses"] == "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10"


def test_trusted_failure_blocks_verifier_using_pre_continue_on_error_outcome(workflow_doc: dict):
    """GIVEN trusted derivation fails, WHEN continuation is enabled, THEN verifier stays skipped."""
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    verify = next(step for step in steps if step.get("id") == "verify")
    publish = next(step for step in steps if "Publish visual-impact-policy-trusted" in step.get("name", ""))

    assert verify["if"] == "${{ steps.pr.outputs.pr_number != '' && steps.trusted.outcome == 'success' }}"
    assert "TRUSTED_OUTCOME" in publish["env"]
    assert publish["env"]["TRUSTED_OUTCOME"] == "${{ steps.trusted.outcome }}"


def test_verifier_failure_is_artifact_rejection_not_verifier_not_run(workflow_doc: dict):
    """GIVEN verifier ran and failed, WHEN publishing, THEN reject the producer artifact."""
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    verify = next(step for step in steps if step.get("id") == "verify")
    publish = next(step for step in steps if "Publish visual-impact-policy-trusted" in step.get("name", ""))

    assert verify.get("continue-on-error") is True
    assert "VERIFY_EXIT_CODE" not in publish["env"]
    assert publish["env"]["VERIFY_OUTCOME"] == "${{ steps.verify.outcome }}"
    assert 'elif [ "${VERIFY_OUTCOME}" = "failure" ]; then' in publish["run"]
    assert "trusted_input_derivation_failed" in publish["run"]
    assert "producer_artifact_verification_rejected" in publish["run"]


def test_skipped_or_cancelled_verifier_is_not_artifact_rejection(workflow_doc: dict):
    """GIVEN verifier has no outcome, WHEN publishing, THEN report verifier_not_run."""
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    publish = next(step for step in steps if "Publish visual-impact-policy-trusted" in step.get("name", ""))
    run = publish["run"]

    failure_branch = run.index('elif [ "${VERIFY_OUTCOME}" = "failure" ]; then')
    rejection_summary = run.index("producer_artifact_verification_rejected")
    no_run_summary = run.index("verifier_not_run after trusted input derivation")
    assert failure_branch < rejection_summary < no_run_summary


def _run_publish_step(
    workflow_doc: dict,
    tmp_path: Path,
    *,
    trusted_outcome: str,
    verify_outcome: str,
) -> list[str]:
    """Execute the publish shell with a fake `gh`, returning its captured argv."""
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    publish = next(step for step in steps if "Publish visual-impact-policy-trusted" in step.get("name", ""))
    captured_args = tmp_path / "gh-args.txt"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf '%s\\n' \"$@\" > \"$FAKE_GH_ARGS\"\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_GH_ARGS": str(captured_args),
        "GH_TOKEN": "test-token",
        "REPO": EXPECTED_REPOSITORY,
        "RUN_HEAD_SHA": EXPECTED_HEAD_SHA,
        "LIVE_HEAD_SHA": EXPECTED_HEAD_SHA,
        "PR_NUMBER": str(EXPECTED_PR_NUMBER),
        "TRUSTED_OUTCOME": trusted_outcome,
        "VERIFY_OUTCOME": verify_outcome,
    }
    result = subprocess.run(
        ["bash", "-c", publish["run"]],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return captured_args.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("trusted_outcome", "verify_outcome", "expected_conclusion", "expected_reason"),
    [
        ("failure", "success", "failure", "trusted_input_derivation_failed; verifier_not_run"),
        ("success", "success", "success", "independently re-verified"),
        ("success", "failure", "failure", "producer_artifact_verification_rejected"),
        ("success", "skipped", "failure", "verifier_not_run after trusted input derivation"),
        ("success", "cancelled", "failure", "verifier_not_run after trusted input derivation"),
        ("success", "", "failure", "verifier_not_run after trusted input derivation"),
    ],
)
def test_publish_step_executes_failure_taxonomy(
    workflow_doc: dict,
    tmp_path: Path,
    trusted_outcome: str,
    verify_outcome: str,
    expected_conclusion: str,
    expected_reason: str,
):
    """GIVEN each outcome combination, WHEN publishing, THEN its CheckRun reason is exact."""
    gh_args = _run_publish_step(
        workflow_doc,
        tmp_path,
        trusted_outcome=trusted_outcome,
        verify_outcome=verify_outcome,
    )

    assert f"conclusion={expected_conclusion}" in gh_args
    summary = next(arg for arg in gh_args if arg.startswith("output[summary]="))
    assert expected_reason in summary


def test_component_vrt_provenance_uses_strict_attempt_and_exact_checkrun(workflow_doc: dict):
    """#2100: final verification receives only the trusted API join tuple.

    PR #2229 review fix_delta P1-3: the attempt-scoped jobs pagination /
    cardinality / canonical-URL / exact-CheckRun-fetch logic that used to
    live inline in this step's shell (and was only ever grep-verified as
    workflow-YAML string content) now runs as the executable, adversarially
    tested `acquire_component_vrt_checkrun()` Python function (see
    `scripts/agent-ops/tests/test_resolve_visual_impact_checkrun_adversarial.py`).
    This test now only asserts the workflow correctly invokes that CLI mode
    with the base-locked run/attempt identity and never checks out /
    executes candidate PR head code to do so."""
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    trusted = next(step for step in steps if step.get("id") == "trusted")
    verify = next(step for step in steps if step.get("id") == "verify")

    assert trusted["env"]["RUN_ID"] == "${{ github.event.workflow_run.id }}"
    assert trusted["env"]["RUN_ATTEMPT"] == "${{ github.event.workflow_run.run_attempt }}"
    assert "--mode acquire-component-vrt-checkrun" in trusted["run"]
    assert '--run-id "${EXPECTED_RUN_ID}"' in trusted["run"]
    assert '--run-attempt "${EXPECTED_RUN_ATTEMPT}"' in trusted["run"]
    assert "--jobs-output-file artifacts/trusted/component_vrt_attempt_jobs.json" in trusted["run"]
    assert "--check-run-output-file artifacts/trusted/component_vrt_check_run.json" in trusted["run"]
    assert "checkout --ref" not in trusted["run"]
    assert "set -euo pipefail" in trusted["run"]

    assert verify["env"]["EXPECTED_WORKFLOW_RUN_ID"] == "${{ github.event.workflow_run.id }}"
    assert verify["env"]["EXPECTED_WORKFLOW_RUN_ATTEMPT"] == "${{ github.event.workflow_run.run_attempt }}"
    for argument in (
        "--expected-workflow-run-id",
        "--expected-workflow-run-attempt",
        "--component-vrt-jobs-file",
        "--component-vrt-check-run-file",
        "--component-vrt-jobs-complete",
    ):
        assert argument in verify["run"]



# --- PR #2229 review fix_delta P1-3: end-to-end CLI runtime evidence -----
# for `--mode acquire-component-vrt-checkrun`, invoked exactly as the
# workflow invokes it (real subprocess execution against a fake `gh`
# binary on PATH, never a self-reported/simulated result). The fake `gh` is
# itself a tiny Python script mimicking `gh api --include`'s status-line +
# blank-line + JSON-body response shape, so `_gh_api_transport()`'s real
# parsing path is exercised end-to-end.


def _write_fake_gh(tmp_path: Path, responses: dict) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(_FAKE_GH_TEMPLATE.format(responses_literal=responses), encoding="utf-8")
    fake_gh.chmod(0o755)


def test_acquire_component_vrt_checkrun_cli_mode_executes_end_to_end(tmp_path: Path):
    check_run_id = 55501
    jobs_body = {
        "total_count": 1,
        "jobs": [
            {
                "id": 42,
                "name": "component-vrt-report",
                "run_id": 999,
                "run_attempt": 3,
                "check_run_url": f"https://api.github.com/repos/{EXPECTED_REPOSITORY}/check-runs/{check_run_id}",
            }
        ],
    }
    check_run_body = {
        "id": check_run_id,
        "name": "component-vrt-report",
        "head_sha": EXPECTED_HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "app": {"id": 15368, "slug": "github-actions"},
    }
    _write_fake_gh(
        tmp_path,
        {
            "/jobs?": {"status_line": "HTTP/2.0 200 OK", "body": json.dumps(jobs_body)},
            "/check-runs/": {"status_line": "HTTP/2.0 200 OK", "body": json.dumps(check_run_body)},
        },
    )

    jobs_output = tmp_path / "jobs.json"
    check_run_output = tmp_path / "check_run.json"
    path_var = "PATH"
    env = {**os.environ, path_var: str(tmp_path) + os.pathsep + os.environ[path_var]}
    result = subprocess.run(
        [
            sys.executable,
            str(_MODULE_PATH),
            "--mode",
            "acquire-component-vrt-checkrun",
            "--repository",
            EXPECTED_REPOSITORY,
            "--run-id",
            "999",
            "--run-attempt",
            "3",
            "--jobs-output-file",
            str(jobs_output),
            "--check-run-output-file",
            str(check_run_output),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["ok"] is True
    assert output["check_run_id"] == check_run_id
    assert json.loads(jobs_output.read_text(encoding="utf-8")) == jobs_body["jobs"]
    assert json.loads(check_run_output.read_text(encoding="utf-8")) == check_run_body


def test_acquire_component_vrt_checkrun_cli_mode_fails_closed_on_non_2xx(tmp_path: Path):
    _write_fake_gh(
        tmp_path,
        {
            "/jobs?": {"status_line": "HTTP/2.0 502 Bad Gateway", "body": "", "exit_zero": False},
        },
    )
    path_var = "PATH"
    env = {**os.environ, path_var: str(tmp_path) + os.pathsep + os.environ[path_var]}
    result = subprocess.run(
        [
            sys.executable,
            str(_MODULE_PATH),
            "--mode",
            "acquire-component-vrt-checkrun",
            "--repository",
            EXPECTED_REPOSITORY,
            "--run-id",
            "999",
            "--run-attempt",
            "3",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["ok"] is False
    assert "component_vrt_acquire_jobs_http_status_invalid" in output["reason_codes"]


# --- Issue #2505 (ADR 0009 "## ZIP Resource Bound" follow-up): entry-limited,
# resource-bounded retrieval of decision.zip / evidence.zip in the `download`
# step -----------------------------------------------------------------------
#
# The bounded-reader logic lives ONLY inline in the `download` step's `run:`
# script (Allowed Paths permit only this workflow YAML and this test file --
# no new production file under scripts/agent-ops/). These tests extract that
# EXACT Python source (a heredoc embedded in the step's shell script) and
# execute it as a real subprocess against synthetic ZIPs and a fake `gh` on
# PATH -- the actual implementation seam, never a re-implemented copy.

DECISION_ENTRY_NAME_2505 = "visual_impact_decision_v1.json"
EVIDENCE_ENTRY_NAME_V3_2505 = "visual_baseline_review_evidence.json"
EVIDENCE_ENTRY_NAME_V2_2505 = "visual_baseline_review_evidence_v2.json"
DECISION_ZIP_MAX_BYTES_2505 = 2097152
EVIDENCE_ZIP_MAX_BYTES_2505 = 8388608
DECISION_ZIP_MAX_ENTRIES_2505 = 16
EVIDENCE_ZIP_MAX_ENTRIES_2505 = 16
DECISION_MAX_READ_BYTES_2505 = 1000000
EVIDENCE_MAX_READ_BYTES_2505 = 5000000


@pytest.fixture(scope="module")
def download_step(workflow_doc: dict) -> dict:
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    return next(step for step in steps if step.get("id") == "download")


@pytest.fixture(scope="module")
def bounded_zip_retrieve_script(tmp_path_factory: pytest.TempPathFactory, download_step: dict) -> Path:
    """Extract the EXACT `bounded_zip_retrieve.py` heredoc source embedded in
    the `download` step's `run:` script -- not a reimplemented copy."""
    match = re.search(
        r"<<'BOUNDED_ZIP_RETRIEVE_PY_EOF'\n(.*?)\nBOUNDED_ZIP_RETRIEVE_PY_EOF",
        download_step["run"],
        re.S,
    )
    assert match is not None, "bounded_zip_retrieve.py heredoc not found in the `download` step"
    script_dir = tmp_path_factory.mktemp("bounded_zip_retrieve_2505")
    script_path = script_dir / "bounded_zip_retrieve.py"
    script_path.write_text(match.group(1), encoding="utf-8")
    compile(match.group(1), str(script_path), "exec")  # syntax sanity check
    return script_path


def _build_zip_2505(entries: list[tuple[str, str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


def _build_zip_stored_2505(entries: list[tuple[str, str]]) -> bytes:
    """STORED (uncompressed) entries -- the on-disk local-file-data bytes
    are byte-for-byte identical to `content`, which `_corrupt_first_occurrence`
    relies on to locate and flip a byte without disturbing ZIP structure."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


def _corrupt_first_occurrence(payload: bytes, marker: bytes) -> bytes:
    """Flip one byte inside `marker`'s (STORED, i.e. uncompressed) on-disk
    occurrence -- same length, so no ZIP structural offset changes, but the
    CRC-32 recorded for that entry no longer matches its actual bytes."""
    idx = payload.find(marker)
    assert idx != -1, "marker not found in payload -- fixture bug"
    corrupted = bytearray(payload)
    corrupted[idx] = corrupted[idx] ^ 0xFF
    return bytes(corrupted)


def _run_bounded_retrieve(
    script_path: Path,
    tmp_path: Path,
    *,
    gh_payload: bytes,
    gh_exit_code: int = 0,
    max_download_bytes: int,
    max_entries: int,
    max_read_bytes: int,
    targets: list[tuple[str, str]],
    label: str = "test",
) -> tuple["subprocess.CompletedProcess[str]", dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    payload_file = tmp_path / "gh_payload.bin"
    payload_file.write_bytes(gh_payload)
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"with open({str(payload_file)!r}, 'rb') as fh:\n"
        "    sys.stdout.buffer.write(fh.read())\n"
        f"sys.exit({gh_exit_code})\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    status_file = tmp_path / "status.kv"
    argv = [
        sys.executable,
        str(script_path),
        "--label",
        label,
        "--zip-url",
        "repos/squne121/loop-protocol/actions/artifacts/1/zip",
        "--max-download-bytes",
        str(max_download_bytes),
        "--max-entries",
        str(max_entries),
        "--max-read-bytes",
        str(max_read_bytes),
        "--status-output-file",
        str(status_file),
    ]
    for name, dest in targets:
        argv += ["--target", name, dest]
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        argv,
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    status: dict[str, str] = {}
    if status_file.exists():
        for line in status_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                status[key] = value
    return result, status, status_file


def test_bounded_retrieve_normal_decision_succeeds(bounded_zip_retrieve_script: Path, tmp_path: Path):
    payload = _build_zip_2505([(DECISION_ENTRY_NAME_2505, '{"schema":"decision-v1"}')])
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    assert result.returncode == 0, result.stderr
    assert status["STATUS"] == "ok"
    assert dest.read_text(encoding="utf-8") == '{"schema":"decision-v1"}'


def test_bounded_retrieve_normal_v3_evidence_succeeds(bounded_zip_retrieve_script: Path, tmp_path: Path):
    payload = _build_zip_2505([(EVIDENCE_ENTRY_NAME_V3_2505, '{"schema":"v3","surfaces":[]}')])
    dest_v3 = tmp_path / "evidence" / EVIDENCE_ENTRY_NAME_V3_2505
    dest_v2 = tmp_path / "evidence" / EVIDENCE_ENTRY_NAME_V2_2505
    dest_v3.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=EVIDENCE_ZIP_MAX_BYTES_2505,
        max_entries=EVIDENCE_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=EVIDENCE_MAX_READ_BYTES_2505,
        targets=[(EVIDENCE_ENTRY_NAME_V3_2505, str(dest_v3)), (EVIDENCE_ENTRY_NAME_V2_2505, str(dest_v2))],
        label="evidence",
    )
    assert result.returncode == 0, result.stderr
    assert status["STATUS"] == "ok"
    assert dest_v3.exists()
    assert not dest_v2.exists()
    assert dest_v3.read_text(encoding="utf-8") == '{"schema":"v3","surfaces":[]}'


def test_bounded_retrieve_accepted_v2_evidence_succeeds(bounded_zip_retrieve_script: Path, tmp_path: Path):
    payload = _build_zip_2505([(EVIDENCE_ENTRY_NAME_V2_2505, '{"schema":"v2","surfaces":[]}')])
    dest_v3 = tmp_path / "evidence" / EVIDENCE_ENTRY_NAME_V3_2505
    dest_v2 = tmp_path / "evidence" / EVIDENCE_ENTRY_NAME_V2_2505
    dest_v3.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=EVIDENCE_ZIP_MAX_BYTES_2505,
        max_entries=EVIDENCE_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=EVIDENCE_MAX_READ_BYTES_2505,
        targets=[(EVIDENCE_ENTRY_NAME_V3_2505, str(dest_v3)), (EVIDENCE_ENTRY_NAME_V2_2505, str(dest_v2))],
        label="evidence",
    )
    assert result.returncode == 0, result.stderr
    assert status["STATUS"] == "ok"
    assert dest_v2.exists()
    assert not dest_v3.exists()
    assert dest_v2.read_text(encoding="utf-8") == '{"schema":"v2","surfaces":[]}'


def test_bounded_retrieve_v2_v3_coexistence_rejected(bounded_zip_retrieve_script: Path, tmp_path: Path):
    """Issue #2505's core regression: V2 and V3 both present must be
    rejected even though each individually appears exactly once -- this must
    be evaluated against the FULL original entry list, never a
    name-narrowed subset (the bug the human-context review flagged)."""
    payload = _build_zip_2505(
        [
            (EVIDENCE_ENTRY_NAME_V3_2505, '{"schema":"v3"}'),
            (EVIDENCE_ENTRY_NAME_V2_2505, '{"schema":"v2"}'),
        ]
    )
    dest_v3 = tmp_path / "evidence" / EVIDENCE_ENTRY_NAME_V3_2505
    dest_v2 = tmp_path / "evidence" / EVIDENCE_ENTRY_NAME_V2_2505
    dest_v3.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=EVIDENCE_ZIP_MAX_BYTES_2505,
        max_entries=EVIDENCE_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=EVIDENCE_MAX_READ_BYTES_2505,
        targets=[(EVIDENCE_ENTRY_NAME_V3_2505, str(dest_v3)), (EVIDENCE_ENTRY_NAME_V2_2505, str(dest_v2))],
        label="evidence",
    )
    assert result.returncode != 0
    assert status["STATUS"] == "entry_ambiguous_or_missing"
    assert not dest_v3.exists()
    assert not dest_v2.exists()


@pytest.mark.parametrize(
    ("download_bytes_offset", "expect_ok"),
    [(-1, True), (0, True), (1, False)],
    ids=["below_limit", "exactly_at_limit", "over_limit"],
)
def test_bounded_retrieve_download_bytes_boundary(
    bounded_zip_retrieve_script: Path, tmp_path: Path, download_bytes_offset: int, expect_ok: bool
):
    """AC4: downloaded ZIP bytes below/exactly-at/over the configured limit."""
    base_payload = _build_zip_2505([(DECISION_ENTRY_NAME_2505, "x")])
    max_download_bytes = len(base_payload) - download_bytes_offset
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=base_payload,
        max_download_bytes=max_download_bytes,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    if expect_ok:
        assert result.returncode == 0, result.stderr
        assert status["STATUS"] == "ok"
        assert dest.exists()
    else:
        assert result.returncode != 0
        assert status["STATUS"] == "download_size_exceeded"
        assert status["LIMIT"] == str(max_download_bytes)
        assert not dest.exists()


def test_bounded_retrieve_download_size_exceeded_kills_and_reaps_process(
    bounded_zip_retrieve_script: Path, tmp_path: Path
):
    """AC4: overflow must be detected DURING streaming (never a post-hoc
    `stat`) and the downloader process must be terminated/reaped -- proven
    here by an effectively-unbounded fake `gh` that would hang forever if
    the bounded reader waited for EOF before checking size."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "chunk = b'Q' * 65536\n"
        "try:\n"
        "    while True:\n"
        "        sys.stdout.buffer.write(chunk)\n"
        "        sys.stdout.buffer.flush()\n"
        "except BrokenPipeError:\n"
        "    pass\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    status_file = tmp_path / "status.kv"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        [
            sys.executable,
            str(bounded_zip_retrieve_script),
            "--label",
            "decision",
            "--zip-url",
            "repos/squne121/loop-protocol/actions/artifacts/1/zip",
            "--max-download-bytes",
            str(DECISION_ZIP_MAX_BYTES_2505),
            "--max-entries",
            str(DECISION_ZIP_MAX_ENTRIES_2505),
            "--max-read-bytes",
            str(DECISION_MAX_READ_BYTES_2505),
            "--status-output-file",
            str(status_file),
            "--target",
            DECISION_ENTRY_NAME_2505,
            str(dest),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,  # would hang past this bound if the abort path is broken
    )
    assert result.returncode != 0
    status_text = status_file.read_text(encoding="utf-8")
    assert "STATUS=download_size_exceeded" in status_text
    assert not dest.exists()


@pytest.mark.parametrize(
    ("entry_count_offset", "expect_ok"),
    [(-1, True), (0, True), (1, False)],
    ids=["below_limit", "exactly_at_limit", "over_limit"],
)
def test_bounded_retrieve_entry_count_boundary(
    bounded_zip_retrieve_script: Path, tmp_path: Path, entry_count_offset: int, expect_ok: bool
):
    """AC5: original ZIP entry count below/exactly-at/over the configured
    limit, judged from the entry list BEFORE any extraction/read."""
    filler_count = DECISION_ZIP_MAX_ENTRIES_2505 - 1 + entry_count_offset
    entries = [(f"filler{i}.json", "x") for i in range(filler_count)]
    entries.append((DECISION_ENTRY_NAME_2505, "target-content"))
    payload = _build_zip_2505(entries)
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    if expect_ok:
        assert result.returncode == 0, result.stderr
        assert status["STATUS"] == "ok"
        assert dest.exists()
    else:
        assert result.returncode != 0
        assert status["STATUS"] == "entry_count_exceeded"
        assert status["LIMIT"] == str(DECISION_ZIP_MAX_ENTRIES_2505)
        assert not dest.exists()


@pytest.mark.parametrize(
    ("read_bytes_offset", "expect_ok"),
    [(-1, True), (0, True), (1, False)],
    ids=["below_limit", "exactly_at_limit", "over_limit"],
)
def test_bounded_retrieve_read_bytes_boundary(
    bounded_zip_retrieve_script: Path, tmp_path: Path, read_bytes_offset: int, expect_ok: bool
):
    """AC6: selected-entry decompressed/read bytes below/exactly-at/over the
    configured limit -- an N-byte truncation of an oversized entry must
    never be reported as success; only genuinely-<=N-byte content passes."""
    max_read_bytes = 32
    content_length = max_read_bytes + read_bytes_offset
    payload = _build_zip_2505([(DECISION_ENTRY_NAME_2505, "a" * content_length)])
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=max_read_bytes,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    if expect_ok:
        assert result.returncode == 0, result.stderr
        assert status["STATUS"] == "ok"
        assert dest.read_text(encoding="utf-8") == "a" * content_length
    else:
        assert result.returncode != 0
        assert status["STATUS"] == "read_size_exceeded"
        assert status["LIMIT"] == str(max_read_bytes)
        assert not dest.exists()


def test_bounded_retrieve_missing_target_entry_rejected(bounded_zip_retrieve_script: Path, tmp_path: Path):
    payload = _build_zip_2505([("unrelated.json", "x")])
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    assert result.returncode != 0
    assert status["STATUS"] == "entry_ambiguous_or_missing"
    assert not dest.exists()


def test_bounded_retrieve_duplicate_target_entry_rejected(bounded_zip_retrieve_script: Path, tmp_path: Path):
    payload = _build_zip_2505(
        [(DECISION_ENTRY_NAME_2505, "first"), (DECISION_ENTRY_NAME_2505, "second")]
    )
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    assert result.returncode != 0
    assert status["STATUS"] == "entry_ambiguous_or_missing"
    assert not dest.exists()


def test_bounded_retrieve_non_target_entries_never_read(bounded_zip_retrieve_script: Path, tmp_path: Path):
    """A CORRUPTED non-target entry (bad CRC-32 -- would raise if opened and
    fully read) sits alongside a valid target entry. Success here proves the
    non-target entry was never opened/read."""
    marker = b"CORRUPT_NON_TARGET_MARKER_2505"
    payload = _build_zip_stored_2505(
        [
            ("bystander.json", marker.decode("ascii")),
            (DECISION_ENTRY_NAME_2505, '{"schema":"decision-v1"}'),
        ]
    )
    payload = _corrupt_first_occurrence(payload, marker)
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    assert result.returncode == 0, result.stderr
    assert status["STATUS"] == "ok"
    assert dest.read_text(encoding="utf-8") == '{"schema":"decision-v1"}'


def test_bounded_retrieve_read_failure_corrupted_target_entry_rejected(
    bounded_zip_retrieve_script: Path, tmp_path: Path
):
    """The TARGET entry itself is corrupted (bad CRC-32, small enough to
    fully decompress within the read bound) -- must be rejected, never
    silently adopted."""
    marker = b"CORRUPT_TARGET_MARKER_2505"
    payload = _build_zip_stored_2505([(DECISION_ENTRY_NAME_2505, marker.decode("ascii"))])
    payload = _corrupt_first_occurrence(payload, marker)
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    assert result.returncode != 0
    assert status["STATUS"] == "invalid_zip"
    assert not dest.exists()


def test_bounded_retrieve_invalid_zip_rejected(bounded_zip_retrieve_script: Path, tmp_path: Path):
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=b"this is not a zip file at all",
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    assert result.returncode != 0
    assert status["STATUS"] == "invalid_zip"
    assert not dest.exists()


def test_bounded_retrieve_download_command_failure_rejected(bounded_zip_retrieve_script: Path, tmp_path: Path):
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=b"",
        gh_exit_code=1,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    assert result.returncode != 0
    assert status["STATUS"] == "download_command_failed"
    assert not dest.exists()


def test_bounded_retrieve_never_extracts_outside_destination_directory(
    bounded_zip_retrieve_script: Path, tmp_path: Path
):
    """Even if the ZIP entry's OWN recorded filename looks like a path
    traversal string, the destination is always the literal `--target ...
    DEST` path passed in by the caller -- never a path derived from the
    entry's own name (there is no `ZipFile.extract()` call anywhere in this
    implementation)."""
    traversal_name = "../../../evil.json"
    payload = _build_zip_2505([(traversal_name, "malicious-content")])
    dest_dir = tmp_path / "decision"
    dest_dir.mkdir()
    dest = dest_dir / "safe_output.json"
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(traversal_name, str(dest))],
    )
    assert result.returncode == 0, result.stderr
    assert status["STATUS"] == "ok"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "malicious-content"
    # Nothing was written anywhere outside the destination directory.
    assert list(dest_dir.iterdir()) == [dest]
    assert not (tmp_path / "evil.json").exists()
    assert not (tmp_path.parent / "evil.json").exists()


def test_bounded_retrieve_success_content_matches_original_bytes(
    bounded_zip_retrieve_script: Path, tmp_path: Path
):
    """The verifier must receive exactly the same bytes a legacy
    `unzip -o -q` extraction would have produced -- no transformation."""
    original_content = json.dumps({"schema": "VISUAL_IMPACT_DECISION_V1", "surfaces": [1, 2, 3]})
    payload = _build_zip_2505([(DECISION_ENTRY_NAME_2505, original_content)])
    dest = tmp_path / "decision" / DECISION_ENTRY_NAME_2505
    dest.parent.mkdir()
    result, status, _ = _run_bounded_retrieve(
        bounded_zip_retrieve_script,
        tmp_path,
        gh_payload=payload,
        max_download_bytes=DECISION_ZIP_MAX_BYTES_2505,
        max_entries=DECISION_ZIP_MAX_ENTRIES_2505,
        max_read_bytes=DECISION_MAX_READ_BYTES_2505,
        targets=[(DECISION_ENTRY_NAME_2505, str(dest))],
    )
    assert result.returncode == 0, result.stderr
    assert status["STATUS"] == "ok"
    assert dest.read_bytes() == original_content.encode("utf-8")


# --- Issue #2505 AC7/AC8/AC9: workflow-level wiring ---------------------


def test_download_step_never_uses_continue_on_error(download_step: dict):
    """AC9: a resource-bound / archive rejection must hard-fail this step so
    GitHub Actions' default same-job step-skip behavior keeps `trusted` and
    `verify` from ever running -- `continue-on-error` would defeat that."""
    assert download_step.get("continue-on-error") in (None, False)
    assert 'if [ "${DOWNLOAD_STATUS}" != "ok" ]; then' in download_step["run"]
    assert "exit 1" in download_step["run"]


def test_download_step_emits_diagnostic_outputs_for_publish_step(download_step: dict):
    """AC8: the download step must expose enough step outputs for the
    Publish step to distinguish an archive/download rejection from
    trusted_input_derivation_failed."""
    run = download_step["run"]
    for output_name in ("download_status", "download_failure_reason", "download_failure_detail"):
        assert f'echo "{output_name}=' in run


def test_publish_step_consumes_download_step_outputs(workflow_doc: dict):
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    publish = next(step for step in steps if "Publish visual-impact-policy-trusted" in step.get("name", ""))
    assert publish["env"]["DOWNLOAD_STATUS"] == "${{ steps.download.outputs.download_status }}"
    assert publish["env"]["DOWNLOAD_FAILURE_REASON"] == "${{ steps.download.outputs.download_failure_reason }}"
    assert publish["env"]["DOWNLOAD_FAILURE_DETAIL"] == "${{ steps.download.outputs.download_failure_detail }}"


def _run_publish_step_2505(
    workflow_doc: dict,
    tmp_path: Path,
    *,
    trusted_outcome: str,
    verify_outcome: str,
    download_status: str = "ok",
    download_failure_reason: str = "",
    download_failure_detail: str = "",
) -> list[str]:
    steps = workflow_doc["jobs"]["visual-impact-policy-trusted"]["steps"]
    publish = next(step for step in steps if "Publish visual-impact-policy-trusted" in step.get("name", ""))
    captured_args = tmp_path / "gh-args.txt"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf '%s\\n' \"$@\" > \"$FAKE_GH_ARGS\"\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_GH_ARGS": str(captured_args),
        "GH_TOKEN": "test-token",
        "REPO": EXPECTED_REPOSITORY,
        "RUN_HEAD_SHA": EXPECTED_HEAD_SHA,
        "LIVE_HEAD_SHA": EXPECTED_HEAD_SHA,
        "PR_NUMBER": str(EXPECTED_PR_NUMBER),
        "TRUSTED_OUTCOME": trusted_outcome,
        "VERIFY_OUTCOME": verify_outcome,
        "DOWNLOAD_STATUS": download_status,
        "DOWNLOAD_FAILURE_REASON": download_failure_reason,
        "DOWNLOAD_FAILURE_DETAIL": download_failure_detail,
    }
    result = subprocess.run(
        ["bash", "-c", publish["run"]],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return captured_args.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    "download_failure_reason",
    [
        "download_size_exceeded",
        "entry_count_exceeded",
        "entry_ambiguous_or_missing",
        "read_size_exceeded",
        "invalid_zip",
        "download_command_failed",
    ],
)
def test_publish_step_reports_actual_download_rejection_reason(
    workflow_doc: dict, tmp_path: Path, download_failure_reason: str
):
    """AC8: the failure CheckRun must name the ACTUAL archive/download
    rejection reason -- never misreported as trusted_input_derivation_failed
    (a different failure class: the `trusted` step, which never even runs
    once `download` fails closed)."""
    gh_args = _run_publish_step_2505(
        workflow_doc,
        tmp_path,
        trusted_outcome="skipped",
        verify_outcome="skipped",
        download_status="rejected",
        download_failure_reason=download_failure_reason,
        download_failure_detail="artifact=decision limit=2097152",
    )
    assert "conclusion=failure" in gh_args
    summary = next(arg for arg in gh_args if arg.startswith("output[summary]="))
    assert download_failure_reason in summary
    assert "trusted_input_derivation_failed" not in summary


def test_publish_step_download_ok_falls_through_to_existing_taxonomy(workflow_doc: dict, tmp_path: Path):
    """Regression guard: a normal (non-rejected) download must not alter the
    pre-existing trusted/verify outcome taxonomy."""
    gh_args = _run_publish_step_2505(
        workflow_doc,
        tmp_path,
        trusted_outcome="success",
        verify_outcome="success",
        download_status="ok",
    )
    assert "conclusion=success" in gh_args

"""Issue #2100 final trusted CheckRun provenance adversarial controls."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2100_checkrun_adversarial"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

REPOSITORY = "squne121/loop-protocol"
HEAD_SHA = "a" * 40
RUN_ID = 123456
RUN_ATTEMPT = 2
CHECK_RUN_ID = 987654


def _check_run() -> dict:
    return {
        "id": CHECK_RUN_ID,
        "name": "component-vrt-report",
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "app": {"id": 15368, "slug": "github-actions"},
    }


def _job() -> dict:
    return {
        "id": 100,
        "name": "component-vrt-report",
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "head_sha": HEAD_SHA,
        "conclusion": "success",
        "check_run_url": f"https://api.github.com/repos/{REPOSITORY}/check-runs/{CHECK_RUN_ID}",
    }


def _provenance(*, jobs: object | None = None, check_run: object | None = None, jobs_complete: bool = True):
    return rvi.verify_component_vrt_checkrun_provenance(
        check_run=_check_run() if check_run is None else check_run,
        workflow_jobs=[_job()] if jobs is None else jobs,
        jobs_complete=jobs_complete,
        expected_workflow_run_id=RUN_ID,
        expected_run_attempt=RUN_ATTEMPT,
        expected_head_sha=HEAD_SHA,
        expected_repository=REPOSITORY,
    )


def _final_verdict(provenance, *, require_component_vrt_checkrun_provenance: bool = True):
    decision = rvi.build_decision(
        repository=REPOSITORY,
        pull_request_number=2100,
        base_sha="b" * 40,
        head_sha=HEAD_SHA,
        base_registry_blob_sha="c" * 40,
        head_registry_blob_sha="d" * 40,
        pr_body="",
        changed_path_entries=[],
        affected_surfaces=[],
        component_vrt_report_check_run_id=str(CHECK_RUN_ID),
        github_actions_app_identity="github-actions[bot]",
        artifact_id="artifact",
        artifact_digest="e" * 64,
    )
    schema = Path(__file__).resolve().parents[3] / "docs/dev/visual-impact.schema.json"
    return rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode(),
        evidence_manifest_raw=None,
        visual_impact_schema_path=schema,
        expected_head_sha=HEAD_SHA,
        expected_repository=REPOSITORY,
        expected_pr_number=2100,
        trusted_rederivation=rvi.TrustedRederivation(
            component_vrt_checkrun_provenance=provenance,
            require_component_vrt_checkrun_provenance=require_component_vrt_checkrun_provenance,
        ),
    )


def test_exact_checkrun_belongs_to_triggering_run_is_accepted():
    provenance = _provenance()
    assert provenance.ok is True
    verdict = _final_verdict(provenance)
    assert verdict.ok is True
    assert verdict.reason_codes == []


def test_same_identity_checkrun_from_different_run_is_rejected():
    job = _job()
    job["run_id"] = RUN_ID + 1
    provenance = _provenance(jobs=[job])
    assert provenance.ok is False
    assert provenance.reason_codes == ["component_vrt_check_run_workflow_mismatch"]
    verdict = _final_verdict(provenance)
    assert verdict.ok is False
    assert "component_vrt_check_run_workflow_mismatch" in verdict.reason_codes


def test_wrong_attempt_is_rejected():
    job = _job()
    job["run_attempt"] = RUN_ATTEMPT - 1
    result = _provenance(jobs=[job])
    assert result.ok is False
    assert "component_vrt_check_run_attempt_mismatch" in result.reason_codes


def test_checkrun_job_zero_match_is_rejected():
    result = _provenance(jobs=[])
    assert result.ok is False
    assert "component_vrt_job_cardinality_invalid" in result.reason_codes


def test_checkrun_job_multiple_matches_are_rejected():
    result = _provenance(jobs=[_job(), copy.deepcopy(_job())])
    assert result.ok is False
    assert "component_vrt_job_cardinality_invalid" in result.reason_codes


def test_malformed_or_api_failure_blocks_final_trusted_verdict():
    malformed = _provenance(jobs={"jobs": [_job()]})
    assert malformed.ok is False
    assert "component_vrt_jobs_payload_invalid" in malformed.reason_codes
    failed = _provenance(jobs_complete=False)
    verdict = _final_verdict(failed)
    assert verdict.ok is False
    assert "component_vrt_jobs_incomplete" in verdict.reason_codes


def test_exact_checkrun_lookup_requires_canonical_url_and_response_id():
    job = _job()
    job["check_run_url"] = f"https://api.github.com/repos/{REPOSITORY}/check-runs/{CHECK_RUN_ID + 1}"
    result = _provenance(jobs=[job])
    assert result.ok is False
    assert "component_vrt_job_check_run_relation_mismatch" in result.reason_codes


def _builder_args(**overrides):
    values = {
        "component_vrt_jobs_file": None,
        "component_vrt_check_run_file": None,
        "expected_workflow_run_id": None,
        "expected_workflow_run_attempt": None,
        "component_vrt_jobs_complete": None,
        "expected_head_sha": HEAD_SHA,
        "expected_repository": REPOSITORY,
        "expected_base_sha": None,
        "pr_body_file": None,
        "changed_paths_typed_file": None,
        "trusted_base_registry_file": None,
        "trusted_head_registry_file": None,
        "schema": None,
        "expected_base_registry_blob_sha": None,
        "expected_head_registry_blob_sha": None,
        "changed_paths_incomplete": False,
        "trusted_candidate_tree_ref": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _final_verdict_from_builder(args):
    trusted, load_errors = rvi._build_trusted_rederivation_from_args(args)
    assert load_errors == []
    assert trusted.require_component_vrt_checkrun_provenance is True
    return _final_verdict(
        trusted.component_vrt_checkrun_provenance,
        require_component_vrt_checkrun_provenance=trusted.require_component_vrt_checkrun_provenance,
    )


def test_trusted_provenance_tuple_all_missing_and_partial_inputs_fail_closed():
    all_missing = _final_verdict_from_builder(_builder_args())
    assert all_missing.ok is False
    assert "component_vrt_trusted_provenance_missing" in all_missing.reason_codes

    partial = _final_verdict_from_builder(_builder_args(expected_workflow_run_id=RUN_ID))
    assert partial.ok is False
    assert "component_vrt_trusted_provenance_partial" in partial.reason_codes


def test_trusted_provenance_tuple_load_error_uses_fixed_taxonomy(tmp_path: Path):
    args = _builder_args(
        component_vrt_jobs_file=str(tmp_path / "missing-jobs.json"),
        component_vrt_check_run_file=str(tmp_path / "missing-check.json"),
        expected_workflow_run_id=RUN_ID,
        expected_workflow_run_attempt=RUN_ATTEMPT,
        component_vrt_jobs_complete=True,
    )
    verdict = _final_verdict_from_builder(args)
    assert verdict.ok is False
    assert "component_vrt_trusted_api_payload_invalid" in verdict.reason_codes
    assert all(":" not in reason for reason in verdict.reason_codes if reason.startswith("component_vrt_"))


def test_trusted_provenance_tuple_required_scalar_types_fail_closed():
    cases = (
        (
            "job run_id float",
            lambda job, check: job.__setitem__("run_id", float(RUN_ID)),
            "component_vrt_job_run_id_invalid",
        ),
        (
            "job run_attempt bool",
            lambda job, check: job.__setitem__("run_attempt", True),
            "component_vrt_job_run_attempt_invalid",
        ),
        (
            "check id float",
            lambda job, check: check.__setitem__("id", float(CHECK_RUN_ID)),
            "component_vrt_check_run_id_invalid",
        ),
        (
            "app id bool",
            lambda job, check: check["app"].__setitem__("id", True),
            "component_vrt_check_run_app_id_invalid",
        ),
    )
    for _, mutate, reason in cases:
        job = _job()
        check = _check_run()
        mutate(job, check)
        result = _provenance(jobs=[job], check_run=check)
        assert result.ok is False
        assert reason in result.reason_codes
        final = _final_verdict(result)
        assert final.ok is False
        assert reason in final.reason_codes


def test_job_cardinality_or_pagination_final_failure_reasons():
    cases = (
        (_provenance(jobs=[]), "component_vrt_job_cardinality_invalid"),
        (_provenance(jobs=[_job(), copy.deepcopy(_job())]), "component_vrt_job_cardinality_invalid"),
        (_provenance(jobs_complete=False), "component_vrt_jobs_incomplete"),
        (_provenance(jobs={"jobs": [_job()]}), "component_vrt_jobs_payload_invalid"),
    )
    for result, reason in cases:
        final = _final_verdict(result)
        assert final.ok is False
        assert reason in final.reason_codes


def test_runtime_verification_contract_fixture_rejects_incomplete_pagination():
    result = _provenance(jobs_complete=False)
    final = _final_verdict(result)
    assert final.ok is False
    assert "component_vrt_jobs_incomplete" in final.reason_codes


def test_trusted_provenance_tuple_wrong_attempt_reaches_final_failure():
    job = _job()
    job["run_attempt"] = RUN_ATTEMPT - 1
    result = _provenance(jobs=[job])
    final = _final_verdict(result)
    assert final.ok is False
    assert "component_vrt_check_run_attempt_mismatch" in final.reason_codes


def test_exact_checkrun_lookup_trusted_provenance_tuple_canonical_relation_reaches_final_failure():
    job = _job()
    job["check_run_url"] = f"https://api.github.com/repos/{REPOSITORY}/check-runs/{CHECK_RUN_ID + 1}"
    result = _provenance(jobs=[job])
    final = _final_verdict(result)
    assert final.ok is False
    assert "component_vrt_job_check_run_relation_mismatch" in final.reason_codes


def test_final_boundary_rejects_absent_or_reasonless_provenance_without_builder():
    missing = _final_verdict(None)
    assert missing.ok is False
    assert "component_vrt_checkrun_provenance_missing" in missing.reason_codes

    legacy_optional = _final_verdict(None, require_component_vrt_checkrun_provenance=False)
    assert legacy_optional.ok is True
    assert legacy_optional.reason_codes == []

    reasonless_rejection = rvi.ComponentVrtCheckrunProvenanceResult(ok=False, reason_codes=[])
    rejected = _final_verdict(reasonless_rejection)
    assert rejected.ok is False
    assert "component_vrt_checkrun_provenance_rejected" in rejected.reason_codes


def test_trusted_provenance_tuple_job_id_requires_strict_positive_integer():
    for bad_id in (True, 100.0, 0, -1, None):
        job = _job()
        if bad_id is None:
            del job["id"]
        else:
            job["id"] = bad_id
        result = _provenance(jobs=[job])
        assert result.ok is False
        assert "component_vrt_job_id_invalid" in result.reason_codes
        final = _final_verdict(result)
        assert final.ok is False
        assert "component_vrt_job_id_invalid" in final.reason_codes


def test_job_cardinality_or_pagination_replayed_page_duplicate_target_fails_closed():
    replayed_target = copy.deepcopy(_job())
    result = _provenance(jobs=[_job(), replayed_target])
    assert result.ok is False
    assert "component_vrt_job_id_duplicate" in result.reason_codes
    final = _final_verdict(result)
    assert final.ok is False
    assert "component_vrt_job_id_duplicate" in final.reason_codes

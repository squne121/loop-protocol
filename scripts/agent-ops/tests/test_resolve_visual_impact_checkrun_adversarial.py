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


_UNSET = object()


def _final_verdict(
    provenance,
    *,
    require_component_vrt_checkrun_provenance: bool = True,
    component_vrt_report_check_run_id=_UNSET,
    github_actions_app_identity: str = "github-actions[bot]",
):
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
        component_vrt_report_check_run_id=(
            str(CHECK_RUN_ID) if component_vrt_report_check_run_id is _UNSET else component_vrt_report_check_run_id
        ),
        github_actions_app_identity=github_actions_app_identity,
        artifact_id="artifact",
        artifact_digest="e" * 64,
        # Issue #2230 AC2: matches the authenticated provenance's own
        # RUN_ID/RUN_ATTEMPT so the new tuple-binding checks never
        # interfere with this file's existing CheckRun-provenance-focused
        # assertions.
        workflow_run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
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


# --- PR #2229 review fix_delta P1-1: authenticated CheckRun identity is --
# cross-checked against the producer's self-reported decision fields ------


def test_decision_check_run_id_matching_authenticated_provenance_is_accepted():
    provenance = _provenance()
    assert provenance.ok is True
    verdict = _final_verdict(provenance)
    assert verdict.ok is True
    assert verdict.reason_codes == []


def test_decision_check_run_id_mismatch_against_authenticated_provenance_is_rejected():
    provenance = _provenance()
    assert provenance.ok is True
    verdict = _final_verdict(provenance, component_vrt_report_check_run_id=str(CHECK_RUN_ID + 1))
    assert verdict.ok is False
    assert "component_vrt_report_check_run_id_decision_mismatch" in verdict.reason_codes


def test_decision_check_run_id_unrelated_but_genuine_run_is_rejected():
    """A producer cannot smuggle a DIFFERENT (real, unrelated) CheckRun ID
    through a decision even though the authenticated provenance for THIS
    run/attempt/head is itself genuine."""
    provenance = _provenance()
    assert provenance.ok is True
    verdict = _final_verdict(provenance, component_vrt_report_check_run_id=str(CHECK_RUN_ID * 7 + 3))
    assert verdict.ok is False
    assert "component_vrt_report_check_run_id_decision_mismatch" in verdict.reason_codes


def test_decision_check_run_id_null_against_authenticated_provenance_is_rejected():
    provenance = _provenance()
    verdict = _final_verdict(provenance, component_vrt_report_check_run_id=None)
    assert verdict.ok is False
    assert "component_vrt_report_check_run_id_decision_mismatch" in verdict.reason_codes


def test_decision_app_identity_mismatch_against_authenticated_provenance_is_rejected():
    provenance = _provenance()
    assert provenance.ok is True
    verdict = _final_verdict(provenance, github_actions_app_identity="not-github-actions[bot]")
    assert verdict.ok is False
    assert "github_actions_app_identity_decision_mismatch" in verdict.reason_codes


def test_decision_identity_cross_check_is_skipped_when_provenance_itself_failed():
    """When the authenticated provenance itself is rejected, the identity
    cross-check must not additionally fire (the provenance rejection reason
    codes already fail the verdict closed; this only asserts no crash/extra
    noise from missing `check_run_id`/`app_id` on a failed provenance)."""
    job = _job()
    job["run_id"] = RUN_ID + 1
    provenance = _provenance(jobs=[job])
    assert provenance.ok is False
    assert provenance.check_run_id is None
    verdict = _final_verdict(provenance)
    assert verdict.ok is False
    assert "component_vrt_report_check_run_id_decision_mismatch" not in verdict.reason_codes
    assert "github_actions_app_identity_decision_mismatch" not in verdict.reason_codes


# --- PR #2229 review fix_delta P1-3: executable, injectable-transport ----
# acquisition of the attempt-scoped jobs list + exact CheckRun -----------


def _transport_from_pages(pages: list[dict], *, check_run_status: int = 200, check_run_body: dict | None = None):
    calls: list[str] = []

    def transport(path: str) -> "rvi.HttpTransportResponse":
        calls.append(path)
        if "/check-runs/" in path:
            body = check_run_body if check_run_body is not None else _check_run()
            return rvi.HttpTransportResponse(status_code=check_run_status, json_body=body)
        page_number = int(path.rsplit("page=", 1)[1])
        page = pages[page_number - 1]
        return rvi.HttpTransportResponse(status_code=page.get("status_code", 200), json_body=page.get("body"))

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def _acquire(pages: list[dict], **kwargs):
    transport_kwargs = {k: v for k, v in kwargs.items() if k in ("check_run_status", "check_run_body")}
    transport = _transport_from_pages(pages, **transport_kwargs)
    return rvi.acquire_component_vrt_checkrun(
        transport=transport,
        repository=REPOSITORY,
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
    ), transport


def test_acquire_multi_page_pagination_completes_and_finds_exact_checkrun():
    jobs_page_1 = [
        {"id": i, "name": f"other-job-{i}", "run_id": RUN_ID, "run_attempt": RUN_ATTEMPT} for i in range(1, 100)
    ]
    jobs_page_2 = [_job()]
    pages = [
        {"body": {"total_count": 100, "jobs": jobs_page_1}},
        {"body": {"total_count": 100, "jobs": jobs_page_2}},
    ]
    result, transport = _acquire(pages)
    assert result.ok is True
    assert result.check_run_id == CHECK_RUN_ID
    assert len(result.jobs) == 100
    assert len({job["id"] for job in result.jobs}) == 100
    assert len(transport.calls) == 3  # 2 job pages + 1 check-run fetch


def test_acquire_target_job_zero_matches_is_rejected():
    pages = [{"body": {"total_count": 1, "jobs": [{"id": 1, "name": "unrelated", "run_id": RUN_ID}]}}]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_component_job_cardinality_invalid" in result.reason_codes


def test_acquire_target_job_two_matches_is_rejected():
    second_job = copy.deepcopy(_job())
    second_job["id"] = _job()["id"] + 1
    pages = [{"body": {"total_count": 2, "jobs": [_job(), second_job]}}]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_component_job_cardinality_invalid" in result.reason_codes


def test_acquire_duplicate_job_id_across_pages_is_rejected():
    pages = [
        {"body": {"total_count": 2, "jobs": [_job()]}},
        {"body": {"total_count": 2, "jobs": [copy.deepcopy(_job())]}},
    ]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_job_id_duplicate" in result.reason_codes


def test_acquire_total_count_changes_mid_pagination_is_rejected():
    pages = [
        {"body": {"total_count": 2, "jobs": [{"id": 1, "name": "x", "run_id": RUN_ID}]}},
        {"body": {"total_count": 3, "jobs": [_job()]}},
    ]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_jobs_total_count_changed" in result.reason_codes


def test_acquire_empty_page_before_total_count_reached_is_rejected():
    pages = [{"body": {"total_count": 5, "jobs": []}}]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_jobs_pagination_incomplete" in result.reason_codes


def test_acquire_non_2xx_jobs_response_is_rejected():
    pages = [{"status_code": 502, "body": None}]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_jobs_http_status_invalid" in result.reason_codes


def test_acquire_malformed_jobs_json_body_is_rejected():
    pages = [{"body": "not-a-dict"}]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_jobs_response_invalid" in result.reason_codes


def test_acquire_jobs_missing_required_fields_is_rejected():
    pages = [{"body": {"jobs": [_job()]}}]  # missing total_count
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_jobs_total_count_invalid" in result.reason_codes

    pages_bad_type = [{"body": {"total_count": "1", "jobs": [_job()]}}]
    result2, _ = _acquire(pages_bad_type)
    assert result2.ok is False
    assert "component_vrt_acquire_jobs_total_count_invalid" in result2.reason_codes


def test_acquire_wrong_run_id_job_fails_cardinality_when_filtered():
    pages = [{"body": {"total_count": 0, "jobs": []}}]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_component_job_cardinality_invalid" in result.reason_codes


def test_acquire_wrong_check_run_name_or_status_never_matched_by_producer_job_is_rejected():
    other_named_job = {
        "id": 100,
        "name": "not-component-vrt-report",
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "check_run_url": f"https://api.github.com/repos/{REPOSITORY}/check-runs/{CHECK_RUN_ID}",
    }
    pages = [{"body": {"total_count": 1, "jobs": [other_named_job]}}]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_component_job_cardinality_invalid" in result.reason_codes


def test_acquire_check_run_url_not_canonical_is_rejected():
    job = _job()
    job["check_run_url"] = "https://evil.example.com/check-runs/1"
    pages = [{"body": {"total_count": 1, "jobs": [job]}}]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert "component_vrt_acquire_check_run_url_not_canonical" in result.reason_codes


def test_acquire_check_run_id_response_mismatch_with_canonical_url_id_is_still_bound_to_url():
    """The acquired `check_run_id` is derived from the canonical URL, never
    the fetched CheckRun response body's own `.id` -- a caller performing a
    subsequent identity comparison (P1-1) is protected even if a malicious
    endpoint tried to return a mismatched `.id` in the body, because the
    downstream `verify_component_vrt_checkrun_provenance()` re-validates
    `check.get('id')` against the job/CheckRun relation independently."""
    mismatched_check_run_body = _check_run()
    mismatched_check_run_body["id"] = CHECK_RUN_ID + 999
    pages = [{"body": {"total_count": 1, "jobs": [_job()]}}]
    result, _ = _acquire(pages, check_run_body=mismatched_check_run_body)
    assert result.ok is True
    assert result.check_run_id == CHECK_RUN_ID
    assert result.check_run["id"] == CHECK_RUN_ID + 999


def test_acquire_non_2xx_check_run_response_is_rejected():
    pages = [{"body": {"total_count": 1, "jobs": [_job()]}}]
    result, _ = _acquire(pages, check_run_status=404)
    assert result.ok is False
    assert "component_vrt_acquire_check_run_http_status_invalid" in result.reason_codes


def test_acquire_malformed_check_run_response_body_is_rejected():
    pages = [{"body": {"total_count": 1, "jobs": [_job()]}}]
    result, _ = _acquire(pages, check_run_body="not-a-dict")
    assert result.ok is False
    assert "component_vrt_acquire_check_run_response_invalid" in result.reason_codes


def test_acquire_failure_propagates_to_final_verify_component_vrt_checkrun_provenance_boundary():
    """AC: an acquisition failure never reaches
    `verify_component_vrt_checkrun_provenance()` as a silently-empty/valid
    input -- the caller (workflow step) must treat a non-zero exit / `ok:
    false` result as a hard stop, same as any other reason code in this
    module's fail-closed contract."""
    pages = [{"body": {"total_count": 0, "jobs": []}}]
    result, _ = _acquire(pages)
    assert result.ok is False
    assert result.check_run_id is None
    assert result.check_run is None

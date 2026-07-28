"""E2E + negative regression tests for materialize_test_verdict_artifact.py.

Issue #1648 (Child C of #1645): exercises the full
execution record -> materializer -> receipt/private bundle -> adjudicator
chain, plus the negative cases the Issue body enumerates: missing receipt,
HEAD drift, artifact id/digest mismatch, and handwritten (self-attested,
non-materializer) TEST_VERDICT JSON.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent / "scripts"
_MATERIALIZER_PATH = _SCRIPTS_DIR / "materialize_test_verdict_artifact.py"
_ADJUDICATOR_PATH = _SCRIPTS_DIR / "adjudicate_vc_result.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mat = _load_module(_MATERIALIZER_PATH, "materialize_test_verdict_artifact_under_test")
adj = _load_module(_ADJUDICATOR_PATH, "adjudicate_vc_result_under_test")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))


REPO = "squne121/loop-protocol"
ISSUE_NUMBER = 1648
PR_NUMBER = 1830
HEAD_SHA = "a" * 40
ISSUE_BODY_SHA256 = "sha256:" + "b" * 64
COMMAND_HASH = "sha256:" + "c" * 64
ALL_ACS = ["AC1", "AC2", "AC3", "AC4", "AC5"]


def _build_execution_record() -> dict[str, Any]:
    producer = {
        "workflow_path": ".github/workflows/ci.yml",
        "workflow_source_ref": "refs/heads/main",
        "workflow_source_sha": "d" * 40,
        "workflow_run_id": 1001,
        "workflow_run_attempt": 1,
        "job_id": 2002,
        "check_run_id": 3003,
    }
    subject = {
        "target_pr_number": PR_NUMBER,
        "head_repository_id": 555,
        "pr_head_sha": HEAD_SHA,
    }
    contract = {
        "linked_issue_number": ISSUE_NUMBER,
        "issue_body_sha256": ISSUE_BODY_SHA256,
        "command_manifest_sha256": "sha256:" + "e" * 64,
    }
    executions = [
        {
            "execution_id": "exec-1",
            "command_id": "uv.pytest.execution-record",
            "argv_sha256": "sha256:" + "f" * 64,
            "exit_code": 0,
            "status": "pass",
            "skipped": False,
            "fallback_detected": False,
            "timed_out": False,
            "stdout_sha256": "sha256:" + "0" * 64,
            "stderr_sha256": "sha256:" + "1" * 64,
        }
    ]
    per_ac = [{"ac": ac, "execution_ids": ["exec-1"]} for ac in ALL_ACS]
    record = {
        "schema": "TEST_VERDICT_EXECUTION_RECORD_V1",
        "schema_version": 1,
        "producer": producer,
        "subject": subject,
        "contract": contract,
        "executions": executions,
        "per_ac": per_ac,
        "pass_eligible": True,
    }
    record["payload_sha256"] = _canonical_sha256(record)
    return record


def _build_receipt(execution_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "TEST_VERDICT_PRODUCER_RECEIPT_V1",
        "schema_version": 1,
        "execution_payload_sha256": execution_record["payload_sha256"],
        "execution_artifact": {
            "artifact_id": 42,
            "artifact_url": "https://github.com/squne121/loop-protocol/actions/runs/1001/artifacts/42",
            "artifact_archive_digest": "sha256:" + "2" * 64,
        },
        "producer": execution_record["producer"],
        "subject": execution_record["subject"],
        "contract": execution_record["contract"],
        "pass_eligible": True,
    }


def _build_publish_request(receipt: dict[str, Any]) -> dict[str, Any]:
    receipt_sha256 = _canonical_sha256(receipt)
    return {
        "schema": "TEST_VERDICT_PUBLISH_INPUT_V1",
        "issue_number": ISSUE_NUMBER,
        "repo": REPO,
        "target_pr_number": PR_NUMBER,
        "linked_issue_number": ISSUE_NUMBER,
        "expected_head_sha": HEAD_SHA,
        "linked_issue_body_sha256": ISSUE_BODY_SHA256,
        "producer_receipt": receipt,
        "receipt_sha256": receipt_sha256,
        "idempotency_key": f"{REPO}:{PR_NUMBER}:{ISSUE_NUMBER}:{HEAD_SHA}:{receipt_sha256}",
    }


def _build_command_hash_map() -> dict[str, str]:
    return {ac: COMMAND_HASH for ac in ALL_ACS}


def _materialize_kwargs(**overrides: Any) -> dict[str, Any]:
    execution_record = overrides.pop("execution_record", None) or _build_execution_record()
    receipt = overrides.pop("receipt", None) or _build_receipt(execution_record)
    publish_request = overrides.pop("publish_request", None) or _build_publish_request(receipt)
    command_hash_map = overrides.pop("command_hash_map", None) or _build_command_hash_map()
    kwargs: dict[str, Any] = {
        "execution_record": execution_record,
        "publish_request": publish_request,
        "command_hash_map": command_hash_map,
        "repo": REPO,
        "issue_number": ISSUE_NUMBER,
        "current_pr_number": PR_NUMBER,
        "current_head_sha": HEAD_SHA,
        "current_issue_body_sha256": ISSUE_BODY_SHA256,
        "reviewed_head_sha": HEAD_SHA,
        "diff_head_sha": HEAD_SHA,
        "run_id": "run-1001-1",
        "run_url": "https://github.com/squne121/loop-protocol/actions/runs/1001",
    }
    kwargs.update(overrides)
    return kwargs


# -- AC1: current Issue/PR/HEAD/body SHA/artifact digest binding -------------


def test_materializer_produces_bundle_when_all_bindings_hold() -> None:
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**_materialize_kwargs())
    assert status == "ok"
    assert errors == []
    assert input_bundle["schema"] == "TEST_VERDICT_MACHINE/v2"
    assert input_bundle["result"] == "PASS"
    assert len(input_bundle["runtime_ac_results"]) == len(ALL_ACS)
    assert all(item["status"] == "pass" for item in input_bundle["runtime_ac_results"])
    assert input_bundle["producer_receipt"]["pass_eligible"] is True
    assert private_bundle["schema"] == "TEST_VERDICT_MATERIALIZE_PRIVATE_BUNDLE_V1"


def test_materializer_blocks_on_current_head_sha_drift() -> None:
    kwargs = _materialize_kwargs(current_head_sha="9" * 40)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert input_bundle is None
    assert private_bundle is None
    assert "current_head_sha_mismatch" in errors


def test_materializer_blocks_on_current_issue_body_sha256_drift() -> None:
    kwargs = _materialize_kwargs(current_issue_body_sha256="sha256:" + "9" * 64)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert input_bundle is None
    assert "current_issue_body_sha256_mismatch" in errors


def test_materializer_blocks_on_current_pr_number_mismatch() -> None:
    kwargs = _materialize_kwargs(current_pr_number=PR_NUMBER + 1)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert "current_pr_number_mismatch" in errors


def test_materializer_blocks_on_current_issue_number_mismatch() -> None:
    kwargs = _materialize_kwargs(issue_number=ISSUE_NUMBER + 1)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    # Child B's own field validator (reused verbatim) rejects this before the
    # materializer's own current-state binding checks run.
    assert "test_verdict_publish_linked_issue_number_mismatch" in errors


def test_materializer_blocks_on_execution_record_digest_tamper() -> None:
    execution_record = _build_execution_record()
    execution_record["executions"][0]["exit_code"] = 1  # mutate content after payload_sha256 was fixed
    kwargs = _materialize_kwargs(execution_record=execution_record)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert "execution_record_payload_sha256_mismatch" in errors


def test_materializer_blocks_on_execution_record_receipt_digest_mismatch() -> None:
    execution_record = _build_execution_record()
    receipt = _build_receipt(execution_record)
    receipt["execution_payload_sha256"] = "sha256:" + "8" * 64
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(
        execution_record=execution_record, receipt=receipt, publish_request=publish_request
    )
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert any("receipt" in error for error in errors)


def test_materializer_blocks_on_producer_receipt_schema_invalid() -> None:
    execution_record = _build_execution_record()
    receipt = _build_receipt(execution_record)
    del receipt["execution_artifact"]["artifact_archive_digest"]  # required field missing
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(
        execution_record=execution_record, receipt=receipt, publish_request=publish_request
    )
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert errors


def test_materializer_blocks_on_missing_command_hash_for_ac() -> None:
    command_hash_map = _build_command_hash_map()
    del command_hash_map["AC3"]
    kwargs = _materialize_kwargs(command_hash_map=command_hash_map)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert "command_hash_map_missing_ac:AC3" in errors


def test_materializer_blocks_on_publish_request_repo_mismatch() -> None:
    execution_record = _build_execution_record()
    receipt = _build_receipt(execution_record)
    publish_request = _build_publish_request(receipt)
    publish_request["repo"] = "someone-else/other-repo"
    kwargs = _materialize_kwargs(
        execution_record=execution_record, receipt=receipt, publish_request=publish_request
    )
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert "test_verdict_publish_repo_mismatch" in errors


# -- AC3: E2E adjudication over the materialized bundle -----------------------


def _build_pr_review_only_results() -> list[dict[str, Any]]:
    return [
        {
            "ac": ac,
            "command_hash": COMMAND_HASH,
            "exit_code": None,
            "runner": "skipped",
            "classification": "skipped",
            "category": "preflight_scope_pr_review_only",
            "decision": "go",
            "scope_class": "pr_review_only",
            "verification_owner": "pr-review-judge",
            "deferred_reason": "VC marked pr_review_only; verification deferred to PR review",
            "runtime_verification_required": False,
        }
        for ac in ALL_ACS
    ]


def _build_adjudication_context() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    results = _build_pr_review_only_results()
    contract_snapshot = {
        "schema": "baseline_vc_preflight/v1",
        "status": "go",
        "body_sha256": ISSUE_BODY_SHA256,
        "results": results,
    }
    current_vc_result = {
        "schema": "baseline_vc_preflight/v1",
        "issue": ISSUE_NUMBER,
        "generated_at": "2026-07-28T00:00:00Z",
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": ISSUE_BODY_SHA256},
        "head_sha": HEAD_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "results": results,
    }
    diff_summary = {
        "pr_number": PR_NUMBER,
        "head_sha": HEAD_SHA,
        "base_sha": "0" * 40,
        "changed_paths": ["foo.py"],
    }
    allowed_paths = ["foo.py"]
    return contract_snapshot, current_vc_result, diff_summary, allowed_paths


def test_e2e_execution_record_to_materializer_to_adjudicator_resolves_pass() -> None:
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(**_materialize_kwargs())
    assert status == "ok"
    assert errors == []

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=input_bundle,
        require_producer_receipt=True,
    )
    assert result["errors"] == []
    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    acs_covered = {item["ac"] for item in result["per_ac"]}
    assert acs_covered == set(ALL_ACS)


# -- AC2: adjudicator fail-closed negative cases ------------------------------


def _manual_handwritten_test_verdict() -> dict[str, Any]:
    """A fully self-attested TEST_VERDICT (Issue #1648: never produced by the
    materializer, no producer_receipt at all) -- internally consistent but
    carries zero Child A/B provenance."""
    artifact_payload = {
        "issue_number": ISSUE_NUMBER,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "diff_head_sha": HEAD_SHA,
        "contract_body_sha256": ISSUE_BODY_SHA256,
        "command_hashes": sorted([COMMAND_HASH] * len(ALL_ACS)),
    }
    return {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "producer_kind": "test-runner",
        "repository": REPO,
        "issue_number": ISSUE_NUMBER,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "diff_head_sha": HEAD_SHA,
        "contract_body_sha256": ISSUE_BODY_SHA256,
        "run_id": "handwritten-1",
        "run_url": "https://example.invalid/runs/handwritten-1",
        "workflow_run_id": 1,
        "workflow_run_attempt": 1,
        "check_run_id": 1,
        "artifact": {
            "name": "test-verdict-machine",
            "artifact_digest": "sha256:" + "3" * 64,
            "url": "https://github.com/squne121/loop-protocol/actions/runs/1/artifacts/1",
        },
        "artifact_payload": artifact_payload,
        "artifact_payload_sha256": _canonical_sha256(artifact_payload),
        "result": "PASS",
        "verification_commands_pass": len(ALL_ACS),
        "verification_commands_fail": 0,
        "verification_skipped_count": 0,
        "runtime_ac_results": [
            {
                "ac": ac,
                "command_hash": COMMAND_HASH,
                "exit_code": 0,
                "status": "pass",
                "fallback_detected": False,
                "human_review_required": False,
                "stop_condition_triggered": False,
            }
            for ac in ALL_ACS
        ],
    }


def test_adjudicator_rejects_handwritten_test_verdict_with_no_receipt_when_required() -> None:
    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=_manual_handwritten_test_verdict(),
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_producer_receipt_missing" in result["errors"]


def test_adjudicator_accepts_handwritten_test_verdict_when_receipt_not_required() -> None:
    """Non-regression: the legacy self-attested TEST_VERDICT path (default
    require_producer_receipt=False) is left unchanged so Step 2 / test-runner
    callers that have not yet migrated to the materializer keep working
    (Issue #1648 body compatibility note)."""
    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=_manual_handwritten_test_verdict(),
    )
    assert result["overall_status"] == "pass"
    assert result["blocking"] is False


def test_adjudicator_rejects_materialized_bundle_missing_receipt_field() -> None:
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(**_materialize_kwargs())
    assert status == "ok"
    assert errors == []
    tampered = dict(input_bundle)
    del tampered["producer_receipt"]

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_producer_receipt_missing" in result["errors"]


def test_adjudicator_rejects_head_drift_between_materialized_bundle_and_receipt() -> None:
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(**_materialize_kwargs())
    assert status == "ok"
    assert errors == []
    tampered = json.loads(json.dumps(input_bundle))
    tampered["head_sha"] = "7" * 40
    tampered["reviewed_head_sha"] = "7" * 40
    tampered["diff_head_sha"] = "7" * 40
    tampered["artifact_payload"]["head_sha"] = "7" * 40
    tampered["artifact_payload"]["reviewed_head_sha"] = "7" * 40
    tampered["artifact_payload"]["diff_head_sha"] = "7" * 40
    tampered["artifact_payload_sha256"] = _canonical_sha256(tampered["artifact_payload"])

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    current_vc_result["head_sha"] = "7" * 40
    current_vc_result["reviewed_head_sha"] = "7" * 40
    diff_summary["head_sha"] = "7" * 40

    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_receipt_subject_head_sha_mismatch" in result["errors"]


def test_adjudicator_rejects_artifact_digest_mismatch() -> None:
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(**_materialize_kwargs())
    assert status == "ok"
    assert errors == []
    tampered = json.loads(json.dumps(input_bundle))
    tampered["artifact"]["artifact_digest"] = "sha256:" + "4" * 64

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_receipt_artifact_digest_mismatch" in result["errors"]


def test_adjudicator_rejects_receipt_sha256_tamper() -> None:
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(**_materialize_kwargs())
    assert status == "ok"
    assert errors == []
    tampered = json.loads(json.dumps(input_bundle))
    tampered["receipt_sha256"] = "sha256:" + "5" * 64

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_receipt_sha256_mismatch" in result["errors"]


def test_adjudicator_rejects_receipt_not_pass_eligible() -> None:
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(**_materialize_kwargs())
    assert status == "ok"
    assert errors == []
    tampered = json.loads(json.dumps(input_bundle))
    tampered["producer_receipt"]["pass_eligible"] = False
    # Recompute receipt_sha256 so the pass_eligible check -- not the sha256
    # binding check -- is what rejects this tampered receipt.
    tampered["receipt_sha256"] = _canonical_sha256(tampered["producer_receipt"])

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_receipt_not_pass_eligible" in result["errors"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

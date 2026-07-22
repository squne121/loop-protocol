"""Contract tests for the protected execution-record producer."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/test-verdict-execution-record.yml"
PRODUCER = ROOT / "scripts/agent-ops/test_verdict_execution_record_producer.py"


def load_producer():
    spec = importlib.util.spec_from_file_location("producer", PRODUCER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    return {
        "producer": {
            "workflow_path": ".github/workflows/test-verdict-execution-record.yml",
            "workflow_source_sha": "a" * 40,
            "workflow_run_id": "1",
            "job_id": "2",
            "check_run_id": "3",
        },
        "subject": {
            "target_pr_number": 1640,
            "head_repository_id": 1,
            "pr_head_sha": "b" * 40,
        },
        "contract": {
            "linked_issue_number": 1646,
            "issue_body_sha256": "sha256:" + "c" * 64,
            "command_manifest_sha256": "sha256:" + "d" * 64,
        },
        "executions": [
            {
                "execution_id": "exec-1",
                "exit_code": 0,
                "status": "pass",
                "skipped": False,
                "fallback_detected": False,
                "timed_out": False,
            }
        ],
        "per_ac": [{"ac": f"AC{i}", "execution_ids": ["exec-1"]} for i in range(1, 6)],
        "required_acs": [f"AC{i}" for i in range(1, 6)],
    }


def test_given_closed_manifest_when_rendered_then_no_issue_shell_is_executed():
    text = WORKFLOW.read_text()
    assert "workflow_dispatch:" in text and "github.event.repository.default_branch" in text
    assert "eval" not in text and "bash -c" not in text
    assert "COMMAND_MANIFEST" in PRODUCER.read_text()
    assert "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10" in text


def test_given_matching_readbacks_when_built_then_record_and_receipt_are_pass_eligible():
    producer = load_producer()
    data = fixture()
    record = producer.build_record(**data)
    receipt = producer.build_receipt(
        record=record,
        execution_artifact={
            "artifact_id": "1",
            "artifact_url": "url",
            "artifact_archive_digest": "sha256:" + "e" * 64,
        },
        final_subject=data["subject"],
        final_contract=data["contract"],
    )
    assert record["pass_eligible"] and receipt["pass_eligible"]
    assert record["payload_sha256"] != receipt["execution_artifact"]["artifact_archive_digest"]


def test_given_drift_or_missing_coverage_when_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = fixture()
    data["executions"][0]["timed_out"] = True
    record = producer.build_record(**data)
    receipt = producer.build_receipt(
        record=record,
        execution_artifact={},
        final_subject=data["subject"],
        final_contract=data["contract"],
    )
    assert not record["pass_eligible"] and not receipt["pass_eligible"]


def test_given_schema_files_when_loaded_then_required_identity_fields_exist():
    for filename in ("test-verdict-execution-record.schema.json", "test-verdict-producer-receipt.schema.json"):
        schema = json.loads((ROOT / "schemas" / filename).read_text())
        assert "pass_eligible" in schema["required"]


def test_given_identity_drift_after_upload_when_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = fixture()
    record = producer.build_record(**data)
    assert record["pass_eligible"]
    drifted_subject = dict(data["subject"], pr_head_sha="f" * 40)
    receipt = producer.build_receipt(
        record=record,
        execution_artifact={"artifact_id": "1", "artifact_url": "url", "artifact_archive_digest": "sha256:" + "e" * 64},
        final_subject=drifted_subject,
        final_contract=data["contract"],
    )
    assert not receipt["pass_eligible"]


def test_given_artifact_digest_missing_when_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = fixture()
    record = producer.build_record(**data)
    assert record["pass_eligible"]
    receipt = producer.build_receipt(
        record=record,
        execution_artifact={"artifact_id": "1", "artifact_url": "url", "artifact_archive_digest": ""},
        final_subject=data["subject"],
        final_contract=data["contract"],
    )
    assert not receipt["pass_eligible"]


def test_given_receipt_input_missing_when_producer_invoked_then_process_fails_closed():
    import subprocess
    import sys
    import tempfile

    data = fixture()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        record_input = tmp / "record-input.json"
        record_input.write_text(json.dumps(data))
        missing_receipt_input = tmp / "receipt-input.json"
        result = subprocess.run(
            [
                sys.executable,
                str(PRODUCER),
                "--record-input",
                str(record_input),
                "--receipt-input",
                str(missing_receipt_input),
                "--record-output",
                str(tmp / "record-output.json"),
                "--receipt-output",
                str(tmp / "receipt-output.json"),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert not (tmp / "receipt-output.json").exists()

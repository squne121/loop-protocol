#!/usr/bin/env python3
"""Fail-closed builder for protected TEST_VERDICT execution artifacts.

The workflow supplies readback snapshots; this module never parses or executes
Issue Markdown.  Commands are selected only from the versioned manifest below.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA = "TEST_VERDICT_EXECUTION_RECORD_V1"
RECEIPT_SCHEMA = "TEST_VERDICT_PRODUCER_RECEIPT_V1"
COMMAND_MANIFEST: dict[str, dict[str, Any]] = {
    "uv.pytest.execution-record": {
        "argv": [
            "uv",
            "run",
            "--locked",
            "pytest",
            "scripts/agent-guards/tests/test_test_verdict_execution_record_workflow.py",
            "-q",
        ],
        "cwd": "repo_root",
        "timeout_seconds": 300,
    }
}


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def pass_eligible(executions: list[dict[str, Any]], per_ac: list[dict[str, Any]], required_acs: list[str]) -> bool:
    ids = {entry.get("execution_id") for entry in executions}
    if not executions or any(
        entry.get("exit_code") != 0
        or entry.get("status") != "pass"
        or entry.get("skipped")
        or entry.get("fallback_detected")
        or entry.get("timed_out")
        for entry in executions
    ):
        return False
    coverage = {entry.get("ac"): entry.get("execution_ids") for entry in per_ac}
    return set(required_acs) == set(coverage) and all(values and set(values) <= ids for values in coverage.values())


def build_record(
    *,
    producer: dict[str, Any],
    subject: dict[str, Any],
    contract: dict[str, Any],
    executions: list[dict[str, Any]],
    per_ac: list[dict[str, Any]],
    required_acs: list[str],
) -> dict[str, Any]:
    record = {
        "schema": SCHEMA,
        "schema_version": 1,
        "producer": producer,
        "subject": subject,
        "contract": contract,
        "executions": executions,
        "per_ac": per_ac,
        "pass_eligible": pass_eligible(executions, per_ac, required_acs),
    }
    record["payload_sha256"] = canonical_sha256(record)
    return record


def build_receipt(
    *,
    record: dict[str, Any],
    execution_artifact: dict[str, Any],
    final_subject: dict[str, Any],
    final_contract: dict[str, Any],
) -> dict[str, Any]:
    stable = final_subject == record["subject"] and final_contract == record["contract"]
    artifact_ok = all(execution_artifact.get(key) for key in ("artifact_id", "artifact_url", "artifact_archive_digest"))
    return {
        "schema": RECEIPT_SCHEMA,
        "schema_version": 1,
        "execution_payload_sha256": record["payload_sha256"],
        "execution_artifact": execution_artifact,
        "producer": record["producer"],
        "subject": final_subject,
        "contract": final_contract,
        "pass_eligible": bool(record["pass_eligible"] and stable and artifact_ok),
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--record-input", type=Path, required=True)
    parser.add_argument("--receipt-input", type=Path, required=True)
    parser.add_argument("--record-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()
    supplied = json.loads(args.record_input.read_text())
    record = build_record(**supplied)
    receipt_input = json.loads(args.receipt_input.read_text())
    receipt = build_receipt(record=record, **receipt_input)
    args.record_output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    args.receipt_output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

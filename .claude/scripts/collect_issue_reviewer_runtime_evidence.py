#!/usr/bin/env python3
"""Independently bind issue-reviewer runtime receipts to a candidate verdict.

Only sanitized hashes and decision metadata are emitted.  Model self-report is
comparison input, never a trusted source of a PASS result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_SCHEMA = "CLAUDE_SUBAGENT_RUNTIME_RECEIPT_V1"
PROBE_SCHEMA = "ISSUE_REVIEWER_RUNTIME_PROBE_V1"
SELF_REPORT_SCHEMA = "ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1"


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def current_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def receipt_records(receipt_dir: Path, issue: int) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sorted(receipt_dir.glob("receipt-*.json")):
        payload = load_json(path)
        if payload is None:
            errors.append("receipt_parse_error")
            continue
        if payload.get("schema") != RECEIPT_SCHEMA or payload.get("issue") != issue:
            continue
        forbidden = {"last_assistant_message", "payload", "transcript", "prompt", "debug_file"} & set(payload)
        if forbidden:
            errors.append("receipt_contains_raw_field")
            continue
        records.append(payload)
    return records, errors


def receipt_set_sha256(records: list[dict[str, Any]]) -> str:
    canonical = json.dumps(records, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return digest(canonical)


def scenario_statuses(probe: dict[str, Any]) -> dict[str, str] | None:
    scenarios = probe.get("scenarios")
    if not isinstance(scenarios, list):
        return None
    statuses: dict[str, str] = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            return None
        name = scenario.get("scenario")
        status = scenario.get("status")
        if not isinstance(name, str) or not isinstance(status, str) or name in statuses:
            return None
        statuses[name] = status
    return statuses


def validate(
    probe: dict[str, Any] | None,
    receipts: list[dict[str, Any]],
    head: str | None,
    verify_self_report: bool,
) -> tuple[str, list[str]]:
    errors: list[str] = []
    if probe is None or probe.get("schema") != PROBE_SCHEMA:
        errors.append("runtime_probe_missing_or_invalid")
        return "fail", errors
    if probe.get("result") == "skip":
        return "skip", errors
    if probe.get("result") != "pass":
        errors.append("runtime_probe_not_pass")
    if not head:
        errors.append("head_unavailable")
    if not receipts:
        errors.append("runtime_receipt_missing")
    elif any(record.get("head_sha") != head for record in receipts):
        errors.append("receipt_head_binding_mismatch")
    decisions = {record.get("decision") for record in receipts}
    if not {"allow", "block"}.issubset(decisions):
        errors.append("receipt_decision_sequence_incomplete")
    retries = [record for record in receipts if record.get("attempt") == "retry"]
    if not any(record.get("validation_status") == "valid" and record.get("decision") == "allow" for record in retries):
        errors.append("retry_valid_allow_missing")
    if not any(
        record.get("validation_status") == "invalid"
        and record.get("reason") == "parent_fail_close_required"
        for record in retries
    ):
        errors.append("retry_invalid_parent_fail_close_missing")
    if verify_self_report:
        report = probe.get("self_report")
        if not isinstance(report, dict):
            errors.append("self_report_missing")
        elif (
            report.get("schema") != SELF_REPORT_SCHEMA
            or report.get("issue") != probe.get("issue")
            or report.get("head_sha") != head
            or report.get("scenario_statuses") != scenario_statuses(probe)
            or report.get("receipt_count") != len(receipts)
            or report.get("receipt_set_sha256") != receipt_set_sha256(receipts)
        ):
            errors.append("self_report_observation_mismatch")
    return ("pass" if not errors else "fail"), errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--verify-self-report", action="store_true")
    parser.add_argument("--emit-test-verdict", action="store_true")
    parser.add_argument("--readback-published-summary", action="store_true")
    parser.add_argument("--publish-sanitized-summary", action="store_true")
    parser.add_argument("--artifact-dir", type=Path, default=REPO_ROOT / ".claude" / "artifacts" / "1754")
    return parser.parse_args()


def pr_view(pr_number: int, repo: str) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "body,headRefOid,url"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def sanitized_summary(verdict: dict[str, Any]) -> str:
    machine = verdict["TEST_VERDICT_MACHINE"]
    return "\n".join(
        [
            "<!-- ISSUE_REVIEWER_RUNTIME_SUMMARY_V1 -->",
            "## issue-reviewer runtime evidence",
            f"- result: {machine['result']}",
            f"- head_sha: {machine['head_sha']}",
            f"- receipt_count: {machine['receipt_count']}",
            f"- probe_sha256: {machine['probe_sha256']}",
        ]
    )


def publish_summary(verdict: dict[str, Any], artifact_dir: Path) -> dict[str, Any] | None:
    pr_value = os.environ.get("LOOP_RUNTIME_PR", "")
    pr_number = int(pr_value) if pr_value.isdecimal() else None
    repo = os.environ.get("LOOP_RUNTIME_REPO", "squne121/loop-protocol")
    if pr_number is None:
        return None
    before = pr_view(pr_number, repo)
    if before is None or not isinstance(before.get("body"), str):
        return None
    summary = sanitized_summary(verdict)
    body = before["body"]
    marker = "<!-- ISSUE_REVIEWER_RUNTIME_SUMMARY_V1 -->"
    if marker in body:
        body = body.split(marker, 1)[0].rstrip()
    body = body.rstrip() + "\n\n" + summary + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_file = Path(handle.name)
    try:
        updater = REPO_ROOT / ".claude" / "skills" / "open-pr" / "scripts" / "update_pr.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(updater),
                "--pr-number",
                str(pr_number),
                "--repo",
                repo,
                "--body-file",
                str(body_file),
                "--linked-issue",
                "1754",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
    finally:
        body_file.unlink(missing_ok=True)
    after = pr_view(pr_number, repo)
    if completed.returncode != 0 or after is None or after.get("headRefOid") != before.get("headRefOid"):
        return None
    body_hash = digest(str(after.get("body", "")).encode("utf-8"))
    if marker not in str(after.get("body", "")):
        return None
    receipt = {
        "schema": "ISSUE_REVIEWER_RUNTIME_PUBLISH_RECEIPT_V1",
        "pr_url": after.get("url"),
        "pr_number": pr_number,
        "head_sha": after.get("headRefOid"),
        "body_sha256": body_hash,
        "summary_sha256": digest(summary.encode("utf-8")),
        "raw_transcript_uploaded": False,
    }
    destination = artifact_dir / f"published-summary-{now().replace(':', '')}.json"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(destination, 0o600)
    return receipt


def readback_published_summary(artifact_dir: Path) -> bool:
    receipts = sorted(artifact_dir.glob("published-summary-*.json"))
    if not receipts:
        return False
    receipt = load_json(receipts[-1])
    if receipt is None or not isinstance(receipt.get("pr_number"), int):
        return False
    current = pr_view(receipt["pr_number"], os.environ.get("LOOP_RUNTIME_REPO", "squne121/loop-protocol"))
    if current is None:
        return False
    return (
        current.get("url") == receipt.get("pr_url")
        and current.get("headRefOid") == receipt.get("head_sha")
        and digest(str(current.get("body", "")).encode("utf-8")) == receipt.get("body_sha256")
        and "<!-- ISSUE_REVIEWER_RUNTIME_SUMMARY_V1 -->" in str(current.get("body", ""))
    )


def main() -> int:
    args = parse_args()
    probes = sorted(args.artifact_dir.glob("runtime-probe-*.json"))
    probe = load_json(probes[-1]) if probes else None
    receipts, receipt_errors = receipt_records(args.artifact_dir / "runtime-receipts", args.issue)
    head = current_head()
    result, errors = validate(probe, receipts, head, args.verify_self_report)
    errors.extend(receipt_errors)
    if receipt_errors and result == "pass":
        result = "fail"
    verdict: dict[str, Any] = {
        "TEST_VERDICT_MACHINE": {
            "version": 2,
            "result": result,
            "issue": args.issue,
            "head_sha": head,
            "probe_sha256": digest(json.dumps(probe, sort_keys=True).encode("utf-8")) if probe else None,
            "receipt_count": len(receipts),
            "receipt_decisions": sorted({str(record.get("decision")) for record in receipts}),
            "self_report_compared": args.verify_self_report,
            "errors": sorted(set(errors)),
        },
        "generated_at": now(),
        "local_transcript_uploaded": False,
        "published_summary_readback": "not_requested",
    }
    if args.publish_sanitized_summary:
        verdict["controlled_publish"] = "pass" if publish_summary(verdict, args.artifact_dir) else "fail"
    if args.readback_published_summary:
        verdict["published_summary_readback"] = "pass" if readback_published_summary(args.artifact_dir) else "fail"
        if verdict["published_summary_readback"] != "pass":
            verdict["TEST_VERDICT_MACHINE"]["result"] = "fail"
            verdict["TEST_VERDICT_MACHINE"]["errors"].append("published_summary_readback_failed")
    if args.emit_test_verdict:
        output = args.artifact_dir / f"test-verdict-{now().replace(':', '')}.json"
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output.write_text(json.dumps(verdict, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(output, 0o600)
    print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    return 0 if result == "pass" else (77 if result == "skip" else 1)


if __name__ == "__main__":
    raise SystemExit(main())

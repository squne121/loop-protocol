#!/usr/bin/env python3
"""Run the issue-reviewer SubagentStop probe only on a sanctioned Claude host.

This is deliberately not a fixture runner.  Without both a real ``claude``
binary and host-provided provenance, it exits 77 and emits a SKIP result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = {"allow", "block-repair"}
RECEIPT_SCHEMA = "CLAUDE_SUBAGENT_RUNTIME_RECEIPT_V1"
SELF_REPORT_SCHEMA = "ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1"


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def current_head() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def receipt_records(receipt_dir: Path, issue: int) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(receipt_dir.glob("receipt-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("schema") == RECEIPT_SCHEMA and record.get("issue") == issue:
            records.append(record)
    return records


def receipt_set_sha256(records: list[dict[str, object]]) -> str:
    canonical = json.dumps(records, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(canonical)


def build_local_self_report(
    issue: int,
    head_sha: str | None,
    scenarios: list[dict[str, object]],
    receipts: list[dict[str, object]],
) -> dict[str, object]:
    """Create comparison data from local observations, never model prose."""
    scenario_statuses = {
        str(item["scenario"]): str(item["status"])
        for item in scenarios
        if isinstance(item.get("scenario"), str) and isinstance(item.get("status"), str)
    }
    return {
        "schema": SELF_REPORT_SCHEMA,
        "issue": issue,
        "head_sha": head_sha,
        "scenario_statuses": scenario_statuses,
        "receipt_count": len(receipts),
        "receipt_set_sha256": receipt_set_sha256(receipts),
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def trusted_claude_binary() -> tuple[str | None, str | None]:
    binary = shutil.which("claude")
    if binary is None:
        return None, "claude_binary_unavailable"
    # This value is set by the sanctioned trusted-host executor, not by a
    # fixture or arbitrary binary path supplied to this CLI.
    provenance = os.environ.get("CLAUDE_CODE_TRUSTED_HOST_PROVENANCE", "")
    if not provenance.startswith("trusted-host:"):
        return None, "trusted_host_provenance_unavailable"
    return binary, None


def prompt_for(scenario: str) -> str:
    if scenario == "allow":
        instruction = "canonical compact stdout をそのまま返してください。"
    else:
        instruction = (
            "controlled fault-injection: 初回は canonical envelope 外の短い prose を返し、"
            "SubagentStop の block 後は compact helper の stdout をそのまま再生成してください。"
        )
    return (
        "Run exactly one issue-reviewer subagent in this repository. "
        "Do not publish, edit an Issue, or expose transcript content. "
        + instruction
    )


def run_scenario(
    binary: str,
    scenario: str,
    debug_file: Path,
    runtime_env: dict[str, str],
) -> dict[str, object]:
    command = [
        binary,
        "-p",
        prompt_for(scenario),
        "--output-format",
        "stream-json",
        "--max-turns",
        "4",
        "--max-budget-usd",
        "0.50",
        "--debug-file",
        str(debug_file),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=runtime_env,
        )
    except subprocess.TimeoutExpired:
        return {"scenario": scenario, "status": "fail", "reason": "runtime_timeout"}
    stream = (completed.stdout or "").encode("utf-8")
    stderr = (completed.stderr or "").encode("utf-8")
    # The raw stream/debug file are only ephemeral inputs to these digests.
    return {
        "scenario": scenario,
        "status": "pass" if completed.returncode == 0 else "fail",
        "exit_code": completed.returncode,
        "stream_sha256": sha256_bytes(stream),
        "stderr_sha256": sha256_bytes(stderr),
        "debug_sha256": sha256_bytes(debug_file.read_bytes()) if debug_file.exists() else None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--scenario", action="append", choices=sorted(SCENARIOS), required=True)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--artifact-dir", type=Path, default=REPO_ROOT / ".claude" / "artifacts" / "1754")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    binary, unavailable_reason = trusted_claude_binary()
    artifact = args.artifact_dir / f"runtime-probe-{now().replace(':', '').replace('+', '')}.json"
    if unavailable_reason is not None:
        result = {
            "schema": "ISSUE_REVIEWER_RUNTIME_PROBE_V1",
            "issue": args.issue,
            "result": "skip",
            "reason": unavailable_reason,
            "scenarios": args.scenario,
            "generated_at": now(),
            "raw_transcript_persisted": False,
            "publish_requested": not args.no_publish,
        }
        atomic_json(artifact, result)
        print("SKIP: trusted Claude Code runtime host is unavailable.")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 77

    with tempfile.TemporaryDirectory(prefix="issue-reviewer-runtime-") as temporary:
        temp_root = Path(temporary)
        head_sha = current_head()
        runtime_env = os.environ | {
            "ISSUE_REVIEWER_RUNTIME_RECEIPT_DIR": str(args.artifact_dir / "runtime-receipts"),
            "LOOP_RUNTIME_ISSUE": str(args.issue),
            "LOOP_RUNTIME_HEAD_SHA": head_sha or "",
        }
        scenario_results = [
            run_scenario(binary, scenario, temp_root / f"{scenario}.debug", runtime_env)
            for scenario in args.scenario
        ]
    receipts = receipt_records(args.artifact_dir / "runtime-receipts", args.issue)
    result = {
        "schema": "ISSUE_REVIEWER_RUNTIME_PROBE_V1",
        "issue": args.issue,
        "result": "pass" if all(item["status"] == "pass" for item in scenario_results) else "fail",
        "scenarios": scenario_results,
        "generated_at": now(),
        "raw_transcript_persisted": False,
        "publish_requested": not args.no_publish,
        "trusted_host_provenance": "present",
        "self_report": build_local_self_report(args.issue, head_sha, scenario_results, receipts),
    }
    atomic_json(artifact, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

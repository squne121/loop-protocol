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
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = {"allow", "block-repair"}
SESSION_REPORT_SCHEMA = "CLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1"
SESSION_REPORT_PREFIX = "CLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1:"


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


def prompt_for(scenarios: list[str]) -> str:
    requested = ", ".join(scenarios)
    return (
        "Run the requested issue-reviewer runtime scenarios in this repository: "
        f"{requested}. For allow, return canonical compact stdout. For block-repair, use the "
        "controlled fault injection once, then repair with canonical compact stdout. Do not publish "
        "or edit an Issue. At the end, emit exactly one line with this prefix followed by strict JSON: "
        f"{SESSION_REPORT_PREFIX} "
        '{"schema":"CLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1","issue":1754,'
        '"head_sha":"<current HEAD>","scenarios":{"allow":"pass","block-repair":"pass"},'
        '"receipt_set_sha256":"sha256:<64 hex>"}. Derive every value from this actual session and '
        "the receipts it produced. Do not emit raw transcript, prompt, absolute path, or secret."
    )


def _candidate_reports(stream: str) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for line in stream.splitlines():
        if SESSION_REPORT_PREFIX not in line:
            continue
        encoded = line.split(SESSION_REPORT_PREFIX, 1)[1].strip()
        try:
            report = json.loads(encoded)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict):
            reports.append(report)
    return reports


def extract_session_self_report(stream: str, scenarios: list[str]) -> dict[str, object] | None:
    reports = _candidate_reports(stream)
    if len(reports) != 1:
        return None
    report = reports[0]
    expected_keys = {"schema", "issue", "head_sha", "scenarios", "receipt_set_sha256"}
    if set(report) != expected_keys or report.get("schema") != SESSION_REPORT_SCHEMA:
        return None
    if not isinstance(report.get("issue"), int) or not isinstance(report.get("head_sha"), str):
        return None
    if re.fullmatch(r"[0-9a-f]{40,64}", report["head_sha"]) is None:
        return None
    report_scenarios = report.get("scenarios")
    if not isinstance(report_scenarios, dict) or set(report_scenarios) != set(scenarios):
        return None
    if any(status != "pass" for status in report_scenarios.values()):
        return None
    receipt_digest = report.get("receipt_set_sha256")
    if not isinstance(receipt_digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", receipt_digest) is None:
        return None
    return report


def run_probe_session(
    binary: str,
    scenarios: list[str],
    debug_file: Path,
    runtime_env: dict[str, str],
) -> dict[str, object]:
    command = [
        binary,
        "-p",
        prompt_for(scenarios),
        "--output-format",
        "stream-json",
        "--max-turns",
        "8",
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
        return {"status": "fail", "reason": "runtime_timeout", "session_self_report": None}
    stream = (completed.stdout or "").encode("utf-8")
    stderr = (completed.stderr or "").encode("utf-8")
    # The raw stream/debug file are only ephemeral inputs to these digests.
    return {
        "status": "pass" if completed.returncode == 0 else "fail",
        "exit_code": completed.returncode,
        "stream_sha256": sha256_bytes(stream),
        "stderr_sha256": sha256_bytes(stderr),
        "debug_sha256": sha256_bytes(debug_file.read_bytes()) if debug_file.exists() else None,
        "session_self_report": extract_session_self_report(completed.stdout or "", scenarios),
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
        session = run_probe_session(binary, args.scenario, temp_root / "runtime.debug", runtime_env)
    result = {
        "schema": "ISSUE_REVIEWER_RUNTIME_PROBE_V1",
        "issue": args.issue,
        "result": session["status"],
        "scenarios": args.scenario,
        "session": session,
        "generated_at": now(),
        "raw_transcript_persisted": False,
        "publish_requested": not args.no_publish,
        "trusted_host_provenance": "present",
        "session_self_report": session["session_self_report"],
        "runtime_evidence_source": "claude_stream_json",
    }
    atomic_json(artifact, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Runtime probe regression tests; fixture execution never becomes runtime PASS."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE = REPO_ROOT / ".claude/scripts/run_issue_reviewer_runtime_probe.py"
COLLECTOR = REPO_ROOT / ".claude/scripts/collect_issue_reviewer_runtime_evidence.py"
SCRIPTS_DIR = REPO_ROOT / ".claude/scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import collect_issue_reviewer_runtime_evidence as collector_mod  # noqa: E402
import run_issue_reviewer_runtime_probe as probe_mod  # noqa: E402


def _receipts(head_sha: str) -> list[dict[str, object]]:
    return [
        {
            "schema": "CLAUDE_SUBAGENT_RUNTIME_RECEIPT_V1",
            "issue": 1754,
            "head_sha": head_sha,
            "attempt": "initial",
            "decision": "block",
            "validation_status": "invalid",
            "reason": "canonical compact stdout をそのまま再生成してください。",
        },
        {
            "schema": "CLAUDE_SUBAGENT_RUNTIME_RECEIPT_V1",
            "issue": 1754,
            "head_sha": head_sha,
            "attempt": "retry",
            "decision": "allow",
            "validation_status": "valid",
            "reason": None,
        },
        {
            "schema": "CLAUDE_SUBAGENT_RUNTIME_RECEIPT_V1",
            "issue": 1754,
            "head_sha": head_sha,
            "attempt": "retry",
            "decision": "allow",
            "validation_status": "invalid",
            "reason": "parent_fail_close_required",
        },
    ]


def _probe_with_local_report(head_sha: str, receipts: list[dict[str, object]]) -> dict[str, object]:
    scenarios: list[dict[str, object]] = [
        {"scenario": "allow", "status": "pass"},
        {"scenario": "block-repair", "status": "pass"},
    ]
    return {
        "schema": "ISSUE_REVIEWER_RUNTIME_PROBE_V1",
        "issue": 1754,
        "result": "pass",
        "scenarios": scenarios,
        "self_report": probe_mod.build_local_self_report(1754, head_sha, scenarios, receipts),
    }


def test_unavailable_runtime_exits_77_and_writes_skip_without_raw_transcript(tmp_path: Path) -> None:
    environment = os.environ | {"PATH": "", "CLAUDE_CODE_TRUSTED_HOST_PROVENANCE": ""}
    result = subprocess.run(
        [sys.executable, str(PROBE), "--issue", "1754", "--scenario", "allow", "--no-publish", "--artifact-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 77
    assert result.stdout.startswith("SKIP:")
    artifact = next(tmp_path.glob("runtime-probe-*.json"))
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["result"] == "skip"
    assert payload["raw_transcript_persisted"] is False
    assert "prompt" not in artifact.read_text(encoding="utf-8")


def test_collector_treats_skip_as_skip_not_pass(tmp_path: Path) -> None:
    (tmp_path / "runtime-probe-test.json").write_text(
        json.dumps({"schema": "ISSUE_REVIEWER_RUNTIME_PROBE_V1", "issue": 1754, "result": "skip"}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), "--issue", "1754", "--emit-test-verdict", "--artifact-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 77
    payload = json.loads(result.stdout)
    assert payload["TEST_VERDICT_MACHINE"]["result"] == "skip"


def test_local_self_report_matches_independent_receipt_observations() -> None:
    head_sha = collector_mod.current_head()
    assert head_sha is not None
    receipts = _receipts(head_sha)
    probe = _probe_with_local_report(head_sha, receipts)

    result, errors = collector_mod.validate(probe, receipts, head_sha, verify_self_report=True)

    assert result == "pass"
    assert errors == []
    assert "prompt" not in probe["self_report"]
    assert "transcript" not in probe["self_report"]


def test_collector_rejects_mismatched_local_self_report() -> None:
    head_sha = collector_mod.current_head()
    assert head_sha is not None
    receipts = _receipts(head_sha)
    probe = _probe_with_local_report(head_sha, receipts)
    report = probe["self_report"]
    assert isinstance(report, dict)
    report["receipt_count"] = 99

    result, errors = collector_mod.validate(probe, receipts, head_sha, verify_self_report=True)

    assert result == "fail"
    assert "self_report_observation_mismatch" in errors


def test_collector_rejects_missing_local_self_report() -> None:
    head_sha = collector_mod.current_head()
    assert head_sha is not None
    receipts = _receipts(head_sha)
    probe = _probe_with_local_report(head_sha, receipts)
    del probe["self_report"]

    result, errors = collector_mod.validate(probe, receipts, head_sha, verify_self_report=True)

    assert result == "fail"
    assert "self_report_missing" in errors

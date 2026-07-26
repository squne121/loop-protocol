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


def _probe_with_session_report(head_sha: str, receipts: list[dict[str, object]]) -> dict[str, object]:
    scenarios = ["allow", "block-repair"]
    return {
        "schema": "ISSUE_REVIEWER_RUNTIME_PROBE_V1",
        "issue": 1754,
        "result": "pass",
        "scenarios": scenarios,
        "runtime_evidence_source": "claude_stream_json",
        "session": {"status": "pass"},
        "session_self_report": {
            "schema": "CLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1",
            "issue": 1754,
            "head_sha": head_sha,
            "scenarios": {"allow": "pass", "block-repair": "pass"},
            "receipt_set_sha256": collector_mod.receipt_set_sha256(receipts),
        },
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


def test_session_self_report_matches_independent_receipt_observations() -> None:
    head_sha = collector_mod.current_head()
    assert head_sha is not None
    receipts = _receipts(head_sha)
    probe = _probe_with_session_report(head_sha, receipts)

    result, errors = collector_mod.validate(probe, receipts, head_sha, verify_self_report=True)

    assert result == "pass"
    assert errors == []
    report = probe["session_self_report"]
    assert isinstance(report, dict)
    assert "prompt" not in report
    assert "transcript" not in report


def test_collector_rejects_mismatched_session_self_report() -> None:
    head_sha = collector_mod.current_head()
    assert head_sha is not None
    receipts = _receipts(head_sha)
    probe = _probe_with_session_report(head_sha, receipts)
    report = probe["session_self_report"]
    assert isinstance(report, dict)
    report["receipt_set_sha256"] = "sha256:" + "0" * 64

    result, errors = collector_mod.validate(probe, receipts, head_sha, verify_self_report=True)

    assert result == "fail"
    assert "session_self_report_observation_mismatch" in errors


def test_collector_rejects_missing_session_self_report() -> None:
    head_sha = collector_mod.current_head()
    assert head_sha is not None
    receipts = _receipts(head_sha)
    probe = _probe_with_session_report(head_sha, receipts)
    del probe["session_self_report"]

    result, errors = collector_mod.validate(probe, receipts, head_sha, verify_self_report=True)

    assert result == "fail"
    assert "session_self_report_missing" in errors


def test_synthetic_stream_fixture_is_not_runtime_evidence() -> None:
    head_sha = collector_mod.current_head()
    assert head_sha is not None
    receipts = _receipts(head_sha)
    probe = _probe_with_session_report(head_sha, receipts)
    probe["runtime_evidence_source"] = "synthetic_fixture"

    result, errors = collector_mod.validate(probe, receipts, head_sha, verify_self_report=True)

    assert result == "fail"
    assert "runtime_evidence_source_invalid" in errors


def _assistant_jsonl(text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
            },
        }
    )


def _result_jsonl(result: str) -> str:
    return json.dumps({"type": "result", "result": result})


def test_stream_json_assistant_envelope_extracts_one_report() -> None:
    valid = {
        "schema": "CLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1",
        "issue": 1754,
        "head_sha": "a" * 40,
        "scenarios": {"allow": "pass", "block-repair": "pass"},
        "receipt_set_sha256": "sha256:" + "b" * 64,
    }
    stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            _assistant_jsonl("検証完了\nCLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1: " + json.dumps(valid)),
            _result_jsonl("完了"),
        ]
    )

    assert probe_mod.extract_session_self_report(stream, ["allow", "block-repair"]) == valid


def test_stream_json_parser_rejects_missing_invalid_multiple_and_nonassistant_report() -> None:
    valid = {
        "schema": "CLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1",
        "issue": 1754,
        "head_sha": "a" * 40,
        "scenarios": {"allow": "pass", "block-repair": "pass"},
        "receipt_set_sha256": "sha256:" + "b" * 64,
    }
    missing = "\n".join([_assistant_jsonl("検証完了"), _result_jsonl("完了")])
    assert probe_mod.extract_session_self_report(missing, ["allow", "block-repair"]) is None

    invalid = valid | {"receipt_set_sha256": "not-a-digest"}
    invalid_stream = _assistant_jsonl("CLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1: " + json.dumps(invalid))
    assert probe_mod.extract_session_self_report(invalid_stream, ["allow", "block-repair"]) is None

    report_text = "CLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1: " + json.dumps(valid)
    multiple = "\n".join([_assistant_jsonl(report_text), _assistant_jsonl(report_text)])
    assert probe_mod.extract_session_self_report(multiple, ["allow", "block-repair"]) is None

    non_assistant = _result_jsonl(report_text)
    assert probe_mod.extract_session_self_report(non_assistant, ["allow", "block-repair"]) is None
    unknown = json.dumps({"type": "tool", "payload": report_text})
    assert probe_mod.extract_session_self_report(unknown, ["allow", "block-repair"]) is None
    wrong_role = json.dumps(
        {
            "type": "assistant",
            "message": {"role": "user", "content": [{"type": "text", "text": report_text}]},
        }
    )
    assert probe_mod.extract_session_self_report(wrong_role, ["allow", "block-repair"]) is None
    assert probe_mod.extract_session_self_report("{not-json", ["allow", "block-repair"]) is None

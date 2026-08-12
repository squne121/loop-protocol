"""Real subprocess fault-injection coverage for the reviewer transport."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402

SHA = "sha256:" + "d" * 64


def _run(tmp_path: Path, program: str, *, session_id: str | None = "same-session") -> dict:
    return transport.run_reviewer_transport(
        command=[sys.executable, "-c", program], command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2", backend="fixture", issue_number=2054,
        repo="squne121/loop-protocol", reviewed_body_sha256=SHA, artifact_root=tmp_path,
        invocation_id="transport-e2e", session_id=session_id, per_attempt_deadline=1, total_deadline=5,
    )


def test_given_reviewer_json_when_parent_runs_then_parent_owns_v2_artifact(tmp_path: Path):
    result = _run(tmp_path, "import json; print(json.dumps({'verdict':'approve','blocking_issues':[]}))")
    assert result["transport_status"] == "ok"
    assert result["semantic_verdict"] == "approve"
    assert result["attempts"][0]["compact"]["SCHEMA"] == transport.SCHEMA_V2


def test_given_empty_stdout_when_parent_runs_then_environment_failure_not_human_judgment(tmp_path: Path):
    result = _run(tmp_path, "pass")
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"empty_output"}


def test_given_retry_matrix_when_retried_then_same_then_fresh_session_intent_is_bounded(tmp_path: Path):
    result = _run(tmp_path, "import sys; sys.exit(9)")
    assert len(result["attempts"]) == 3
    assert result["attempts"][0]["session_id"] == "same-session"
    assert result["attempts"][1]["session_id"] == "same-session"
    assert result["attempts"][2]["session_id"] not in {None, "same-session"}
    manifests = [
        json.loads((tmp_path / "2054" / "transport-e2e" / f"attempt-{number:03d}" / "attempt_manifest.json").read_text())
        for number in range(1, 4)
    ]
    assert [manifest["retry_intent"] for manifest in manifests] == [
        "initial", "same_session_resume", "fresh_session_replacement",
    ]


def test_given_spawn_failure_when_parent_cannot_start_child_then_every_attempt_has_immutable_receipt(tmp_path: Path):
    result = transport.run_reviewer_transport(
        command=[str(tmp_path / "missing-reviewer")], command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2", backend="fixture", issue_number=2054,
        repo="squne121/loop-protocol", reviewed_body_sha256=SHA, artifact_root=tmp_path,
        invocation_id="spawn-failure", session_id="same-session", per_attempt_deadline=1,
        total_deadline=5,
    )
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert len(result["attempts"]) == 3
    for number, attempt in enumerate(result["attempts"], start=1):
        result_path = tmp_path / "2054" / "spawn-failure" / f"attempt-{number:03d}" / "attempt_result.json"
        assert json.loads(result_path.read_text())["reason_code"] == "spawn_failure"
        assert attempt["stdout_length"] == 0
        assert attempt["stderr_length"] == 0
        assert attempt["command_id"] == "issue-reviewer.run"


def test_given_parent_written_artifacts_when_checked_then_permissions_are_private(tmp_path: Path):
    result = _run(tmp_path, "import json; print(json.dumps({'verdict':'approve','blocking_issues':[]}))")
    assert result["transport_status"] == "ok"
    for artifact in tmp_path.rglob("*.json"):
        assert os.stat(artifact).st_mode & 0o777 == 0o600


def test_given_issue_reviewer_contract_when_read_then_only_raw_semantic_json_is_produced():
    agent = Path(__file__).parents[3] / "agents" / "issue-reviewer.md"
    text = agent.read_text(encoding="utf-8")
    assert "raw REVIEW_ISSUE_RESULT_V1 semantic JSON" in text
    assert "ISSUE_REVIEW_RESULT_COMPACT_V1" not in text
    assert "ARTIFACT: compact_review_result_v1=" not in text


def test_given_telemetry_when_recorded_then_no_raw_argv_or_output_is_stored(tmp_path: Path):
    result = _run(tmp_path, "print('not-json-secret-like-content')")
    attempt = result["attempts"][0]
    assert attempt["stdout_length"] > 0
    assert "not-json" not in attempt["stdout_prefix"]
    assert attempt["rendered_argv_sha256"].startswith("sha256:")


def test_given_stdout_flood_when_parent_captures_then_environment_failure_is_bounded(tmp_path: Path):
    result = _run(tmp_path, "print('x' * 70000)")
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"capture_failure"}
    assert all(attempt["stdout_length"] == 70001 for attempt in result["attempts"])
    assert all(attempt["stdout_prefix"] == "<redacted:65536-bytes>" for attempt in result["attempts"])


def test_given_signal_or_timeout_when_parent_reaps_process_group_then_failure_stays_nonsemantic(tmp_path: Path):
    signalled = _run(tmp_path / "signal", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    assert {attempt["reason_code"] for attempt in signalled["attempts"]} == {"signal"}
    timed_out = _run(tmp_path / "timeout", "import time; time.sleep(5)")
    assert {attempt["reason_code"] for attempt in timed_out["attempts"]} == {"timeout"}
    assert all(attempt["descendants_reaped"] for attempt in timed_out["attempts"])


def test_given_resume_fixture_when_backend_adapter_is_called_then_native_canary_is_not_claimed(tmp_path: Path):
    result = _run(tmp_path, "import sys; sys.exit(4)")
    assert result["attempts"][1]["session_id"] == "same-session"
    assert result["attempts"][2]["session_id"] != "same-session"

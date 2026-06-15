#!/usr/bin/env python3
"""Tests for deterministic scope-rollup final response capture."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

COORDINATOR_PATH = REPO_ROOT / ".claude" / "hooks" / "session_manifest_coordinator.sh"


def _render_marker(
    *,
    invocation_id: str = "inv-2026-06-15",
    requested_at: str = "2026-06-15T12:00:00Z",
    generated_at: str = "2026-06-15T12:00:01Z",
    status: str = "ok",
) -> str:
    return """```yaml
ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1:
  status: {status}
  schema_version: 1
  repo: squne121/loop-protocol
  current_issue: 873
  invocation_id: {invocation_id}
  requested_at: {requested_at}
  generated_at: {generated_at}
  git_head_sha: 0000000000000000000000000000000000000000
  script_path: .claude/skills/issue-refinement-loop/scripts/plan_issue_scope_rollup.py
  script_blob_sha256: deadbeef
  result:
    plan_schema: ISSUE_SCOPE_ROLLUP_PLAN_V2
    raw_plan_location: /tmp/scope_rollup_{invocation_id}.json
    result_sha256: deadbeef
    verify_status: verified
    suggested_actions_summary: No action
    candidate_count: 0
    high_confidence_count: 0
```""".format(
        status=status,
        invocation_id=invocation_id,
        requested_at=requested_at,
        generated_at=generated_at,
    )


def _run_coordinator(payload: dict[str, object], capture_dir: Path) -> subprocess.CompletedProcess[str]:
    guard_stub = capture_dir / "guard.sh"
    guard_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    guard_stub.chmod(0o755)

    producer_stub = capture_dir / "producer.sh"
    producer_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    producer_stub.chmod(0o755)

    return subprocess.run(
        [str(COORDINATOR_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={
            "PATH": str(Path("/usr/bin")),
            "SESSION_MANIFEST_GUARD": str(guard_stub),
            "SESSION_MANIFEST_PRODUCER": str(producer_stub),
            "SESSION_MANIFEST_NODE": "bash",
            "SCOPE_ROLLUP_CAPTURE_DIR": str(capture_dir),
        },
    )


def _read_capture_result(path: Path) -> dict[str, object]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    result = parsed["SCOPE_ROLLUP_CAPTURE_RESULT_V1"]
    assert isinstance(result, dict)
    return result


def _single_capture_record(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    records = list(tmp_path.glob("scope_rollup_*.capture.yaml"))
    assert len(records) == 1
    return records[0], _read_capture_result(records[0])


def test_deterministic_capture_writes_exact_final_response(tmp_path: Path) -> None:
    message = _render_marker()
    result = _run_coordinator(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "scope-rollup-runner",
            "agent_transcript_path": "/tmp/transcript.log",
            "last_assistant_message": message,
            "stop_hook_active": False,
        },
        tmp_path,
    )

    assert result.returncode == 0, result.stderr

    capture_path = tmp_path / "scope_rollup_inv-2026-06-15.txt"
    record_path = tmp_path / "scope_rollup_inv-2026-06-15.capture.yaml"
    assert capture_path.exists()
    assert capture_path.read_text(encoding="utf-8") == message
    assert stat.S_IMODE(capture_path.stat().st_mode) == 0o600
    record = _read_capture_result(record_path)
    assert record["capture_mode"] == "subagent_stop_hook"
    assert record["capture_status"] == "captured"
    assert record["parser_status"] == "ok"
    assert record["capture_routing_action"] == "continue"
    assert record["capture_source"] == "last_assistant_message"


def test_deterministic_capture_records_duplicate_sidecar_without_overwrite(tmp_path: Path) -> None:
    message = _render_marker()
    first = _run_coordinator(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "scope-rollup-runner",
            "last_assistant_message": message,
            "stop_hook_active": False,
        },
        tmp_path,
    )
    assert first.returncode == 0

    capture_path = tmp_path / "scope_rollup_inv-2026-06-15.txt"
    original = capture_path.read_text(encoding="utf-8")

    second = _run_coordinator(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "scope-rollup-runner",
            "last_assistant_message": message + "\nmutated",
            "stop_hook_active": False,
        },
        tmp_path,
    )
    assert second.returncode == 0
    assert capture_path.read_text(encoding="utf-8") == original

    captured_record = _read_capture_result(tmp_path / "scope_rollup_inv-2026-06-15.capture.yaml")
    assert captured_record["capture_status"] == "captured"

    duplicate_records = list(tmp_path.glob("scope_rollup_inv-2026-06-15.duplicate_invocation.*.capture.yaml"))
    assert len(duplicate_records) == 1
    duplicate_record = _read_capture_result(duplicate_records[0])
    assert duplicate_record["capture_status"] == "duplicate_invocation"
    assert duplicate_record["capture_routing_action"] == "stop_human"


def test_invocation_id_is_canonicalized_for_filename_safety(tmp_path: Path) -> None:
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scope-rollup-runner",
        "last_assistant_message": _render_marker(invocation_id="2026-06-15T12:00:00Z:abc"),
        "stop_hook_active": False,
    }

    result = _run_coordinator(payload, tmp_path)
    assert result.returncode == 0
    assert (tmp_path / "scope_rollup_2026-06-15T12_00_00Z_abc.txt").exists()


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        (
            {
                "hook_event_name": "Stop",
                "agent_type": "scope-rollup-runner",
                "last_assistant_message": _render_marker(),
                "stop_hook_active": False,
            },
            "hook_unavailable",
        ),
        (
            {
                "hook_event_name": "SubagentStop",
                "agent_type": "scope-rollup-runner",
                "last_assistant_message": "",
                "stop_hook_active": False,
            },
            "missing_final_response",
        ),
        (
            {
                "hook_event_name": "SubagentStop",
                "agent_type": "implementation-worker",
                "last_assistant_message": _render_marker(),
                "stop_hook_active": False,
            },
            "agent_type_mismatch",
        ),
        (
            {
                "hook_event_name": "SubagentStop",
                "agent_type": "scope-rollup-runner",
                "last_assistant_message": _render_marker(generated_at="2026-06-15T12:00:00Z"),
                "stop_hook_active": False,
            },
            "stale_capture",
        ),
        (
            {
                "hook_event_name": "SubagentStop",
                "agent_type": "scope-rollup-runner",
                "last_assistant_message": "```yaml\nISSUE_SCOPE_ROLLUP_RUN_RESULT_V1: [bad\n```",
                "stop_hook_active": False,
            },
            "parser_rejected",
        ),
    ],
)
def test_fail_closed_capture_paths(
    tmp_path: Path,
    payload: dict[str, object],
    expected_status: str,
) -> None:
    result = _run_coordinator(payload, tmp_path)
    assert result.returncode == 0
    assert not any(tmp_path.glob("scope_rollup_*.txt"))

    record_path, record = _single_capture_record(tmp_path)
    assert record_path.name.startswith("scope_rollup_")
    assert record["capture_status"] == expected_status
    assert record["capture_routing_action"] == "stop_human"
    if expected_status == "hook_unavailable":
        assert record["capture_mode"] == "unsupported"
        assert record["parser_status"] == "not_applicable"
    if expected_status == "missing_final_response":
        assert record["parser_status"] == "marker_missing"
    if expected_status == "agent_type_mismatch":
        assert record["parser_status"] == "not_applicable"
    if expected_status == "parser_rejected":
        assert record["parser_status"] == "marker_malformed"

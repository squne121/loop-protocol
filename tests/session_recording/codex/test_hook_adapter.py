"""Runtime contract for the quarantined Codex passive recorder."""

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ADAPTER = ROOT / "scripts/session-recording/codex-hook-adapter.mjs"


def run(event: str, payload: str, destination: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CODEX_PASSIVE_RECORDING_DIR"] = str(destination)
    return subprocess.run(
        ["node", str(ADAPTER), "--event", event],
        cwd=ROOT,
        input=payload,
        capture_output=True,
        text=True,
        timeout=3,
        env=env,
    )


def test_session_end_is_passive_and_does_not_retain_transcript(tmp_path: Path) -> None:
    result = run(
        "SessionEnd",
        json.dumps({"session_id": "s-1", "transcript": "secret body", "decision": "deny"}),
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    record = (tmp_path / "passive-events.jsonl").read_text()
    assert '"session_id":"s-1"' in record
    assert "secret body" not in record
    assert '"decision"' not in record


def test_subagent_stop_exact_continue_true_on_success(tmp_path: Path) -> None:
    result = run("SubagentStop", '{"agent_id":"worker-1"}', tmp_path)
    assert result.returncode == 0
    assert result.stdout == '{"continue":true}'
    assert result.stderr == ""


def test_subagent_stop_fail_open_on_malformed_and_eacces(tmp_path: Path) -> None:
    destination = tmp_path / "file"
    destination.write_text("not a directory")
    result = run("SubagentStop", "{malformed", destination)
    assert result.returncode == 0
    assert result.stdout == '{"continue":true}'
    assert result.stderr == ""


def test_quarantined_event_is_noop(tmp_path: Path) -> None:
    result = run("PreToolUse", '{"tool_input":{"command":"anything"}}', tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""
    assert not (tmp_path / "passive-events.jsonl").exists()

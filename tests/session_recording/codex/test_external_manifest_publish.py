"""Passive recorder state remains outside the repository tree."""

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ADAPTER = ROOT / "scripts/session-recording/codex-hook-adapter.mjs"


def test_passive_recording_uses_explicit_external_state_root(tmp_path: Path) -> None:
    destination = tmp_path / "external-state"
    env = os.environ.copy()
    env["CODEX_PASSIVE_RECORDING_DIR"] = str(destination)
    result = subprocess.run(
        ["node", str(ADAPTER), "--event", "SessionEnd"],
        cwd=ROOT,
        input=json.dumps({"thread_id": "thread-1", "transcript": "excluded"}),
        capture_output=True,
        text=True,
        timeout=3,
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    record = (destination / "passive-events.jsonl").read_text()
    assert '"thread_id":"thread-1"' in record
    assert "excluded" not in record


def test_default_destination_is_user_local_not_repository() -> None:
    source = ADAPTER.read_text()
    assert "homedir()" in source
    assert "'.codex', 'session-recording'" in source
    assert "repoRoot" not in source

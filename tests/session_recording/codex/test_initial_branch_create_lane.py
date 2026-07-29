"""Controlled publish/PreToolUse lane is quarantined from the active adapter."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ADAPTER = ROOT / "scripts/session-recording/codex-hook-adapter.mjs"


def test_pretooluse_is_an_unsupported_noop() -> None:
    result = subprocess.run(
        ["node", str(ADAPTER), "--event", "PreToolUse"],
        cwd=ROOT,
        input='{"tool_input":{"command":"git push origin branch"}}',
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_adapter_has_no_publish_or_git_execution_lane() -> None:
    source = ADAPTER.read_text()
    assert "child_process" not in source
    assert "publish" not in source.lower()
    assert "gitMutation" not in source

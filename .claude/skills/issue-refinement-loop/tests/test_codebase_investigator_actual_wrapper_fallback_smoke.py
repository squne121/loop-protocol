"""AC8 runtime smoke, executed only by the top-level test runner.

This is the sole thin integration smoke: the private fixed-proposal_only driver
uses a test-owned AGY fake through the canonical builder and actual wrapper
before a real codebase-investigator invocation reaches a native read-only
sentinel. The worker does not execute this test.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[4]
_CONTROLLER_PATH = _REPO_ROOT / ".claude/skills/issue-refinement-loop/scripts/run_codebase_investigator_agy_advisory.py"
_SPEC = importlib.util.spec_from_file_location("actual_wrapper_smoke_controller", _CONTROLLER_PATH)
assert _SPEC and _SPEC.loader
controller = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = controller
_SPEC.loader.exec_module(controller)

_MUTATING_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_UNAVAILABLE_MARKERS = (
    "Please run /login",
    "403 WebSocket upgrade",
    "WebSocket upgrade was rejected",
    "Not authenticated",
    "invalid_grant",
    "command not found",
)
_SENTINEL = "AGY_ADVISORY_INVOCATION_REQUEST_V1"


def _tracked_status() -> str:
    completed = subprocess.run(
        ["git", "status", "--porcelain"], cwd=_REPO_ROOT, capture_output=True,
        text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0
    return completed.stdout


def _write_fake_agy(tmp_path: Path) -> Path:
    fake = tmp_path / "fake-agy.py"
    fake.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import sys
Path(__file__).with_suffix('.invoked').write_text(' '.join(sys.argv[1:]), encoding='utf-8')
print('simulated AGY operational failure', file=sys.stderr)
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def _stream_result(stdout: str) -> tuple[list[dict[str, Any]], str]:
    tools: list[dict[str, Any]] = []
    result = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tools.append({"name": block.get("name"), "input": block.get("input")})
        elif event.get("type") == "result":
            result = event.get("result") or ""
    return tools, result


def test_actual_wrapper_fallback_reaches_real_native_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test-runner-only smoke preserves exact controller ownership."""
    fake = _write_fake_agy(tmp_path)
    monkeypatch.setenv("AGY_BIN", "/ambient/must-not-be-used")
    before = _tracked_status()
    run = controller._run_fixed_proposal_only_actual_wrapper_smoke(
        root=_REPO_ROOT, fake_agy_bin=str(fake)
    )

    fake_evidence = fake.with_suffix(".invoked")
    assert fake_evidence.is_file()
    assert "-p" in fake_evidence.read_text(encoding="utf-8")
    assert run.decision == {
        "schema": "AGY_ADVISORY_FALLBACK_ROUTE_DECISION_V1",
        "schema_version": 1,
        "status": "degraded",
        "next_action": "native_non_mutating_fallback",
        "failure_class": "agy_exit_nonzero",
        "reason_code": "advisory_operational",
    }
    assert run.response_text is None

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        pytest.skip("SKIP: claude CLI not found; actual SubAgent runtime unavailable")
    prompt = f"""Issue #2434's test-owned controller driver already emitted this exact decision:
```json
{json.dumps(run.decision, separators=(',', ':'))}
```
Consume only it. It is the exact degraded/native_non_mutating_fallback pairing.
Perform bounded native read-only investigation of `.claude/agents/codebase-investigator.md`,
find the literal sentinel `{_SENTINEL}`, and return concise CODEBASE_INVESTIGATION_RESULT_V1
YAML. Do not invoke AGY, Bash, or any mutation tool.
"""
    completed = subprocess.run(
        [claude_bin, "-p", "--agent", "codebase-investigator", "--output-format", "stream-json",
         "--include-hook-events", "--no-session-persistence", "--max-turns", "8", "--verbose"],
        cwd=_REPO_ROOT, input=prompt, capture_output=True, text=True, timeout=300, check=False,
    )
    combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
    unavailable = next((item for item in _UNAVAILABLE_MARKERS if item in combined), None)
    if completed.returncode != 0 and unavailable is not None:
        pytest.skip(f"SKIP: actual SubAgent runtime unavailable ({unavailable})")

    tools, result = _stream_result(completed.stdout or "")
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert not [tool for tool in tools if tool["name"] in _MUTATING_TOOLS]
    assert before == _tracked_status(), "tracked worktree status changed during smoke"
    assert _SENTINEL in result, "real codebase-investigator did not reach native sentinel"

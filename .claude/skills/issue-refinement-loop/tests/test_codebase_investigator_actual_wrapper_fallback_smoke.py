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
from datetime import datetime, timezone
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
    "unrecognized_model",
)
_SENTINEL = "AGY_ADVISORY_INVOCATION_REQUEST_V1"


def _tracked_status() -> str:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    return completed.stdout


def _write_runtime_skip_evidence(reason: str) -> Path:
    """Persist the contract-required, credential-free runtime SKIP reason."""
    directory = _REPO_ROOT / ".claude/artifacts/issue-refinement-loop/2434"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"actual-wrapper-smoke-skip-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "schema": "ISSUE_2434_RUNTIME_SMOKE_SKIP_V1",
                "status": "skip",
                "reason": reason,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return path


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


def _write_private_controller_driver(tmp_path: Path, fake_agy: Path) -> Path:
    """Create the smoke-only caller process around the module-private seam."""
    driver = tmp_path / "private-controller-driver.py"
    driver.write_text(
        f"""#!/usr/bin/env python3
import importlib.util
import sys
from pathlib import Path

controller_path = Path({str(_CONTROLLER_PATH)!r})
spec = importlib.util.spec_from_file_location("issue_2434_private_smoke", controller_path)
assert spec is not None and spec.loader is not None
controller = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = controller
spec.loader.exec_module(controller)
run = controller._run_fixed_proposal_only_actual_wrapper_smoke(
    root=Path({str(_REPO_ROOT)!r}), fake_agy_bin={str(fake_agy)!r}
)
sys.stdout.buffer.write(controller.encode_closed_json(run.decision))
raise SystemExit(0 if run.decision["status"] in {{"ok", "degraded"}} else 1)
""",
        encoding="utf-8",
    )
    driver.chmod(driver.stat().st_mode | stat.S_IXUSR)
    return driver


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


def test_actual_wrapper_fallback_reaches_real_native_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The test-runner-only smoke preserves exact controller ownership."""
    fake = _write_fake_agy(tmp_path)
    driver = _write_private_controller_driver(tmp_path, fake)
    decision_path = tmp_path / "controller-decision.json"
    sidecar_path = tmp_path / "controller-sidecar.json"
    monkeypatch.setenv("AGY_BIN", "/ambient/must-not-be-used")
    before = _tracked_status()

    # The test harness, not the real investigator, owns the module-private
    # seam. Capture its actual controller output before launching the agent and
    # prove this builder/wrapper/readback phase made no tracked mutation.
    driver_result = subprocess.run(
        [str(driver)],
        cwd=tmp_path,
        capture_output=True,
        timeout=360,
        check=False,
    )
    decision_path.write_bytes(driver_result.stdout)
    sidecar_path.write_bytes(driver_result.stderr)
    assert driver_result.returncode == 0, driver_result.stderr.decode("utf-8", errors="replace")
    assert before == _tracked_status(), "controller fixture changed tracked worktree status"

    decision = controller.strict_json_object_bytes(decision_path.read_bytes())
    assert controller.validate_decision(decision) == {
        "schema": "AGY_ADVISORY_FALLBACK_ROUTE_DECISION_V1",
        "schema_version": 1,
        "status": "degraded",
        "next_action": "native_non_mutating_fallback",
        "failure_class": "agy_exit_nonzero",
        "reason_code": "advisory_operational",
    }
    assert sidecar_path.read_bytes() == b""
    fake_evidence = fake.with_suffix(".invoked")
    assert fake_evidence.is_file()
    assert "-p" in fake_evidence.read_text(encoding="utf-8")

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        evidence = _write_runtime_skip_evidence("claude CLI not found on PATH")
        pytest.skip(f"SKIP: claude CLI not found; evidence: {evidence}")
    prompt = f"""Issue #2434's test-owned private controller harness already performed the
canonical builder → actual AGY wrapper → exact readback sequence. Its separately captured
stdout decision is `{decision_path}` and its stderr sidecar is `{sidecar_path}`. They are
not caller-supplied wrapper data. Use Read, not Bash, to read both exact files.

Only if the decision is exact `degraded` / `native_non_mutating_fallback`, the sidecar is
empty, and the test harness has supplied the successful process pairing, perform bounded
native read-only investigation of `.claude/agents/codebase-investigator.md`. Find literal
sentinel `{_SENTINEL}` and return concise CODEBASE_INVESTIGATION_RESULT_V1 YAML whose
`discovery_summary` contains that sentinel exactly. Do not invoke AGY separately, Bash, or
any mutation tool.
"""
    completed = subprocess.run(
        [
            claude_bin,
            "-p",
            "--agent",
            "codebase-investigator",
            "--output-format",
            "stream-json",
            "--include-hook-events",
            "--no-session-persistence",
            "--max-turns",
            "8",
            "--verbose",
        ],
        cwd=_REPO_ROOT,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
    unavailable = next((item for item in _UNAVAILABLE_MARKERS if item in combined), None)
    if completed.returncode != 0 and unavailable is not None:
        evidence = _write_runtime_skip_evidence(f"actual SubAgent runtime unavailable ({unavailable})")
        pytest.skip(f"SKIP: actual SubAgent runtime unavailable ({unavailable}); evidence: {evidence}")

    tools, result = _stream_result(completed.stdout or "")
    read_paths = [
        str(tool["input"].get("file_path", ""))
        for tool in tools
        if tool["name"] == "Read" and isinstance(tool["input"], dict)
    ]
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert str(decision_path) in read_paths
    assert str(sidecar_path) in read_paths
    assert not [tool for tool in tools if tool["name"] == "Bash"]
    assert not [tool for tool in tools if tool["name"] in _MUTATING_TOOLS]
    assert before == _tracked_status(), "tracked worktree status changed during smoke"
    assert _SENTINEL in result, "real codebase-investigator did not reach native sentinel"

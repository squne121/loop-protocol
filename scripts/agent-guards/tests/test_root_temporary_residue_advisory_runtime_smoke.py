"""test_root_temporary_residue_advisory_runtime_smoke.py -- AC11 runtime
verification pytest suite (Issue #2007).

Spawns the real ``.claude/hooks/root_temporary_residue_advisory.sh``
PreToolUse hook process (bash script -> python3 producer invoked with
``--schema-version v2``) for ``.claude/tmp/**`` write / read / scan / delete
operations, and asserts:

  - process ``returncode == 0`` in every case (fail-open, never blocks)
  - the output JSON never contains ``hookSpecificOutput.permissionDecision``
    (the hook must never alter the normal Claude Code permission flow)
  - when advisory output is produced (write case), the
    ``additionalContext`` prefix matches the inner JSON ``schema`` field,
    and no ``*_fallback: true`` marker is present -- fallback-success-as-PASS
    is prohibited (docs/dev/runtime-verification-policy.md fallback_policy)
  - read / scan / delete cases produce no advisory output at all

Skips (``pytest.skip``) every test if the ``claude`` CLI binary or a
runnable hook execution environment (bash + python3 + the hook script
itself) is not available in this environment. This mirrors the existing
``scripts/agent-ops/tests/run_runtime_verification_ac18.py`` wrapped-suite
pattern (Issue #2161) so that the non-pytest-collected
``run_runtime_verification_ac11.py`` wrapper can classify PASS / FAIL / SKIP
per docs/dev/runtime-verification-policy.md section 4.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / ".claude" / "hooks" / "root_temporary_residue_advisory.sh"
LEGACY_ROOT_WRITE_PATH = str(REPO_ROOT / ".claude" / "tmp" / "session" / "output.md")

_SUBPROCESS_TIMEOUT_SECONDS = 30.0


def _hook_environment_available() -> bool:
    return (
        shutil.which("claude") is not None
        and shutil.which("bash") is not None
        and shutil.which("python3") is not None
        and HOOK.is_file()
    )


pytestmark = pytest.mark.skipif(
    not _hook_environment_available(),
    reason=(
        "SKIP: claude CLI binary or hook execution environment "
        "(bash/python3/hook script) not available"
    ),
)


def _make_input_write(file_path: str) -> str:
    return json.dumps(
        {"cwd": str(REPO_ROOT), "tool_name": "Write", "tool_input": {"file_path": file_path}}
    )


def _make_input_read(file_path: str) -> str:
    return json.dumps(
        {"cwd": str(REPO_ROOT), "tool_name": "Read", "tool_input": {"file_path": file_path}}
    )


def _make_input_bash(command: str) -> str:
    return json.dumps(
        {"cwd": str(REPO_ROOT), "tool_name": "Bash", "tool_input": {"command": command}}
    )


def _run_hook(stdin_data: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["bash", str(HOOK)],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )


def _assert_no_permission_decision_and_no_fallback(
    result: subprocess.CompletedProcess,
) -> dict | None:
    assert result.returncode == 0, (
        f"hook exited non-zero: {result.returncode}, stderr={result.stderr!r}"
    )
    stdout = result.stdout.strip()
    if not stdout:
        return None
    outer = json.loads(stdout)
    hso = outer["hookSpecificOutput"]
    assert "permissionDecision" not in hso, (
        "hookSpecificOutput.permissionDecision must not be present "
        "(the hook must never alter the normal permission flow)"
    )
    ctx = hso["additionalContext"]
    schema_prefix, _, inner_raw = ctx.partition(" ")
    inner = json.loads(inner_raw)
    assert schema_prefix == inner["schema"], (
        f"additionalContext prefix {schema_prefix!r} does not match "
        f"inner JSON schema {inner['schema']!r}"
    )
    for key, value in inner.items():
        if key.endswith("_fallback"):
            assert value is not True, f"fallback-success-as-PASS detected: {key}={value!r}"
    return inner


def test_write_to_legacy_root_produces_v2_advisory_live():
    result = _run_hook(_make_input_write(LEGACY_ROOT_WRITE_PATH))
    inner = _assert_no_permission_decision_and_no_fallback(result)
    assert inner is not None, "expected advisory output for .claude/tmp/** write, got none"
    assert inner["schema"] == "REPO_TEMP_FOLDER_ADVICE_V2"
    assert inner["reason_code"] == "deprecated_legacy_root_write"
    assert inner["observed_path"] == ".claude/tmp/"
    assert inner["block"] is False


def test_read_legacy_root_produces_no_advisory_live():
    result = _run_hook(_make_input_read(LEGACY_ROOT_WRITE_PATH))
    inner = _assert_no_permission_decision_and_no_fallback(result)
    assert inner is None, "expected no advisory output for .claude/tmp/** read"


@pytest.mark.parametrize(
    "command",
    [
        "cat .claude/tmp/session/output.md",
        "ls .claude/tmp",
        "find .claude/tmp -name '*.json'",
    ],
)
def test_scan_legacy_root_produces_no_advisory_live(command: str):
    result = _run_hook(_make_input_bash(command))
    inner = _assert_no_permission_decision_and_no_fallback(result)
    assert inner is None, f"expected no advisory output for scan command: {command}"


def test_delete_legacy_root_produces_no_advisory_live():
    result = _run_hook(_make_input_bash("rm -rf .claude/tmp/session-x"))
    inner = _assert_no_permission_decision_and_no_fallback(result)
    assert inner is None, "expected no advisory output for .claude/tmp/** delete"

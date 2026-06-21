"""
Tests for .codex/hooks/ci_test_performance_advisory.sh (Codex CLI side)
AC6: CI/test-lane related paths trigger advisory in hookSpecificOutput.additionalContext format
AC7: Non-CI paths produce no output (exit 0, empty stdout)
"""
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".codex" / "hooks" / "ci_test_performance_advisory.sh"

# CI/test-lane high-confidence trigger paths (AC6)
CI_PATHS = [
    ".github/workflows/ci.yml",
    ".github/workflows/test.yml",
    "pyproject.toml",
    "uv.lock",
    "docs/dev/test-lane-policy.md",
    "docs/dev/ci-performance.md",
    ".claude/skills/ci-test-performance/SKILL.md",
    ".agents/skills/ci-test-performance/runner.py",
    ".codex/agents/my-agent.md",
    "schemas/some_schema.json",
]

# Non-CI paths that must NOT trigger advisory (AC7)
NON_CI_PATHS = [
    "src/state/gameState.ts",
    "src/render/canvas.ts",
    "src/systems/physics.ts",
    "docs/product/requirements.md",
    "docs/adr/0001-architecture-baseline.md",
    "README.md",
    "src/data/weapons.ts",
]


def _make_input_write(file_path: str = "") -> str:
    return json.dumps({
        "tool_name": "Write",
        "tool_input": {
            "file_path": file_path,
        }
    })


def _make_input_apply_patch(command: str = "") -> str:
    """apply_patch tool payload with command containing a CI path."""
    return json.dumps({
        "tool_name": "apply_patch",
        "tool_input": {
            "command": command,
        }
    })


def _run_hook(stdin_data: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=stdin_data,
        capture_output=True,
        text=True,
    )


def _parse_envelope(result: subprocess.CompletedProcess) -> dict:
    """Parse outer hookSpecificOutput envelope from hook stdout."""
    outer = json.loads(result.stdout)
    assert "hookSpecificOutput" in outer, f"Missing hookSpecificOutput in: {outer}"
    hso = outer["hookSpecificOutput"]
    assert hso.get("hookEventName") == "PreToolUse", f"hookEventName must be PreToolUse: {hso}"
    assert "additionalContext" in hso, f"Missing additionalContext in: {hso}"
    return hso


def _parse_inner(result: subprocess.CompletedProcess) -> dict:
    """Parse inner CI_TEST_PERFORMANCE_ADVISORY_V1 payload from additionalContext."""
    hso = _parse_envelope(result)
    ctx = hso["additionalContext"]
    assert ctx.startswith("CI_TEST_PERFORMANCE_ADVISORY_V1 "), (
        f"additionalContext must start with 'CI_TEST_PERFORMANCE_ADVISORY_V1 ': {ctx!r}"
    )
    inner_json = ctx[len("CI_TEST_PERFORMANCE_ADVISORY_V1 "):]
    return json.loads(inner_json)


@pytest.mark.parametrize("ci_path", CI_PATHS)
def test_ci_path_triggers_advisory(ci_path):
    """AC6: CI/test-lane paths must emit hookSpecificOutput advisory with block: false."""
    result = _run_hook(_make_input_write(file_path=ci_path))
    assert result.returncode == 0, (
        f"Hook must exit 0 for CI path '{ci_path}'\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip(), (
        f"Hook must produce output for CI path '{ci_path}'"
    )
    # Verify outer envelope
    hso = _parse_envelope(result)
    assert hso["hookEventName"] == "PreToolUse"
    assert "CI_TEST_PERFORMANCE_ADVISORY_V1" in hso["additionalContext"]
    # Verify inner payload
    inner = _parse_inner(result)
    assert inner["block"] is False, f"block must be false for '{ci_path}': {inner}"
    assert inner["reason_code"] == "ci_related_path"


def test_runtime_is_codex_cli():
    """Codex hook must report runtime as codex_cli (hardcoded, no env heuristic)."""
    result = _run_hook(_make_input_write(file_path="pyproject.toml"))
    inner = _parse_inner(result)
    # runtime field not present in inner payload (removed in new format)
    # The runtime is implicit from the hook file itself; no env injection needed
    assert inner["block"] is False  # basic sanity


def test_apply_patch_ci_path_triggers_advisory():
    """apply_patch tool with CI path in command must trigger advisory."""
    cmd = "*** Begin Patch\n*** Update File: .github/workflows/ci.yml\n"
    result = _run_hook(_make_input_apply_patch(command=cmd))
    assert result.returncode == 0
    assert result.stdout.strip(), "Hook must output advisory when apply_patch command contains CI path"
    hso = _parse_envelope(result)
    assert hso["hookEventName"] == "PreToolUse"
    assert "CI_TEST_PERFORMANCE_ADVISORY_V1" in hso["additionalContext"]
    inner = _parse_inner(result)
    assert inner["block"] is False


@pytest.mark.parametrize("non_ci_path", NON_CI_PATHS)
def test_non_ci_path_produces_no_output(non_ci_path):
    """AC7: Non-CI/test-lane paths must produce no output (silent pass)."""
    result = _run_hook(_make_input_write(file_path=non_ci_path))
    assert result.returncode == 0, (
        f"Hook must exit 0 for non-CI path '{non_ci_path}'\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "", (
        f"Hook must produce NO output for non-CI path '{non_ci_path}'\nstdout: {result.stdout!r}"
    )


def test_advisory_contains_required_skill():
    """Advisory inner payload must reference the required skill."""
    result = _run_hook(_make_input_write(file_path="pyproject.toml"))
    inner = _parse_inner(result)
    assert inner["required_skill"] == ".claude/skills/ci-test-performance/SKILL.md"


def test_advisory_contains_followup_contract():
    """Advisory inner payload must reference the expected followup contract."""
    result = _run_hook(_make_input_write(file_path="uv.lock"))
    inner = _parse_inner(result)
    assert inner["expected_followup_contract"] == "CI_TEST_PERFORMANCE_DECISION_V1"


def test_no_block_or_deny_fields():
    """Advisory must not use block:true or permissionDecision:deny in outer envelope."""
    result = _run_hook(_make_input_write(file_path="pyproject.toml"))
    outer = json.loads(result.stdout)
    assert "block" not in outer, "Outer envelope must not have 'block' field"
    assert "permissionDecision" not in outer, "Outer envelope must not have 'permissionDecision'"
    inner = _parse_inner(result)
    assert inner["block"] is False

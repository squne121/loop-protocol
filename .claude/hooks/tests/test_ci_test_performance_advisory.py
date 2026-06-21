"""
Tests for .claude/hooks/ci_test_performance_advisory.sh
AC6: CI/test-lane related paths trigger advisory (block: false JSON output)
AC7: Non-CI paths produce no output (exit 0, empty stdout)
"""
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / ".claude" / "hooks" / "ci_test_performance_advisory.sh"

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


def _make_input(file_path: str = "", command: str = "") -> str:
    return json.dumps({
        "tool_name": "Write",
        "tool_input": {
            "file_path": file_path,
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


@pytest.mark.parametrize("ci_path", CI_PATHS)
def test_ci_path_triggers_advisory(ci_path):
    """AC6: CI/test-lane paths must emit CI_TEST_PERFORMANCE_ADVISORY_V1 with block: false."""
    result = _run_hook(_make_input(file_path=ci_path))
    assert result.returncode == 0, (
        f"Hook must exit 0 for CI path '{ci_path}'\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip(), (
        f"Hook must produce output for CI path '{ci_path}'"
    )
    data = json.loads(result.stdout)
    assert data["schema"] == "CI_TEST_PERFORMANCE_ADVISORY_V1", (
        f"schema field mismatch for '{ci_path}': {data}"
    )
    assert data["block"] is False, (
        f"block must be false for CI path '{ci_path}': {data}"
    )
    assert data["triggered"] is True, (
        f"triggered must be true for CI path '{ci_path}': {data}"
    )
    assert data["reason_code"] == "ci_related_path", (
        f"reason_code mismatch for '{ci_path}': {data}"
    )


@pytest.mark.parametrize("non_ci_path", NON_CI_PATHS)
def test_non_ci_path_produces_no_output(non_ci_path):
    """AC7: Non-CI/test-lane paths must produce no output (silent pass)."""
    result = _run_hook(_make_input(file_path=non_ci_path))
    assert result.returncode == 0, (
        f"Hook must exit 0 for non-CI path '{non_ci_path}'\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "", (
        f"Hook must produce NO output for non-CI path '{non_ci_path}'\nstdout: {result.stdout!r}"
    )


def test_ci_command_triggers_advisory():
    """AC6: CI-related path in command field also triggers advisory."""
    result = _run_hook(_make_input(command="cat .github/workflows/ci.yml"))
    assert result.returncode == 0
    assert result.stdout.strip(), "Hook must output advisory when command contains CI path"
    data = json.loads(result.stdout)
    assert data["schema"] == "CI_TEST_PERFORMANCE_ADVISORY_V1"
    assert data["block"] is False


def test_empty_input_no_output():
    """Hook must handle empty or minimal input gracefully (fail-open)."""
    result = _run_hook(json.dumps({"tool_name": "Bash", "tool_input": {}}))
    assert result.returncode == 0, (
        f"Hook must exit 0 on minimal input\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "", "Hook must produce no output for empty tool_input"


def test_advisory_contains_required_skill():
    """Advisory must reference the required skill."""
    result = _run_hook(_make_input(file_path="pyproject.toml"))
    data = json.loads(result.stdout)
    assert data["required_skill"] == ".claude/skills/ci-test-performance/SKILL.md"


def test_advisory_contains_followup_contract():
    """Advisory must reference the expected followup contract."""
    result = _run_hook(_make_input(file_path="uv.lock"))
    data = json.loads(result.stdout)
    assert data["expected_followup_contract"] == "CI_TEST_PERFORMANCE_DECISION_V1"

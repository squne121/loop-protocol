"""Issue #1612 AC6: `node scripts/check-codex-agents.mjs --self-test` must
pass in full under the redesigned protected-path-only write guard (the
CODEX_ALLOWED_PATHS_MODE workspace/strict/shadow self-test sections were
removed along with the mode enum itself; this pytest wrapper is the
Verification Command AC6 points at).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check-codex-agents.mjs"


def test_check_codex_agents_self_test_passes() -> None:
    result = subprocess.run(  # noqa: S603
        ["node", str(CHECK_SCRIPT), "--self-test"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"self-test failed (exit {result.returncode}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "FAIL" not in result.stdout
    assert "SELF-TEST FAIL" not in result.stderr
    assert "ok self-test: all assertions passed" in result.stdout


def test_check_codex_agents_self_test_covers_protected_path_sections() -> None:
    """Sanity check that the redesigned self-test suite still exercises the
    protected-path enforcement sections (not just the unrelated TOML-parser /
    Bash-hook sections) so a future accidental deletion of those sections
    would be caught by test_check_codex_agents_self_test_passes() actually
    exercising something."""
    result = subprocess.run(  # noqa: S603
        ["node", str(CHECK_SCRIPT), "--self-test"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
        check=False,
    )
    for expected_section in (
        "self-test: protected-path enforcement (Edit/Write)",
        "self-test: protected-path enforcement (apply_patch)",
        "self-test: PROTECTED_PATHS_POLICY_V1 JSON SSOT mirrors",
    ):
        assert expected_section in result.stdout, f"missing self-test section: {expected_section}"

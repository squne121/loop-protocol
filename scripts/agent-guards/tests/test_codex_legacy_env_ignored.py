"""Issue #1612 AC7: setting the legacy CODEX_ALLOWED_PATHS_MODE /
CODEX_ALLOWED_PATHS / CODEX_LEGACY_ALLOW_WRITES env vars must have NO effect
on scripts/check-codex-agents.mjs's write-guard decisions any more --
same-authority conflicts between the old env-driven mode system and the new
protected-path-only design must never occur (they are simply never read).

This is an out-of-process negative test: it spawns real `node` subprocesses
with the legacy env vars set to adversarial values that WOULD have changed
the pre-#1612 outcome (e.g. `CODEX_ALLOWED_PATHS_MODE=strict` with a
non-matching `CODEX_ALLOWED_PATHS` used to deny an otherwise-normal path;
`CODEX_LEGACY_ALLOW_WRITES=1` used to be a workspace-mode alias) and asserts
the observed decision is identical to the same call with none of those env
vars set.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check-codex-agents.mjs"

_LEGACY_ENV_VAR_NAMES = (
    "CODEX_ALLOWED_PATHS_MODE",
    "CODEX_ALLOWED_PATHS",
    "CODEX_LEGACY_ALLOW_WRITES",
)


def _run_check_write_tool(tool_name: str, path_or_command: str, env_overrides: dict | None = None) -> str:
    env = os.environ.copy()
    for name in _LEGACY_ENV_VAR_NAMES:
        env.pop(name, None)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(  # noqa: S603
        ["node", str(CHECK_SCRIPT), "--check-write-tool", tool_name, path_or_command],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=20,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "adversarial_env",
    [
        pytest.param(
            {"CODEX_ALLOWED_PATHS_MODE": "strict", "CODEX_ALLOWED_PATHS": "some/unrelated/dir"},
            id="strict-mode-with-non-matching-allowed-paths",
        ),
        pytest.param({"CODEX_ALLOWED_PATHS_MODE": "unknown"}, id="unknown-mode"),
        pytest.param({"CODEX_ALLOWED_PATHS_MODE": "strict"}, id="strict-mode-no-allowed-paths"),
    ],
)
def test_legacy_env_does_not_narrow_normal_path_allow(adversarial_env: dict) -> None:
    """Pre-#1612, each of these env combinations would have denied a normal,
    non-protected path (`src/main.ts`). Post-#1612 they must all be ignored
    and the path must still be allowed."""
    baseline = _run_check_write_tool("Edit", "src/main.ts")
    with_legacy_env = _run_check_write_tool("Edit", "src/main.ts", adversarial_env)
    assert baseline == "allow"
    assert with_legacy_env == baseline


@pytest.mark.parametrize(
    "adversarial_env",
    [
        pytest.param({"CODEX_LEGACY_ALLOW_WRITES": "1"}, id="legacy-allow-writes-1"),
        pytest.param({"CODEX_ALLOWED_PATHS_MODE": "workspace"}, id="workspace-mode"),
        pytest.param(
            {"CODEX_ALLOWED_PATHS_MODE": "workspace", "CODEX_LEGACY_ALLOW_WRITES": "1"},
            id="workspace-mode-plus-legacy-allow-writes",
        ),
    ],
)
def test_legacy_env_does_not_widen_protected_path_deny(adversarial_env: dict) -> None:
    """Protected paths (assets/**) were always denied in every pre-#1612
    mode too, but this proves the removed mode system cannot be resurrected
    by setting these env vars to accidentally create a bypass path."""
    baseline = _run_check_write_tool("Edit", "assets/sprite.png")
    with_legacy_env = _run_check_write_tool("Edit", "assets/sprite.png", adversarial_env)
    assert baseline.startswith("deny:")
    assert with_legacy_env == baseline


def test_legacy_env_does_not_affect_apply_patch_decisions() -> None:
    patch_cmd = "*** Update File: scripts/check-codex-agents.mjs\n--- a\n+++ b\n"
    baseline = _run_check_write_tool("apply_patch", patch_cmd)
    with_legacy_env = _run_check_write_tool(
        "apply_patch",
        patch_cmd,
        {"CODEX_ALLOWED_PATHS_MODE": "strict", "CODEX_ALLOWED_PATHS": "docs/only"},
    )
    assert baseline == "allow"
    assert with_legacy_env == baseline

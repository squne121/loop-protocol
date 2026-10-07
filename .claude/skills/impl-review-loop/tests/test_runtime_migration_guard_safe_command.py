"""Issue #2810 AC11 — fixes the canonical post-repair verification command
(`bash -c 'unset CLAUDE_GPT_PROXY_BIN; bash scripts/claude-gpt/launch.sh
--check-only'`) against a synthetic `secret_boundary_guard.sh` PreToolUse
payload, and fixes that the ORIGINAL `env -u CLAUDE_GPT_PROXY_BIN ...` form
remains denied. Mirrors the established synthetic-PreToolUse test pattern
in `.claude/hooks/tests/test_secret_boundary_contract.py` (never modifies
the guard itself -- Issue #2810 In Scope explicitly keeps the guard
unchanged)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "secret_boundary_guard.sh"

CANONICAL_COMMAND = "bash -c 'unset CLAUDE_GPT_PROXY_BIN; bash scripts/claude-gpt/launch.sh --check-only'"
LEGACY_ENV_FORM_COMMAND = "env -u CLAUDE_GPT_PROXY_BIN bash scripts/claude-gpt/launch.sh --check-only"


def _invoke_guard(command: str) -> subprocess.CompletedProcess:
    assert GUARD_PATH.exists(), f"guard not found: {GUARD_PATH}"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_canonical_unset_form_is_allowed():
    """GIVEN the canonical `bash -c 'unset CLAUDE_GPT_PROXY_BIN; ...'` AC11
    verification command WHEN sent to secret_boundary_guard.sh as a
    synthetic PreToolUse Bash payload THEN the guard allows it (exit 0) --
    guard is NOT weakened for this case."""
    result = _invoke_guard(CANONICAL_COMMAND)
    assert result.returncode == 0, (
        f"expected exit 0 (allow) for canonical command, got "
        f"{result.returncode}\nstderr: {result.stderr[:300]}"
    )


def test_legacy_env_u_form_remains_denied():
    """GIVEN the original `env -u CLAUDE_GPT_PROXY_BIN ...` form (which the
    canonical command replaces) WHEN sent to secret_boundary_guard.sh THEN
    the guard STILL denies it (exit 2) -- proving the guard was not
    globally weakened, only avoided via a differently-shaped command."""
    result = _invoke_guard(LEGACY_ENV_FORM_COMMAND)
    assert result.returncode == 2, (
        f"expected exit 2 (deny) for legacy `env -u ...` form, got "
        f"{result.returncode}\nstderr: {result.stderr[:300]}"
    )


def test_bare_env_dump_still_denied_negative_control():
    """Negative control: a bare `env` (no arguments, dumps all env vars)
    WHEN sent to the guard THEN it is still denied (exit 2) -- confirms
    this test's guard invocation methodology actually exercises the deny
    path, not just an always-allow misconfiguration."""
    result = _invoke_guard("env")
    assert result.returncode == 2


def test_printenv_still_denied_ac10_contract_command():
    """AC10's contract-out-of-bounds probe command (`printenv`) WHEN sent
    to the guard THEN it is denied (exit 2) -- the same guard the
    apply_runtime_migration_fix_delta worker relies on for AC10's deny
    evidence."""
    result = _invoke_guard("printenv")
    assert result.returncode == 2

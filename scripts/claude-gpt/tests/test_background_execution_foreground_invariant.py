"""scripts/claude-gpt/tests/test_background_execution_foreground_invariant.py

Issue #2274 AC13: `CLAUDE_CODE_FORK_SUBAGENT` / `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`
production invariant (`CLAUDE_CODE_FORK_SUBAGENT`: unset/0,
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`: 1). Any deviation from the invariant
-- across all 5 distinguishable states (both unset, fork-only enabled,
disable-background-only disabled i.e. unset, both enabled/unset, or a
settings-layer re-injection) -- denies the Spark Agent launch BEFORE it
happens (fail-closed), never a post-hoc detection.

These tests exercise the *exact* python source embedded in
``scripts/claude-gpt/launch.sh`` between the ``SPARK_GATE_WRITER_PY_BEGIN``/
``_END`` markers (extracted via
``run_worktree_agent_runtime_smoke.extract_spark_gate_writer_source``), same
mechanism as ``test_delegation_directive.py`` /
``test_model_precedence_negative_controls.py``, so there is a single source
of truth between what actually runs in a live claude-gpt session and what
this suite verifies.

Covers (see Issue #2274 Verification Commands, AC13):
- state 1: `CLAUDE_CODE_FORK_SUBAGENT` unset, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`
  unset -> deny (invariant requires disable-background explicitly == "1").
- state 2: fork enabled (`1`), disable-background compliant (`1`) -> deny
  (fork must never be enabled for spark-codex).
- state 3: fork unset/compliant, disable-background NOT `1` (explicit `0`)
  -> deny.
- state 4: fork enabled AND disable-background not `1` -> deny.
- state 5 (positive control): fork unset, disable-background == `1` ->
  allow.

NOTE (scope boundary): the `PostToolUse.status == completed` foreground-
completion authority (asserting `async_launched` is FAIL, never a silent
pass) belongs to the `run_worktree_agent_runtime_smoke.py` live-smoke
evidence layer, not this pre-launch gate hook -- this suite covers only the
gate hook's pre-launch invariant deny. The PostToolUse-status classification
helper itself is tracked as remaining work for this Issue and is
intentionally NOT asserted here (no SKIP/placeholder substitute for it).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
LAUNCH_SH = REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"

LAUNCH_NONCE_PLACEHOLDER = "__CLAUDE_GPT_SPARK_LAUNCH_NONCE__"

_DEFAULT_COMPLIANT_EFFECTIVE_ENV = {
    "CLAUDE_CODE_FORK_SUBAGENT": "",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_SUBAGENT_MODEL": "",
}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_worktree_agent_runtime_smoke_background_execution_foreground_invariant", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate_script_source() -> str:
    module = _load_module()
    launch_sh_text = LAUNCH_SH.read_text(encoding="utf-8")
    source = module.extract_spark_gate_writer_source(launch_sh_text)
    assert source is not None, "SPARK_GATE_WRITER_PY_BEGIN/_END markers not found in launch.sh"
    assert LAUNCH_NONCE_PLACEHOLDER in source
    assert "background_execution_invariant_violation" in source, (
        "gate writer source must contain the Issue #2274 AC13 fork/background "
        "invariant detection; if this drifted, this suite would silently stop "
        "testing the actual mechanism."
    )
    return source


def _render_gate_script(directory: Path, source: str, launch_nonce: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    gate_path = directory / f"spark_gate_{uuid.uuid4().hex}.py"
    gate_path.write_text(source.replace(LAUNCH_NONCE_PLACEHOLDER, launch_nonce), encoding="utf-8")
    return gate_path


def _run_gate(
    gate_script_source: str,
    event: str,
    payload: dict,
    *,
    auth_dir: Path,
    launch_nonce: str = "nonce-fixture",
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    gate_script_path = _render_gate_script(auth_dir.parent / "gate-scripts", gate_script_source, launch_nonce)
    env = {**os.environ, **_DEFAULT_COMPLIANT_EFFECTIVE_ENV, "CLAUDE_GPT_SPARK_AUTH_DIR": str(auth_dir)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(gate_script_path), event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _required_directive(prompt_prefix: str = "please use spark for this:\n") -> str:
    return prompt_prefix + (
        "schema: DELEGATION_REQUEST_V1\n"
        "agent_id: spark-codex\n"
        "model: gpt-5.3-codex-spark\n"
        "mode: required\n"
        "fallback: forbidden\n"
        "wait: true\n"
        "authorization_source: explicit_directive\n"
    )


def _user_prompt_submit(gate_script_source, auth_dir, session_id, prompt, *, launch_nonce="nonce-fixture"):
    return _run_gate(
        gate_script_source,
        "user-prompt-submit",
        {"session_id": session_id, "prompt": prompt},
        auth_dir=auth_dir,
        launch_nonce=launch_nonce,
    )


def _pre_tool_use_agent(gate_script_source, auth_dir, session_id, *, extra_env=None):
    return _run_gate(
        gate_script_source,
        "pre-tool-use-agent",
        {"session_id": session_id, "tool_input": {"subagent_type": "spark-codex"}},
        auth_dir=auth_dir,
        extra_env=extra_env,
    )


def _output(result: subprocess.CompletedProcess[str]) -> dict | None:
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def _decision(result: subprocess.CompletedProcess[str]) -> str | None:
    payload = _output(result)
    if payload is None:
        return None
    return payload["hookSpecificOutput"].get("permissionDecision")


def _reason(result: subprocess.CompletedProcess[str]) -> str | None:
    payload = _output(result)
    if payload is None:
        return None
    return payload["hookSpecificOutput"].get("permissionDecisionReason")


def _authorize_and_invoke(gate_script_source, tmp_path, name, *, extra_env=None):
    auth_dir = tmp_path / f"auth-{name}"
    session_id = f"sess-{name}"
    _user_prompt_submit(gate_script_source, auth_dir, session_id, _required_directive())
    return _pre_tool_use_agent(gate_script_source, auth_dir, session_id, extra_env=extra_env)


STATES = [
    pytest.param({"CLAUDE_CODE_FORK_SUBAGENT": "", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": ""}, id="both-unset"),
    pytest.param(
        {"CLAUDE_CODE_FORK_SUBAGENT": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}, id="fork-only-enabled"
    ),
    pytest.param(
        {"CLAUDE_CODE_FORK_SUBAGENT": "", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"},
        id="disable-background-only-explicit-0",
    ),
    pytest.param(
        {"CLAUDE_CODE_FORK_SUBAGENT": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": ""}, id="both-violating"
    ),
]


@pytest.mark.parametrize("extra_env", STATES)
def test_invariant_violation_states_are_denied_before_launch(gate_script_source, tmp_path, extra_env, request):
    name = request.node.callspec.id
    result = _authorize_and_invoke(gate_script_source, tmp_path, name, extra_env=extra_env)
    assert _decision(result) == "deny"
    assert _reason(result) == "background_execution_invariant_violation"
    payload = _output(result)
    assert "updatedInput" not in payload["hookSpecificOutput"]


def test_invariant_compliant_state_is_allowed(gate_script_source, tmp_path):
    result = _authorize_and_invoke(
        gate_script_source,
        tmp_path,
        "compliant",
        extra_env={"CLAUDE_CODE_FORK_SUBAGENT": "", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
    )
    assert _decision(result) == "allow"


def test_invariant_settings_layer_reinjection_surfaces_identically_to_shell_export(gate_script_source, tmp_path):
    # A settings-layer (managed/user/project/local) `env` re-injection of
    # CLAUDE_CODE_FORK_SUBAGENT surfaces in os.environ identically to an
    # ambient shell export by the time this hook runs -- the same check
    # covers both sources without needing to distinguish provenance.
    result = _authorize_and_invoke(
        gate_script_source,
        tmp_path,
        "settings-reinjected",
        extra_env={"CLAUDE_CODE_FORK_SUBAGENT": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
    )
    assert _decision(result) == "deny"
    assert _reason(result) == "background_execution_invariant_violation"


def test_launch_sh_exports_production_invariant_before_child_process_launch():
    # Static regression: the launcher itself must pin the invariant for the
    # real `claude` child process (previously only CLAUDE_CODE_SUBAGENT_MODEL
    # was unset; CLAUDE_CODE_DISABLE_BACKGROUND_TASKS was never set at all).
    launch_sh_text = LAUNCH_SH.read_text(encoding="utf-8")
    assert "unset CLAUDE_CODE_FORK_SUBAGENT" in launch_sh_text
    assert "export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1" in launch_sh_text

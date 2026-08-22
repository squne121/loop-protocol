"""scripts/claude-gpt/tests/test_model_precedence_negative_controls.py

Issue #2274 AC11: `CLAUDE_CODE_SUBAGENT_MODEL` precedence negative/positive
controls, plus the effective-environment fail-closed
`unsupported_effective_model_override` detection for a re-injected value
(shell export or managed/user/project/local Claude Code settings `env` block
-- both surface identically in `os.environ` at hook execution time; see
``launch.sh``'s ``effective_env_override_reason()``).

These tests exercise the *exact* python source embedded in
``scripts/claude-gpt/launch.sh`` between the ``SPARK_GATE_WRITER_PY_BEGIN``/
``_END`` markers (extracted via
``run_worktree_agent_runtime_smoke.extract_spark_gate_writer_source``), same
mechanism as ``test_delegation_directive.py`` / the sibling agent-ops
hermetic gate test file, so there is a single source of truth between what
actually runs in a live claude-gpt session and what this suite verifies.

Covers (see Issue #2274 Verification Commands, AC11):
- unset `CLAUDE_CODE_SUBAGENT_MODEL` -> allow (positive control).
- `CLAUDE_CODE_SUBAGENT_MODEL` == the Spark model -> fail-closed deny,
  `unsupported_effective_model_override` (PR #2285 OWNER fix-delta P0-3:
  per Claude Code's official model resolution precedence, a same-value env
  override is STILL the env var winning binding authority, not the
  session-local agent definition -- allowing it would contradict this
  Issue's "definition-only authority" Outcome, so it is denied identically
  to a genuinely conflicting value).
- `CLAUDE_CODE_SUBAGENT_MODEL` set to a conflicting model -> fail-closed
  deny, `unsupported_effective_model_override` (negative control).
- `CLAUDE_CODE_SUBAGENT_MODEL` set to `inherit` -> fail-closed deny,
  `unsupported_effective_model_override` (negative control; Claude Code's
  own inherit sentinel is not an acceptable re-injected value here).
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

DELEGATION_MODEL = "gpt-5.3-codex-spark"

_DEFAULT_COMPLIANT_EFFECTIVE_ENV = {
    "CLAUDE_CODE_FORK_SUBAGENT": "",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_SUBAGENT_MODEL": "",
}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_worktree_agent_runtime_smoke_model_precedence_negative_controls", SCRIPT
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
    assert "effective_env_override_reason" in source, (
        "gate writer source must contain the Issue #2274 AC11 effective-environment "
        "detection helper; if this drifted, this suite would silently stop testing "
        "the actual mechanism."
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


def _pre_tool_use_agent(
    gate_script_source, auth_dir, session_id, *, launch_nonce="nonce-fixture", extra_env=None
):
    return _run_gate(
        gate_script_source,
        "pre-tool-use-agent",
        {"session_id": session_id, "tool_input": {"subagent_type": "spark-codex"}},
        auth_dir=auth_dir,
        launch_nonce=launch_nonce,
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


# --- positive controls -------------------------------------------------------


def test_subagent_model_unset_is_allowed(gate_script_source, tmp_path):
    result = _authorize_and_invoke(
        gate_script_source, tmp_path, "unset", extra_env={"CLAUDE_CODE_SUBAGENT_MODEL": ""}
    )
    assert _decision(result) == "allow"


def test_subagent_model_matching_spark_value_is_denied(gate_script_source, tmp_path):
    """PR #2285 OWNER fix-delta P0-3: a same-value `CLAUDE_CODE_SUBAGENT_MODEL`
    override is now denied, not allowed -- the env var is still the true
    binding authority per Claude Code's official model resolution
    precedence even when it happens to match the Spark model, so allowing
    it would make `definition.source: launcher_owned_agents_json` in
    SPARK_DELEGATION_EVIDENCE_V2 a false claim about which layer actually
    won for that run."""
    result = _authorize_and_invoke(
        gate_script_source,
        tmp_path,
        "matching",
        extra_env={"CLAUDE_CODE_SUBAGENT_MODEL": DELEGATION_MODEL},
    )
    assert _decision(result) == "deny"
    assert _reason(result) == "unsupported_effective_model_override"


# --- negative controls --------------------------------------------------------


def test_subagent_model_conflicting_value_is_denied(gate_script_source, tmp_path):
    result = _authorize_and_invoke(
        gate_script_source,
        tmp_path,
        "conflicting",
        extra_env={"CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-4-5"},
    )
    assert _decision(result) == "deny"
    assert _reason(result) == "unsupported_effective_model_override"


def test_subagent_model_inherit_sentinel_is_denied(gate_script_source, tmp_path):
    result = _authorize_and_invoke(
        gate_script_source, tmp_path, "inherit", extra_env={"CLAUDE_CODE_SUBAGENT_MODEL": "inherit"}
    )
    assert _decision(result) == "deny"
    assert _reason(result) == "unsupported_effective_model_override"


def test_subagent_model_override_denied_even_with_compliant_tool_input(gate_script_source, tmp_path):
    # The effective-environment check runs before -- and independent of -- the
    # tool_input model-field normalization contract: an env-level override
    # denies even when tool_input.model is absent/compliant.
    auth_dir = tmp_path / "auth-env-precedence"
    session_id = "sess-env-precedence"
    _user_prompt_submit(gate_script_source, auth_dir, session_id, _required_directive())
    result = _run_gate(
        gate_script_source,
        "pre-tool-use-agent",
        {"session_id": session_id, "tool_input": {"subagent_type": "spark-codex"}},
        auth_dir=auth_dir,
        extra_env={"CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-4-5"},
    )
    assert _decision(result) == "deny"
    assert _reason(result) == "unsupported_effective_model_override"
    payload = _output(result)
    assert "updatedInput" not in payload["hookSpecificOutput"]

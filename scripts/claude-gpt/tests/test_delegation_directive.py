"""scripts/claude-gpt/tests/test_delegation_directive.py

Issue #2258: structured `DELEGATION_REQUEST_V1` delegation directive support
for the `@agent-spark-codex` explicit-only authorization gate.

These tests exercise the *exact* python source embedded in
``scripts/claude-gpt/launch.sh`` between the ``SPARK_GATE_WRITER_PY_BEGIN``/
``_END`` markers (extracted via
``run_worktree_agent_runtime_smoke.extract_spark_gate_writer_source``, same
mechanism as
``scripts/agent-ops/tests/test_run_worktree_agent_runtime_smoke_spark_explicit_gate.py``),
so there is a single source of truth between what actually runs in a live
claude-gpt session and what this hermetic test suite verifies. No gate logic
is re-implemented/duplicated here.

Covers (see Issue #2258 Verification Commands):
- ``-k required_authorization`` (AC1): a well-formed ``mode: required``
  directive is parsed and grants a pending Spark authorization.
- ``-k preferred_fallback`` (AC2): a well-formed ``mode: preferred``
  directive allows fallback to a different subagent_type afterwards, and the
  fallback event is logged (via ``additionalContext``), once.
- ``-k required_terminal_failure`` (AC3): after a ``mode: required`` +
  ``fallback: forbidden`` directive's Spark authorization is consumed, ANY
  other subagent_type Agent tool call in the same turn is denied (no silent
  substitute/fallback delegation) -- the delegation lock is released only by
  the next UserPromptSubmit turn.
- ``-k non_authorizing_context`` (AC4): a directive-shaped text appearing
  only inside a quoted string / fenced code block / inline code span /
  blockquote never authorizes.
- ``-k malformed_directive`` (AC5): typo/case-mismatched enum values and
  missing required keys are rejected as malformed (explicit, non-silent,
  never an authorization).
- ``-k stale_authorization_rejected`` (AC6): a directive-derived
  authorization from a stale/different launch_nonce (representing a
  different session/resume) cannot be reused as the current turn's
  authorization.
- ``-k exactly_once_consume`` (AC7): a directive-derived authorization is
  consumed exactly once; a second Agent tool call in the same turn without a
  fresh authorization is denied.
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

# Same fixed placeholder launch.sh substitutes via `sed` post-heredoc (Issue
# #2186 P0 fix-delta); kept in sync deliberately (see the sibling agent-ops
# hermetic test file for the full forgery-prevention rationale).
LAUNCH_NONCE_PLACEHOLDER = "__CLAUDE_GPT_SPARK_LAUNCH_NONCE__"

VALID_DIRECTIVE_LINES = (
    "schema: DELEGATION_REQUEST_V1\n"
    "agent_id: spark-codex\n"
    "model: gpt-5.3-codex-spark\n"
    "mode: {mode}\n"
    "fallback: {fallback}\n"
    "wait: true\n"
    "authorization_source: explicit_directive\n"
)


def _required_directive(prompt_prefix: str = "please use spark for this:\n") -> str:
    return prompt_prefix + VALID_DIRECTIVE_LINES.format(mode="required", fallback="forbidden")


def _preferred_directive(prompt_prefix: str = "please try spark for this:\n") -> str:
    return prompt_prefix + VALID_DIRECTIVE_LINES.format(mode="preferred", fallback="allowed")


def _load_module():
    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke_delegation_directive", SCRIPT)
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
    assert "DELEGATION_REQUEST_V1" in source, (
        "gate writer source must contain the Issue #2258 DELEGATION_REQUEST_V1 "
        "directive parser; if this drifted, this suite would silently stop "
        "testing the actual structured-directive mechanism."
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
) -> subprocess.CompletedProcess[str]:
    gate_script_path = _render_gate_script(auth_dir.parent / "gate-scripts", gate_script_source, launch_nonce)
    env = {**os.environ, "CLAUDE_GPT_SPARK_AUTH_DIR": str(auth_dir)}
    return subprocess.run(
        [sys.executable, str(gate_script_path), event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _pending_path(auth_dir: Path, session_id: str) -> Path:
    return auth_dir / f"pending-{session_id}.json"


def _required_lock_path(auth_dir: Path, session_id: str) -> Path:
    return auth_dir / f"required-lock-{session_id}.json"


def _preferred_marker_path(auth_dir: Path, session_id: str) -> Path:
    return auth_dir / f"preferred-marker-{session_id}.json"


def _user_prompt_submit(gate_script_source, auth_dir, session_id, prompt, *, launch_nonce="nonce-fixture"):
    return _run_gate(
        gate_script_source,
        "user-prompt-submit",
        {"session_id": session_id, "prompt": prompt},
        auth_dir=auth_dir,
        launch_nonce=launch_nonce,
    )


def _pre_tool_use_agent(
    gate_script_source, auth_dir, session_id, subagent_type="spark-codex", *, launch_nonce="nonce-fixture"
):
    return _run_gate(
        gate_script_source,
        "pre-tool-use-agent",
        {"session_id": session_id, "tool_input": {"subagent_type": subagent_type}},
        auth_dir=auth_dir,
        launch_nonce=launch_nonce,
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


def _additional_context(result: subprocess.CompletedProcess[str]) -> str | None:
    payload = _output(result)
    if payload is None:
        return None
    return payload["hookSpecificOutput"].get("additionalContext")


# --- required_authorization (AC1) -------------------------------------------


def test_required_authorization_valid_directive_grants_pending(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-req-valid"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-req", _required_directive())
    assert _pending_path(auth_dir, "sess-req").exists()


def test_required_authorization_grants_spark_invocation(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-req-invoke"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-req2", _required_directive())
    result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-req2")
    assert _decision(result) == "allow"
    context = _additional_context(result)
    assert context is not None
    assert context.startswith("CLAUDE_GPT_SPARK_DELEGATION_V1 ")
    payload = json.loads(context[len("CLAUDE_GPT_SPARK_DELEGATION_V1 ") :])
    assert payload["mode"] == "required"
    assert payload["fallback"] == "forbidden"


def test_required_authorization_directive_without_mention_still_authorizes(gate_script_source, tmp_path):
    # AC1 is exact-positive for the directive alone -- no canonical mention
    # needed alongside it.
    auth_dir = tmp_path / "auth-req-no-mention"
    prompt = _required_directive("no @ mention anywhere in this text, only the directive below:\n")
    _user_prompt_submit(gate_script_source, auth_dir, "sess-req3", prompt)
    assert _pending_path(auth_dir, "sess-req3").exists()


# --- preferred_fallback (AC2) ------------------------------------------------


def test_preferred_fallback_grants_spark_invocation(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-pref-invoke"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-pref", _preferred_directive())
    result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-pref")
    assert _decision(result) == "allow"
    assert _preferred_marker_path(auth_dir, "sess-pref").exists()


def test_preferred_fallback_allowed_and_logged_once(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-pref-fallback"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-pref2", _preferred_directive())
    spark_result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-pref2")
    assert _decision(spark_result) == "allow"

    fallback_result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-pref2", subagent_type="generic-worker")
    # Fallback is allowed (no permissionDecision -> not denied), and logged.
    assert _decision(fallback_result) is None
    context = _additional_context(fallback_result)
    assert context is not None
    assert context.startswith("CLAUDE_GPT_SPARK_FALLBACK_LOGGED_V1 ")
    payload = json.loads(context[len("CLAUDE_GPT_SPARK_FALLBACK_LOGGED_V1 ") :])
    assert payload["reason"] == "preferred_mode_fallback_to_non_spark_agent"
    assert payload["subagent_type"] == "generic-worker"
    # Marker is single-shot: a further ordinary Agent call is a plain no-op.
    assert not _preferred_marker_path(auth_dir, "sess-pref2").exists()
    second_fallback = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-pref2", subagent_type="generic-worker")
    assert second_fallback.stdout.strip() == ""
    assert second_fallback.returncode == 0


def test_preferred_fallback_without_prior_spark_call_never_logs(gate_script_source, tmp_path):
    # No Spark invocation happened this turn (no marker written yet) --
    # ordinary subagent calls remain a silent no-op exactly like before.
    auth_dir = tmp_path / "auth-pref-no-spark-yet"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-pref3", _preferred_directive())
    fallback_result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-pref3", subagent_type="generic-worker")
    assert fallback_result.stdout.strip() == ""
    assert fallback_result.returncode == 0


# --- required_terminal_failure (AC3) -----------------------------------------


def test_required_terminal_failure_blocks_fallback_agent_same_turn(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-req-terminal"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-term", _required_directive())
    spark_result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-term")
    assert _decision(spark_result) == "allow"
    assert _required_lock_path(auth_dir, "sess-term").exists()

    fallback_attempt = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-term", subagent_type="generic-worker")
    assert _decision(fallback_attempt) == "deny"
    payload = _output(fallback_attempt)
    assert (
        payload["hookSpecificOutput"]["permissionDecisionReason"]
        == "required_delegation_lock_active_no_fallback_agent_allowed"
    )
    assert fallback_attempt.returncode == 0


def test_required_terminal_failure_lock_released_next_turn(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-req-terminal-next-turn"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-term2", _required_directive())
    _pre_tool_use_agent(gate_script_source, auth_dir, "sess-term2")
    assert _required_lock_path(auth_dir, "sess-term2").exists()

    # Next turn (ordinary, no directive/mention) clears the lock.
    _user_prompt_submit(gate_script_source, auth_dir, "sess-term2", "thanks, let's continue normally")
    assert not _required_lock_path(auth_dir, "sess-term2").exists()
    ordinary = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-term2", subagent_type="generic-worker")
    assert ordinary.stdout.strip() == ""
    assert ordinary.returncode == 0


def test_required_terminal_failure_without_forbidden_fallback_does_not_lock(gate_script_source, tmp_path):
    # mode: required with fallback: allowed is a distinct, valid combination
    # from the Technical Recommendation's example (required+forbidden) --
    # only required+forbidden creates the no-fallback lock.
    auth_dir = tmp_path / "auth-req-allowed-fallback"
    prompt = "please:\n" + VALID_DIRECTIVE_LINES.format(mode="required", fallback="allowed")
    _user_prompt_submit(gate_script_source, auth_dir, "sess-req-allow", prompt)
    result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-req-allow")
    assert _decision(result) == "allow"
    assert not _required_lock_path(auth_dir, "sess-req-allow").exists()
    ordinary = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-req-allow", subagent_type="generic-worker")
    assert ordinary.stdout.strip() == ""


# --- non_authorizing_context (AC4) -------------------------------------------


@pytest.mark.parametrize(
    "wrap",
    [
        lambda directive: '"' + directive.replace("\n", " ") + '"',
        lambda directive: "```\n" + directive + "```\n",
        lambda directive: "`" + directive.replace("\n", " ") + "`",
        lambda directive: "\n".join("> " + line for line in directive.split("\n") if line),
        lambda directive: "'" + directive.replace("\n", " ") + "'",
    ],
    ids=["double-quoted", "fenced", "inline-code", "blockquote", "single-quoted"],
)
def test_non_authorizing_context_wrapped_directive_does_not_grant(gate_script_source, tmp_path, wrap):
    auth_dir = tmp_path / "auth-noauth"
    directive = VALID_DIRECTIVE_LINES.format(mode="required", fallback="forbidden")
    prompt = "context around it:\n" + wrap(directive) + "\nend"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-noauth", prompt)
    assert not _pending_path(auth_dir, "sess-noauth").exists()


def test_non_authorizing_context_fenced_directive_alongside_genuine_plain_directive_still_authorizes(
    gate_script_source, tmp_path
):
    auth_dir = tmp_path / "auth-mixed-directive"
    directive = VALID_DIRECTIVE_LINES.format(mode="required", fallback="forbidden")
    prompt = "example (ignore):\n```\n" + directive + "```\nnow for real:\n" + directive
    _user_prompt_submit(gate_script_source, auth_dir, "sess-mixed-directive", prompt)
    assert _pending_path(auth_dir, "sess-mixed-directive").exists()


# --- malformed_directive (AC5) ------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt-5.3-codex-spark\nmode: Required\n"
        "fallback: forbidden\nwait: true\nauthorization_source: explicit_directive\n",
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt-5.3-codex-spark\nmode: requiredd\n"
        "fallback: forbidden\nwait: true\nauthorization_source: explicit_directive\n",
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt-5.3-codex-spark\nmode: required\n"
        "fallback: Forbidden\nwait: true\nauthorization_source: explicit_directive\n",
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt-5.3-codex-spark\nmode: required\n"
        "wait: true\nauthorization_source: explicit_directive\n",
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark\nmodel: gpt-5.3-codex-spark\nmode: required\n"
        "fallback: forbidden\nwait: true\nauthorization_source: explicit_directive\n",
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt4\nmode: required\n"
        "fallback: forbidden\nwait: true\nauthorization_source: explicit_directive\n",
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt-5.3-codex-spark\nmode: required\n"
        "fallback: forbidden\nwait: false\nauthorization_source: explicit_directive\n",
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt-5.3-codex-spark\nmode: required\n"
        "fallback: forbidden\nwait: true\nauthorization_source: implicit\n",
    ],
    ids=[
        "mode-case-typo",
        "mode-suffix-typo",
        "fallback-case-typo",
        "missing-fallback-key",
        "wrong-agent-id",
        "wrong-model",
        "wait-not-true",
        "wrong-authorization-source",
    ],
)
def test_malformed_directive_never_authorizes(gate_script_source, tmp_path, prompt):
    auth_dir = tmp_path / "auth-malformed-directive"
    result = _user_prompt_submit(gate_script_source, auth_dir, "sess-malformed", prompt)
    assert not _pending_path(auth_dir, "sess-malformed").exists()
    # Explicit, non-silent rejection -- distinguishable from "no directive
    # attempted at all" (Issue #2258 AC5).
    context = _additional_context(result)
    assert context is not None
    assert context.startswith("CLAUDE_GPT_SPARK_DIRECTIVE_MALFORMED_V1 ")
    malformed_markers = list(auth_dir.glob("malformed-sess-malformed-*.json"))
    assert len(malformed_markers) == 1


def test_malformed_directive_distinct_from_no_directive_at_all(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-no-directive-at-all"
    result = _user_prompt_submit(gate_script_source, auth_dir, "sess-plain", "just an ordinary prompt, nothing special")
    assert not _pending_path(auth_dir, "sess-plain").exists()
    assert result.stdout.strip() == ""
    assert not list(auth_dir.glob("malformed-*.json"))


# --- stale_authorization_rejected (AC6) --------------------------------------


def test_stale_authorization_rejected_different_launch_nonce(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-stale-directive"
    _user_prompt_submit(
        gate_script_source, auth_dir, "sess-stale", _required_directive(), launch_nonce="nonce-old-session"
    )
    result = _pre_tool_use_agent(
        gate_script_source, auth_dir, "sess-stale", launch_nonce="nonce-new-session-after-resume"
    )
    assert _decision(result) == "deny"
    payload = _output(result)
    assert payload["hookSpecificOutput"]["permissionDecisionReason"] == "authorization_carryover_stale_launch_nonce"


def test_stale_authorization_rejected_prior_turn_directive_does_not_carry_over(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-stale-turn"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-stale-turn", _required_directive())
    _user_prompt_submit(gate_script_source, auth_dir, "sess-stale-turn", "an unrelated ordinary next turn")
    result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-stale-turn")
    assert _decision(result) == "deny"


# --- exactly_once_consume (AC7) -----------------------------------------------


def test_exactly_once_consume_second_invocation_denied(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-once-directive"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-once", _required_directive())
    first = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-once")
    second = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-once")
    assert _decision(first) == "allow"
    assert _decision(second) == "deny"
    assert second.returncode == 0


def test_exactly_once_consume_new_directive_next_turn_reauthorizes(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-once-reauth"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-reauth", _preferred_directive())
    first = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-reauth")
    assert _decision(first) == "allow"
    second_attempt_same_turn = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-reauth")
    assert _decision(second_attempt_same_turn) == "deny"
    # A fresh directive on the NEXT turn grants a brand-new single-use
    # authorization.
    _user_prompt_submit(gate_script_source, auth_dir, "sess-reauth", _required_directive())
    third = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-reauth")
    assert _decision(third) == "allow"

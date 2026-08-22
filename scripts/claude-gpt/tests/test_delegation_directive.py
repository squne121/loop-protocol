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


# Issue #2274 AC11/AC13: the production launcher always exports the
# fork/background invariant (`CLAUDE_CODE_FORK_SUBAGENT` unset,
# `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`) and never re-exports
# `CLAUDE_CODE_SUBAGENT_MODEL` before the real `claude` child process (and
# therefore this hook) runs. Every pre-existing test in this suite implicitly
# assumes that compliant baseline, so `_run_gate` applies it by default;
# `extra_env` lets the AC11/AC13-specific negative-control tests override
# individual variables to simulate a violation (ambient shell re-export or
# settings-layer re-injection -- both surface identically in `os.environ`).
_DEFAULT_COMPLIANT_EFFECTIVE_ENV = {
    "CLAUDE_CODE_FORK_SUBAGENT": "",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_SUBAGENT_MODEL": "",
}


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
    # Empty-string entries model "unset" without actually removing the key
    # from `env` (subprocess.run env dict never omits an empty-string value,
    # and `os.environ.get(..., "")` treats "" the same as absent).
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
    gate_script_source,
    auth_dir,
    session_id,
    subagent_type="spark-codex",
    *,
    launch_nonce="nonce-fixture",
    extra_env=None,
):
    return _run_gate(
        gate_script_source,
        "pre-tool-use-agent",
        {"session_id": session_id, "tool_input": {"subagent_type": subagent_type}},
        auth_dir=auth_dir,
        launch_nonce=launch_nonce,
        extra_env=extra_env,
    )


def _pre_tool_use_agent_full(
    gate_script_source, auth_dir, session_id, tool_input, *, launch_nonce="nonce-fixture", extra_env=None
):
    return _run_gate(
        gate_script_source,
        "pre-tool-use-agent",
        {"session_id": session_id, "tool_input": tool_input},
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


def test_required_terminal_failure_required_with_allowed_fallback_is_contradictory_malformed(
    gate_script_source, tmp_path
):
    # Issue #2258 P1-5 fix-delta: `mode: required` + `fallback: allowed` is a
    # semantically contradictory combination under this Issue's contract
    # (`required => no fallback`, `preferred => fallback allowed`) and is now
    # rejected as malformed rather than silently treated as a distinct valid
    # combination that grants an unlocked Spark authorization.
    auth_dir = tmp_path / "auth-req-allowed-fallback"
    prompt = "please:\n" + VALID_DIRECTIVE_LINES.format(mode="required", fallback="allowed")
    submit_result = _user_prompt_submit(gate_script_source, auth_dir, "sess-req-allow", prompt)
    assert not _pending_path(auth_dir, "sess-req-allow").exists()
    context = _additional_context(submit_result)
    assert context is not None
    assert context.startswith("CLAUDE_GPT_SPARK_DIRECTIVE_MALFORMED_V1 ")
    payload = json.loads(context[len("CLAUDE_GPT_SPARK_DIRECTIVE_MALFORMED_V1 ") :])
    assert payload["reason"] == "contradictory_mode_fallback"
    result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-req-allow")
    assert _decision(result) == "deny"
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


# --- Issue #2258 PR #2263 human REQUEST_CHANGES fix_delta ---------------------


# P0-1: the required-lock must exist at UserPromptSubmit time, before ANY
# Agent tool call this turn happens -- not only after a Spark authorization
# is consumed.


def test_p0_1_required_lock_established_at_user_prompt_submit_time(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-p0-1-lock-at-submit"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-p0-1", _required_directive())
    assert _required_lock_path(auth_dir, "sess-p0-1").exists()


def test_p0_1_first_non_spark_agent_call_before_any_spark_call_is_denied(gate_script_source, tmp_path):
    # Previously the FIRST Agent tool call of a `required`+`forbidden` turn
    # being a non-Spark subagent_type would silently pass through the
    # ordinary allow no-op path, because the lock was only written after a
    # Spark authorization was consumed. It must now be denied.
    auth_dir = tmp_path / "auth-p0-1-first-non-spark"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-p0-1-first", _required_directive())
    first_call = _pre_tool_use_agent(
        gate_script_source, auth_dir, "sess-p0-1-first", subagent_type="generic-worker"
    )
    assert _decision(first_call) == "deny"
    payload = _output(first_call)
    assert (
        payload["hookSpecificOutput"]["permissionDecisionReason"]
        == "required_delegation_lock_active_no_fallback_agent_allowed"
    )


# P0-2: required-lock write failures at UserPromptSubmit time must be
# fail-CLOSED -- no pending authorization record either, and no non-Spark
# Agent call silently allowed (denied for lack of authorization, not because
# of a lock).


def test_p0_2_required_lock_write_failure_is_fail_closed(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-p0-2-lock-write-fail"
    auth_dir.mkdir(parents=True)
    os.chmod(auth_dir, 0o500)
    try:
        submit_result = _user_prompt_submit(gate_script_source, auth_dir, "sess-p0-2", _required_directive())
        assert submit_result.returncode == 0
        # (a) no pending authorization record exists.
        assert not _pending_path(auth_dir, "sess-p0-2").exists()
        assert not _required_lock_path(auth_dir, "sess-p0-2").exists()
        # (b) a subsequent Spark PreToolUse call is denied.
        spark_attempt = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-p0-2")
        assert _decision(spark_attempt) == "deny"
        payload = _output(spark_attempt)
        assert payload["hookSpecificOutput"]["permissionDecisionReason"] == "no_pending_authorization"
        # (c) no non-Spark Agent call is silently allowed either -- it is
        # simply denied because no authorization exists at all, not because
        # of a lock (stdout is empty, i.e. the ordinary ungated no-op path,
        # not a `deny` gate decision citing an active lock).
        fallback_attempt = _pre_tool_use_agent(
            gate_script_source, auth_dir, "sess-p0-2", subagent_type="generic-worker"
        )
        assert fallback_attempt.stdout.strip() == ""
        assert fallback_attempt.returncode == 0
    finally:
        os.chmod(auth_dir, 0o700)


# Issue #2274 (AC1-AC4): model binding for spark-codex is owned exclusively
# by the session-local custom agent definition. The PreToolUse(Agent) hook
# never generates/forwards a `model` field in `updatedInput` -- it only
# inspects a caller-proposed `model` field per the normalization contract
# (absent -> allow untouched; exact match -> allow, key stripped; other
# string -> deny `explicit_model_override_mismatch`; non-string JSON type
# -> deny `invalid_model_field_type`).


def test_p0_3_explicit_model_override_mismatch_denied(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-p0-3-model-mismatch"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-p0-3-mismatch", _required_directive())
    result = _pre_tool_use_agent_full(
        gate_script_source,
        auth_dir,
        "sess-p0-3-mismatch",
        {"subagent_type": "spark-codex", "model": "claude-opus-4-5"},
    )
    assert _decision(result) == "deny"
    payload = _output(result)
    assert payload["hookSpecificOutput"]["permissionDecisionReason"] == "explicit_model_override_mismatch"
    assert "updatedInput" not in payload["hookSpecificOutput"]


def test_ac2_absent_model_key_allowed_no_model_forwarded(gate_script_source, tmp_path):
    # AC2: `tool_input.model` unspecified -> allow, and `updatedInput` never
    # gains a `model` key (custom agent definition owns model resolution).
    auth_dir = tmp_path / "auth-p0-3-model-absent"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-p0-3-absent", _required_directive())
    result = _pre_tool_use_agent_full(
        gate_script_source, auth_dir, "sess-p0-3-absent", {"subagent_type": "spark-codex"}
    )
    assert _decision(result) == "allow"
    payload = _output(result)
    assert "model" not in payload["hookSpecificOutput"]["updatedInput"]


def test_ac1_exact_model_match_allowed_key_stripped(gate_script_source, tmp_path):
    # AC1: an explicit `model` field that already exactly matches the Spark
    # model is allowed, but the hook still does not *forward* the key in
    # `updatedInput` -- it is stripped, not re-injected/pinned.
    auth_dir = tmp_path / "auth-p0-3-model-pinned"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-p0-3-pinned", _required_directive())
    result = _pre_tool_use_agent_full(
        gate_script_source,
        auth_dir,
        "sess-p0-3-pinned",
        {"subagent_type": "spark-codex", "model": "gpt-5.3-codex-spark"},
    )
    assert _decision(result) == "allow"
    payload = _output(result)
    assert "model" not in payload["hookSpecificOutput"]["updatedInput"]


@pytest.mark.parametrize("alias_value", ["spark", "gpt-5.3-codex", "inherit"])
def test_ac3_alias_or_inherit_model_denied(gate_script_source, tmp_path, alias_value):
    # AC3: alias / `inherit` / any other full model id that is not an exact
    # match is a hard negative control -- fail-closed deny, never a silent
    # substitution.
    auth_dir = tmp_path / f"auth-ac3-{alias_value}"
    _user_prompt_submit(gate_script_source, auth_dir, f"sess-ac3-{alias_value}", _required_directive())
    result = _pre_tool_use_agent_full(
        gate_script_source,
        auth_dir,
        f"sess-ac3-{alias_value}",
        {"subagent_type": "spark-codex", "model": alias_value},
    )
    assert _decision(result) == "deny"
    payload = _output(result)
    assert payload["hookSpecificOutput"]["permissionDecisionReason"] == "explicit_model_override_mismatch"
    assert "updatedInput" not in payload["hookSpecificOutput"]


@pytest.mark.parametrize("bad_value", [None, {}, [], 5, 5.3])
def test_ac4_invalid_model_field_type_denied(gate_script_source, tmp_path, bad_value):
    # AC4: a `model` field present but of a non-string JSON type (including
    # explicit JSON `null`) is denied with a distinct typed reason, never
    # silently coerced or treated as "absent".
    auth_dir = tmp_path / f"auth-ac4-{type(bad_value).__name__}"
    session_id = f"sess-ac4-{type(bad_value).__name__}"
    _user_prompt_submit(gate_script_source, auth_dir, session_id, _required_directive())
    result = _pre_tool_use_agent_full(
        gate_script_source,
        auth_dir,
        session_id,
        {"subagent_type": "spark-codex", "model": bad_value},
    )
    assert _decision(result) == "deny"
    payload = _output(result)
    assert payload["hookSpecificOutput"]["permissionDecisionReason"] == "invalid_model_field_type"
    assert "updatedInput" not in payload["hookSpecificOutput"]


# P0-4: on the allow path, `run_in_background` is force-pinned to `false` in
# `updatedInput` regardless of what value the caller proposed.


def test_p0_4_run_in_background_forced_false_on_allow(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-p0-4-wait-foreground"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-p0-4", _required_directive())
    result = _pre_tool_use_agent_full(
        gate_script_source,
        auth_dir,
        "sess-p0-4",
        {"subagent_type": "spark-codex", "run_in_background": True},
    )
    assert _decision(result) == "allow"
    payload = _output(result)
    assert payload["hookSpecificOutput"]["updatedInput"]["run_in_background"] is False


# P1-5: contradictory `mode`/`fallback` combinations are rejected as
# malformed instead of silently accepted.


@pytest.mark.parametrize(
    "mode,fallback",
    [("required", "allowed"), ("preferred", "forbidden")],
    ids=["required-allowed", "preferred-forbidden"],
)
def test_p1_5_contradictory_mode_fallback_rejected_as_malformed(gate_script_source, tmp_path, mode, fallback):
    auth_dir = tmp_path / f"auth-p1-5-contradictory-{mode}-{fallback}"
    prompt = "please:\n" + VALID_DIRECTIVE_LINES.format(mode=mode, fallback=fallback)
    result = _user_prompt_submit(gate_script_source, auth_dir, "sess-p1-5", prompt)
    assert not _pending_path(auth_dir, "sess-p1-5").exists()
    context = _additional_context(result)
    assert context is not None
    assert context.startswith("CLAUDE_GPT_SPARK_DIRECTIVE_MALFORMED_V1 ")
    payload = json.loads(context[len("CLAUDE_GPT_SPARK_DIRECTIVE_MALFORMED_V1 ") :])
    assert payload["reason"] == "contradictory_mode_fallback"


# P1-6: a malformed-directive diagnostic marker is always written when
# `status == "malformed"`, independent of whether a SEPARATE canonical
# mention also grants authorization this same turn.


def test_p1_6_malformed_marker_written_alongside_canonical_mention_authorization(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-p1-6-malformed-plus-canonical"
    malformed_block = (
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt-5.3-codex-spark\nmode: Required\n"
        "fallback: forbidden\nwait: true\nauthorization_source: explicit_directive\n"
    )
    prompt = malformed_block + "\nalso @agent-spark-codex please handle this directly.\n"
    _user_prompt_submit(gate_script_source, auth_dir, "sess-p1-6", prompt)
    # Canonical mention still authorizes Spark (backward compat preserved).
    assert _pending_path(auth_dir, "sess-p1-6").exists()
    # AND a malformed-directive diagnostic marker is still written for the
    # malformed directive attempt (both happen, not either/or).
    malformed_markers = list(auth_dir.glob("malformed-sess-p1-6-*.json"))
    assert len(malformed_markers) == 1
    spark_result = _pre_tool_use_agent(gate_script_source, auth_dir, "sess-p1-6")
    assert _decision(spark_result) == "allow"


# P1-7: a duplicated key or an unrecognized/extra key within an already
# schema-matched directive block is rejected as malformed instead of
# silently keeping the last-write-wins value / silently ignoring the extra
# key.


def test_p1_7_duplicate_key_in_directive_block_rejected_as_malformed(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-p1-7-duplicate-key"
    prompt = (
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt-5.3-codex-spark\nmode: required\n"
        "mode: preferred\nfallback: forbidden\nwait: true\nauthorization_source: explicit_directive\n"
    )
    result = _user_prompt_submit(gate_script_source, auth_dir, "sess-p1-7-dup", prompt)
    assert not _pending_path(auth_dir, "sess-p1-7-dup").exists()
    context = _additional_context(result)
    assert context is not None
    payload = json.loads(context[len("CLAUDE_GPT_SPARK_DIRECTIVE_MALFORMED_V1 ") :])
    assert payload["reason"] == "duplicate_key"


def test_p1_7_unknown_key_in_directive_block_rejected_as_malformed(gate_script_source, tmp_path):
    auth_dir = tmp_path / "auth-p1-7-unknown-key"
    prompt = (
        "schema: DELEGATION_REQUEST_V1\nagent_id: spark-codex\nmodel: gpt-5.3-codex-spark\nmode: required\n"
        "fallback: forbidden\nwait: true\nauthorization_source: explicit_directive\npriority: high\n"
    )
    result = _user_prompt_submit(gate_script_source, auth_dir, "sess-p1-7-unknown", prompt)
    assert not _pending_path(auth_dir, "sess-p1-7-unknown").exists()
    context = _additional_context(result)
    assert context is not None
    payload = json.loads(context[len("CLAUDE_GPT_SPARK_DIRECTIVE_MALFORMED_V1 ") :])
    assert payload["reason"] == "unknown_key"

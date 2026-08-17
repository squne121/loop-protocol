"""Issue #2186: explicit-only GPT-5.3-Codex-Spark SubAgent gate.

These tests exercise the *exact* python source embedded in
``scripts/claude-gpt/launch.sh`` between the ``SPARK_GATE_WRITER_PY_BEGIN``/
``_END`` markers (extracted via
``run_worktree_agent_runtime_smoke.extract_spark_gate_writer_source``), so
there is a single source of truth between what actually runs in a live
claude-gpt session and what this hermetic test suite verifies. No logic is
re-implemented/duplicated here.

Covers (see Issue #2186 Verification Commands):
- ``-k mention_authorization`` (AC2): canonical mention detection, positive
  and negative controls (quoted text, fenced/inline code, blockquote, typo,
  different agent name).
- ``-k authorization_single_use`` (AC3): single consumption, duplicate
  invocation rejection, past-turn-only mention rejection, next-turn
  carryover rejection, stale launch-nonce (resume) rejection.
- ``-k nested_delegation_forbidden`` (AC5): no UserPromptSubmit authorization
  present -> deny; a spoofed/self-reported authorization without a genuine
  pending sidecar record does not grant access; unrelated non-spark
  subagent_type calls are an explicit no-op allow (not a gate decision) so
  ordinary SubAgent mapping is never touched.
- ``-k failure_isolation`` (AC6): a denied/failed spark-codex authorization
  attempt does not corrupt subsequent ordinary-turn gate state for the same
  session.
- ``-k no_fallback_skip_blocked`` (AC7): every sanitized failure
  classification category is exit 77 / non-PASS / non-fallback-eligible.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
LAUNCH_SH = REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_worktree_agent_runtime_smoke_spark_gate", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate_script_path(tmp_path_factory) -> Path:
    module = _load_module()
    launch_sh_text = LAUNCH_SH.read_text(encoding="utf-8")
    source = module.extract_spark_gate_writer_source(launch_sh_text)
    assert source is not None, "SPARK_GATE_WRITER_PY_BEGIN/_END markers not found in launch.sh"
    tmp_dir = tmp_path_factory.mktemp("spark-gate")
    gate_path = tmp_dir / "spark_gate.py"
    gate_path.write_text(source, encoding="utf-8")
    return gate_path


def _run_gate(
    gate_script_path: Path,
    event: str,
    payload: dict,
    *,
    auth_dir: Path,
    launch_nonce: str = "nonce-fixture",
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "CLAUDE_GPT_SPARK_AUTH_DIR": str(auth_dir),
        "CLAUDE_GPT_SPARK_LAUNCH_NONCE": launch_nonce,
    }
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


def _user_prompt_submit(gate_script_path, auth_dir, session_id, prompt, *, launch_nonce="nonce-fixture"):
    return _run_gate(
        gate_script_path,
        "user-prompt-submit",
        {"session_id": session_id, "prompt": prompt},
        auth_dir=auth_dir,
        launch_nonce=launch_nonce,
    )


def _pre_tool_use_agent(
    gate_script_path, auth_dir, session_id, subagent_type="spark-codex", *, launch_nonce="nonce-fixture"
):
    return _run_gate(
        gate_script_path,
        "pre-tool-use-agent",
        {"session_id": session_id, "tool_input": {"subagent_type": subagent_type}},
        auth_dir=auth_dir,
        launch_nonce=launch_nonce,
    )


def _decision(result: subprocess.CompletedProcess[str]) -> str | None:
    if not result.stdout.strip():
        return None
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"]["permissionDecision"]


# --- mention_authorization (AC2) -------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "please help @agent-spark-codex with this",
        "@agent-spark-codex",
        "can you delegate this to @agent-spark-codex now?",
    ],
)
def test_mention_authorization_canonical_mention_grants_pending(gate_script_path, tmp_path, prompt):
    auth_dir = tmp_path / "auth-canon"
    _user_prompt_submit(gate_script_path, auth_dir, "sess-canon", prompt)
    assert _pending_path(auth_dir, "sess-canon").exists()


@pytest.mark.parametrize(
    "prompt",
    [
        'quoted: "@agent-spark-codex" appears here',
        "single-quoted: '@agent-spark-codex' appears here",
        "```\n@agent-spark-codex\n```",
        "inline code `@agent-spark-codex` mention",
        "> quoting a previous message: @agent-spark-codex",
        "@Agent-Spark-Codex (wrong case typo)",
        "@agent_spark_codex (underscore typo)",
        "@agent-spark-codexx (suffix typo)",
        "@agent-spark-code (truncated typo)",
        "@agent-other-subagent please help",
        "no mention here at all",
    ],
)
def test_mention_authorization_non_canonical_does_not_grant(gate_script_path, tmp_path, prompt):
    auth_dir = tmp_path / "auth-negative"
    _user_prompt_submit(gate_script_path, auth_dir, "sess-neg", prompt)
    assert not _pending_path(auth_dir, "sess-neg").exists()


def test_mention_authorization_canonical_mention_survives_alongside_quoted_decoy(gate_script_path, tmp_path):
    auth_dir = tmp_path / "auth-mixed"
    prompt = 'earlier text quoted "@agent-spark-codex" as an example, but now really: @agent-spark-codex'
    _user_prompt_submit(gate_script_path, auth_dir, "sess-mixed", prompt)
    assert _pending_path(auth_dir, "sess-mixed").exists()


# --- authorization_single_use (AC3) ----------------------------------------


def test_authorization_single_use_first_invocation_allows_and_consumes(gate_script_path, tmp_path):
    auth_dir = tmp_path / "auth-single"
    _user_prompt_submit(gate_script_path, auth_dir, "sess-a", "@agent-spark-codex go")
    result = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-a")
    assert _decision(result) == "allow"
    assert result.returncode == 0
    assert not _pending_path(auth_dir, "sess-a").exists()


def test_authorization_single_use_duplicate_invocation_rejected(gate_script_path, tmp_path):
    auth_dir = tmp_path / "auth-dup"
    _user_prompt_submit(gate_script_path, auth_dir, "sess-b", "@agent-spark-codex go")
    first = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-b")
    second = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-b")
    assert _decision(first) == "allow"
    assert _decision(second) == "deny"
    assert second.returncode == 2


def test_authorization_single_use_past_turn_only_mention_rejected(gate_script_path, tmp_path):
    auth_dir = tmp_path / "auth-past-turn"
    _user_prompt_submit(gate_script_path, auth_dir, "sess-c", "@agent-spark-codex please")
    # A subsequent ordinary turn (no mention) must clear the prior
    # authorization before the tool call ever happens.
    _user_prompt_submit(gate_script_path, auth_dir, "sess-c", "thanks, now do something ordinary")
    result = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-c")
    assert _decision(result) == "deny"


def test_authorization_single_use_next_turn_carryover_rejected(gate_script_path, tmp_path):
    auth_dir = tmp_path / "auth-carryover"
    _user_prompt_submit(gate_script_path, auth_dir, "sess-d", "@agent-spark-codex please")
    _user_prompt_submit(gate_script_path, auth_dir, "sess-d", "next turn, unrelated")
    _user_prompt_submit(gate_script_path, auth_dir, "sess-d", "yet another ordinary turn")
    result = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-d")
    assert _decision(result) == "deny"


def test_authorization_single_use_stale_resume_launch_nonce_rejected(gate_script_path, tmp_path):
    auth_dir = tmp_path / "auth-resume"
    _user_prompt_submit(
        gate_script_path, auth_dir, "sess-e", "@agent-spark-codex please", launch_nonce="nonce-old-session"
    )
    result = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-e", launch_nonce="nonce-new-session-after-resume")
    assert _decision(result) == "deny"


def test_authorization_single_use_no_prior_prompt_submit_rejected(gate_script_path, tmp_path):
    auth_dir = tmp_path / "auth-no-prior"
    result = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-f")
    assert _decision(result) == "deny"
    assert result.returncode == 2


# --- nested_delegation_forbidden (AC5) --------------------------------------


def test_nested_delegation_forbidden_without_user_prompt_submit(gate_script_path, tmp_path):
    """A directly-invoked Agent tool_use for spark-codex, with no prior
    UserPromptSubmit having recorded a pending authorization in this
    session, is denied -- this is exactly the shape of an autonomous/
    nested-delegation invocation attempt."""
    auth_dir = tmp_path / "auth-nested-1"
    result = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-nested-1")
    assert _decision(result) == "deny"


def test_nested_delegation_forbidden_different_session_cannot_reuse_authorization(gate_script_path, tmp_path):
    """A different session_id (representing e.g. a nested child/other
    SubAgent's own hook context, or an attempted authorization-file
    forgery/reuse across sessions) cannot consume an authorization recorded
    for a different session_id."""
    auth_dir = tmp_path / "auth-nested-2"
    _user_prompt_submit(gate_script_path, auth_dir, "sess-parent", "@agent-spark-codex please")
    result = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-other-nested-child")
    assert _decision(result) == "deny"
    # The parent's genuine authorization must remain untouched/consumable.
    parent_result = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-parent")
    assert _decision(parent_result) == "allow"


def test_nested_delegation_forbidden_self_reported_marker_alone_is_not_evidence(gate_script_path, tmp_path):
    """A crafted payload that merely *claims* authorization (e.g. an
    ``authorized: true`` field injected into the PreToolUse payload) is
    ignored -- the gate only consults the independently-written sidecar
    pending-authorization file keyed by session_id/launch_nonce, never the
    tool_input payload's own self-reported claims."""
    auth_dir = tmp_path / "auth-nested-3"
    result = _run_gate(
        gate_script_path,
        "pre-tool-use-agent",
        {
            "session_id": "sess-spoof",
            "tool_input": {"subagent_type": "spark-codex", "authorized": True, "authorization_turn_id": "sess-spoof:1"},
        },
        auth_dir=auth_dir,
    )
    assert _decision(result) == "deny"


def test_nested_delegation_forbidden_unrelated_ordinary_subagent_is_noop(gate_script_path, tmp_path):
    """Non-spark subagent_type invocations are an explicit pass-through
    no-op (no stdout, exit 0) -- the gate never touches ordinary SubAgent
    mapping (Issue #2186 AC1/AC5), and an unrelated ordinary child's
    completion never consumes a pending spark-codex authorization for the
    same session."""
    auth_dir = tmp_path / "auth-nested-4"
    _user_prompt_submit(gate_script_path, auth_dir, "sess-mixed-agents", "@agent-spark-codex please")
    ordinary = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-mixed-agents", subagent_type="issue-editor")
    assert ordinary.stdout.strip() == ""
    assert ordinary.returncode == 0
    # The genuine spark-codex authorization must still be intact/consumable.
    spark_result = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-mixed-agents")
    assert _decision(spark_result) == "allow"


# --- failure_isolation (AC6) -------------------------------------------------


def test_failure_isolation_denied_attempt_does_not_corrupt_subsequent_ordinary_turn(gate_script_path, tmp_path):
    auth_dir = tmp_path / "auth-isolation"
    # A denied (unauthorized) attempt happens first.
    denied = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-iso")
    assert _decision(denied) == "deny"
    # The main session must still be able to continue an ordinary ensuing
    # turn, and a later, properly-authorized spark-codex invocation in a
    # subsequent turn must still work normally (no corrupted gate state).
    ordinary = _user_prompt_submit(gate_script_path, auth_dir, "sess-iso", "let's continue normally")
    assert ordinary.returncode == 0
    _user_prompt_submit(gate_script_path, auth_dir, "sess-iso", "@agent-spark-codex now really")
    allowed = _pre_tool_use_agent(gate_script_path, auth_dir, "sess-iso")
    assert _decision(allowed) == "allow"


def test_failure_isolation_gate_script_never_crashes_on_malformed_payload(gate_script_path, tmp_path):
    auth_dir = tmp_path / "auth-malformed"
    result = subprocess.run(
        [sys.executable, str(gate_script_path), "pre-tool-use-agent"],
        input="not valid json{{{",
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_GPT_SPARK_AUTH_DIR": str(auth_dir), "CLAUDE_GPT_SPARK_LAUNCH_NONCE": "n"},
        timeout=15,
    )
    # Fail-closed: malformed input must never crash uncaught nor grant access.
    assert result.returncode in (0, 2)
    assert "Traceback" not in result.stderr


# --- no_fallback_skip_blocked (AC7) -----------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected_classification"),
    [
        ("account not entitled to gpt-5.3-codex-spark", "unsupported_entitlement"),
        ("model not found in catalog", "unavailable_catalog"),
        ("unsupported parameter reasoning_effort for this model", "request_parameter_incompatibility"),
        ("quota exceeded, rate limit hit (429)", "quota"),
        ("tool web_search unsupported/incompatible for this model", "tool_incompatibility"),
        ("encrypted continuation/context compaction error", "context_continuation_error"),
        ("totally unexpected upstream error", "other_safe_failure"),
    ],
)
def test_no_fallback_skip_blocked_classification_never_promotes_to_pass(reason, expected_classification):
    module = _load_module()
    result = module.classify_spark_failure(reason)
    assert result["classification"] == expected_classification
    assert result["exit_code"] == 77
    assert result["status"] != "pass"
    assert result["fallback_eligible"] is False
    assert result["promotable_to_pass"] is False
    assert result["redaction_confirmed"] is True


def test_no_fallback_skip_blocked_all_categories_covered():
    module = _load_module()
    assert set(module.SPARK_FAILURE_CLASSIFICATIONS) == {
        "unsupported_entitlement",
        "unavailable_catalog",
        "request_parameter_incompatibility",
        "quota",
        "tool_incompatibility",
        "context_continuation_error",
        "other_safe_failure",
    }
    for category in module.SPARK_FAILURE_CLASSIFICATIONS:
        classification = module.classify_spark_failure(category.replace("_", " "))
        assert classification["fallback_eligible"] is False
        assert classification["promotable_to_pass"] is False

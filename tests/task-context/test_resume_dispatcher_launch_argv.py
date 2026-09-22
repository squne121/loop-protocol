"""Issue #2569 -- resume dispatcher launch argv construction.

Locks in the two fixed, repository-owned launch shapes
(``build_launch_argv``) against the causal-probe-validated invocation
observed in the Issue's P0 recovery investigation
(https://github.com/squne121/loop-protocol/issues/2569#issuecomment-5772732319:
the `[[startup]]` hook itself executed
``herdr agent start ... --kind claude --pane w1:p1 -- --resume <sid>``)."""

from __future__ import annotations

import pytest

import task_context_resume_dispatcher as dispatcher


def _native_decision(session_id="s-native-1", binding_id="binding_abc123"):
    return dispatcher.ResumeDecision(
        action=dispatcher.ACTION_LAUNCH_NATIVE,
        reason_code="active_managed_binding_dispatchable",
        session_id=session_id,
        binding_id=binding_id,
        effective_profile="native_claude_v1",
    )


def _claude_gpt_decision(session_id="s-gpt-1", binding_id="binding_def456"):
    return dispatcher.ResumeDecision(
        action=dispatcher.ACTION_LAUNCH_CLAUDE_GPT,
        reason_code="active_managed_binding_dispatchable",
        session_id=session_id,
        binding_id=binding_id,
        effective_profile="claude_gpt_v1",
    )


def test_given_native_decision_when_building_launch_argv_then_matches_causal_probe_observed_shape():
    decision = _native_decision()
    argv = dispatcher.build_launch_argv(decision, pane_id="w1:p1", agent_name="r-abc123")
    assert argv == [
        "agent",
        "start",
        "r-abc123",
        "--kind",
        "claude",
        "--pane",
        "w1:p1",
        "--",
        "--resume",
        "s-native-1",
    ]


def test_given_claude_gpt_decision_when_building_launch_argv_then_uses_pane_run_with_repo_launcher():
    decision = _claude_gpt_decision()
    argv = dispatcher.build_launch_argv(decision, pane_id="w1:p2", claude_gpt_launch_script="/repo/scripts/claude-gpt/launch.sh")
    assert argv == ["pane", "run", "w1:p2", "/repo/scripts/claude-gpt/launch.sh", "--", "--resume", "s-gpt-1"]


def test_given_claude_gpt_decision_when_no_script_override_then_defaults_to_repository_owned_launcher():
    decision = _claude_gpt_decision()
    argv = dispatcher.build_launch_argv(decision, pane_id="w1:p2")
    assert argv[3].endswith("scripts/claude-gpt/launch.sh")
    assert "--kind" not in argv, "Claude-GPT must never go through --kind claude (would bypass the wrapper env swap)"


def test_given_restore_blocked_decision_when_building_launch_argv_then_raises():
    decision = dispatcher.ResumeDecision(
        action=dispatcher.ACTION_RESTORE_BLOCKED, reason_code="invalid_managed_profile", session_id="s1"
    )
    with pytest.raises(ValueError):
        dispatcher.build_launch_argv(decision, pane_id="w1:p1")


def test_given_missing_pane_id_when_building_launch_argv_then_raises():
    with pytest.raises(ValueError):
        dispatcher.build_launch_argv(_native_decision(), pane_id="")


def test_given_no_explicit_agent_name_when_building_native_argv_then_derives_deterministic_name_from_binding_id():
    decision = _native_decision(binding_id="binding_deadbeef")
    argv = dispatcher.build_launch_argv(decision, pane_id="w1:p1")
    name = argv[2]
    assert name == "r-deadbeef"
    # Same binding_id must always resolve to the same agent name (so a
    # re-dispatch after a transient failure targets the same name).
    argv_again = dispatcher.build_launch_argv(decision, pane_id="w1:p1")
    assert argv_again[2] == name

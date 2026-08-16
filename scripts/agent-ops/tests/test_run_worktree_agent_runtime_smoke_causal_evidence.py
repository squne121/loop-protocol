"""Issue #2183 (follow-up to Issue #2174 OWNER REQUEST_CHANGES
https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173,
and PR #2214 OWNER review
https://github.com/squne121/loop-protocol/pull/2214#issuecomment-5307009937):
hermetic regression tests for ``subagent_causal_evidence_verdict()``, the
hook-ID-correlated SubAgent causal evidence judgment that replaces a bare
marker-string observation as the PASS-determining signal.

These tests are entirely hermetic: they call the pure stdout-parsing
functions in ``run_worktree_agent_runtime_smoke.py`` directly with synthetic
fixture stream-json text -- no live Claude Code / Codex CLI process is ever
spawned (Runtime Verification Applicability: not_applicable, per Issue
#2183's own contract). Fixture shapes mirror
``test_child_spawn_completion_evidence.py`` (Issue #2015 AC11).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "run_worktree_agent_runtime_smoke.py"
_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2183_causal_evidence"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = smoke
_spec.loader.exec_module(smoke)


SESSION_ID = "9846ca4d-0893-43dd-bcec-519360aa31fb"
CHILD_AGENT_ID = "a14b7e0673d997e52"
OTHER_AGENT_ID = "ffffffffffffffffff"
AGENT_TYPE = "codebase-investigator"
TRANSCRIPT_PATH = "/home/user/.claude/projects/-repo/a14b7e0673d997e52.jsonl"
AGENT_TOOL_USE_ID = "toolu_01AgentInvocation"
COMPLETION_MARKER = "SUBAGENT_RUN_COMPLETE"


def _line(payload: dict) -> str:
    return json.dumps(payload)


def _hook_payload(
    agent_id: str,
    hook_event_name: str,
    *,
    agent_type: str = AGENT_TYPE,
    agent_transcript_path: str | None = None,
) -> str:
    payload = {
        "session_id": SESSION_ID,
        "hook_event_name": hook_event_name,
        "agent_id": agent_id,
        "agent_type": agent_type,
    }
    if agent_transcript_path is not None:
        payload["agent_transcript_path"] = agent_transcript_path
    return json.dumps(payload)


def _hook_event(
    hook_event: str,
    *,
    agent_id: str,
    agent_type: str = AGENT_TYPE,
    agent_transcript_path: str | None = None,
) -> str:
    return _line(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": hook_event,
            "hook_name": hook_event,
            "session_id": SESSION_ID,
            "stdout": _hook_payload(
                agent_id, hook_event, agent_type=agent_type, agent_transcript_path=agent_transcript_path
            ),
            "output": _hook_payload(
                agent_id, hook_event, agent_type=agent_type, agent_transcript_path=agent_transcript_path
            ),
        }
    )


def _agent_tool_use_event(tool_use_id: str = AGENT_TOOL_USE_ID) -> str:
    return _line(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {
                "content": [
                    {"type": "tool_use", "id": tool_use_id, "name": "Agent", "input": {}},
                ]
            },
        }
    )


def _agent_tool_result_event(agent_id: str, tool_use_id: str = AGENT_TOOL_USE_ID) -> str:
    return _line(
        {
            "type": "user",
            "session_id": SESSION_ID,
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": "done"},
                ]
            },
            "tool_use_result": {"status": "completed", "agentId": agent_id, "agentType": AGENT_TYPE},
        }
    )


# ---------------------------------------------------------------------------
# AC1/AC2/AC3: function + field presence (also asserted structurally below).
# ---------------------------------------------------------------------------


def test_function_exists_and_returns_required_fields() -> None:
    verdict = smoke.subagent_causal_evidence_verdict("")
    assert "causal_evidence_source" in verdict
    assert "tool_invocation_id_correlated" in verdict
    assert "agent_id" in verdict
    assert "agent_transcript_path" in verdict


# ---------------------------------------------------------------------------
# Positive case: correlated Start/Stop pair + transcript path + tool
# invocation correlation -> hook_id_correlated, tool_invocation_id_correlated
# True.
# ---------------------------------------------------------------------------


def test_given_correlated_start_stop_with_transcript_when_verdict_computed_then_hook_id_correlated() -> None:
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop", agent_id=CHILD_AGENT_ID, agent_transcript_path=TRANSCRIPT_PATH
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["agent_id"] == CHILD_AGENT_ID
    assert verdict["agent_transcript_path"] == TRANSCRIPT_PATH
    assert verdict["tool_invocation_id_correlated"] is True
    assert verdict["subagent_start_observed"] is True
    assert verdict["subagent_stop_observed"] is True


def test_given_correlated_start_stop_no_transcript_path_when_verdict_computed_then_not_correlated() -> None:
    # A Stop event that correlates by agent_id but never recovered a
    # transcript path must not be silently promoted to hook_id_correlated
    # (AC2's literal requirement: both must be present).
    stdout = "\n".join(
        [
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _hook_event("SubagentStop", agent_id=CHILD_AGENT_ID),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["agent_transcript_path"] is None


# ---------------------------------------------------------------------------
# AC4: SubagentStart observed with no matching SubagentStop -- must never be
# falsely reported as hook_id_correlated.
# ---------------------------------------------------------------------------


def test_given_subagent_start_without_stop_when_verdict_computed_then_not_hook_id_correlated() -> None:
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE
    assert verdict["subagent_start_observed"] is True
    assert verdict["subagent_stop_observed"] is False
    assert verdict["agent_id"] is None
    assert verdict["agent_transcript_path"] is None


def test_given_subagent_start_with_mismatched_stop_agent_id_when_verdict_computed_then_no_evidence() -> None:
    stdout = "\n".join(
        [
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _hook_event("SubagentStop", agent_id=OTHER_AGENT_ID, agent_transcript_path=TRANSCRIPT_PATH),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE
    assert verdict["agent_id"] is None


# ---------------------------------------------------------------------------
# AC5: no hook events observed at all, only a marker string in stdout ->
# marker_only_insufficient (never promoted to hook_id_correlated).
# ---------------------------------------------------------------------------


def test_given_marker_only_no_hook_events_when_verdict_computed_then_marker_only_insufficient() -> None:
    stdout = f"some assistant text containing {COMPLETION_MARKER} and nothing else"
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_MARKER_ONLY_INSUFFICIENT
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["subagent_start_observed"] is False
    assert verdict["subagent_stop_observed"] is False
    assert verdict["tool_invocation_id_correlated"] is False


def test_given_no_hook_events_and_no_marker_when_verdict_computed_then_no_evidence() -> None:
    stdout = "some assistant text with nothing recognizable at all"
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_empty_stdout_when_verdict_computed_then_no_evidence() -> None:
    verdict = smoke.subagent_causal_evidence_verdict("")
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE
    assert verdict["agent_id"] is None
    assert verdict["tool_invocation_id_correlated"] is False


# ---------------------------------------------------------------------------
# AC3: tool_invocation_id_correlated must fail closed to False when the
# Agent tool_use/tool_result envelope is absent, even if the hook channel
# itself is fully correlated.
# ---------------------------------------------------------------------------


def test_given_hook_correlated_but_no_agent_tool_result_when_verdict_computed_then_no_correlation() -> None:
    stdout = "\n".join(
        [
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop", agent_id=CHILD_AGENT_ID, agent_transcript_path=TRANSCRIPT_PATH
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["tool_invocation_id_correlated"] is False


def test_given_agent_tool_result_agent_id_mismatch_when_verdict_computed_then_tool_invocation_not_correlated() -> None:
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _agent_tool_result_event(OTHER_AGENT_ID),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop", agent_id=CHILD_AGENT_ID, agent_transcript_path=TRANSCRIPT_PATH
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["tool_invocation_id_correlated"] is False


# ---------------------------------------------------------------------------
# extract_claude_hook_lifecycle_events: agent_transcript_path now recovered
# (regression guard for the AC1/AC2 field extension).
# ---------------------------------------------------------------------------


def test_extract_claude_hook_lifecycle_events_recovers_agent_transcript_path() -> None:
    stdout = _hook_event(
        "SubagentStop", agent_id=CHILD_AGENT_ID, agent_transcript_path=TRANSCRIPT_PATH
    )
    events = smoke.extract_claude_hook_lifecycle_events(stdout)
    assert len(events) == 1
    assert events[0]["agent_transcript_path"] == TRANSCRIPT_PATH


def test_extract_claude_hook_lifecycle_events_agent_transcript_path_none_when_absent() -> None:
    stdout = _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID)
    events = smoke.extract_claude_hook_lifecycle_events(stdout)
    assert len(events) == 1
    assert events[0]["agent_transcript_path"] is None

"""Issue #2015 AC11 (OWNER Scope Reframe 2026-08-09): child spawn observation
and child completion observation must be separate, explicit, independently
recorded signals -- a ``SubagentStart`` hook event (or an ``async_launched``
tool_use_result) must never be silently promoted to "completed" just because
identity evidence (agent id / agent type) was recovered.

These tests are entirely hermetic: they call the pure stdout-parsing
functions in ``run_worktree_agent_runtime_smoke.py`` directly with synthetic
fixture stream-json text -- no live Claude Code / Codex CLI process is ever
spawned. Fixture shapes mirror ``test_claude_spawn_identity_evidence.py``
(Issue #2021 research artifact), extended with distinct SubagentStart vs
SubagentStop hook events.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "run_worktree_agent_runtime_smoke.py"
_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2015_ac11"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = smoke
_spec.loader.exec_module(smoke)


SESSION_ID = "9846ca4d-0893-43dd-bcec-519360aa31fb"
CHILD_AGENT_ID = "a14b7e0673d997e52"
OTHER_AGENT_ID = "ffffffffffffffffff"
AGENT_TYPE = "codebase-investigator"


def _line(payload: dict) -> str:
    return json.dumps(payload)


def _async_launched_tool_result(agent_id: str = CHILD_AGENT_ID) -> str:
    return _line(
        {
            "type": "user",
            "session_id": SESSION_ID,
            "tool_use_result": {
                "isAsync": True,
                "status": "async_launched",
                "agentId": agent_id,
                "description": "spawn probe",
            },
        }
    )


def _completed_tool_result(agent_id: str = CHILD_AGENT_ID, agent_type: str = AGENT_TYPE) -> str:
    return _line(
        {
            "type": "user",
            "session_id": SESSION_ID,
            "tool_use_result": {
                "status": "completed",
                "agentId": agent_id,
                "agentType": agent_type,
                "content": "done",
            },
        }
    )


def _hook_payload(agent_id: str, hook_event_name: str, agent_type: str = AGENT_TYPE) -> str:
    return json.dumps(
        {
            "session_id": SESSION_ID,
            "hook_event_name": hook_event_name,
            "agent_id": agent_id,
            "agent_type": agent_type,
        }
    )


def _hook_event(hook_event: str, *, agent_id: str, agent_type: str = AGENT_TYPE) -> str:
    return _line(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": hook_event,
            "hook_name": hook_event,
            "session_id": SESSION_ID,
            "stdout": _hook_payload(agent_id, hook_event, agent_type),
            "output": _hook_payload(agent_id, hook_event, agent_type),
        }
    )


def _result_event() -> str:
    return _line({"type": "result", "subtype": "success", "session_id": SESSION_ID})


# ---------------------------------------------------------------------------
# extract_claude_hook_lifecycle_events: Start and Stop kept as separate,
# independently-typed records (never merged).
# ---------------------------------------------------------------------------


def test_hook_lifecycle_events_keeps_start_and_stop_distinct() -> None:
    stdout = "\n".join(
        [
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _hook_event("SubagentStop", agent_id=CHILD_AGENT_ID),
        ]
    )
    events = smoke.extract_claude_hook_lifecycle_events(stdout)
    kinds = [e["hook_event"] for e in events]
    assert kinds == ["SubagentStart", "SubagentStop"]
    assert all(e["agent_id"] == CHILD_AGENT_ID for e in events)


# ---------------------------------------------------------------------------
# classify_claude_child_spawn_agent_id
# ---------------------------------------------------------------------------


def test_spawn_agent_id_recovered_from_tool_use_result() -> None:
    stdout = "\n".join([_async_launched_tool_result()])
    agent_id, source = smoke.classify_claude_child_spawn_agent_id(stdout)
    assert agent_id == CHILD_AGENT_ID
    assert source == "tool_use_result"


def test_spawn_agent_id_recovered_from_hook_start_when_tool_result_absent() -> None:
    stdout = "\n".join([_hook_event("SubagentStart", agent_id=CHILD_AGENT_ID)])
    agent_id, source = smoke.classify_claude_child_spawn_agent_id(stdout)
    assert agent_id == CHILD_AGENT_ID
    assert source == "hook_subagent_start"


def test_spawn_agent_id_none_when_no_evidence() -> None:
    stdout = "\n".join([_result_event()])
    agent_id, source = smoke.classify_claude_child_spawn_agent_id(stdout)
    assert (agent_id, source) == (None, None)


# ---------------------------------------------------------------------------
# classify_claude_child_completion -- the core AC11 regression coverage.
# ---------------------------------------------------------------------------


def test_synchronous_completed_envelope_is_observed_as_completion() -> None:
    stdout = "\n".join([_completed_tool_result()])
    result = smoke.classify_claude_child_completion(stdout, CHILD_AGENT_ID)
    assert result["observed"] is True
    assert result["source"] == smoke.CHILD_COMPLETION_SOURCE_TOOL_RESULT
    assert result["terminal_status"] == smoke.CHILD_TERMINAL_STATUS_COMPLETED


def test_async_launched_with_matching_subagent_stop_is_completion() -> None:
    """The genuine fixed-async-completion path: an ``async_launched``
    envelope followed (later in the SAME captured stdout, i.e. the parent
    process did not exit until this fired) by a ``SubagentStop`` whose
    ``agent_id`` matches the spawned child."""
    stdout = "\n".join(
        [
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _async_launched_tool_result(),
            _hook_event("SubagentStop", agent_id=CHILD_AGENT_ID),
            _result_event(),
        ]
    )
    agent_id, _source = smoke.classify_claude_child_spawn_agent_id(stdout)
    result = smoke.classify_claude_child_completion(stdout, agent_id)
    assert result["observed"] is True
    assert result["source"] == smoke.CHILD_COMPLETION_SOURCE_HOOK_STOP


def test_subagent_start_without_stop_is_not_falsely_reported_as_completion() -> None:
    """AC11 fixture: SubagentStart present but SubagentStop missing must
    NOT be treated as completion."""
    stdout = "\n".join(
        [
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _async_launched_tool_result(),
        ]
    )
    agent_id, _source = smoke.classify_claude_child_spawn_agent_id(stdout)
    result = smoke.classify_claude_child_completion(stdout, agent_id)
    assert result["observed"] is False
    assert result["source"] is None
    assert result["terminal_status"] is None


def test_agent_id_mismatch_between_start_and_stop_is_not_falsely_matched() -> None:
    """AC11 fixture: a SubagentStop for a DIFFERENT agent id must never be
    accepted as this spawn's completion evidence."""
    stdout = "\n".join(
        [
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _async_launched_tool_result(agent_id=CHILD_AGENT_ID),
            _hook_event("SubagentStop", agent_id=OTHER_AGENT_ID),
        ]
    )
    result = smoke.classify_claude_child_completion(stdout, CHILD_AGENT_ID)
    assert result["observed"] is False


def test_completion_never_asserted_when_spawn_agent_id_unknown() -> None:
    """When spawn itself was never observed (agent id unknown), completion
    must fail closed to False rather than matching an unrelated SubagentStop
    event by coincidence (e.g. one from a prior unrelated Task call in the
    same session)."""
    stdout = "\n".join([_hook_event("SubagentStop", agent_id=CHILD_AGENT_ID)])
    result = smoke.classify_claude_child_completion(stdout, None)
    assert result["observed"] is False


def test_async_launched_with_no_subagent_hooks_at_all_fails_closed() -> None:
    """Plausible real-world shape: async_launched envelope, but
    ``--include-hook-events`` hooks never fired at all (upstream known
    issue referenced in the module docstring) -- must not be silently
    treated as completed."""
    stdout = "\n".join([_async_launched_tool_result(), _result_event()])
    agent_id, _source = smoke.classify_claude_child_spawn_agent_id(stdout)
    result = smoke.classify_claude_child_completion(stdout, agent_id)
    assert result["observed"] is False

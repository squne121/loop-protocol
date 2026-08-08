"""Issue #2021: child identity evidence must survive the asynchronous
``Agent`` launch envelope.

Evidence source: the Issue #2013 research artifact
(``artifacts/claude-code-spawn-observability-research/``, Claude Code 2.1.225,
30 live trials). The ``Agent`` tool returns either a synchronous
``status: "completed"`` envelope (which carries ``agentType``) or an
asynchronous ``status: "async_launched"`` envelope (which carries ``agentId``
but no ``agentType``). Only the tool_use_result channel was consulted, so the
async shape produced ``native_spawn_event_observed == False`` on runs where
the spawn was fully observable on the hook channel.

These fixtures reproduce both envelope shapes plus the hook lifecycle events
exactly as they appear in the captured raw stream-json.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "run_worktree_agent_runtime_smoke.py"
_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2021"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = smoke
_spec.loader.exec_module(smoke)


SESSION_ID = "9846ca4d-0893-43dd-bcec-519360aa31fb"
CHILD_AGENT_ID = "a14b7e0673d997e52"
AGENT_TYPE = "codebase-investigator"


def _line(payload: dict) -> str:
    return json.dumps(payload)


def _system_init() -> str:
    return _line({"type": "system", "subtype": "init", "session_id": SESSION_ID})


def _assistant_agent_dispatch() -> str:
    return _line(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Agent",
                        "input": {"subagent_type": AGENT_TYPE, "prompt": "probe"},
                    }
                ]
            },
        }
    )


def _async_launched_tool_result() -> str:
    """The envelope observed in the #2013 research: ``agentId`` present,
    ``agentType`` absent."""
    return _line(
        {
            "type": "user",
            "session_id": SESSION_ID,
            "tool_use_result": {
                "isAsync": True,
                "status": "async_launched",
                "agentId": CHILD_AGENT_ID,
                "description": "spawn probe",
                "resolvedModel": "claude-sonnet-5",
                "prompt": "probe",
                "outputFile": "/tmp/agent-output.txt",
            },
        }
    )


def _completed_tool_result(agent_type: str = AGENT_TYPE) -> str:
    return _line(
        {
            "type": "user",
            "session_id": SESSION_ID,
            "tool_use_result": {
                "status": "completed",
                "agentId": CHILD_AGENT_ID,
                "agentType": agent_type,
                "content": "done",
                "totalDurationMs": 1234,
            },
        }
    )


def _hook_event(hook_event: str, *, hook_name: str, stdout: str | None = None) -> str:
    payload: dict = {
        "type": "system",
        "subtype": "hook_response" if stdout is not None else "hook_started",
        "hook_event": hook_event,
        "hook_name": hook_name,
        "session_id": SESSION_ID,
    }
    if stdout is not None:
        payload.update({"stdout": stdout, "output": stdout, "exit_code": 0, "outcome": "success"})
    return _line(payload)


def _official_hook_payload(agent_type: str = AGENT_TYPE) -> str:
    return json.dumps(
        {
            "session_id": SESSION_ID,
            "hook_event_name": "SubagentStart",
            "agent_id": CHILD_AGENT_ID,
            "agent_type": agent_type,
            "agent_transcript_path": "/tmp/child.jsonl",
        }
    )


def _result_event() -> str:
    return _line({"type": "result", "subtype": "success", "session_id": SESSION_ID})


# --- AC1: async envelope + hook payload -> hook-channel agent type ----------


def test_async_envelope_recovers_agent_type_from_hook_payload() -> None:
    stdout = "\n".join(
        [
            _system_init(),
            _assistant_agent_dispatch(),
            _hook_event("SubagentStart", hook_name="SubagentStart", stdout=_official_hook_payload()),
            _async_launched_tool_result(),
            _result_event(),
        ]
    )
    assert smoke.extract_claude_child_agent_type(stdout) == AGENT_TYPE


def test_async_envelope_records_hook_payload_provenance() -> None:
    stdout = "\n".join(
        [
            _assistant_agent_dispatch(),
            _hook_event("SubagentStart", hook_name="SubagentStart", stdout=_official_hook_payload()),
            _async_launched_tool_result(),
        ]
    )
    agent_type, source = smoke.extract_claude_child_agent_type_with_source(stdout)
    assert (agent_type, source) == (AGENT_TYPE, smoke.AGENT_TYPE_SOURCE_HOOK_PAYLOAD)


def test_hook_payload_prefixed_by_logger_text_is_still_parsed() -> None:
    """A no-op logger hook typically prefixes the echoed payload so Claude Code
    does not read it as hook control JSON."""
    stdout = "\n".join(
        [
            _assistant_agent_dispatch(),
            _hook_event(
                "SubagentStart",
                hook_name="SubagentStart",
                stdout=f"SPAWN_OBS_HOOK_PAYLOAD {_official_hook_payload()}",
            ),
            _async_launched_tool_result(),
        ]
    )
    assert smoke.extract_claude_child_agent_type(stdout) == AGENT_TYPE


def test_hook_identity_exposes_agent_id_matching_the_tool_result() -> None:
    stdout = "\n".join(
        [
            _hook_event("SubagentStart", hook_name="SubagentStart", stdout=_official_hook_payload()),
            _async_launched_tool_result(),
        ]
    )
    identity = smoke.extract_claude_hook_agent_identity(stdout)
    assert identity["agent_id"] == CHILD_AGENT_ID
    assert identity["agent_type"] == AGENT_TYPE
    assert smoke._extract_claude_child_session_id_from_stream(stdout) == identity["agent_id"]


# --- AC2: tool_use_result keeps precedence ---------------------------------


def test_tool_result_agent_type_takes_precedence_over_hook_channel() -> None:
    """Non-regression: when the synchronous envelope carries ``agentType``,
    that value wins even if the hook channel disagrees."""
    stdout = "\n".join(
        [
            _assistant_agent_dispatch(),
            _hook_event(
                "SubagentStart",
                hook_name="SubagentStart:web-researcher",
                stdout=_official_hook_payload("web-researcher"),
            ),
            _completed_tool_result("codebase-investigator"),
        ]
    )
    agent_type, source = smoke.extract_claude_child_agent_type_with_source(stdout)
    assert agent_type == "codebase-investigator"
    assert source == smoke.AGENT_TYPE_SOURCE_TOOL_RESULT


def test_completed_envelope_without_hooks_still_works() -> None:
    stdout = "\n".join([_assistant_agent_dispatch(), _completed_tool_result(), _result_event()])
    assert smoke.extract_claude_child_agent_type(stdout) == AGENT_TYPE


# --- AC3: hook_name suffix fallback ----------------------------------------


def test_hook_name_suffix_is_used_when_no_payload_is_echoed() -> None:
    """The repo-tracked settings.json hooks do not echo their stdin payload,
    so the only in-stream hook evidence is the ``<HookEvent>:<agent_type>``
    label the runtime puts on ``hook_name``."""
    stdout = "\n".join(
        [
            _assistant_agent_dispatch(),
            _hook_event("SubagentStart", hook_name=f"SubagentStart:{AGENT_TYPE}"),
            _async_launched_tool_result(),
        ]
    )
    agent_type, source = smoke.extract_claude_child_agent_type_with_source(stdout)
    assert agent_type == AGENT_TYPE
    assert source == smoke.AGENT_TYPE_SOURCE_HOOK_NAME


def test_bare_hook_name_without_suffix_yields_no_agent_type() -> None:
    """``SubagentStop`` is emitted without the agent-type suffix; a bare hook
    name must not be mistaken for identity evidence."""
    stdout = "\n".join(
        [
            _assistant_agent_dispatch(),
            _hook_event("SubagentStop", hook_name="SubagentStop"),
            _async_launched_tool_result(),
        ]
    )
    assert smoke.extract_claude_child_agent_type(stdout) is None


# --- AC4: fail-closed -------------------------------------------------------


def test_no_identity_evidence_anywhere_returns_none() -> None:
    stdout = "\n".join([_system_init(), _assistant_agent_dispatch(), _async_launched_tool_result(), _result_event()])
    agent_type, source = smoke.extract_claude_child_agent_type_with_source(stdout)
    assert agent_type is None
    assert source is None


def test_empty_and_malformed_stdout_return_none() -> None:
    for stdout in ("", "   ", "not json at all", "{", '{"type": "user"}'):
        assert smoke.extract_claude_child_agent_type(stdout) is None
        assert smoke.extract_claude_hook_agent_identity(stdout)["agent_type"] is None


def test_hook_stdout_that_is_not_json_is_ignored() -> None:
    stdout = "\n".join(
        [
            _assistant_agent_dispatch(),
            _hook_event("SubagentStart", hook_name="SubagentStart", stdout="ok\n"),
            _async_launched_tool_result(),
        ]
    )
    assert smoke.extract_claude_child_agent_type(stdout) is None


def test_hook_payload_without_agent_type_does_not_invent_one() -> None:
    payload = json.dumps({"session_id": SESSION_ID, "agent_id": CHILD_AGENT_ID})
    stdout = "\n".join(
        [
            _assistant_agent_dispatch(),
            _hook_event("SubagentStart", hook_name="SubagentStart", stdout=payload),
            _async_launched_tool_result(),
        ]
    )
    identity = smoke.extract_claude_hook_agent_identity(stdout)
    assert identity["agent_id"] == CHILD_AGENT_ID
    assert identity["agent_type"] is None
    assert smoke.extract_claude_child_agent_type(stdout) is None


def test_non_lifecycle_hook_events_are_not_treated_as_identity_evidence() -> None:
    """A ``PreToolUse`` hook that happens to echo an agent_type-shaped payload
    must not be read as SubAgent identity evidence."""
    stdout = "\n".join(
        [
            _assistant_agent_dispatch(),
            _hook_event("PreToolUse", hook_name="PreToolUse:Agent", stdout=_official_hook_payload()),
            _async_launched_tool_result(),
        ]
    )
    assert smoke.extract_claude_child_agent_type(stdout) is None


def test_requested_agent_type_is_never_substituted() -> None:
    """The extractor takes no requested-type argument at all, so it is
    structurally incapable of echoing the caller's expectation back."""
    import inspect

    signature = inspect.signature(smoke.extract_claude_child_agent_type)
    assert list(signature.parameters) == ["stdout"]
    signature_with_source = inspect.signature(smoke.extract_claude_child_agent_type_with_source)
    assert list(signature_with_source.parameters) == ["stdout"]


# --- AC5: parent_session_id short circuit removed ---------------------------


def test_child_session_id_found_without_parent_session_id() -> None:
    stdout = "\n".join([_assistant_agent_dispatch(), _async_launched_tool_result()])
    assert smoke.extract_claude_child_session_id(None, "/tmp", stdout) == CHILD_AGENT_ID
    assert smoke.extract_claude_child_session_id("", "/tmp", stdout) == CHILD_AGENT_ID


def test_child_session_id_still_none_without_any_stdout_evidence() -> None:
    assert smoke.extract_claude_child_session_id(None, "/tmp", "") is None
    assert smoke.extract_claude_child_session_id(None, "/tmp", None) is None


def test_child_session_id_prefers_stream_over_file_fallback() -> None:
    stdout = "\n".join([_completed_tool_result()])
    assert smoke.extract_claude_child_session_id(SESSION_ID, "/tmp", stdout) == CHILD_AGENT_ID


# --- AC7: launch mode classification ---------------------------------------


def test_launch_mode_async_launched() -> None:
    stdout = "\n".join([_assistant_agent_dispatch(), _async_launched_tool_result()])
    assert smoke.classify_claude_spawn_launch_mode(stdout) == smoke.SPAWN_LAUNCH_MODE_ASYNC


def test_launch_mode_completed() -> None:
    stdout = "\n".join([_assistant_agent_dispatch(), _completed_tool_result()])
    assert smoke.classify_claude_spawn_launch_mode(stdout) == smoke.SPAWN_LAUNCH_MODE_COMPLETED


def test_launch_mode_unknown_without_tool_result() -> None:
    stdout = "\n".join([_system_init(), _assistant_agent_dispatch(), _result_event()])
    assert smoke.classify_claude_spawn_launch_mode(stdout) is smoke.SPAWN_LAUNCH_MODE_UNKNOWN


def test_launch_mode_source_labels_are_distinct() -> None:
    labels = {
        smoke.AGENT_TYPE_SOURCE_TOOL_RESULT,
        smoke.AGENT_TYPE_SOURCE_HOOK_PAYLOAD,
        smoke.AGENT_TYPE_SOURCE_HOOK_NAME,
    }
    assert len(labels) == 3


# --- AC6/AC7 end-to-end: the async run is no longer spawn_not_observed ------


def _native_spawn_event_observed(stdout: str, requested_agent_type: str) -> bool:
    """Reproduce the production conjunction in
    ``run_worktree_agent_runtime_smoke.py`` for a given captured stream."""
    parent_session_id = smoke.extract_claude_parent_session_id(stdout)
    child_session_id = smoke.extract_claude_child_session_id(parent_session_id, "/tmp", stdout)
    observed = smoke.extract_claude_child_agent_type(stdout)
    identity_verified = observed is not None and observed == requested_agent_type
    return bool(
        parent_session_id
        and child_session_id
        and parent_session_id != child_session_id
        and identity_verified
    )


def test_async_launch_with_hook_evidence_is_now_observed_as_a_spawn() -> None:
    """The exact regression from Issue #2013: this stream previously produced
    ``native_spawn_event_observed == False`` despite a fully observable
    spawn."""
    stdout = "\n".join(
        [
            _system_init(),
            _assistant_agent_dispatch(),
            _hook_event("SubagentStart", hook_name=f"SubagentStart:{AGENT_TYPE}"),
            _async_launched_tool_result(),
            _hook_event("SubagentStop", hook_name="SubagentStop"),
            _result_event(),
        ]
    )
    assert _native_spawn_event_observed(stdout, AGENT_TYPE) is True


def test_agent_type_mismatch_still_fails_closed() -> None:
    """A child of the wrong type must NOT satisfy the evidence bar, on either
    channel -- the bar is not lowered by Issue #2021."""
    stdout = "\n".join(
        [
            _system_init(),
            _assistant_agent_dispatch(),
            _hook_event("SubagentStart", hook_name="SubagentStart:general-purpose"),
            _async_launched_tool_result(),
            _result_event(),
        ]
    )
    assert _native_spawn_event_observed(stdout, AGENT_TYPE) is False


def test_absent_identity_evidence_still_fails_closed_end_to_end() -> None:
    stdout = "\n".join(
        [_system_init(), _assistant_agent_dispatch(), _async_launched_tool_result(), _result_event()]
    )
    assert _native_spawn_event_observed(stdout, AGENT_TYPE) is False


@pytest.mark.parametrize(
    "attribute",
    [
        "extract_claude_hook_agent_identity",
        "extract_claude_child_agent_type_with_source",
        "classify_claude_spawn_launch_mode",
        "AGENT_TYPE_SOURCE_TOOL_RESULT",
        "AGENT_TYPE_SOURCE_HOOK_PAYLOAD",
        "AGENT_TYPE_SOURCE_HOOK_NAME",
        "SPAWN_LAUNCH_MODE_ASYNC",
        "SPAWN_LAUNCH_MODE_COMPLETED",
    ],
)
def test_public_surface_exists(attribute: str) -> None:
    assert hasattr(smoke, attribute), f"missing public surface: {attribute}"


def test_summary_records_source_and_launch_mode_keys() -> None:
    """AC6/AC7 wiring: the runner must persist both new evidence fields into
    the schema summary it writes."""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert 'schema_summary["child_agent_type_source"]' in source
    assert 'schema_summary["child_spawn_launch_mode"]' in source

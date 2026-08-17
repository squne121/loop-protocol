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
``test_child_spawn_completion_evidence.py`` (Issue #2015 AC11). Tests that
need a durable transcript ``agent_transcript_path`` to actually exist on
disk (AC11's file-existence/content-verification requirement) use pytest's
``tmp_path`` fixture rather than a hardcoded non-existent path -- a hook
payload that merely CLAIMS a transcript path is not, on its own, honest
evidence of one (that is the exact gap AC11 closes).
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
    last_assistant_message: str | None = None,
    session_id: str = SESSION_ID,
    prompt_id: str | None = None,
    stop_hook_active: bool | None = None,
) -> str:
    payload = {
        "session_id": session_id,
        "hook_event_name": hook_event_name,
        "agent_id": agent_id,
        "agent_type": agent_type,
    }
    if agent_transcript_path is not None:
        payload["agent_transcript_path"] = agent_transcript_path
    if last_assistant_message is not None:
        payload["last_assistant_message"] = last_assistant_message
    if prompt_id is not None:
        payload["prompt_id"] = prompt_id
    if stop_hook_active is not None:
        payload["stop_hook_active"] = stop_hook_active
    return json.dumps(payload)


def _hook_event(
    hook_event: str,
    *,
    agent_id: str,
    agent_type: str = AGENT_TYPE,
    agent_transcript_path: str | None = None,
    last_assistant_message: str | None = None,
    session_id: str = SESSION_ID,
    prompt_id: str | None = None,
    stop_hook_active: bool | None = None,
    outer_session_id: str | None = None,
    stdout_override: str | None = None,
    output_override: str | None = None,
) -> str:
    embedded = _hook_payload(
        agent_id,
        hook_event,
        agent_type=agent_type,
        agent_transcript_path=agent_transcript_path,
        last_assistant_message=last_assistant_message,
        session_id=session_id,
        prompt_id=prompt_id,
        stop_hook_active=stop_hook_active,
    )
    return _line(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": hook_event,
            "hook_name": hook_event,
            "session_id": outer_session_id if outer_session_id is not None else session_id,
            "stdout": stdout_override if stdout_override is not None else embedded,
            "output": output_override if output_override is not None else embedded,
        }
    )


def _agent_tool_use_event(
    tool_use_id: str = AGENT_TOOL_USE_ID,
    *,
    session_id: str = SESSION_ID,
    prompt_id: str | None = None,
) -> str:
    payload = {
        "type": "assistant",
        "session_id": session_id,
        "message": {
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": "Agent", "input": {}},
            ]
        },
    }
    if prompt_id is not None:
        payload["prompt_id"] = prompt_id
    return _line(payload)


def _agent_tool_result_event(
    agent_id: str,
    tool_use_id: str = AGENT_TOOL_USE_ID,
    *,
    session_id: str = SESSION_ID,
    prompt_id: str | None = None,
    status: str = "completed",
    is_error: bool = False,
) -> str:
    payload = {
        "type": "user",
        "session_id": session_id,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "done",
                    "is_error": is_error,
                },
            ]
        },
        "tool_use_result": {"status": status, "agentId": agent_id, "agentType": AGENT_TYPE},
    }
    if prompt_id is not None:
        payload["prompt_id"] = prompt_id
    return _line(payload)


# ---------------------------------------------------------------------------
# AC1/AC2/AC3: function + field presence (also asserted structurally below).
# ---------------------------------------------------------------------------


def test_function_exists_and_returns_required_fields() -> None:
    verdict = smoke.subagent_causal_evidence_verdict("")
    assert "causal_evidence_source" in verdict
    assert "tool_invocation_id_correlated" in verdict
    assert "agent_id" in verdict
    assert "agent_transcript_path" in verdict
    assert "agent_transcript_verified" in verdict
    assert "marker_provenance_verified" in verdict


# ---------------------------------------------------------------------------
# Positive case: correlated Start/Stop pair + a REAL, non-empty transcript
# file + tool invocation correlation + marker recovered from the child's own
# transcript content -> hook_id_correlated, tool_invocation_id_correlated
# True, agent_transcript_verified True, marker_provenance_verified True.
# ---------------------------------------------------------------------------


def test_given_correlated_start_stop_with_transcript_when_verdict_computed_then_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "assistant", "message": {"content": COMPLETION_MARKER}}) + "\n",
        encoding="utf-8",
    )
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["agent_id"] == CHILD_AGENT_ID
    assert verdict["agent_transcript_path"] == str(transcript_path)
    assert verdict["agent_transcript_verified"] is True
    assert verdict["marker_provenance_verified"] is True
    assert verdict["tool_invocation_id_correlated"] is True
    assert verdict["subagent_start_observed"] is True
    assert verdict["subagent_stop_observed"] is True


def test_given_correlated_start_stop_no_expected_markers_when_verdict_computed_then_hook_id_correlated(
    tmp_path,
) -> None:
    # When the caller does not request any expected marker at all,
    # marker_provenance_verified makes no claim and defaults True -- it
    # must not itself block promotion.
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text("unrelated transcript content\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["marker_provenance_verified"] is True


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
    assert verdict["agent_transcript_verified"] is False


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
    assert verdict["agent_transcript_verified"] is False


# ---------------------------------------------------------------------------
# AC3 (strengthened): tool_invocation_id_correlated must fail closed to
# False when the Agent tool_use/tool_result envelope is absent, AND that
# False value must now, by itself, be sufficient to keep
# causal_evidence_source OUT of hook_id_correlated -- even when the hook
# Start/Stop pair and a genuinely real, verified transcript are both
# present. Prior to the AC3 strengthening this field was computed but
# never consulted by the promotion decision; these tests pin the new,
# effective gate.
# ---------------------------------------------------------------------------


def test_given_hook_correlated_but_no_agent_tool_result_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    # Deliberately gives this Stop event a REAL, non-empty, readable
    # transcript file (unlike the raw hardcoded TRANSCRIPT_PATH used
    # elsewhere in this module, which does not exist on disk) so that this
    # test isolates exactly one failure cause: the missing Agent
    # tool_use/tool_result correlation, not an AC11 transcript-verification
    # failure that would otherwise also block promotion.
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text("real transcript content, but no tool_use/tool_result pair\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["tool_invocation_id_correlated"] is False
    assert verdict["agent_transcript_verified"] is True
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_agent_tool_result_agent_id_mismatch_when_verdict_computed_then_tool_invocation_not_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text("real transcript content\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _agent_tool_result_event(OTHER_AGENT_ID),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["tool_invocation_id_correlated"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


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


def test_extract_claude_hook_lifecycle_events_recovers_last_assistant_message() -> None:
    stdout = _hook_event(
        "SubagentStop",
        agent_id=CHILD_AGENT_ID,
        agent_transcript_path=TRANSCRIPT_PATH,
        last_assistant_message=COMPLETION_MARKER,
    )
    events = smoke.extract_claude_hook_lifecycle_events(stdout)
    assert len(events) == 1
    assert events[0]["last_assistant_message"] == COMPLETION_MARKER


# ---------------------------------------------------------------------------
# AC11: agent_transcript_path realism (existence/readability/non-emptiness)
# and marker/artifact content-provenance verification.
# ---------------------------------------------------------------------------


def test_given_agent_transcript_path_file_does_not_exist_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    missing_path = tmp_path / "does-not-exist.jsonl"
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(missing_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["agent_transcript_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_agent_transcript_path_file_is_empty_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    empty_path = tmp_path / "empty-transcript.jsonl"
    empty_path.write_text("", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(empty_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["agent_transcript_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


def test_given_whitespace_only_transcript_file_when_verdict_computed_then_not_verified(tmp_path) -> None:
    whitespace_path = tmp_path / "whitespace-only-transcript.jsonl"
    whitespace_path.write_text("   \n\t\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(whitespace_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout)
    assert verdict["agent_transcript_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


def test_given_expected_marker_absent_from_real_transcript_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    # The transcript file is real, non-empty, and readable, and the hook
    # chain + tool-invocation correlation are all fully valid -- but the
    # expected marker text never appears in that transcript's own content,
    # and no last_assistant_message field was supplied either. This must
    # still fail closed: a genuinely real but content-irrelevant transcript
    # is not evidence the EXPECTED artifact came from this child.
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text("the child did unrelated work and said nothing matching\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_transcript_verified"] is True
    assert verdict["tool_invocation_id_correlated"] is True
    assert verdict["marker_provenance_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_expected_marker_recovered_only_from_last_assistant_message_when_verdict_computed_then_hook_id_correlated(
    tmp_path,
) -> None:
    # The child's transcript FILE does not itself contain the marker (e.g.
    # it only logged tool activity, not the final response text), but the
    # SubagentStop hook payload's own last_assistant_message field -- still
    # a CHILD-scoped field, not the parent's -- does. This is a valid
    # alternate provenance source per AC11's contract text.
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text("tool activity log only, no final response text\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
                last_assistant_message=f"child finished: {COMPLETION_MARKER}",
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["marker_provenance_verified"] is True
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


# ---------------------------------------------------------------------------
# AC12: negative control -- an UNRELATED SubAgent completes cleanly (real
# agent_id correlation, real transcript, real tool-invocation correlation),
# but the expected marker only ever appears in the PARENT model's own final
# response text (i.e. plain assistant prose in the combined stdout stream,
# never inside the unrelated child's transcript or last_assistant_message).
# This must not be promoted to hook_id_correlated -- the marker's
# provenance is the parent, not the (unrelated) correlated child.
# ---------------------------------------------------------------------------


def test_given_unrelated_subagent_completes_and_parent_emits_marker_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    unrelated_transcript_path = tmp_path / "unrelated-child-transcript.jsonl"
    unrelated_transcript_path.write_text(
        "the unrelated child did its own unrelated work\n", encoding="utf-8"
    )
    parent_final_message_event = _line(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": f"Parent's own final response, unrelated to any child: {COMPLETION_MARKER}",
                    }
                ]
            },
        }
    )
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=OTHER_AGENT_ID),
            _agent_tool_result_event(OTHER_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=OTHER_AGENT_ID,
                agent_transcript_path=str(unrelated_transcript_path),
            ),
            parent_final_message_event,
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    # Sanity: the unrelated child's own hook/tool-invocation correlation IS
    # genuinely valid on its own terms -- this negative control specifically
    # isolates the marker-provenance requirement, not a weaker hook-chain
    # failure that would trivially explain the non-promotion.
    assert verdict["agent_id"] == OTHER_AGENT_ID
    assert verdict["agent_transcript_verified"] is True
    assert verdict["tool_invocation_id_correlated"] is True
    assert verdict["marker_provenance_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


# ---------------------------------------------------------------------------
# PR #2220 OWNER REQUEST_CHANGES P0-2
# (https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514):
# marker_provenance_verified must require EVERY expected marker to be
# recovered from a CHILD-scoped source (the correlated SubagentStop's own
# last_assistant_message, or an assistant-authored record of the child's
# own transcript file) -- never "at least one" (the pre-fix-delta any()
# check), and never a user-prompt/tool-input record inside that same
# transcript file.
# ---------------------------------------------------------------------------


def _assistant_transcript_line(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def _user_transcript_line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": text}]}})


def _tool_use_transcript_line(command_text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": command_text}}
                ]
            },
        }
    )


MARKER_1 = "MARKER_ONE_CHILD"
MARKER_2 = "MARKER_TWO_PARENT_ONLY"


def _fully_correlated_stdout_lines(
    transcript_path: str,
    *,
    agent_id: str = CHILD_AGENT_ID,
    last_assistant_message: str | None = None,
) -> list[str]:
    return [
        _agent_tool_use_event(),
        _hook_event("SubagentStart", agent_id=agent_id),
        _agent_tool_result_event(agent_id),
        _hook_event(
            "SubagentStop",
            agent_id=agent_id,
            agent_transcript_path=transcript_path,
            last_assistant_message=last_assistant_message,
        ),
    ]


def test_given_marker_two_only_in_parent_final_message_when_verdict_computed_then_not_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(MARKER_1) + "\n", encoding="utf-8")
    parent_message = _line(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {"content": [{"type": "text", "text": f"parent says {MARKER_2}"}]},
        }
    )
    stdout = "\n".join(
        _fully_correlated_stdout_lines(str(transcript_path)) + [parent_message]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [MARKER_1, MARKER_2])
    assert verdict["agent_transcript_verified"] is True
    assert verdict["tool_invocation_id_correlated"] is True
    assert verdict["marker_provenance_verified"] is False
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_marker_only_in_child_user_prompt_record_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_user_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(_fully_correlated_stdout_lines(str(transcript_path)))
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_transcript_verified"] is True
    assert verdict["tool_invocation_id_correlated"] is True
    assert verdict["marker_provenance_verified"] is False
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_marker_only_in_child_tool_input_record_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(
        _tool_use_transcript_line(f"echo {COMPLETION_MARKER}") + "\n", encoding="utf-8"
    )
    stdout = "\n".join(_fully_correlated_stdout_lines(str(transcript_path)))
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_transcript_verified"] is True
    assert verdict["marker_provenance_verified"] is False
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_marker_only_in_child_assistant_final_message_when_verdict_computed_then_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(_fully_correlated_stdout_lines(str(transcript_path)))
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["marker_provenance_verified"] is True
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


def test_given_multiple_expected_markers_all_in_child_assistant_output_when_verdict_computed_then_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(MARKER_1) + "\n", encoding="utf-8")
    stdout = "\n".join(
        _fully_correlated_stdout_lines(
            str(transcript_path), last_assistant_message=f"done: {MARKER_2}"
        )
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [MARKER_1, MARKER_2])
    assert verdict["marker_provenance_verified"] is True
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


def test_given_parent_and_child_emit_same_marker_when_verdict_computed_then_child_provenance_independently_confirmed(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    parent_message = _line(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {"content": [{"type": "text", "text": f"parent also says {COMPLETION_MARKER}"}]},
        }
    )
    stdout = "\n".join(
        _fully_correlated_stdout_lines(str(transcript_path)) + [parent_message]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["marker_provenance_verified"] is True
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


# ---------------------------------------------------------------------------
# PR #2220 OWNER REQUEST_CHANGES P0-3
# (https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514):
# session_id / prompt_id / stream ordering / agent_type / tool-invocation
# session-prompt scoping / terminal-success must all participate in
# correlation -- a bare agent_id set-membership match is not enough.
# ---------------------------------------------------------------------------


def test_given_start_and_stop_in_different_sessions_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID, session_id="session-A"),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
                session_id="session-B",
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_id"] is None
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_stop_stream_index_before_start_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_id"] is None
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_start_and_stop_in_different_prompts_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID, prompt_id="prompt-1"),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
                prompt_id="prompt-2",
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_id"] is None
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_start_and_stop_agent_type_mismatch_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID, agent_type="type-a"),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_type="type-b",
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_id"] is None
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_agent_tool_call_in_different_session_when_verdict_computed_then_tool_invocation_not_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(session_id="other-session"),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID, session_id="other-session"),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["tool_invocation_id_correlated"] is False
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_agent_tool_call_in_different_prompt_when_verdict_computed_then_tool_invocation_not_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(prompt_id="other-prompt"),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID, prompt_id="prompt-1"),
            _agent_tool_result_event(CHILD_AGENT_ID, prompt_id="other-prompt"),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
                prompt_id="prompt-1",
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["tool_invocation_id_correlated"] is False
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_agent_tool_result_status_error_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID, status="error", is_error=True),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["tool_invocation_id_correlated"] is True
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_agent_tool_result_status_async_not_terminal_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event("SubagentStart", agent_id=CHILD_AGENT_ID),
            _agent_tool_result_event(CHILD_AGENT_ID, status="async_launched"),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["tool_invocation_id_correlated"] is True
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


# ---------------------------------------------------------------------------
# PR #2220 OWNER REQUEST_CHANGES P1-1
# (https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514):
# a durable child transcript must not merely exist -- it must not be a
# symlink, must be within any caller-supplied allowed roots, must be
# consistent with a caller-supplied run time window, and must not exceed a
# bounded size.
# ---------------------------------------------------------------------------


def test_given_transcript_path_is_symlink_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    real_target = tmp_path / "real-transcript.jsonl"
    real_target.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    symlink_path = tmp_path / "symlinked-transcript.jsonl"
    symlink_path.symlink_to(real_target)
    stdout = "\n".join(_fully_correlated_stdout_lines(str(symlink_path)))
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_transcript_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


def test_given_read_transcript_content_symlink_path_when_read_then_rejected_is_symlink(tmp_path) -> None:
    real_target = tmp_path / "real-transcript.jsonl"
    real_target.write_text("content\n", encoding="utf-8")
    symlink_path = tmp_path / "symlinked-transcript.jsonl"
    symlink_path.symlink_to(real_target)
    result = smoke._read_claude_agent_transcript_content(str(symlink_path))
    assert result["content"] is None
    assert result["rejected_reason"] == "is_symlink"


def test_given_transcript_path_outside_allowed_roots_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "outside" / "child-transcript.jsonl"
    transcript_path.parent.mkdir()
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    allowed_root = tmp_path / "allowed-worktree"
    allowed_root.mkdir()
    stdout = "\n".join(_fully_correlated_stdout_lines(str(transcript_path)))
    verdict = smoke.subagent_causal_evidence_verdict(
        stdout, [COMPLETION_MARKER], transcript_allowed_roots=[str(allowed_root)]
    )
    assert verdict["agent_transcript_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


def test_given_read_transcript_content_within_allowed_roots_when_read_then_content_returned(tmp_path) -> None:
    allowed_root = tmp_path / "allowed-worktree"
    allowed_root.mkdir()
    transcript_path = allowed_root / "child-transcript.jsonl"
    transcript_path.write_text("real content\n", encoding="utf-8")
    result = smoke._read_claude_agent_transcript_content(
        str(transcript_path), allowed_roots=[str(allowed_root)]
    )
    assert result["content"] == "real content\n"
    assert result["rejected_reason"] is None
    assert result["sha256"] is not None


def test_given_transcript_mtime_outside_run_window_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    stdout = "\n".join(_fully_correlated_stdout_lines(str(transcript_path)))
    far_future_start = 4102444800.0  # 2100-01-01T00:00:00Z
    far_future_end = 4102448400.0
    verdict = smoke.subagent_causal_evidence_verdict(
        stdout,
        [COMPLETION_MARKER],
        run_start_time=far_future_start,
        run_end_time=far_future_end,
    )
    assert verdict["agent_transcript_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


def test_given_read_transcript_content_oversized_when_read_then_rejected_oversized(tmp_path) -> None:
    transcript_path = tmp_path / "big-transcript.jsonl"
    transcript_path.write_text("x" * 1000, encoding="utf-8")
    result = smoke._read_claude_agent_transcript_content(str(transcript_path), max_bytes=100)
    assert result["content"] is None
    assert result["rejected_reason"] == "oversized"


# ---------------------------------------------------------------------------
# PR #2220 OWNER REQUEST_CHANGES P1-2
# (https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514):
# a stdout/output contradiction on the same hook event, and an ambiguous
# multi-candidate chain, must both fail closed rather than silently prefer
# one channel/candidate.
# ---------------------------------------------------------------------------


def test_given_stdout_output_contradiction_on_hook_event_when_extracted_then_marked_contradictory() -> None:
    conflicting_stdout = _hook_payload(CHILD_AGENT_ID, "SubagentStart")
    conflicting_output = _hook_payload(OTHER_AGENT_ID, "SubagentStart")
    stdout = _hook_event(
        "SubagentStart",
        agent_id=CHILD_AGENT_ID,
        stdout_override=conflicting_stdout,
        output_override=conflicting_output,
    )
    events = smoke.extract_claude_hook_lifecycle_events(stdout)
    assert len(events) == 1
    assert events[0]["contradictory"] is True
    assert events[0]["agent_id"] is None


def test_given_stdout_output_contradiction_when_verdict_computed_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    transcript_path = tmp_path / "child-transcript.jsonl"
    transcript_path.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    conflicting_stdout = _hook_payload(
        CHILD_AGENT_ID, "SubagentStart", agent_transcript_path=str(transcript_path)
    )
    conflicting_output = _hook_payload(
        OTHER_AGENT_ID, "SubagentStart", agent_transcript_path=str(transcript_path)
    )
    stdout = "\n".join(
        [
            _agent_tool_use_event(),
            _hook_event(
                "SubagentStart",
                agent_id=CHILD_AGENT_ID,
                stdout_override=conflicting_stdout,
                output_override=conflicting_output,
            ),
            _agent_tool_result_event(CHILD_AGENT_ID),
            _hook_event(
                "SubagentStop",
                agent_id=CHILD_AGENT_ID,
                agent_transcript_path=str(transcript_path),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_id"] is None
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_multiple_fully_qualifying_candidate_chains_when_verdict_computed_then_ambiguous_not_hook_id_correlated(
    tmp_path,
) -> None:
    first_transcript = tmp_path / "first-transcript.jsonl"
    first_transcript.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    second_transcript = tmp_path / "second-transcript.jsonl"
    second_transcript.write_text(_assistant_transcript_line(COMPLETION_MARKER) + "\n", encoding="utf-8")
    second_agent_id = "b25c8f0673d997e52"
    second_tool_use_id = "toolu_02AgentInvocation"
    stdout = "\n".join(
        _fully_correlated_stdout_lines(str(first_transcript))
        + [
            _agent_tool_use_event(second_tool_use_id),
            _hook_event("SubagentStart", agent_id=second_agent_id),
            _agent_tool_result_event(second_agent_id, second_tool_use_id),
            _hook_event(
                "SubagentStop",
                agent_id=second_agent_id,
                agent_transcript_path=str(second_transcript),
            ),
        ]
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["causal_evidence_ambiguous_candidate_count"] == 2


# ---------------------------------------------------------------------------
# PR #2220 fix_delta (P1-1-vs-``last_assistant_message``-primacy):
# live-verified regression -- ``run_worktree_agent_runtime_smoke.py`` run
# against native Claude Code (``--runtime claude --mode structured``,
# run_id ``2e93d701786c``, tested_head ``28c16484``) genuinely observed
# ``subagent_start_observed``/``subagent_stop_observed`` True (matching
# ``agent_id``), ``tool_invocation_id_correlated`` True, and
# ``marker_provenance_verified`` True via ``last_assistant_message`` --
# yet the correlated Stop's ``agent_transcript_path`` pointed at a
# ``.jsonl`` file that was never actually written (the structured lane's
# ``--no-session-persistence`` mode instead wrote only a small
# ``agent-<id>.meta.json`` stub), so ``agent_transcript_verified`` was
# False and the pre-fix-delta code fell all the way through to
# ``causal_evidence_source: 'no_evidence'``, wrongly FAILing
# ``--require-subagent-causal-evidence`` despite genuine, hook-ID-
# correlated SubAgent evidence being present. Per OWNER's own P0-2
# comment, ``last_assistant_message`` is the correct PRIMARY provenance
# source and the transcript file is merely supplementary audit evidence,
# not a hard gate, once marker provenance is independently confirmed from
# ``last_assistant_message`` alone.
# ---------------------------------------------------------------------------


def test_given_last_assistant_message_satisfies_markers_transcript_missing_then_hook_id_correlated(
    tmp_path,
) -> None:
    # Regression fixture mirroring the live run_id 2e93d701786c evidence:
    # a genuine, fully-correlated Start/Stop/tool-invocation chain whose
    # SubagentStop payload DOES carry a synthetic ``agent_transcript_path``
    # (exactly the shape a real hook payload has), but no file was ever
    # written at that path -- the structured ``--no-session-persistence``
    # lane's actual observed behavior. ``last_assistant_message`` alone
    # already contains every expected marker.
    missing_transcript_path = tmp_path / "agent-a14b7e0673d997e52.jsonl"
    stdout = "\n".join(
        _fully_correlated_stdout_lines(
            str(missing_transcript_path),
            last_assistant_message=f"Subagent finished: {COMPLETION_MARKER}",
        )
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["subagent_start_observed"] is True
    assert verdict["subagent_stop_observed"] is True
    assert verdict["agent_id"] == CHILD_AGENT_ID
    assert verdict["tool_invocation_id_correlated"] is True
    assert verdict["agent_transcript_path"] == str(missing_transcript_path)
    # The audit-only P1-1 file-existence check is genuinely False here --
    # this fixture must NOT paper over that fact -- but it must not, by
    # itself, block promotion once last_assistant_message alone has
    # already confirmed marker provenance.
    assert verdict["agent_transcript_verified"] is False
    assert verdict["marker_provenance_verified"] is True
    assert verdict["marker_provenance_transcript_fallback_used"] is False
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED


def test_given_no_last_assistant_message_and_transcript_missing_then_not_hook_id_correlated(
    tmp_path,
) -> None:
    # Companion negative case for the fix above (task item 3): when
    # ``last_assistant_message`` is NOT available at all, marker
    # provenance must still fall back to the transcript file, and a
    # missing transcript file must still fail closed exactly as before --
    # the P1-1-vs-primacy relaxation must never apply when there is no
    # last_assistant_message to serve as the primary source.
    missing_transcript_path = tmp_path / "agent-a14b7e0673d997e52.jsonl"
    stdout = "\n".join(_fully_correlated_stdout_lines(str(missing_transcript_path)))
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_transcript_verified"] is False
    assert verdict["marker_provenance_verified"] is False
    assert verdict["marker_provenance_transcript_fallback_used"] is True
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_last_assistant_message_partial_markers_and_transcript_missing_then_not_correlated(
    tmp_path,
) -> None:
    # When last_assistant_message covers only SOME of the expected
    # markers, the fast path must NOT apply (it requires ALL expected
    # markers to already be satisfied from last_assistant_message alone) --
    # provenance falls back to the (here, missing) transcript file, and
    # must still fail closed even though last_assistant_message contains
    # one of the two markers.
    missing_transcript_path = tmp_path / "agent-a14b7e0673d997e52.jsonl"
    stdout = "\n".join(
        _fully_correlated_stdout_lines(
            str(missing_transcript_path),
            last_assistant_message=f"done: {MARKER_1}",
        )
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [MARKER_1, MARKER_2])
    assert verdict["agent_transcript_verified"] is False
    assert verdict["marker_provenance_transcript_fallback_used"] is True
    assert verdict["marker_provenance_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE


def test_given_unrelated_subagent_message_has_marker_transcript_missing_then_not_correlated(
    tmp_path,
) -> None:
    # AC12 negative-control companion: even with the P1-1-vs-primacy
    # relaxation, an UNRELATED SubAgent's own last_assistant_message must
    # still independently confirm the parent's expected marker on its own
    # scoped terms -- this is not a weakening of AC12, it is the same
    # marker-provenance check, just exercised via the
    # last_assistant_message fast path instead of the transcript-fallback
    # path. Included to pin that the relaxation is scoped to the
    # transcript-verification gate only, never to which child's own text
    # is searched.
    missing_transcript_path = tmp_path / "unrelated-agent-transcript.jsonl"
    stdout = "\n".join(
        _fully_correlated_stdout_lines(
            str(missing_transcript_path),
            agent_id=OTHER_AGENT_ID,
            last_assistant_message="the unrelated child said something else entirely",
        )
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [COMPLETION_MARKER])
    assert verdict["agent_id"] == OTHER_AGENT_ID
    assert verdict["marker_provenance_verified"] is False
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["causal_evidence_source"] == smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE

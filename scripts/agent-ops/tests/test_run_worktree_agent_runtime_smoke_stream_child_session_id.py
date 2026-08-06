"""Regression test for Issue #1886 AC7 fix-delta (iteration 6): the Claude
Code child spawn session id must be derivable directly from the already-
captured ``stdout`` stream-json, without depending on a persisted session
transcript file. The structured lane always passes
``--no-session-persistence`` (see ``run_structured_claude``), so no such
transcript file is ever written -- the prior file-only lookup in
``extract_claude_child_session_id`` was therefore structurally unable to
ever return a value, making ``native_spawn_event_observed`` permanently
``False`` regardless of whether a real spawn happened.

This is a dedicated new test file (not appended to the pre-existing general
smoke suite), per Issue #1960's Current Validated Scope / Issue #1285 /
PR #1305 VC contract convention (also followed by
``test_run_worktree_agent_runtime_smoke_runtime_evidence.py``).

This test is fully hermetic: it feeds synthetic stream-json lines (shaped
exactly like a real, live-observed ``claude -p --output-format stream-json
--include-hook-events --no-session-persistence`` capture of a single
``Task``/``Agent`` tool_use) directly into the module's extraction
functions -- no real ``claude`` binary or network access involved.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke_stream_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _synthetic_stream_lines() -> list[dict]:
    """Shaped after a real captured stdout stream for a single Task/Agent
    tool_use under ``--no-session-persistence`` (no transcript file is ever
    written for this stream)."""
    parent_session_id = "parent-session-aaaa"
    child_agent_id = "a72066e6f732aa768"
    return [
        {"type": "system", "subtype": "init", "session_id": parent_session_id},
        {
            "type": "assistant",
            "session_id": parent_session_id,
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Agent", "input": {}},
                ]
            },
        },
        {
            "type": "user",
            "session_id": parent_session_id,
            "message": {
                "content": [
                    {
                        "tool_use_id": "toolu_x",
                        "type": "tool_result",
                        "content": [
                            {"type": "text", "text": "OK"},
                            {
                                "type": "text",
                                "text": (
                                    f"agentId: {child_agent_id} (use SendMessage with "
                                    f"to: '{child_agent_id}', summary: '...' to continue "
                                    "this agent)"
                                ),
                            },
                        ],
                    }
                ]
            },
            "tool_use_result": {
                "status": "completed",
                "agentId": child_agent_id,
                "agentType": "general-purpose",
            },
        },
        {"type": "result", "subtype": "success", "session_id": parent_session_id},
    ]


def test_extract_claude_child_session_id_from_stdout_without_transcript_file(tmp_path):
    """Primary path: ``tool_use_result.agentId`` on a ``type: "user"`` event
    is found directly in the captured stdout stream, with no dependency on
    any file under a (here, deliberately nonexistent) ``~/.claude/projects``
    directory -- proving the fix works precisely in the
    ``--no-session-persistence`` case this bug was about."""
    module = _load_module()
    stdout = "\n".join(json.dumps(line) for line in _synthetic_stream_lines())

    parent_session_id = module.extract_claude_parent_session_id(stdout)
    assert parent_session_id == "parent-session-aaaa"

    # cwd is an arbitrary nonexistent path -- the stream-based primary path
    # must succeed without ever touching the filesystem-based fallback.
    child_session_id = module.extract_claude_child_session_id(
        parent_session_id, str(tmp_path / "does-not-exist"), stdout
    )
    assert child_session_id == "a72066e6f732aa768"
    assert child_session_id != parent_session_id


def test_extract_claude_child_session_id_falls_back_to_text_block_regex(tmp_path):
    """Fallback within the stream path: if ``tool_use_result`` is absent but
    the human-readable ``agentId: <hex>`` text line is still present in a
    tool_result content block, it must still be recovered."""
    module = _load_module()
    lines = _synthetic_stream_lines()
    # Drop the structured tool_use_result field to exercise the text-block
    # regex fallback exclusively.
    for line in lines:
        line.pop("tool_use_result", None)
    stdout = "\n".join(json.dumps(line) for line in lines)

    parent_session_id = module.extract_claude_parent_session_id(stdout)
    child_session_id = module.extract_claude_child_session_id(
        parent_session_id, str(tmp_path / "does-not-exist"), stdout
    )
    assert child_session_id == "a72066e6f732aa768"


def test_extract_claude_child_session_id_returns_none_without_spawn_evidence():
    """Fail-closed: no Agent/Task tool_use in the stream -> ``None``, never a
    guess."""
    module = _load_module()
    stdout = "\n".join(
        json.dumps(line)
        for line in [
            {"type": "system", "subtype": "init", "session_id": "parent-only"},
            {"type": "result", "subtype": "success", "session_id": "parent-only"},
        ]
    )
    parent_session_id = module.extract_claude_parent_session_id(stdout)
    assert module.extract_claude_child_session_id(parent_session_id, "/nonexistent", stdout) is None

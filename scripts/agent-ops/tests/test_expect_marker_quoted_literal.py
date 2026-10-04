"""Issue #2923: ``--expect-marker`` literals containing a double quote.

In raw stream-json a ``"`` inside model text is serialised as ``\\"``, so the
``--expect-marker-source subagent`` ``expected_markers_missing`` check (which
searched the raw stdout/stderr) reported a quoted literal as missing even when
the child produced it, while ``marker_provenance_verified`` (which matches the
child's unescaped ``last_assistant_message``) already treated it as present.
These hermetic tests pin that both judgements now agree in ONE evidence file
(AC1), that a literal present only in prompt / tool input is never adopted as
provenance (AC3), and that a literal absent from the child's body stays missing
(AC4).  A fake ``claude`` (a Python script, so fixture JSON never has to
survive bash quoting) emits synthetic stream-json; no live Claude Code process
is spawned.  The runner is always invoked as a subprocess, so no runner module
is imported into this test session (no ``sys.modules`` collision).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"

SESSION_ID = "9846ca4d-0893-43dd-bcec-519360aa31fb"
CHILD_AGENT_ID = "a14b7e0673d997e52"
AGENT_TYPE = "test-runner"
TRANSCRIPT_PATH = "/nonexistent/agent-a14b7e0673d997e52.jsonl"
AGENT_TOOL_USE_ID = "toolu_01QuotedLiteral"

QUOTED_MARKER = 'quoted_marker_probe: "ok-2923"'
SECOND_QUOTED_MARKER = 'status_probe: "pass"'
UNQUOTED_MARKER = "UNQUOTED_MARKER_2923"


def _git(*args: str, cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env)


def _build_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)
    worktree = repo / ".claude" / "worktrees" / "issue-0000-fixture"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-fixture", cwd=repo)
    _git("worktree", "add", str(worktree), "worktree-fixture", cwd=repo)
    return repo, worktree


def _write_fake_claude(path: Path, stdout_lines: list[str]) -> None:
    script_lines = ["#!/usr/bin/env python3", "import sys", "sys.stdin.read()"]
    for line in stdout_lines:
        script_lines.append(f"print({line!r})")
    script_lines.append("sys.exit(0)")
    path.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _line(payload: dict) -> str:
    return json.dumps(payload)


def _hook_event(hook_event: str, *, last_assistant_message: str | None = None) -> str:
    embedded_payload = {
        "session_id": SESSION_ID,
        "hook_event_name": hook_event,
        "agent_id": CHILD_AGENT_ID,
        "agent_type": AGENT_TYPE,
    }
    if last_assistant_message is not None:
        embedded_payload["last_assistant_message"] = last_assistant_message
        embedded_payload["agent_transcript_path"] = TRANSCRIPT_PATH
    embedded = json.dumps(embedded_payload)
    return _line(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": hook_event,
            "hook_name": hook_event,
            "session_id": SESSION_ID,
            "stdout": embedded,
            "output": embedded,
        }
    )


def _agent_tool_use_event(prompt_text: str) -> str:
    return _line(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": AGENT_TOOL_USE_ID,
                        "name": "Agent",
                        "input": {"subagent_type": AGENT_TYPE, "prompt": prompt_text},
                    }
                ]
            },
        }
    )


def _agent_tool_result_event() -> str:
    return _line(
        {
            "type": "user",
            "session_id": SESSION_ID,
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": AGENT_TOOL_USE_ID,
                        "content": "done",
                        "is_error": False,
                    }
                ]
            },
            "tool_use_result": {"status": "completed", "agentId": CHILD_AGENT_ID, "agentType": AGENT_TYPE},
        }
    )


def _assistant_text_event(text: str) -> str:
    return _line({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def _result_event() -> str:
    return _line({"type": "result", "subtype": "success"})


def _stream(*, child_body: str, parent_text: str, tool_input_prompt: str = "run the probe") -> list[str]:
    return [
        _line({"type": "system", "subtype": "init"}),
        _agent_tool_use_event(tool_input_prompt),
        _hook_event("SubagentStart"),
        _agent_tool_result_event(),
        _hook_event("SubagentStop", last_assistant_message=child_body),
        _assistant_text_event(parent_text),
        _result_event(),
    ]


def _run_runner(tmp_path: Path, stream: list[str], markers: list[str]) -> tuple[int, dict]:
    repo, worktree = _build_repo_with_worktree(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_claude(fake_bin / "claude", stream)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("run the quoted probe\n", encoding="utf-8")
    evidence_json = tmp_path / "evidence.json"
    args = [
        "--runtime",
        "claude",
        "--mode",
        "structured",
        "--prompt-file",
        str(prompt),
        "--output-dir",
        str(tmp_path / "out"),
        "--evidence-json",
        str(evidence_json),
        "--agent-type",
        AGENT_TYPE,
        "--expect-marker-source",
        "subagent",
        "--require-subagent-causal-evidence",
        "--require-min-subagents",
        "1",
    ]
    for marker in markers:
        args += ["--expect-marker", marker]
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--worktree", str(worktree), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    evidence = json.loads(evidence_json.read_text(encoding="utf-8"))
    return result.returncode, evidence


def test_quoted_positive_marker_missing_and_provenance_agree_in_one_evidence(tmp_path):
    """AC1: a child body that satisfies double-quoted literals (plus one
    unquoted literal) yields ``expected_markers_missing == []`` AND
    ``marker_provenance_verified is True`` in the same evidence file."""
    child_body = f"report ok\n{UNQUOTED_MARKER}\n{QUOTED_MARKER}\n{SECOND_QUOTED_MARKER}"
    stream = _stream(child_body=child_body, parent_text=child_body)
    # Sanity: the raw stream really carries only the JSON-escaped form, which
    # is what the pre-#2923 raw-substring check could not match.
    assert QUOTED_MARKER not in "\n".join(stream)
    code, evidence = _run_runner(tmp_path, stream, [QUOTED_MARKER, SECOND_QUOTED_MARKER, UNQUOTED_MARKER])
    assert evidence["expected_markers_missing"] == []
    causal = evidence["subagent_causal_evidence"]
    assert causal["marker_provenance_verified"] is True
    assert causal["causal_evidence_source"] == "hook_id_correlated"
    assert code == 0, evidence.get("errors")


def test_quoted_prompt_only_rejected_as_provenance(tmp_path):
    """AC3: a quoted literal that appears only in the Agent tool-input prompt,
    never in the child's own body, must NOT be adopted as marker provenance;
    the run must not PASS."""
    stream = _stream(
        child_body="report ok, nothing else",
        parent_text="parent summary without the literal",
        tool_input_prompt=f"please end with {QUOTED_MARKER}",
    )
    code, evidence = _run_runner(tmp_path, stream, [QUOTED_MARKER])
    causal = evidence["subagent_causal_evidence"]
    assert causal["marker_provenance_verified"] is False
    assert causal["causal_evidence_source"] != "hook_id_correlated"
    assert code == 1


def test_quoted_prompt_only_rejected_when_parent_text_echoes_literal(tmp_path):
    """AC3 (variant): the literal is present in the PARENT's assistant text
    but not in the correlated child's own body -- still not provenance."""
    stream = _stream(
        child_body="report ok, nothing else",
        parent_text=f"parent repeats {QUOTED_MARKER}",
    )
    code, evidence = _run_runner(tmp_path, stream, [QUOTED_MARKER])
    assert evidence["subagent_causal_evidence"]["marker_provenance_verified"] is False
    assert code == 1


def test_quoted_absent_quoted_missing_stays_in_expected_markers_missing(tmp_path):
    """AC4: a quoted literal absent from the whole stream stays in
    ``expected_markers_missing`` (and the run FAILs); a quoted literal that IS
    produced by the child is not reported."""
    absent = 'never_produced_probe: "zzz-2923"'
    child_body = f"report ok\n{QUOTED_MARKER}"
    stream = _stream(child_body=child_body, parent_text=child_body)
    code, evidence = _run_runner(tmp_path, stream, [QUOTED_MARKER, absent])
    assert evidence["expected_markers_missing"] == [absent]
    assert code == 1


def test_quoted_absent_quoted_missing_unquoted_marker_unchanged(tmp_path):
    """AC2/AC4 companion: an absent unquoted marker is still missing exactly as
    before the change."""
    child_body = f"report ok\n{UNQUOTED_MARKER}"
    stream = _stream(child_body=child_body, parent_text=child_body)
    code, evidence = _run_runner(tmp_path, stream, [UNQUOTED_MARKER, "ABSENT_UNQUOTED_2923"])
    assert evidence["expected_markers_missing"] == ["ABSENT_UNQUOTED_2923"]
    assert code == 1

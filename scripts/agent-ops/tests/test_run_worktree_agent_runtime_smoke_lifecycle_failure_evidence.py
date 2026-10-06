"""Issue #2958: behavioral tests for the opt-in
``--lifecycle-failure-evidence-json`` flag of
``run_worktree_agent_runtime_smoke.py``.

Every test drives the real runner ``main`` path in a subprocess against a
hermetic fake ``claude`` executable that replays a prepared stream-json file.
Nothing here spawns a real Claude Code process (Runtime Verification
Applicability: not_applicable).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"

_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2958_lifecycle_failure_evidence"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = smoke
_spec.loader.exec_module(smoke)

SESSION_ID = "11111111-2222-3333-4444-555555555555"
AGENT_ID = "a8bcdb5faaae506a2"
AGENT_TYPE = "general-purpose"
SECRET_SENTINEL = "sk-ant-api03-SECRETSENTINELZZZZ0123456789abcdef"
PROMPT_SENTINEL = "PROMPT_SENTINEL_do_not_persist_2958"
MESSAGE_SENTINEL = "ASSISTANT_MESSAGE_BODY_SENTINEL_2958"
LAST_MESSAGE_SENTINEL = "LAST_ASSISTANT_MESSAGE_SENTINEL_2958"
TRANSCRIPT_SENTINEL = "UNRELATED_TRANSCRIPT_SENTINEL_2958"
NOTIFICATION_BODY_SENTINEL = "NOTIFICATION_BODY_SENTINEL_2958"
EVIDENCE_NAME = "lifecycle-failure-evidence.json"

HOOK_ENTRY_KEYS = {
    "hook_event",
    "stream_index",
    "agent_id",
    "agent_type",
    "agent_transcript_path_present",
    "session_id",
    "prompt_id",
    "stop_hook_active",
    "contradictory",
}


def _git(*args: str, cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env)


@pytest.fixture()
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
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
    (worktree / ".claude").mkdir(exist_ok=True)
    (worktree / ".claude" / "settings.json").write_text('{"fixture": 2958}\n', encoding="utf-8")
    return repo, worktree


def _settings_digest(worktree: Path) -> str:
    return hashlib.sha256((worktree / ".claude" / "settings.json").read_bytes()).hexdigest()


def _fake_claude(bin_dir: Path) -> None:
    """Shared, byte-identical fake executable (the stream is chosen through
    ``FAKE_CLAUDE_STREAM_FILE``) so ``resolved_executable_sha256`` is stable
    across the with/without-flag comparisons."""
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--help" ]; then\n'
        '  echo "--output-format --include-hook-events --no-session-persistence --max-turns"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "--version" ]; then\n'
        '  echo "1.2.3 (Claude Code)"\n'
        "  exit 0\n"
        "fi\n"
        "cat > /dev/null\n"
        'cat "$FAKE_CLAUDE_STREAM_FILE"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _hook_event(
    hook_event: str,
    *,
    agent_id: str = AGENT_ID,
    transcript_path: str | None = None,
    last_assistant_message: str | None = None,
    prompt_id: str | None = "prompt-1",
    stop_hook_active: bool | None = None,
) -> dict:
    inner: dict = {
        "session_id": SESSION_ID,
        "hook_event_name": hook_event,
        "agent_id": agent_id,
        "agent_type": AGENT_TYPE,
    }
    if transcript_path is not None:
        inner["agent_transcript_path"] = transcript_path
    if last_assistant_message is not None:
        inner["last_assistant_message"] = last_assistant_message
    if prompt_id is not None:
        inner["prompt_id"] = prompt_id
    if stop_hook_active is not None:
        inner["stop_hook_active"] = stop_hook_active
    embedded = json.dumps(inner)
    return {
        "type": "system",
        "subtype": "hook_response",
        "hook_event": hook_event,
        "hook_name": hook_event,
        "session_id": SESSION_ID,
        "stdout": embedded,
        "output": embedded,
    }


def _tool_result(agent_id: str = AGENT_ID, status: str = "completed") -> dict:
    return {
        "type": "user",
        "session_id": SESSION_ID,
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": MESSAGE_SENTINEL}]},
        "tool_use_result": {"status": status, "agentId": agent_id, "agentType": AGENT_TYPE},
    }


def _notification(agent_id: str = AGENT_ID, status: str = "completed") -> dict:
    return {
        "type": "queue-operation",
        "content": (
            f"<task-notification><task-id>{agent_id}</task-id>"
            f"<result>{NOTIFICATION_BODY_SENTINEL}</result><status>{status}</status></task-notification>"
        ),
    }


def _noise_events(tmp_path: Path) -> list[dict]:
    """Prompt echo, assistant message body, secret sentinel: content that must
    never be persisted by the lifecycle failure evidence."""
    return [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": f"{MESSAGE_SENTINEL} {PROMPT_SENTINEL} {SECRET_SENTINEL}"}
                ]
            },
        },
    ]


def _failing_stream(tmp_path: Path) -> list[dict]:
    transcript = tmp_path / "unrelated-transcript.jsonl"
    transcript.write_text(TRANSCRIPT_SENTINEL + "\n", encoding="utf-8")
    return [
        *_noise_events(tmp_path),
        _hook_event("SubagentStart"),
        _tool_result(),
        _notification(),
        _hook_event(
            "SubagentStop",
            transcript_path=str(transcript),
            last_assistant_message=f"{LAST_MESSAGE_SENTINEL} {SECRET_SENTINEL}",
            stop_hook_active=False,
        ),
        # Injected duplicate completion on the same channel.
        _hook_event(
            "SubagentStop",
            transcript_path=str(transcript),
            last_assistant_message=f"{LAST_MESSAGE_SENTINEL} {SECRET_SENTINEL}",
            stop_hook_active=True,
        ),
        {"type": "result", "subtype": "success"},
    ]


def _passing_stream(tmp_path: Path) -> list[dict]:
    return [
        *_noise_events(tmp_path),
        _hook_event("SubagentStart"),
        _tool_result(),
        _hook_event("SubagentStop", stop_hook_active=False),
        {"type": "result", "subtype": "success"},
    ]


def _run_runner(
    tmp_path: Path,
    repo: Path,
    worktree: Path,
    events: list[dict],
    out_name: str,
    *extra: str,
    flag: str | None = None,
    min_subagents: str = "1",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    stream_file = tmp_path / f"stream-{out_name}.jsonl"
    stream_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _fake_claude(bin_dir)
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_SENTINEL + "\n", encoding="utf-8")
    out_dir = worktree / "artifacts" / "runtime-smoke" / out_name
    argv = [
        sys.executable,
        str(SCRIPT),
        "--repo-root", str(repo),
        "--worktree", str(worktree),
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt),
        "--output-dir", str(out_dir),
        "--timeout-seconds", "30",
        "--require-min-subagents", min_subagents,
    ]
    if flag is not None:
        argv += ["--lifecycle-failure-evidence-json", flag]
    argv += list(extra)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_CLAUDE_STREAM_FILE"] = str(stream_file)
    result = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, check=False, env=env)
    return result, out_dir


def _normalized_summary(out_dir: Path) -> list[str]:
    return [
        line
        for line in (out_dir / "summary.md").read_text(encoding="utf-8").splitlines()
        if not line.startswith("- run_id:")
    ]


def test_given_duplicate_stop_when_flag_set_then_allowlisted_events_preserved_on_lifecycle_failure(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    out_dir_probe = worktree / "artifacts" / "runtime-smoke" / "dup"
    result, out_dir = _run_runner(
        tmp_path, repo, worktree, _failing_stream(tmp_path), "dup", flag=str(out_dir_probe / EVIDENCE_NAME)
    )
    assert result.returncode == 1, result.stderr
    evidence_path = out_dir / EVIDENCE_NAME
    assert evidence_path.is_file()
    data = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert "duplicate_completions" in data["failure_reasons"]
    assert "multi_child_lifecycle_not_verified" in data["failure_reasons"]

    hooks = data["hook_lifecycle_events"]
    assert [h["hook_event"] for h in hooks] == ["SubagentStart", "SubagentStop", "SubagentStop"]
    for entry in hooks:
        assert set(entry) == HOOK_ENTRY_KEYS
        assert entry["agent_id"] == AGENT_ID
        assert entry["agent_type"] == AGENT_TYPE
        assert entry["session_id"] == SESSION_ID
        assert entry["prompt_id"] == "prompt-1"
        assert entry["contradictory"] is False
        assert isinstance(entry["stream_index"], int)
    assert [h["stream_index"] for h in hooks] == sorted(h["stream_index"] for h in hooks)
    assert [h["agent_transcript_path_present"] for h in hooks] == [False, True, True]
    assert [h["stop_hook_active"] for h in hooks] == [None, False, True]

    assert data["tool_use_results"] == [
        {"stream_index": data["tool_use_results"][0]["stream_index"], "agent_id": AGENT_ID, "status": "completed"}
    ]
    assert [(n["agent_id"], n["status"]) for n in data["task_notification_completions"]] == [
        (AGENT_ID, "completed")
    ]
    assert data["settings_provenance"] == {"digest_sha256": _settings_digest(worktree)}


def test_given_failure_stream_when_flag_set_then_output_excludes_message_prompt_and_secret_content(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    flag = str(worktree / "artifacts" / "runtime-smoke" / "excl" / EVIDENCE_NAME)
    result, out_dir = _run_runner(tmp_path, repo, worktree, _failing_stream(tmp_path), "excl", flag=flag)
    assert result.returncode == 1, result.stderr
    text = (out_dir / EVIDENCE_NAME).read_text(encoding="utf-8")
    assert text.strip()
    for sentinel in (
        SECRET_SENTINEL,
        PROMPT_SENTINEL,
        MESSAGE_SENTINEL,
        LAST_MESSAGE_SENTINEL,
        TRANSCRIPT_SENTINEL,
        NOTIFICATION_BODY_SENTINEL,
        "last_assistant_message",
        "unrelated-transcript",
    ):
        assert sentinel not in text, sentinel
    # Only the three allowlisted channels plus the digest and bookkeeping.
    assert set(json.loads(text)) == {
        "run_id",
        "failure_reasons",
        "max_events_per_channel",
        "hook_lifecycle_events",
        "hook_lifecycle_events_total",
        "tool_use_results",
        "tool_use_results_total",
        "task_notification_completions",
        "task_notification_completions_total",
        "settings_provenance",
    }


def test_given_pass_stream_or_no_flag_then_nothing_preserved_on_pass_or_without_flag(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree

    flag_pass = str(worktree / "artifacts" / "runtime-smoke" / "pass" / EVIDENCE_NAME)
    passed, pass_dir = _run_runner(tmp_path, repo, worktree, _passing_stream(tmp_path), "pass", flag=flag_pass)
    assert passed.returncode == 0, passed.stderr
    assert sorted(p.name for p in pass_dir.iterdir()) == ["summary.md"]

    failed, fail_dir = _run_runner(tmp_path, repo, worktree, _failing_stream(tmp_path), "nofl")
    assert failed.returncode == 1, failed.stderr
    assert sorted(p.name for p in fail_dir.iterdir()) == ["summary.md"]

    # A run that asserted no lifecycle at all (no --require-min-subagents, no
    # --expect-marker) has no lifecycle verdict to fail, even with the flag.
    flag_none = str(worktree / "artifacts" / "runtime-smoke" / "none" / EVIDENCE_NAME)
    plain, plain_dir = _run_runner(
        tmp_path,
        repo,
        worktree,
        [{"type": "system", "subtype": "init"}, {"type": "result", "subtype": "success"}],
        "none",
        flag=flag_none,
        min_subagents="0",
    )
    assert plain.returncode == 0, plain.stderr
    assert sorted(p.name for p in plain_dir.iterdir()) == ["summary.md"]


def test_given_same_stream_with_and_without_flag_then_verdict_and_exit_code_unchanged(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    events = _failing_stream(tmp_path)
    without, without_dir = _run_runner(tmp_path, repo, worktree, events, "cmp-off")
    flag = str(worktree / "artifacts" / "runtime-smoke" / "cmp-on" / EVIDENCE_NAME)
    with_flag, with_dir = _run_runner(tmp_path, repo, worktree, events, "cmp-on", flag=flag)

    assert without.returncode == with_flag.returncode == 1
    assert (with_dir / EVIDENCE_NAME).is_file()
    assert not (without_dir / EVIDENCE_NAME).exists()
    assert _normalized_summary(without_dir) == _normalized_summary(with_dir)
    fail_lines_without = [line for line in without.stderr.splitlines() if "[FAIL]" in line]
    fail_lines_with = [line for line in with_flag.stderr.splitlines() if "[FAIL]" in line]
    assert fail_lines_without and fail_lines_without == fail_lines_with

    # Also for a passing stream: identical exit code and summary.
    pass_off, pass_off_dir = _run_runner(tmp_path, repo, worktree, _passing_stream(tmp_path), "p-off")
    pass_flag = str(worktree / "artifacts" / "runtime-smoke" / "p-on" / EVIDENCE_NAME)
    pass_on, pass_on_dir = _run_runner(tmp_path, repo, worktree, _passing_stream(tmp_path), "p-on", flag=pass_flag)
    assert pass_off.returncode == pass_on.returncode == 0
    assert _normalized_summary(pass_off_dir) == _normalized_summary(pass_on_dir)


def test_given_existing_file_or_outside_path_then_existing_file_not_overwritten_and_path_confined(
    repo_with_worktree, tmp_path, capsys
):
    repo, worktree = repo_with_worktree
    events = _failing_stream(tmp_path)

    # (a) End to end: the target already exists once the run reaches the
    # write (summary.md is written first) -> never overwritten, exit code kept.
    baseline, baseline_dir = _run_runner(tmp_path, repo, worktree, events, "ex-base")
    target = str(worktree / "artifacts" / "runtime-smoke" / "ex-on" / "summary.md")
    result, out_dir = _run_runner(tmp_path, repo, worktree, events, "ex-on", flag=target)
    assert result.returncode == baseline.returncode == 1
    assert "already exists; not overwritten" in result.stderr
    assert sorted(p.name for p in out_dir.iterdir()) == ["summary.md"]
    assert _normalized_summary(out_dir) == _normalized_summary(baseline_dir)
    assert "# Runtime Smoke Summary" in (out_dir / "summary.md").read_text(encoding="utf-8")

    # (b) Unit level: a pre-existing file keeps its content, with a warning.
    out_root = tmp_path / "unit-out"
    out_root.mkdir()
    existing = out_root / EVIDENCE_NAME
    existing.write_text("KEEP-ME", encoding="utf-8")
    assert smoke.write_lifecycle_failure_evidence(str(existing), out_root, {"x": 1}) is False
    assert existing.read_text(encoding="utf-8") == "KEEP-ME"
    assert "already exists" in capsys.readouterr().err
    # ... and a fresh path is created exclusively.
    fresh = out_root / "fresh.json"
    assert smoke.write_lifecycle_failure_evidence(str(fresh), out_root, {"x": 1}) is True
    assert json.loads(fresh.read_text(encoding="utf-8")) == {"x": 1}

    # (c) Symlink escape: a symlinked sub-directory pointing outside is refused.
    outside = tmp_path / "outside"
    outside.mkdir()
    (out_root / "link").symlink_to(outside, target_is_directory=True)
    assert smoke.write_lifecycle_failure_evidence(str(out_root / "link" / "e.json"), out_root, {"x": 1}) is False
    assert list(outside.iterdir()) == []

    # (d) A path outside --output-dir is rejected before the run starts.
    outside_file = tmp_path / "escaped.json"
    bad_paths = (
        str(outside_file),
        "artifacts/runtime-smoke/esc/../escaped.json",
        str(worktree / "artifacts" / "runtime-smoke" / "esc"),
    )
    for bad in bad_paths:
        rejected, rejected_dir = _run_runner(tmp_path, repo, worktree, events, "esc", flag=bad)
        assert rejected.returncode == 2, rejected.stderr
        assert "must be a path inside --output-dir" in rejected.stderr
        assert not rejected_dir.exists()
    assert not outside_file.exists()
    assert not (worktree / "artifacts" / "runtime-smoke" / "escaped.json").exists()


def test_given_huge_event_count_then_output_is_bounded(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    total = 600
    events: list[dict] = [{"type": "system", "subtype": "init"}]
    for index in range(total):
        agent_id = f"agent{index:05d}"
        events.append(_hook_event("SubagentStart", agent_id=agent_id))
        events.append(_tool_result(agent_id=agent_id, status="async_launched"))
        events.append(_notification(agent_id=agent_id, status="running"))
    events.append({"type": "result", "subtype": "success"})
    flag = str(worktree / "artifacts" / "runtime-smoke" / "big" / EVIDENCE_NAME)
    result, out_dir = _run_runner(tmp_path, repo, worktree, events, "big", flag=flag)
    assert result.returncode == 1, result.stderr
    evidence_path = out_dir / EVIDENCE_NAME
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    cap = smoke._LIFECYCLE_FAILURE_EVIDENCE_MAX_EVENTS_PER_CHANNEL
    assert cap < total
    assert data["max_events_per_channel"] == cap
    assert len(data["hook_lifecycle_events"]) == cap
    assert len(data["tool_use_results"]) == cap
    assert len(data["task_notification_completions"]) == cap
    # The true totals are still reported so truncation is visible.
    assert data["hook_lifecycle_events_total"] == total
    assert data["tool_use_results_total"] == total
    assert data["task_notification_completions_total"] == total
    assert evidence_path.stat().st_size < 100_000

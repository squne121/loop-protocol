"""Issue #2783 AC16: runtime verification (`decision: immediate`).

The Allowed Paths inventory for this Issue includes
``scripts/agent-ops/pr_head_replay_publish_exec.py``, which the hard
risk-trigger rule ``subagent-lifecycle-start-stop-delegation-fallback``
(``docs/dev/extension-surface-runtime-policy.yaml``) matches via its
``scripts/agent-ops/**`` selector. This Issue's own change to that file
(``_allowed_paths()`` no-path marker integration) does not touch
SubAgentStart/SubagentStop delegation/fallback/start-stop signal handling,
but the policy still requires a runtime assertion binding
(``subagent-lifecycle-causal-evidence-smoke`` / assertion
``subagent_start_stop_causal_evidence_correlated``) to be exercised for
AC16, rather than accepting a marker-string-only claim as causal evidence.

This test spawns the REAL ``run_worktree_agent_runtime_smoke.py`` runner
(the ``worktree-agent-runtime-smoke`` skill's structured-mode entrypoint,
``--mode structured``) against a hermetic ``tmp_path``-backed git
repo+worktree fixture, with a FAKE ``claude`` executable on ``$PATH`` that
emits a correlated SubagentStart/SubagentStop hook-lifecycle pair plus a
real ``Agent`` tool_use/tool_result envelope (mirroring
``test_run_worktree_agent_runtime_smoke.py``'s own
``_subagent_hook_lines()`` fixture shape, Issue #2183). No live Claude Code
/ Codex CLI process is ever spawned; only ``subprocess`` + a disposable git
repo + a fake bash executable, so the SubAgentStart/SubagentStop hook
payload IS actually correlated with a real, observable
tool_use/tool_result Agent invocation in this run, not merely claimed by a
static marker string.

Environment preflight (2-stage, per this Issue's runtime-verification
policy): if `git` / `bash` / the runner script itself are unavailable, this
test SKIPS (pytest.skip(), not a silent PASS) rather than fabricating
causal evidence.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _preflight_skip_reason() -> str | None:
    """Stage 1 (before any fixture is built): required external tools."""
    if shutil.which("git") is None:
        return "environment preflight failed: git not found"
    if shutil.which("bash") is None:
        return "environment preflight failed: bash not found"
    if not SCRIPT.is_file():
        return f"environment preflight failed: runner script not found at {SCRIPT}"
    return None


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env)


def _build_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)

    worktree = repo / ".claude" / "worktrees" / "issue-2783-runtime-smoke-fixture"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-fixture", cwd=repo)
    _git("worktree", "add", str(worktree), "worktree-fixture", cwd=repo)
    return repo, worktree


def _write_fake_exe(path: Path, script_body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{script_body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _prompt_file(tmp_path: Path, text: str = "hello from AC16 runtime smoke\n") -> Path:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(text, encoding="utf-8")
    return prompt


def _run(repo: Path, worktree: Path, *args: str, fake_bin_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--worktree", str(worktree), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# `claude --help` fake branch: must advertise every flag preflight_claude_flags
# requires (mirrors test_run_worktree_agent_runtime_smoke.py's _HELP_BRANCH).
_HELP_BRANCH = """
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence --max-turns"
  exit 0
fi
"""


def _subagent_hook_lines(agent_id: str, marker: str) -> str:
    """Correlated SubagentStart/SubagentStop hook lifecycle pair PLUS a real
    Agent tool_use/tool_result envelope PLUS an actual on-disk transcript
    carrying `marker` -- the causal-evidence shape
    `subagent_causal_evidence_verdict()` requires for
    `causal_evidence_source: hook_id_correlated` (Issue #2183)."""
    tool_use_id = "toolu_ac16_agent_invocation"
    transcript_path = f"/tmp/{agent_id}-ac16-transcript.jsonl"

    def _hook_event(hook_event: str, *, with_transcript: bool) -> str:
        inner: dict[str, str] = {"agent_id": agent_id, "agent_type": "general-purpose"}
        if with_transcript:
            inner["agent_transcript_path"] = transcript_path
        inner_json = json.dumps(inner)
        payload = {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": hook_event,
            "hook_name": hook_event,
            "session_id": "ac16-fixture-session",
            "stdout": inner_json,
            "output": inner_json,
        }
        return f"echo {shlex.quote(json.dumps(payload))}\n"

    def _agent_tool_use_line() -> str:
        payload = {
            "type": "assistant",
            "session_id": "ac16-fixture-session",
            "message": {"content": [{"type": "tool_use", "id": tool_use_id, "name": "Agent", "input": {}}]},
        }
        return f"echo {shlex.quote(json.dumps(payload))}\n"

    def _agent_tool_result_line() -> str:
        payload = {
            "type": "user",
            "session_id": "ac16-fixture-session",
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "done"}]},
            "tool_use_result": {"status": "completed", "agentId": agent_id, "agentType": "general-purpose"},
        }
        return f"echo {shlex.quote(json.dumps(payload))}\n"

    transcript_content = json.dumps({"type": "assistant", "message": {"content": marker}}) + "\n"
    write_transcript_line = (
        f"printf %s {shlex.quote(transcript_content)} > {shlex.quote(transcript_path)}\n"
    )

    return (
        write_transcript_line
        + _agent_tool_use_line()
        + _hook_event("SubagentStart", with_transcript=False)
        + _agent_tool_result_line()
        + _hook_event("SubagentStop", with_transcript=True)
    )


def test_subagent_lifecycle_causal_evidence_unaffected_by_marker_change(tmp_path):
    """AC16: run the REAL structured lane against a fake `claude` that
    emits a hook-ID-correlated SubagentStart/SubagentStop pair (with a real
    Agent tool_use/tool_result envelope and an actual on-disk transcript
    carrying the expected marker). The resulting causal-evidence verdict
    must be `hook_id_correlated` -- proving Issue #2783's
    `pr_head_replay_publish_exec.py::_allowed_paths()` change did not
    disturb SubAgentStart/SubagentStop delegation/fallback/start-stop
    signal handling (marker-string match alone is not accepted as causal
    evidence, per docs/dev/runtime-verification-policy.md section 10)."""
    skip_reason = _preflight_skip_reason()
    if skip_reason is not None:
        pytest.skip(skip_reason)

    repo, worktree = _build_repo_with_worktree(tmp_path)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = "AC16_CAUSAL_EVIDENCE_MARKER"
    _write_fake_exe(
        fake_bin / "claude",
        _HELP_BRANCH
        + """
cat > /dev/null
echo '{"type":"system","subtype":"init"}'
"""
        + _subagent_hook_lines(agent_id="ac16-child-agent", marker=marker)
        + f"""echo '{{"type":"result","subtype":"success","marker":"{marker}"}}'
exit 0
""",
    )

    prompt = _prompt_file(tmp_path, f"{marker}\n")
    out_dir = worktree / "artifacts" / "runtime-smoke" / "ac16-structured"

    # Stage 2 preflight: the artifact output dir must resolve under the
    # worktree, not leak outside it (per this Issue's own worktree-write
    # policy for runtime verification artifacts).
    assert str(out_dir.resolve()).startswith(str(worktree.resolve()))

    result = _run(
        repo,
        worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "30", "--expect-marker", marker,
        fake_bin_dir=fake_bin,
    )

    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "subagent_causal_evidence" in summary
    assert "'causal_evidence_source': 'hook_id_correlated'" in summary
    assert "terminal_event_observed: True" in summary

"""Regression tests for Issue #2176: ``--claude-bin`` launcher observability
hook channel.

Background: ``run_structured_claude()`` used to unconditionally append a
fixed ``--settings <JSON>`` CLI flag (the ``SubagentStart``/``SubagentStop``
observability hooks introduced for Issue #2015) to the structured-lane
``claude`` invocation argv. A ``claude-gpt`` launcher wrapper pinned via
``--claude-bin`` (``scripts/claude-gpt/launch.sh``) rejects *any*
``--settings`` flag outright as a policy-weakening extra flag
(``CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS``), which made every structured-lane
launcher invocation a deterministic BLOCKED (PR #2176 AC3 finding).

This module verifies the narrow-channel fix:

- when ``--claude-bin`` is supplied, ``--settings`` is never appended to
  argv, and the fixed environment variable
  ``CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop`` is set in the
  child process environment instead;
- when ``--claude-bin`` is omitted (native ``claude`` on ``PATH``), argv
  keeps the pre-existing fixed ``--settings <JSON>`` flag unchanged
  (backward compatibility) and the new environment variable is not
  injected.

A second, related live-AC3 finding: ``scripts/claude-gpt/launch.sh`` only
accepts its own launcher options (``--claude-bin``, ``--check-only``,
``--dry-run``) before a literal ``--`` separator -- any other ``-*`` token
there is rejected as ``unknown_launcher_option``. This module also verifies
that a launcher-bound invocation inserts a leading ``--`` before the claude
CLI flags, while the native (no override) lane never does.

These tests use a fake ``claude``/launcher binary that records its own
argv and environment to files, rather than the real Claude Code CLI or a
real claude-gpt launcher (no live-environment dependency in the general
test suite), following the convention established by
``test_run_worktree_agent_runtime_smoke_claude_bin.py``.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env)


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
    return repo, worktree


def _write_fake_exe(path: Path, script_body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{script_body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _prompt_file(tmp_path: Path, text: str = "hello from test\n") -> Path:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(text, encoding="utf-8")
    return prompt


def _run(
    repo: Path,
    worktree: Path,
    *args: str,
    fake_bin_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fake_bin_dir is not None:
        env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root", str(repo),
            "--worktree", str(worktree),
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _recording_fake_claude_script(argv_marker: Path, env_marker: Path, version: str = "1.2.3") -> str:
    return f"""
if [ "$1" = "--version" ]; then
  echo "{version} (Claude Code)"
  exit 0
fi
cat > /dev/null
printf '%s\\n' "$@" > "{argv_marker}"
env | grep '^CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=' > "{env_marker}" || true
echo '{{"type":"result","subtype":"success"}}'
exit 0
"""


def test_claude_gpt_adapter_omits_settings_flag_and_sets_env_channel(repo_with_worktree, tmp_path):
    """Issue #2174 AC1 fix_delta: with ``--claude-bin`` AND the explicit
    ``--claude-adapter claude-gpt`` opt-in, the structured-lane invocation
    never carries a ``--settings`` flag on argv, and instead sets
    ``CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop`` in the child
    process environment. (Renamed from
    ``test_claude_bin_override_omits_settings_flag_and_sets_env_channel``:
    this launcher-specific behavior is no longer implied by
    ``bool(--claude-bin)`` alone -- see
    ``test_claude_bin_without_adapter_keeps_native_argv_shape`` below for the
    corrected default.)"""
    repo, worktree = repo_with_worktree

    launcher_dir = tmp_path / "claude-gpt-launcher"
    launcher_dir.mkdir()
    launcher_bin = launcher_dir / "launch.sh"
    argv_marker = tmp_path / "argv.recorded"
    env_marker = tmp_path / "env.recorded"
    _write_fake_exe(
        launcher_bin,
        _recording_fake_claude_script(argv_marker, env_marker, version="9.0.0-claude-gpt"),
    )

    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", str(launcher_bin),
        "--claude-adapter", "claude-gpt",
    )
    assert result.returncode == 0, result.stderr
    assert argv_marker.exists(), "launcher was not invoked"
    recorded_argv = argv_marker.read_text(encoding="utf-8")
    assert "--settings" not in recorded_argv, (
        "structured lane must never pass --settings to a --claude-adapter "
        "claude-gpt launcher (forbidden extra flag, PR #2176 AC3 BLOCKED finding)"
    )
    argv_lines = recorded_argv.splitlines()
    assert argv_lines and argv_lines[0] == "--", (
        "the claude-gpt launcher only accepts its own launcher options "
        "before a literal -- separator (unknown_launcher_option "
        "otherwise); the -- must be the first forwarded token"
    )
    assert env_marker.exists()
    recorded_env = env_marker.read_text(encoding="utf-8").strip()
    assert recorded_env == "CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop"


def test_claude_bin_without_adapter_keeps_native_argv_shape(repo_with_worktree, tmp_path):
    """Issue #2174 AC1 fix_delta (OWNER REQUEST_CHANGES Blocker 1,
    https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173):
    ``--claude-bin`` BY ITSELF (default ``--claude-adapter native``) is a
    pure binary-path override -- it must NOT insert a ``--`` separator, must
    NOT drop the pre-existing fixed ``--settings <JSON>`` flag, and must NOT
    inject ``CLAUDE_GPT_RUNTIME_SMOKE_HOOKS``. This is the exact bug the
    prior ``claude_bin_is_override=bool(args.claude_bin)`` design had: EVERY
    --claude-bin override (even an absolute-path native claude binary or a
    transparent wrapper) was forced through claude-gpt launcher argv/env
    handling."""
    repo, worktree = repo_with_worktree

    plain_dir = tmp_path / "plain-claude-bin"
    plain_dir.mkdir()
    plain_bin = plain_dir / "claude"
    argv_marker = tmp_path / "argv.recorded"
    env_marker = tmp_path / "env.recorded"
    _write_fake_exe(
        plain_bin,
        _recording_fake_claude_script(argv_marker, env_marker, version="1.0.0-plain"),
    )

    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", str(plain_bin),
    )
    assert result.returncode == 0, result.stderr
    assert argv_marker.exists(), "claude binary was not invoked"
    recorded_argv = argv_marker.read_text(encoding="utf-8")
    argv_lines = recorded_argv.splitlines()
    assert argv_lines and argv_lines[0] == "-p", (
        "default --claude-adapter native must NOT insert a leading -- "
        f"separator for a plain --claude-bin override, got: {argv_lines[:3]!r}"
    )
    assert "--settings" in recorded_argv, (
        "default --claude-adapter native must keep the pre-existing fixed "
        "--settings <JSON> observability flag, even with --claude-bin set"
    )
    assert env_marker.exists()
    recorded_env = env_marker.read_text(encoding="utf-8").strip()
    assert recorded_env == "", (
        "default --claude-adapter native must never inject "
        f"CLAUDE_GPT_RUNTIME_SMOKE_HOOKS, got env: {recorded_env!r}"
    )


def test_claude_bin_unspecified_keeps_settings_flag_and_no_env_channel(repo_with_worktree, tmp_path):
    """Backward compatibility: with no ``--claude-bin`` (native ``claude`` on
    ``PATH``), the pre-existing fixed ``--settings <JSON>`` argv flag is
    unchanged, and the new env channel is not injected."""
    repo, worktree = repo_with_worktree

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv_marker = tmp_path / "argv.recorded"
    env_marker = tmp_path / "env.recorded"
    _write_fake_exe(
        fake_bin / "claude",
        _recording_fake_claude_script(argv_marker, env_marker, version="1.0.0-path"),
    )

    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    assert argv_marker.exists(), "PATH-resolved claude was not invoked"
    recorded_argv = argv_marker.read_text(encoding="utf-8")
    assert "--settings" in recorded_argv, (
        "native claude (no --claude-bin) must keep the pre-existing fixed "
        "--settings <JSON> observability flag unchanged"
    )
    assert '"SubagentStart"' in recorded_argv
    assert '"SubagentStop"' in recorded_argv
    assert recorded_argv.splitlines()[0] != "--", (
        "native claude (no --claude-bin) must not gain a leading -- "
        "separator; that is a launcher-only accommodation"
    )
    assert env_marker.exists()
    assert env_marker.read_text(encoding="utf-8").strip() == "", (
        "CLAUDE_GPT_RUNTIME_SMOKE_HOOKS must not be injected for the "
        "native claude default lane"
    )

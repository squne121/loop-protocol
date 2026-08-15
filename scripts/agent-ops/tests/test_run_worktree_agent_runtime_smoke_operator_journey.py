"""Regression tests for Issue #2176's AC4 strengthening.

Background (Issue #2176, PR #2162 real-operator finding): AC4's pre-existing
isolated herdr interactive lane only ever sent a single hello/response
prompt and confirmed session cleanup -- it never exercised a representative
*operator journey* (launcher startup, model/effort confirmation, a Skill
load, a lightweight SubAgent spawn + completion confirmation, and a second
follow-up turn, all inside the SAME interactive session) and never asserted
that a context-limit / prompt-too-long / auto-compaction-failure /
unknown-model diagnostic never appeared.

This file adds dedicated regression coverage (fake ``herdr``/``claude``, no
live-environment dependency) for the two additive mechanisms this Issue adds
to the shared runner to make that stronger check expressible without a new
verification framework:

- ``--additional-prompt`` (repeatable): extra turns sent to the same
  already-started isolated-session agent after the initial prompt settles.
- ``--forbid-marker`` (repeatable): a literal string that, if observed
  anywhere in the captured output, unconditionally FAILs the run regardless
  of any other signal.

Live-environment verification of the actual claude-gpt launcher operator
journey (real herdr, real launcher, real SubAgent spawn) is a separate,
human-observed step recorded in ``summary.md`` evidence per the Issue's
Runtime Verification Applicability section -- it is not simulated here.
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
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fake_bin_dir is not None:
        env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
    if extra_env:
        env.update(extra_env)
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


_HELP_BRANCH = """
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence --max-turns"
  exit 0
fi
"""


# Fake herdr models an isolated named session as filesystem markers under
# $FAKE_HERDR_STATE_DIR (see test_run_worktree_agent_runtime_smoke.py's
# _FAKE_ISOLATED_HERDR_BODY, which this mirrors). Extended for this Issue:
# each ``agent prompt`` call appends the literal prompt text to
# $FAKE_HERDR_PROMPT_LOG (so a test can assert exactly which/how many turns
# were sent), and ``agent read`` echoes the contents of
# $FAKE_HERDR_READ_OUTPUT_FILE when set (so a test can control what the
# bounded pane transcript "contains", e.g. a forbidden marker).
_FAKE_ISOLATED_HERDR_BODY = """
STATE_DIR="$FAKE_HERDR_STATE_DIR"
mkdir -p "$STATE_DIR"
if [ "$1" = "--session" ]; then
  touch "$STATE_DIR/$2.session"
  sleep 300
  exit 0
fi
case "$1 $2" in
  "status server")
    exit 0
    ;;
esac
case "$1" in
  session)
    case "$2" in
      list)
        out="{\\"sessions\\":["
        first=1
        for f in "$STATE_DIR"/*.session; do
          [ -e "$f" ] || continue
          name=$(basename "$f" .session)
          if [ -e "$STATE_DIR/$name.stopped" ]; then running=false; else running=true; fi
          if [ $first -eq 0 ]; then out="$out,"; fi
          out="$out{\\"name\\":\\"$name\\",\\"running\\":$running}"
          first=0
        done
        out="$out]}"
        echo "$out"
        exit 0
        ;;
      stop)
        touch "$STATE_DIR/$3.stopped"
        exit 0
        ;;
      delete)
        rm -f "$STATE_DIR/$3.session" "$STATE_DIR/$3.stopped"
        exit 0
        ;;
    esac
    ;;
  workspace)
    case "$2" in
      create)
        touch "$STATE_DIR/${HERDR_SESSION}.session"
        echo '{"result":{"root_pane":{"pane_id":"pane-xyz"},"workspace":{"workspace_id":"w1"}}}'
        exit 0
        ;;
    esac
    ;;
  agent)
    case "$2" in
      start) exit 0 ;;
      prompt)
        if [ -n "$FAKE_HERDR_PROMPT_LOG" ]; then
          printf '%s\\n' "$4" >> "$FAKE_HERDR_PROMPT_LOG"
        fi
        exit 0
        ;;
      get) echo '{"state":"idle"}'; exit 0 ;;
      explain) echo '{"agent":"claude","confidence":"high"}'; exit 0 ;;
      read)
        if [ -n "$FAKE_HERDR_READ_OUTPUT_FILE" ] && [ -f "$FAKE_HERDR_READ_OUTPUT_FILE" ]; then
          cat "$FAKE_HERDR_READ_OUTPUT_FILE"
        else
          echo "OBSERVED_MARKER pane transcript line"
        fi
        exit 0
        ;;
    esac
    ;;
  api)
    case "$2" in
      snapshot)
        # Issue #2174 AC7: deterministic, unchanging workspace/agent/focus
        # snapshot -- the same content on every call means before/after
        # comparisons in the runner under test always observe zero drift.
        echo '{"result":{"snapshot":{"agents":[],"focused_workspace_id":"w0",'\
'"focused_tab_id":"w0:t0","focused_pane_id":"w0:p0"}}}'
        exit 0
        ;;
    esac
    ;;
esac
exit 0
"""


def _fake_claude_success_body(text: str = "") -> str:
    return (
        "\n"
        "cat > /dev/null\n"
        'echo \'{"type":"system","subtype":"init"}\'\n'
        f"echo '{{\"type\":\"result\",\"subtype\":\"success\",\"marker\":\"{text}\"}}'\n"
        "exit 0\n"
    )


# ---------------------------------------------------------------------------
# --additional-prompt: multi-turn interactive lane
# ---------------------------------------------------------------------------


def test_given_additional_prompts_when_interactive_lane_runs_then_all_turns_sent_in_order(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt_log = tmp_path / "prompt-log.txt"
    prompt = _prompt_file(tmp_path, "TURN1_MODEL_CHECK")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--additional-prompt", "TURN2_SKILL_AND_SUBAGENT",
        "--additional-prompt", "TURN3_FOLLOWUP",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "FAKE_HERDR_PROMPT_LOG": str(prompt_log),
        },
    )
    assert result.returncode == 0, result.stderr
    sent_turns = prompt_log.read_text(encoding="utf-8").splitlines()
    assert sent_turns == ["TURN1_MODEL_CHECK", "TURN2_SKILL_AND_SUBAGENT", "TURN3_FOLLOWUP"]
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "turns_completed: 3" in summary


def test_given_no_additional_prompts_when_interactive_lane_runs_then_single_turn_completed(
    repo_with_worktree, tmp_path
):
    """Regression: omitting --additional-prompt leaves the pre-existing
    single-turn behavior unchanged (turns_completed == 1)."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "turns_completed: 1" in summary


def test_additional_prompt_argparse_default_is_empty_list():
    """AC6-equivalent (unit-level): the parser's --additional-prompt default
    is an empty list, so a pre-existing caller that never passes this flag
    is unaffected."""
    sys.path.insert(0, str(SCRIPT.parent))
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    parser = module.build_parser()
    args = parser.parse_args([
        "--runtime", "claude", "--mode", "interactive",
        "--worktree", "/tmp/does-not-matter",
        "--prompt-file", "/tmp/does-not-matter.md",
        "--output-dir", "/tmp/does-not-matter-out",
    ])
    assert args.additional_prompt == []
    assert args.forbid_marker == []


# ---------------------------------------------------------------------------
# --forbid-marker: unconditional FAIL guard, both lanes
# ---------------------------------------------------------------------------


def test_given_forbid_marker_present_when_interactive_lane_runs_then_fail_even_with_expect_marker_satisfied(
    repo_with_worktree, tmp_path
):
    """The four operator-journey FAIL conditions (Context limit reached /
    Prompt is too long / automatic compaction failed / unknown-model
    warning) must FAIL the run even if the required --expect-marker was
    also observed -- a forbidden marker is never absorbed by an otherwise
    -satisfied positive check."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    read_output = tmp_path / "read-output.txt"
    read_output.write_text("OBSERVED_MARKER\nContext limit reached\n", encoding="utf-8")
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        "--forbid-marker", "Context limit reached",
        "--forbid-marker", "Prompt is too long",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "FAKE_HERDR_READ_OUTPUT_FILE": str(read_output),
        },
    )
    assert result.returncode == 1, result.stderr
    assert "forbidden markers observed" in result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "forbidden_markers_observed: ['Context limit reached']" in summary


def test_given_forbid_marker_absent_when_interactive_lane_runs_then_pass(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        "--forbid-marker", "Context limit reached",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "forbidden_markers_observed: []" in summary


def test_given_forbid_marker_present_when_structured_lane_runs_then_fail(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    body = (
        _HELP_BRANCH
        + "\ncat > /dev/null\n"
        + 'echo \'{"type":"system","subtype":"init"}\'\n'
        + 'echo \'automatic compaction failed\' >&2\n'
        + 'echo \'{"type":"result","subtype":"success"}\'\n'
        + "exit 0\n"
    )
    _write_fake_exe(fake_bin / "claude", body)
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--forbid-marker", "automatic compaction failed",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "forbidden_markers_observed: ['automatic compaction failed']" in summary


def test_given_forbid_marker_absent_when_structured_lane_runs_then_pass(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + _fake_claude_success_body())
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--forbid-marker", "automatic compaction failed",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "forbidden_markers_observed: []" in summary

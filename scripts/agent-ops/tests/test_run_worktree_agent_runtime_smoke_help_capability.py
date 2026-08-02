"""Regression tests for Issue #1960: Claude Code capability judgement must be
derived from the actual fixed-argv structured-lane invocation result, not
from ``claude --help`` text.

This is a dedicated new test file (not new test functions appended to
``test_run_worktree_agent_runtime_smoke.py``), per Issue #1960's Current
Validated Scope / Issue #1285 / PR #1305 VC contract convention: false-SKIP
capability-classification regression tests and runtime-evidence tests live
in their own files, separate from the pre-existing general smoke test suite.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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


# Real Claude Code 2.1.220 ``--help`` output does not advertise ``--max-turns``
# (Issue #1960 Background), while the flag is a documented, accepted
# print-mode flag. This fixture reproduces exactly that split: ``--help``
# omits the flag, but the real fixed-argv invocation accepts it.
_HELP_OMITS_MAX_TURNS = """
if [ "$1" = "--version" ]; then
  echo "2.1.220 (Claude Code)"
  exit 0
fi
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence"
  exit 0
fi
"""


def test_help_omits_max_turns_but_runtime_accepts_flag_then_structured_smoke_runs(
    repo_with_worktree, tmp_path
):
    """AC1: ``claude --help`` not listing ``--max-turns`` must not prevent
    the structured lane from running when the real invocation accepts the
    flag and returns a terminal result -- exit 0, not SKIP."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_OMITS_MAX_TURNS + """
cat > /dev/null
echo '{"type":"system","subtype":"init"}'
echo '{"type":"result","subtype":"success"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: runtime_outcome" in summary
    assert "process_exit_code: 0" in summary


def test_runtime_rejects_max_turns_as_unknown_option_then_skip77_with_summary(
    repo_with_worktree, tmp_path
):
    """AC2: a genuine parser-level unknown/unrecognized-option rejection of
    a fixed-argv flag is the only condition that SKIPs (exit 77), and
    summary.md must record the runtime version and capability reason."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_OMITS_MAX_TURNS + """
cat > /dev/null
echo "error: unknown option '--max-turns'" >&2
exit 1
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 77
    assert result.stderr.startswith("SKIP:")
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: capability_skip" in summary
    assert "runtime_version: 2.1.220" in summary
    assert "capability_error_classification: claude runtime rejected" in summary


@pytest.mark.parametrize(
    "failure_body",
    [
        # Authentication failure.
        pytest.param('echo "Error: Not authenticated. Run claude login." >&2\nexit 1\n', id="auth"),
        # Network failure.
        pytest.param(
            'echo "Error: network request failed: connection reset" >&2\nexit 1\n', id="network"
        ),
        # Model failure.
        pytest.param('echo "Error: model overloaded_error" >&2\nexit 1\n', id="model"),
        # Generic non-zero runtime failure unrelated to any flag.
        pytest.param('echo "Error: internal error" >&2\nexit 1\n', id="generic"),
    ],
)
def test_non_capability_runtime_failure_is_not_misclassified_as_skip(
    repo_with_worktree, tmp_path, failure_body
):
    """AC3: auth failure, network failure, model failure, and generic
    non-zero runtime errors must classify as FAIL (exit 1), never as an
    unknown-option capability SKIP."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_OMITS_MAX_TURNS + f"""
cat > /dev/null
{failure_body}
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1, result.stderr
    assert not result.stderr.startswith("SKIP:")
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: runtime_outcome" in summary


def test_max_turn_limit_reached_is_fail_not_capability_skip(repo_with_worktree, tmp_path):
    """AC4: reaching the ``--max-turns`` bound is evidence the flag was
    accepted -- it must be a runtime failure (exit 1), never a capability
    SKIP."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_OMITS_MAX_TURNS + """
cat > /dev/null
echo "Error: Reached max turns limit: 5" >&2
exit 1
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--max-turns", "5",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert not result.stderr.startswith("SKIP:")
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: turn_limit_reached" in summary


# Fake herdr models an isolated named session as filesystem markers under
# $FAKE_HERDR_STATE_DIR -- mirrors
# test_run_worktree_agent_runtime_smoke.py's ``_FAKE_ISOLATED_HERDR_BODY``
# fixture exactly, with one addition: ``agent start`` logs its full argv to
# ``$FAKE_HERDR_STATE_DIR/agent_start_argv.txt`` so this test can assert
# structured-only flags were never forwarded (AC5).
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
      start) echo "$@" > "$STATE_DIR/agent_start_argv.txt"; exit 0 ;;
      prompt) exit 0 ;;
      get) echo '{"state":"idle"}'; exit 0 ;;
      explain) echo '{"agent":"claude","confidence":"high"}'; exit 0 ;;
      read) echo "OBSERVED_MARKER pane transcript line"; exit 0 ;;
    esac
    ;;
esac
exit 0
"""


def test_interactive_claude_does_not_require_or_forward_structured_only_flags(
    repo_with_worktree, tmp_path
):
    """AC5: the interactive Claude lane must not depend on a structured-only
    flag preflight (help omitting ``--max-turns`` must not SKIP interactive
    mode either) and must not forward ``--max-turns`` (or any other
    structured-only flag) into the ``herdr agent start`` invocation."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Deliberately omits --max-turns from --help, exactly like the
    # structured-lane fixture above -- this alone must not SKIP interactive
    # mode (unlike the pre-fix behavior where the shared help-preflight
    # gated both lanes identically).
    _write_fake_exe(fake_bin / "claude", _HELP_OMITS_MAX_TURNS + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    state_dir.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY)

    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--max-turns", "9",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, result.stderr

    argv_log = state_dir / "agent_start_argv.txt"
    assert argv_log.exists(), "herdr agent start was never invoked"
    argv_text = argv_log.read_text(encoding="utf-8")
    assert "--max-turns" not in argv_text
    assert "--output-format" not in argv_text
    assert "--include-hook-events" not in argv_text
    assert "--no-session-persistence" not in argv_text


def test_max_turns_must_be_positive(repo_with_worktree, tmp_path):
    """AC6: ``--max-turns`` only accepts positive integers; ``0`` and
    negative values are rejected as an argument error."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    for bad_value in ("0", "-1", "-100"):
        result = _run(
            repo, worktree,
            "--runtime", "claude", "--mode", "structured",
            "--prompt-file", str(prompt), "--output-dir", str(tmp_path / f"out-{bad_value}"),
            "--max-turns", bad_value,
        )
        assert result.returncode == 2, result.stderr
        assert "--max-turns" in result.stderr

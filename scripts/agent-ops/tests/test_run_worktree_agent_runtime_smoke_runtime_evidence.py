"""Runtime-evidence regression test for Issue #1960 AC9: a structured smoke
run must reach the real subprocess invocation (not merely stop at a
``claude --help`` preflight) even when ``--help`` omits ``--max-turns``.

This is a dedicated new test file (not appended to the pre-existing general
smoke suite), per Issue #1960's Current Validated Scope / Issue #1285 /
PR #1305 VC contract convention.

This test uses a fake ``claude`` binary rather than the real Claude Code CLI
(no network/auth dependency in the general test suite); it independently
proves the runner reaches the actual fixed-argv subprocess call by writing a
process-invocation marker file from within the fake binary itself, which can
only appear if the runner actually ``subprocess.run``'d the fixed argv (a
preflight-only SKIP path would never create it).
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


def test_structured_smoke_reaches_real_invocation_via_subprocess_despite_help_omission(
    repo_with_worktree, tmp_path
):
    """AC9: even though ``claude --help`` omits ``--max-turns`` (as observed
    for real Claude Code 2.1.220), the structured lane must reach the real
    subprocess invocation of the fixed argv -- not stop at a help-based
    preflight SKIP. The fake binary writes an ``invoked.marker`` file only
    from its non-``--help`` branch (the actual fixed-argv call path), so its
    presence is independent, non-self-reported evidence that real invocation
    was reached."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invoked_marker = tmp_path / "invoked.marker"
    argv_marker = tmp_path / "invoked.argv"
    _write_fake_exe(fake_bin / "claude", f"""
if [ "$1" = "--version" ]; then
  echo "2.1.220 (Claude Code)"
  exit 0
fi
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence"
  exit 0
fi
# Only the real fixed-argv invocation path reaches here.
echo "$@" > "{argv_marker}"
touch "{invoked_marker}"
cat > /dev/null
echo '{{"type":"system","subtype":"init"}}'
echo '{{"type":"result","subtype":"success"}}'
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
    assert invoked_marker.exists(), "runner never reached the real fixed-argv subprocess invocation"
    argv_text = argv_marker.read_text(encoding="utf-8")
    assert "--max-turns" in argv_text
    assert "--output-format" in argv_text
    assert "--include-hook-events" in argv_text
    assert "--no-session-persistence" in argv_text
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: runtime_outcome" in summary
    assert "runtime_version: 2.1.220" in summary
    assert "tested_head:" in summary

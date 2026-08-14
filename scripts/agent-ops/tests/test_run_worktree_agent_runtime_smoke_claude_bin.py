"""Regression tests for Issue #2174 AC1/AC6: ``--claude-bin`` input.

AC1: ``run_worktree_agent_runtime_smoke.py`` accepts ``--claude-bin
<absolute path>`` and, when supplied, uses that absolute path directly as
the claude executable -- bypassing ``shutil.which("claude")`` PATH
resolution entirely.

AC6: when ``--claude-bin`` is NOT supplied, the pre-existing
``shutil.which("claude")`` PATH-resolution default behavior is unchanged.

This is a dedicated new test file (not appended to the pre-existing general
smoke suite), per Issue #1960 / #2174's Current Validated Scope test-location
convention (also followed by
``test_run_worktree_agent_runtime_smoke_runtime_evidence.py``).

These tests use a fake ``claude`` binary rather than the real Claude Code
CLI or a real ``herdr``/claude-gpt launcher (no live-environment dependency
in the general test suite). Live-environment runtime verification of
AC3/AC4 (the actual claude-gpt launcher, structured + isolated herdr
interactive lane) is a separate, human-observed step recorded in
``summary.md`` evidence per the Issue's Runtime Verification Applicability
section -- it is not simulated here.
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


def _fake_claude_script(marker: Path, version: str = "1.2.3") -> str:
    return f"""
if [ "$1" = "--version" ]; then
  echo "{version} (Claude Code)"
  exit 0
fi
cat > /dev/null
touch "{marker}"
echo '{{"type":"result","subtype":"success"}}'
exit 0
"""


def test_claude_bin_absolute_path_bypasses_path_resolution(repo_with_worktree, tmp_path):
    """AC1: with ``--claude-bin <absolute path>``, the runner uses that exact
    executable, never consulting ``PATH`` at all. A decoy ``claude`` on
    ``PATH`` (which would fail loudly if invoked) proves ``--claude-bin`` was
    used instead of any PATH-resolved binary."""
    repo, worktree = repo_with_worktree

    decoy_bin = tmp_path / "decoy-bin"
    decoy_bin.mkdir()
    decoy_marker = tmp_path / "decoy-invoked.marker"
    _write_fake_exe(decoy_bin / "claude", f'touch "{decoy_marker}"\necho "DECOY SHOULD NEVER RUN" >&2\nexit 99\n')

    override_dir = tmp_path / "claude-gpt-launcher"
    override_dir.mkdir()
    override_marker = tmp_path / "override-invoked.marker"
    override_bin = override_dir / "launch.sh"
    _write_fake_exe(override_bin, _fake_claude_script(override_marker, version="9.0.0-claude-gpt"))

    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", str(override_bin),
        fake_bin_dir=decoy_bin,
    )
    assert result.returncode == 0, result.stderr
    assert override_marker.exists(), "runner did not invoke the --claude-bin override executable"
    assert not decoy_marker.exists(), "runner invoked the PATH-resolved decoy instead of --claude-bin"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "runtime_version: 9.0.0-claude-gpt" in summary
    resolved_line = next(
        line for line in summary.splitlines() if line.startswith("- resolved_executable:")
    )
    resolved_value = resolved_line.split(":", 1)[1].strip()
    assert resolved_value == os.path.realpath(str(override_bin))


def test_claude_bin_nonexecutable_path_is_skip_not_crash(repo_with_worktree, tmp_path):
    """AC1: an invalid ``--claude-bin`` (does not exist / not executable) is
    a controlled SKIP (exit 77), never a crash and never silently falling
    back to PATH resolution."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    missing_bin = tmp_path / "does-not-exist" / "launch.sh"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", str(missing_bin),
    )
    assert result.returncode == 77, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "resolved_executable: None" in summary


def test_claude_bin_rejects_relative_path(repo_with_worktree, tmp_path):
    """AC1: ``--claude-bin`` only accepts absolute paths; a relative path is
    an argparse usage error (exit 2), not a silently-accepted relative
    lookup."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", "relative/launch.sh",
    )
    assert result.returncode == 2, result.stderr
    assert "--claude-bin must be an absolute path" in result.stderr


def test_claude_bin_unspecified_default_behavior_is_unchanged(repo_with_worktree, tmp_path):
    """AC6: omitting ``--claude-bin`` entirely leaves the pre-existing
    ``shutil.which("claude")`` PATH-resolution default behavior unchanged --
    the PATH-resolved binary is invoked exactly as before this flag
    existed."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    path_marker = tmp_path / "path-invoked.marker"
    _write_fake_exe(fake_bin / "claude", _fake_claude_script(path_marker, version="1.0.0-path"))

    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    assert path_marker.exists(), "PATH-resolved claude was not invoked when --claude-bin is omitted"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "runtime_version: 1.0.0-path" in summary
    resolved_line = next(
        line for line in summary.splitlines() if line.startswith("- resolved_executable:")
    )
    resolved_value = resolved_line.split(":", 1)[1].strip()
    assert resolved_value == os.path.realpath(str(fake_bin / "claude"))


def test_claude_bin_argparse_default_is_none():
    """AC6 (unit-level): the parser's ``--claude-bin`` default is ``None``,
    so pre-existing callers that never pass this flag get an unchanged
    ``args.claude_bin is None`` -- the exact pre-#2174 argv shape."""
    module = _load_module()
    parser = module.build_parser()
    args = parser.parse_args([
        "--runtime", "claude", "--mode", "structured",
        "--worktree", "/tmp/does-not-matter",
        "--prompt-file", "/tmp/does-not-matter.md",
        "--output-dir", "/tmp/does-not-matter-out",
    ])
    assert args.claude_bin is None


def test_preflight_claude_available_without_override_uses_path(monkeypatch, tmp_path):
    """AC6 (unit-level): ``preflight_claude_available()`` called with no
    argument (or ``None``) is byte-for-byte the pre-#2174
    ``shutil.which("claude")`` PATH-resolution path."""
    module = _load_module()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_path = fake_bin / "claude"
    _write_fake_exe(claude_path, "exit 0\n")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    resolved, skip_reason = module.preflight_claude_available()
    assert skip_reason is None
    assert resolved == os.path.realpath(str(claude_path))

    resolved_none_override, skip_reason_none_override = module.preflight_claude_available(None)
    assert skip_reason_none_override is None
    assert resolved_none_override == resolved


def test_preflight_claude_available_with_override_ignores_path(monkeypatch, tmp_path):
    """AC1 (unit-level): a supplied ``claude_bin_override`` is used directly,
    regardless of what (if anything) is on ``PATH``."""
    module = _load_module()
    monkeypatch.setenv("PATH", "/nonexistent-path-entry")

    override_dir = tmp_path / "claude-gpt-launcher"
    override_dir.mkdir()
    override_bin = override_dir / "launch.sh"
    _write_fake_exe(override_bin, "exit 0\n")

    resolved, skip_reason = module.preflight_claude_available(str(override_bin))
    assert skip_reason is None
    assert resolved == os.path.realpath(str(override_bin))

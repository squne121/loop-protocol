"""Issue #2174 AC8 matrix test: --claude-bin combinations (PATH native /
absolute native / claude-gpt / claude-gpt + --hermetic-agent-definition /
arbitrary wrapper), including confirming that the claude-gpt adapter +
hermetic --settings forwarding combination is DETECTABLY rejected by the
launcher's own forbidden-flag policy (not silently swallowed).

The fake ``launch.sh`` fixture below reproduces the two load-bearing
behaviors of the real ``scripts/claude-gpt/launch.sh`` (Issue #2158/#2162)
relevant to this matrix: (1) it only accepts its own launcher options
before a literal ``--`` separator, and (2) it rejects a ``--settings`` flag
forwarded after ``--`` as a policy-weakening extra flag, emitting its own
``CLAUDE_GPT_LAUNCH_RESULT_V1`` JSON receipt to stderr and exiting 2.
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
    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke_matrix", SCRIPT)
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
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fake_bin_dir is not None:
        env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--worktree", str(worktree), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# A minimal, behaviorally-faithful stand-in for scripts/claude-gpt/launch.sh:
# accepts --claude-bin/--check-only/--dry-run before "--", rejects a
# --settings token forwarded after "--" with the real launcher's own
# CLAUDE_GPT_LAUNCH_RESULT_V1 JSON receipt shape, otherwise runs the
# resolved claude-compatible binary transparently.
_FAKE_CLAUDE_GPT_LAUNCHER = """
while [ $# -gt 0 ]; do
  case "$1" in
    --claude-bin) shift 2 ;;
    --check-only|--dry-run) shift ;;
    --) shift; break ;;
    -*)
      printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked",'\
'"reason":"unknown_launcher_option","option":"%s"}\\n' "$1" 1>&2
      exit 2
      ;;
    *) shift ;;
  esac
done
for arg in "$@"; do
  case "$arg" in
    --settings|--settings=*)
      printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked",'\
'"reason":"policy_weakening_flag_rejected","flag":"--settings"}\\n' 1>&2
      exit 2
      ;;
  esac
done
echo '{"type":"result","subtype":"success"}'
exit 0
"""


def _fake_native_claude(version: str = "1.0.0") -> str:
    return f"""
if [ "$1" = "--version" ]; then
  echo "{version} (Claude Code)"
  exit 0
fi
cat > /dev/null
echo '{{"type":"result","subtype":"success"}}'
exit 0
"""


@pytest.mark.parametrize(
    "case_name",
    [
        "path_native",
        "absolute_path_native",
        "claude_gpt_adapter",
        "arbitrary_wrapper_native",
    ],
)
def test_claude_bin_matrix_non_hermetic_combinations_succeed(repo_with_worktree, tmp_path, case_name):
    """AC8 matrix (non-hermetic legs): PATH native / absolute-path native /
    claude-gpt adapter / arbitrary transparent wrapper each complete
    successfully with the expected adapter-specific argv."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / f"out-{case_name}"

    extra_args: list[str] = []
    if case_name == "path_native":
        _write_fake_exe(fake_bin / "claude", _fake_native_claude())
    elif case_name == "absolute_path_native":
        abs_bin = tmp_path / "abs-claude" / "claude"
        abs_bin.parent.mkdir()
        _write_fake_exe(abs_bin, _fake_native_claude())
        extra_args = ["--claude-bin", str(abs_bin)]
    elif case_name == "claude_gpt_adapter":
        launcher_bin = tmp_path / "claude-gpt" / "launch.sh"
        launcher_bin.parent.mkdir()
        _write_fake_exe(launcher_bin, _FAKE_CLAUDE_GPT_LAUNCHER)
        extra_args = ["--claude-bin", str(launcher_bin), "--claude-adapter", "claude-gpt"]
    else:  # arbitrary_wrapper_native
        wrapper_bin = tmp_path / "wrapper" / "claude"
        wrapper_bin.parent.mkdir()
        _write_fake_exe(wrapper_bin, _fake_native_claude(version="9.9.9-wrapper"))
        extra_args = ["--claude-bin", str(wrapper_bin)]

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        *extra_args,
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_claude_gpt_adapter_plus_hermetic_settings_flag_causes_launcher_rejection(
    repo_with_worktree, tmp_path
):
    """AC8: the claude-gpt adapter + --hermetic-agent-definition combination
    forwards a hermetic --settings flag after the launcher's own `--`
    separator; the launcher's forbidden-flag policy structurally rejects
    it. This must be independently DETECTABLE (recorded in evidence via
    claude_gpt_launcher_receipt / a non-zero exit code), never silently
    absorbed as a successful run."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launcher_bin = tmp_path / "claude-gpt" / "launch.sh"
    launcher_bin.parent.mkdir()
    _write_fake_exe(launcher_bin, _FAKE_CLAUDE_GPT_LAUNCHER)

    agent_name = "matrix-test-agent"
    agents_dir = worktree / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_name}.md").write_text(
        f"---\nname: {agent_name}\ndescription: AC8 matrix fixture\n---\nbody\n",
        encoding="utf-8",
    )

    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out-claude-gpt-hermetic"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", str(launcher_bin),
        "--claude-adapter", "claude-gpt",
        "--claude-agent-name", agent_name,
        "--hermetic-agent-definition",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "policy_weakening_flag_rejected" in summary, summary
    assert "claude_gpt_launcher_receipt" in summary

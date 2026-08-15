"""Regression tests for Issue #2174 AC7 (OWNER REQUEST_CHANGES Blocker 2,
https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173):
full Herdr session snapshot preservation (workspace ID / agent ID /
focused workspace-tab-pane selection), not just session-level identity
(name/default/running/socket_path/session_dir, already covered by
``test_run_worktree_agent_runtime_smoke_herdr_isolation.py``'s
``snapshot_herdr_sessions`` / ``diff_herdr_session_baseline``).

This file covers:

1. Unit coverage for ``capture_herdr_workspace_snapshot`` /
   ``diff_herdr_workspace_snapshot``: fail-closed ``None`` handling, and
   exact-equality diffs for focus/workspace/agent-location changes.
2. A "poison test" (AC7's explicit requirement): deliberately mutate one
   field (focused workspace, in this case) between the before- and
   after-capture and confirm ``diff_herdr_workspace_snapshot`` reliably
   detects it (never a false "preserved").
3. An end-to-end CLI poison test: a fake ``herdr`` whose ``api snapshot``
   answer changes between the first (before) and second (after) call
   (simulating a genuine ambient workspace/focus mutation happening during
   the isolated interactive lane run) must FAIL the whole run, not exit 0.
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
    spec = importlib.util.spec_from_file_location(
        "run_worktree_agent_runtime_smoke_workspace_snapshot", SCRIPT
    )
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


# ---------------------------------------------------------------------------
# 1. Unit coverage: capture_herdr_workspace_snapshot / diff_herdr_workspace_snapshot
# ---------------------------------------------------------------------------


def _snapshot(*, workspace="w0", tab="w0:t0", pane="w0:p0", agents=()):
    return {
        "focused_workspace_id": workspace,
        "focused_tab_id": tab,
        "focused_pane_id": pane,
        "agent_locations": sorted(agents),
    }


def test_diff_workspace_snapshot_identical_is_empty():
    module = _load_module()
    before = _snapshot(agents=[("w1", "w1:t1", "w1:p1")])
    after = _snapshot(agents=[("w1", "w1:t1", "w1:p1")])
    assert module.diff_herdr_workspace_snapshot(before, after) == []


def test_diff_workspace_snapshot_none_is_fail_closed():
    module = _load_module()
    before = _snapshot()
    assert module.diff_herdr_workspace_snapshot(None, before) != []
    assert module.diff_herdr_workspace_snapshot(before, None) != []
    assert module.diff_herdr_workspace_snapshot(None, None) != []


def test_diff_workspace_snapshot_poison_focused_workspace_is_detected():
    """AC7 poison test: an operator focus change (e.g. a human clicking a
    different workspace tab while the isolated lane was running) must be
    detected, never silently absorbed as 'preserved'."""
    module = _load_module()
    before = _snapshot(workspace="w0")
    poisoned_after = _snapshot(workspace="w9")
    diffs = module.diff_herdr_workspace_snapshot(before, poisoned_after)
    assert diffs, "poisoned focused_workspace_id must be detected, not silently preserved"
    assert any("focused_workspace_id" in d for d in diffs)


def test_diff_workspace_snapshot_poison_focused_tab_is_detected():
    module = _load_module()
    before = _snapshot(tab="w0:t0")
    poisoned_after = _snapshot(tab="w0:t9")
    diffs = module.diff_herdr_workspace_snapshot(before, poisoned_after)
    assert diffs and any("focused_tab_id" in d for d in diffs)


def test_diff_workspace_snapshot_poison_focused_pane_is_detected():
    module = _load_module()
    before = _snapshot(pane="w0:p0")
    poisoned_after = _snapshot(pane="w0:p9")
    diffs = module.diff_herdr_workspace_snapshot(before, poisoned_after)
    assert diffs and any("focused_pane_id" in d for d in diffs)


def test_diff_workspace_snapshot_poison_agent_location_is_detected():
    """AC7 poison test: an existing agent pane moving to a different
    workspace/tab/pane (or a new/removed agent) must be detected."""
    module = _load_module()
    before = _snapshot(agents=[("w1", "w1:t1", "w1:p1")])
    poisoned_after = _snapshot(agents=[("w2", "w2:t1", "w2:p1")])
    diffs = module.diff_herdr_workspace_snapshot(before, poisoned_after)
    assert diffs and any("agent" in d for d in diffs)


def test_capture_workspace_snapshot_missing_focus_field_is_fail_closed(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(
        fake_bin / "herdr",
        (
            'if [ "$1" = "api" ] && [ "$2" = "snapshot" ]; then\n'
            "  echo '{\"result\":{\"snapshot\":{\"agents\":[],\"focused_workspace_id\":\"w0\"}}}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        ),
    )
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    probe = (
        "import importlib.util,sys\n"
        f"spec=importlib.util.spec_from_file_location('m', {str(SCRIPT)!r})\n"
        "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(m.capture_herdr_workspace_snapshot('herdr', None))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, env=env,
    )
    assert result.stdout.strip() == "None", result.stdout


# ---------------------------------------------------------------------------
# 2. End-to-end CLI poison test: ambient focus mutation during the isolated
#    interactive lane run must FAIL the whole run.
# ---------------------------------------------------------------------------

_STABLE_SNAPSHOT_CASE = (
    "      snapshot)\n"
    "        echo '{\"result\":{\"snapshot\":{\"agents\":[],\"focused_workspace_id\":"
    "\"w0\",\"focused_tab_id\":\"w0:t0\",\"focused_pane_id\":\"w0:p0\"}}}'\n"
    "        exit 0\n"
    "        ;;"
)

_POISON_SNAPSHOT_CASE = (
    "      snapshot)\n"
    "        # First call (before the isolated lane) returns focused_workspace_id\n"
    "        # w0; every subsequent call (after) returns w9 -- simulating an\n"
    "        # ambient operator focus change occurring while the isolated lane\n"
    "        # was running. This must be DETECTED, never silently preserved.\n"
    "        if [ -f \"$CALL_COUNTER_FILE\" ]; then\n"
    "          echo '{\"result\":{\"snapshot\":{\"agents\":[],\"focused_workspace_id\":"
    "\"w9\",\"focused_tab_id\":\"w0:t0\",\"focused_pane_id\":\"w0:p0\"}}}'\n"
    "        else\n"
    "          touch \"$CALL_COUNTER_FILE\"\n"
    "          echo '{\"result\":{\"snapshot\":{\"agents\":[],\"focused_workspace_id\":"
    "\"w0\",\"focused_tab_id\":\"w0:t0\",\"focused_pane_id\":\"w0:p0\"}}}'\n"
    "        fi\n"
    "        exit 0\n"
    "        ;;"
)

_POISON_HERDR_BODY = (
    'STATE_DIR="$FAKE_HERDR_STATE_DIR"\n'
    'mkdir -p "$STATE_DIR"\n'
    'CALL_COUNTER_FILE="$STATE_DIR/api_snapshot_calls"\n'
    'if [ "$1" = "--session" ]; then\n'
    '  touch "$STATE_DIR/$2.session"\n'
    "  sleep 300\n"
    "  exit 0\n"
    "fi\n"
    'case "$1 $2" in\n'
    '  "status server")\n'
    "    exit 0\n"
    "    ;;\n"
    "esac\n"
    'case "$1" in\n'
    "  session)\n"
    '    case "$2" in\n'
    "      list)\n"
    '        out="{\\"sessions\\":["\n'
    "        first=1\n"
    '        for f in "$STATE_DIR"/*.session; do\n'
    '          [ -e "$f" ] || continue\n'
    '          name=$(basename "$f" .session)\n'
    '          if [ -e "$STATE_DIR/$name.stopped" ]; then running=false; else running=true; fi\n'
    '          if [ $first -eq 0 ]; then out="$out,"; fi\n'
    '          out="$out{\\"name\\":\\"$name\\",\\"running\\":$running}"\n'
    "          first=0\n"
    "        done\n"
    '        out="$out]}"\n'
    '        echo "$out"\n'
    "        exit 0\n"
    "        ;;\n"
    '      stop) touch "$STATE_DIR/$3.stopped"; exit 0 ;;\n'
    '      delete) rm -f "$STATE_DIR/$3.session" "$STATE_DIR/$3.stopped"; exit 0 ;;\n'
    "    esac\n"
    "    ;;\n"
    "  workspace)\n"
    '    case "$2" in\n'
    "      create)\n"
    '        touch "$STATE_DIR/${HERDR_SESSION}.session"\n'
    "        echo '{\"result\":{\"root_pane\":{\"pane_id\":\"pane-xyz\"},\"workspace\":"
    "{\"workspace_id\":\"w1\"}}}'\n"
    "        exit 0\n"
    "        ;;\n"
    "    esac\n"
    "    ;;\n"
    "  agent)\n"
    '    case "$2" in\n'
    "      start) exit 0 ;;\n"
    "      prompt) exit 0 ;;\n"
    "      get) echo '{\"state\":\"idle\"}'; exit 0 ;;\n"
    "      explain) echo '{\"agent\":\"claude\",\"confidence\":\"high\"}'; exit 0 ;;\n"
    '      read) echo "OBSERVED_MARKER pane transcript line"; exit 0 ;;\n'
    "    esac\n"
    "    ;;\n"
    "  api)\n"
    '    case "$2" in\n'
    f"{_POISON_SNAPSHOT_CASE}\n"
    "    esac\n"
    "    ;;\n"
    "esac\n"
    "exit 0\n"
)


def test_given_ambient_focus_poisoned_during_run_when_interactive_lane_runs_then_fails_closed(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _POISON_HERDR_BODY)
    _write_fake_exe(
        fake_bin / "claude",
        'if [ "$1" = "--help" ]; then exit 0; fi\nexit 0\n',
    )
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "herdr workspace/agent/focus snapshot not preserved" in result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "herdr_workspace_snapshot_preserved: False" in summary


def test_given_stable_ambient_focus_when_interactive_lane_runs_then_workspace_snapshot_preserved(
    repo_with_worktree, tmp_path
):
    """Positive control for the poison test above: an unchanging ``api
    snapshot`` answer across before/after must exit 0 with
    ``herdr_workspace_snapshot_preserved: True``."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    assert _POISON_SNAPSHOT_CASE in _POISON_HERDR_BODY
    stable_body = _POISON_HERDR_BODY.replace(_POISON_SNAPSHOT_CASE, _STABLE_SNAPSHOT_CASE)
    _write_fake_exe(fake_bin / "herdr", stable_body)
    _write_fake_exe(
        fake_bin / "claude",
        'if [ "$1" = "--help" ]; then exit 0; fi\nexit 0\n',
    )
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "herdr_workspace_snapshot_preserved: True" in summary

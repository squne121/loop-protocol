#!/usr/bin/env python3
"""worktree_scope_guard_testkit.py — shared pytest fixtures/helpers for the
worktree_scope_guard test family (Issue #1657 AC8).

These helpers were originally defined only inside `test_worktree_scope_guard.py`
and reused by sibling test modules (`.claude/hooks/tests/test_issue1215_*.py`,
`test_issue1241_hook_repair_hint.py`) via a `from test_worktree_scope_guard
import ...` bare test-to-test import that only worked because
`.claude/hooks/tests/conftest.py` injected `tests/agent_guards` onto
`sys.path` ahead of pytest's own collection of `test_worktree_scope_guard.py`
as a module — a collection-order-fragile arrangement under
`--import-mode=importlib`. This module extracts the shared, non-test-specific
helpers so every consumer (old and new) imports them explicitly from a
dedicated helper module instead of from another test file.

Path anchoring: this module resolves the guard scripts via `__file__`
(worktree-local), NOT via `git rev-parse --show-toplevel`. Inside a linked
git worktree, `git rev-parse --show-toplevel` returns THAT worktree's own
toplevel — not the main repository root (see
`test_git_rev_parse_show_toplevel_returns_linked_worktree_root` in
`test_worktree_scope_guard.py` for a regression test of this exact
distinction; an earlier revision of this comment incorrectly claimed the
opposite).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# tests/agent_guards/worktree_scope_guard_testkit.py -> repo root is two
# directories up (tests/agent_guards/<file> -> tests/ -> repo root).
_THIS_FILE = Path(__file__).resolve()
REPO_ROOT = _THIS_FILE.parent.parent.parent
GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"
GUARD_PY = REPO_ROOT / "scripts" / "agent-guards" / "worktree_scope_guard.py"
CODEX_APPLY_PATCH_ADAPTER_PY = REPO_ROOT / "scripts" / "agent-guards" / "codex_apply_patch_adapter.py"
SETTINGS_JSON = REPO_ROOT / ".claude" / "settings.json"


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_repo_with_worktree(tmp_path: Path, issue: str = "942", slug: str = "x", extra_worktrees=None) -> dict:
    """Create a git repo + a real issue worktree. Returns dict with paths."""
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=main)
    (main / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n")
    (main / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=main)
    _git("commit", "-q", "-m", "seed", cwd=main)

    worktrees = {}
    wt_path = main / ".claude" / "worktrees" / f"issue-{issue}-{slug}"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"issue-{issue}-{slug}"
    _git("branch", branch, cwd=main)
    _git("worktree", "add", "-q", str(wt_path), branch, cwd=main)
    worktrees[issue] = wt_path

    for extra in extra_worktrees or []:
        ei, es = extra
        ewt = main / ".claude" / "worktrees" / f"issue-{ei}-{es}"
        eb = f"issue-{ei}-{es}"
        _git("branch", eb, cwd=main)
        _git("worktree", "add", "-q", str(ewt), eb, cwd=main)
        worktrees[ei] = ewt

    return {"root": main, "worktree": wt_path, "worktrees": worktrees}


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _run_guard_script(argv: list[str], payload: dict, project_root: Path, issue: str | None = None, extra_env: dict | None = None):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    if issue is not None:
        env["LOOP_ISSUE_NUMBER"] = str(issue)
    else:
        env.pop("LOOP_ISSUE_NUMBER", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        argv,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )


def _run_guard(payload: dict, project_root: Path, issue: str | None = None, extra_env: dict | None = None):
    """Run worktree_scope_guard.sh — the Claude Code Bash/Write/Edit adapter."""
    return _run_guard_script(["bash", str(GUARD_SH)], payload, project_root, issue, extra_env)


def _run_codex_apply_patch_adapter(payload: dict, project_root: Path, issue: str | None = None, extra_env: dict | None = None):
    """Run codex_apply_patch_adapter.py — mirrors `.codex/hooks.json`'s
    `apply_patch|Edit|Write` matcher invocation (`python3 codex_apply_patch_adapter.py`)."""
    return _run_guard_script(["python3", str(CODEX_APPLY_PATCH_ADAPTER_PY)], payload, project_root, issue, extra_env)


def _bash_payload(command: str, cwd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _apply_patch_payload(command: str, cwd: str) -> dict:
    return {"tool_name": "apply_patch", "tool_input": {"command": command}, "cwd": cwd}


def _write_tool_payload(tool_name: str, file_path: str, cwd: str) -> dict:
    return {"tool_name": tool_name, "tool_input": {"file_path": file_path}, "cwd": cwd}

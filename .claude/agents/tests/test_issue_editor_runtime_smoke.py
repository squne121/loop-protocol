"""Issue #1734 AC7: runtime smoke evidence that issue-editor's canonical Skill
body (edit-issue/SKILL.md) is actually read by a fresh Claude Code session.

This is a pytest wrapper around `.claude/skills/worktree-agent-runtime-smoke`
(`scripts/agent-ops/run_worktree_agent_runtime_smoke.py`). It does not fabricate
runtime evidence: a real `claude -p` structured-lane subprocess is launched
against the linked worktree, and the test's outcome is derived strictly from
the runner's exit code and its persisted `summary.md` evidence file.

Runtime Verification Applicability (live Issue #1734 body): decision=immediate,
applicable_acs=[AC7]. Per `docs/dev/runtime-verification-policy.md`, an
unavailable runtime/capability is SKIP (exit 77), never promoted to PASS.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RUNNER = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
WORKTREE = REPO_ROOT / ".claude" / "worktrees" / "issue-1734-issue-creator-editor-split"

EXPECT_MARKER = "RUNTIME_SMOKE_ISSUE_EDITOR_READ_OK"

PROMPT = f"""You are running as a bounded, non-interactive runtime smoke check.

Use the Read tool to read the file `.claude/skills/edit-issue/SKILL.md`
(repo-relative to your current working directory) in full, then use the Read
tool to read one file referenced from it under `.claude/skills/edit-issue/`
(for example `.claude/skills/edit-issue/scripts/edit_issue_txn.py`).

After both reads succeed, output the single literal line:
{EXPECT_MARKER}

Do not output anything else after that line. Do not attempt to edit, write,
or mutate any file.
"""


def test_canonical_skill_read_smoke():
    """AC7: real read of edit-issue/SKILL.md and a referenced file, observed
    via a fresh Claude Code structured-lane runtime smoke subprocess.
    """
    if not WORKTREE.is_dir():
        pytest.skip(f"linked worktree not present: {WORKTREE}")

    with tempfile.TemporaryDirectory(prefix="runtime-smoke-1734-") as tmp:
        tmp_path = Path(tmp)
        prompt_file = tmp_path / "issue-editor-smoke-prompt.md"
        prompt_file.write_text(PROMPT, encoding="utf-8")

        output_dir = tmp_path / "output" / f"issue-editor-smoke-{int(time.time())}"

        argv = [
            sys.executable,
            str(RUNNER),
            "--runtime",
            "claude",
            "--mode",
            "structured",
            "--worktree",
            str(WORKTREE),
            "--prompt-file",
            str(prompt_file),
            "--output-dir",
            str(output_dir),
            "--timeout-seconds",
            "180",
            "--max-turns",
            "8",
            "--expect-marker",
            EXPECT_MARKER,
            "--require-clean-postcondition",
        ]

        result = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )

        if result.returncode == 77:
            pytest.skip(
                "worktree-agent-runtime-smoke SKIP (exit 77): capability/auth/"
                f"herdr unavailable. stderr={result.stderr.strip()[-2000:]}"
            )

        assert result.returncode == 0, (
            "worktree-agent-runtime-smoke did not report success "
            f"(exit={result.returncode}).\nstdout={result.stdout[-2000:]}\n"
            f"stderr={result.stderr[-2000:]}"
        )

        summary_path = output_dir / "summary.md"
        assert summary_path.is_file(), f"expected persisted evidence file at {summary_path}"
        summary_text = summary_path.read_text(encoding="utf-8")
        assert summary_text.strip(), "summary.md must not be empty"

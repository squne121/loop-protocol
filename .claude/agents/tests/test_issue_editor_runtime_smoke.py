"""Issue #1734 AC7 (fix_delta 3) / Issue #2046 AC7: runtime smoke evidence
that `issue-editor` is actually launched as the active Claude Code session
persona (`claude --agent issue-editor -p ...`), and that its canonical
Skill body (edit-issue/SKILL.md) and a referenced file are actually read in
that persona-bound session.

This is a pytest wrapper around `.claude/skills/worktree-agent-runtime-smoke`
(`scripts/agent-ops/run_worktree_agent_runtime_smoke.py`). It does not fabricate
runtime evidence: a real `claude --agent issue-editor -p` structured-lane
subprocess is launched against a linked worktree via the runner's opt-in
`--claude-agent-name` flag, and the test's outcome is derived strictly from
the runner's exit code and its persisted `summary.md` evidence file. Static
declaration of `--agent-type` alone (without `--claude-agent-name`) does not
bind any persona to the real CLI process and is not sufficient evidence for
this AC.

Issue #2046 AC8: the target worktree is no longer a fixed reference to any
single historical Issue's worktree (the prior
`.claude/worktrees/issue-1734-issue-creator-editor-split` constant). It is
resolved from an explicitly-declared "current candidate worktree" -- the
`RUNTIME_SMOKE_CANDIDATE_WORKTREE` environment variable, set by whichever
caller knows which linked worktree is actually under test (this is a
real-runtime lane and needs a real, full repository checkout for the
persona to Read from). Auto-discovering an arbitrary worktree under
`.claude/worktrees/` was deliberately rejected: a machine may have
unrelated, stray linked worktrees left over from other sessions, and
silently launching a real Claude Code process against one of those would be
exactly the kind of undeclared side effect this Issue's evidence-hygiene
discipline exists to prevent. SKIP (never FAIL) when the env var is unset
or does not point at a linked worktree -- the common case for a fresh CI
checkout.

Runtime Verification Applicability (live Issue #1734 body / Issue #2046
body): decision=immediate. Per `docs/dev/runtime-verification-policy.md`, an
unavailable runtime/capability is SKIP (exit 77), never promoted to PASS.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RUNNER = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"

_CANDIDATE_WORKTREE_ENV_VAR = "RUNTIME_SMOKE_CANDIDATE_WORKTREE"

EXPECT_MARKER = "RUNTIME_SMOKE_ISSUE_EDITOR_READ_OK"

AUTO_MODE_CANARY = REPO_ROOT / "scripts" / "claude-gpt" / "auto_mode_canary.py"


def _load_auto_mode_canary():
    spec = importlib.util.spec_from_file_location("issue_editor_auto_mode_canary", AUTO_MODE_CANARY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


auto_mode_canary = _load_auto_mode_canary()

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


def _resolve_candidate_worktree() -> Path | None:
    """Issue #2046 AC8: an explicitly-declared current candidate worktree
    -- never an auto-discovered, arbitrary worktree. Returns ``None`` (never
    a guess) when the env var is unset or the path is not a real worktree
    checkout."""
    raw = os.environ.get(_CANDIDATE_WORKTREE_ENV_VAR)
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_dir() and (candidate / ".git").exists():
        return candidate
    return None


def test_canonical_skill_read_smoke():
    """AC7: real read of edit-issue/SKILL.md and a referenced file, observed
    via a fresh Claude Code structured-lane runtime smoke subprocess. Issue
    #2046 additionally asserts the new main_agent_identity/skill_evidence
    fields are present in the persisted evidence.
    """
    worktree = _resolve_candidate_worktree()
    if worktree is None:
        pytest.skip(
            f"real-runtime lane requires {_CANDIDATE_WORKTREE_ENV_VAR} to point at "
            "an explicit linked worktree; unset or invalid here"
        )

    with tempfile.TemporaryDirectory(prefix="runtime-smoke-issue-editor-") as tmp:
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
            str(worktree),
            "--prompt-file",
            str(prompt_file),
            "--output-dir",
            str(output_dir),
            "--timeout-seconds",
            "180",
            "--max-turns",
            "8",
            "--agent-type",
            "issue-editor",
            "--claude-agent-name",
            "issue-editor",
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
        # Issue #2046 AC1/AC4/AC7: the SKIP path above is never promoted to
        # this assertion -- these run only on a genuine exit 0 (real runtime
        # capability confirmed available and successfully exercised).
        assert "main_agent_identity" in summary_text
        assert "skill_evidence" in summary_text


def test_claude_gpt_auto_issue_editor_permission_request_canary():
    """AC3: consume the canonical actual Auto canary runner unchanged."""
    worktree = _resolve_candidate_worktree()
    if worktree is None:
        pytest.skip(
            f"real Auto lane requires {_CANDIDATE_WORKTREE_ENV_VAR} to point at "
            "the explicit linked worktree"
        )
    if os.environ.get(auto_mode_canary.ISSUE_EDITOR_PERMISSION_CANARY_OPT_IN_ENV) != "1":
        pytest.skip(
            "real Claude-GPT Auto lane requires explicit "
            f"{auto_mode_canary.ISSUE_EDITOR_PERMISSION_CANARY_OPT_IN_ENV}=1"
        )

    rc, detail = auto_mode_canary.run_issue_editor_permission_request_canary(worktree)
    if rc == auto_mode_canary.EXIT_SKIP:
        pytest.skip(f"Claude-GPT Auto capability unavailable: {detail.get('skip_reason', 'unknown')}")
    assert rc == auto_mode_canary.EXIT_OK, detail

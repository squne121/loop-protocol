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

import hashlib
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

_PERMISSION_CANARY_OPT_IN_ENV_VAR = "CLAUDE_GPT_ISSUE_EDITOR_PERMISSION_CANARY"
_PERMISSION_CANARY_MARKER = "ISSUE_EDITOR_PERMISSION_CANARY_ENTRYPOINT_REACHED"
_PERMISSION_CANARY_INPUT = ".claude/agents/tests/test_issue_editor_runtime_smoke.py"
_PERMISSION_CANARY_COMMAND = (
    "uv run --locked python3 .claude/skills/edit-issue/scripts/edit_issue_txn.py "
    f"--input-file {_PERMISSION_CANARY_INPUT}"
)
CLAUDE_GPT_LAUNCHER = REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"

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


def _digest_runtime_output(stdout: str, stderr: str) -> str:
    """Return a diagnostic that cannot disclose runtime transcript content."""
    return hashlib.sha256((stdout + "\n" + stderr).encode("utf-8")).hexdigest()[:16]


def _stream_json_has_tool_use(stdout: str, tool_name: str, **input_values: object) -> bool:
    """Inspect structured runtime events in memory without persisting them."""
    import json

    def walk(node: object) -> bool:
        if isinstance(node, dict):
            tool_input = node.get("input")
            if node.get("name") == tool_name and isinstance(tool_input, dict):
                if all(tool_input.get(key) == value for key, value in input_values.items()):
                    return True
            return any(walk(value) for value in node.values())
        if isinstance(node, list):
            return any(walk(value) for value in node)
        return False

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if walk(event):
            return True
    return False


def test_claude_gpt_auto_issue_editor_permission_request_canary():
    """AC3: real Auto parent -> issue-editor -> canonical Bash -> helper entrypoint.

    This external-runtime lane is deliberately opt-in and never writes a
    transcript or credential/config material.  Its input is an existing Python
    test source, so the helper must reject JSON parsing before any GitHub
    operation; seeing ``failed_no_mutation`` is the entrypoint observation.
    """
    worktree = _resolve_candidate_worktree()
    if worktree is None:
        pytest.skip(
            f"real Auto lane requires {_CANDIDATE_WORKTREE_ENV_VAR} to point at "
            "the explicit linked worktree"
        )
    if os.environ.get(_PERMISSION_CANARY_OPT_IN_ENV_VAR) != "1":
        pytest.skip(
            f"real Claude-GPT Auto lane requires explicit {_PERMISSION_CANARY_OPT_IN_ENV_VAR}=1"
        )
    if not CLAUDE_GPT_LAUNCHER.is_file():
        pytest.skip("Claude-GPT launcher is unavailable")

    child_prompt = f"""You are the issue-editor child in a bounded, non-interactive permission canary.

Use the Bash tool exactly once with this exact command and no shell operators:
{_PERMISSION_CANARY_COMMAND}

The input is intentionally not JSON. Confirm the helper's failed_no_mutation
result, then output exactly this marker and nothing else:
{_PERMISSION_CANARY_MARKER}

Do not edit files, invoke gh, inspect credentials/configuration, delegate, or
attempt any fallback or direct invocation."""
    prompt = f"""You are the bounded, non-interactive Claude-GPT Auto parent for a permission canary.

Use the Agent tool exactly once to delegate to subagent_type `issue-editor`.
Pass the following child instructions verbatim:

--- CHILD INSTRUCTIONS BEGIN ---
{child_prompt}
--- CHILD INSTRUCTIONS END ---

Do not use Bash, invoke gh, inspect credentials/configuration, edit files, or
attempt a fallback/direct execution yourself. After the child returns its exact
marker, output exactly this marker and nothing else:
{_PERMISSION_CANARY_MARKER}"""
    try:
        result = subprocess.run(
            [
                str(CLAUDE_GPT_LAUNCHER),
                "--",
                "--output-format",
                "stream-json",
                "--verbose",
                "-p",
                prompt,
            ],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=360,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("Claude-GPT Auto runtime timed out; capability is unavailable")

    output_digest = _digest_runtime_output(result.stdout, result.stderr)
    if result.returncode in (3, 4, 7, 8):
        pytest.skip(f"Claude-GPT Auto capability unavailable (launcher exit {result.returncode}, digest={output_digest})")

    assert result.returncode == 0, f"Auto canary failed (exit={result.returncode}, digest={output_digest})"
    # stream-json is inspected in memory only; tests never save the raw output.
    assert _stream_json_has_tool_use(result.stdout, "Agent", subagent_type="issue-editor"), (
        f"parent-to-issue-editor delegation missing (digest={output_digest})"
    )
    assert _stream_json_has_tool_use(result.stdout, "Bash", command=_PERMISSION_CANARY_COMMAND), (
        f"canonical child Bash event missing (digest={output_digest})"
    )
    assert "failed_no_mutation" in result.stdout, f"helper entrypoint was not observed (digest={output_digest})"
    assert _PERMISSION_CANARY_MARKER in result.stdout, f"canary marker missing (digest={output_digest})"

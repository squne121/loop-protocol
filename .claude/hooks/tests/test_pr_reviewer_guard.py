#!/usr/bin/env python3
"""test_pr_reviewer_guard.py -- Structural/behavioral tests for the
`pr-reviewer` agent-scoped mutation guardrail (Issue #1881).

AC1: `.claude/settings.json` no longer has a blanket `Read(**/.claude/**)`
     deny, while explicit sensitive-file denies remain.
AC2: `pr-reviewer.md` frontmatter wires an `agent_type == pr-reviewer`
     scoped `PreToolUse` hook for each canonical mutation command family,
     and the global `secret_boundary_guard.sh` is untouched.
AC3: read-only / non-mutation commands do not match any of the guard's
     `if` rules (non-interference), and a non-`pr-reviewer` agent_type is
     never denied by `pr_reviewer_guard.py deny`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# Test file lives at <worktree>/.claude/hooks/tests/test_pr_reviewer_guard.py
_THIS_FILE = Path(__file__).resolve()
REPO_ROOT = _THIS_FILE.parent.parent.parent.parent

SETTINGS_JSON_PATH = REPO_ROOT / ".claude" / "settings.json"
PR_REVIEWER_MD_PATH = REPO_ROOT / ".claude" / "agents" / "pr-reviewer.md"
GUARD_SCRIPT_PATH = REPO_ROOT / ".claude" / "hooks" / "pr_reviewer_guard.py"
SECRET_BOUNDARY_GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "secret_boundary_guard.sh"

# sha256 of .claude/hooks/secret_boundary_guard.sh on current main (unchanged
# by this Issue -- Out of Scope). If this hash ever legitimately needs to
# change, that change must happen in a different, dedicated Issue/PR, not
# silently alongside this guard.
SECRET_BOUNDARY_GUARD_SHA256 = (
    "089145e4f3b662b37e26a5ddb76ffdaef3b8dd9fa496dcfb17ffb43d117206bd"
)

REQUIRED_CANONICAL_MUTATION_RULES = {
    "Bash(git commit *)",
    "Bash(git push *)",
    "Bash(git worktree *)",
    "Bash(gh pr review *)",
    "Bash(gh pr comment *)",
    "Bash(gh pr merge *)",
    "Bash(gh issue edit *)",
    "Bash(gh issue comment *)",
    "Bash(gh issue close *)",
}

# Representative non-interference commands (AC3): must never match any of
# the registered `if` rules above.
NON_INTERFERENCE_COMMANDS = [
    "git status",
    "git status --porcelain",
    "git diff",
    "git diff --stat HEAD",
    "git log --oneline -5",
    "git show HEAD",
    "gh pr view 1881",
    "gh pr diff 1881",
    "gh pr checks 1881",
    "gh issue view 1881",
    "uv run --locked pytest .claude/hooks/tests/test_pr_reviewer_guard.py -q",
    "uv run --locked python3 .claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py --pr-number 1881",
]


def _extract_frontmatter_text(markdown_text: str) -> str:
    match = re.match(r"^---\n(.*?)\n---\n", markdown_text, re.DOTALL)
    assert match is not None, "frontmatter delimiters (---) not found"
    return match.group(1)


def _parse_frontmatter(markdown_text: str) -> dict[str, Any]:
    return yaml.safe_load(_extract_frontmatter_text(markdown_text))


def _bash_rule_matches(rule: str, command: str) -> bool:
    """Mimic Claude Code's `Bash(<pattern>)` permission-rule matching for
    test purposes only (this is NOT part of the shipped enforcement path --
    that classification is delegated entirely to the platform's native
    hook `if`/`matcher` engine, per Issue #1881's explicit constraint
    against implementing a custom shell parser in the guard script)."""
    inner = re.fullmatch(r"Bash\((.*)\)", rule)
    assert inner is not None, f"unexpected rule shape: {rule!r}"
    pattern = inner.group(1)
    return fnmatch.fnmatchcase(command, pattern)


# ─── AC1: settings.json deny list ──────────────────────────────────────────


class TestSettingsDenyList:
    def test_no_blanket_claude_read_deny(self) -> None:
        settings = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        assert "Read(**/.claude/**)" not in deny

    def test_explicit_sensitive_read_denies_remain(self) -> None:
        settings = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        expected_remaining = [
            "Read(.env)",
            "Read(.env.*)",
            "Read(./.env)",
            "Read(./.env.*)",
            "Read(secrets/**)",
            "Read(./secrets/**)",
            "Read(**/.netrc)",
            "Read(**/.npmrc)",
            "Read(**/.pypirc)",
            "Read(**/credentials)",
            "Read(**/settings.local.json)",
            "Read(**/.ssh/**)",
            "Read(**/.config/gh/**)",
        ]
        for entry in expected_remaining:
            assert entry in deny, f"expected sensitive deny missing: {entry!r}"


# ─── AC2: frontmatter hook wiring ───────────────────────────────────────────


class TestFrontmatterHookWiring:
    def test_frontmatter_hook_wiring(self) -> None:
        text = PR_REVIEWER_MD_PATH.read_text(encoding="utf-8")
        frontmatter = _parse_frontmatter(text)

        hooks = frontmatter.get("hooks")
        assert isinstance(hooks, dict), f"hooks frontmatter missing/malformed: {hooks!r}"
        pre_tool_use = hooks.get("PreToolUse")
        assert isinstance(pre_tool_use, list) and pre_tool_use, "PreToolUse hooks missing"

        observed_rules: dict[str, dict[str, Any]] = {}
        for entry in pre_tool_use:
            rule = entry.get("if")
            assert isinstance(rule, str), f"hook entry missing 'if' condition: {entry!r}"
            observed_rules[rule] = entry

        assert set(observed_rules.keys()) == REQUIRED_CANONICAL_MUTATION_RULES, (
            f"canonical mutation command families mismatch: {sorted(observed_rules.keys())}"
        )

        for rule, entry in observed_rules.items():
            inner_hooks = entry.get("hooks")
            assert isinstance(inner_hooks, list) and len(inner_hooks) == 1, (
                f"rule {rule!r} must wire exactly one command hook"
            )
            hook_def = inner_hooks[0]
            assert hook_def.get("type") == "command"
            assert hook_def.get("command", "").endswith(
                "/.claude/hooks/pr_reviewer_guard.py"
            ), f"rule {rule!r} does not invoke pr_reviewer_guard.py: {hook_def!r}"
            assert hook_def.get("args") == ["deny"], (
                f"rule {rule!r} must invoke the 'deny' subcommand: {hook_def!r}"
            )

    def test_frontmatter_wires_identity_and_reference_read_observability(self) -> None:
        text = PR_REVIEWER_MD_PATH.read_text(encoding="utf-8")
        frontmatter = _parse_frontmatter(text)
        hooks = frontmatter["hooks"]

        session_start = hooks.get("SessionStart")
        assert isinstance(session_start, list) and session_start
        identity_hook = session_start[0]["hooks"][0]
        assert identity_hook["command"].endswith("/.claude/hooks/pr_reviewer_guard.py")
        assert identity_hook["args"] == ["observe-identity"]

        post_tool_use = hooks.get("PostToolUse")
        assert isinstance(post_tool_use, list) and post_tool_use
        read_entry = post_tool_use[0]
        assert read_entry["matcher"] == "Read"
        reference_hook = read_entry["hooks"][0]
        assert reference_hook["command"].endswith("/.claude/hooks/pr_reviewer_guard.py")
        assert reference_hook["args"] == ["observe-reference-read"]

    def test_secret_boundary_guard_unchanged(self) -> None:
        digest = hashlib.sha256(SECRET_BOUNDARY_GUARD_PATH.read_bytes()).hexdigest()
        assert digest == SECRET_BOUNDARY_GUARD_SHA256, (
            "secret_boundary_guard.sh changed -- Out of Scope for Issue #1881"
        )

    def test_secret_boundary_guard_settings_wiring_unchanged(self) -> None:
        settings = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
        pre_tool_use = settings["hooks"]["PreToolUse"]
        secret_entries = [
            entry
            for entry in pre_tool_use
            if any(
                "secret_boundary_guard.sh" in h.get("command", "")
                for h in entry.get("hooks", [])
            )
        ]
        assert len(secret_entries) == 1
        entry = secret_entries[0]
        assert entry["matcher"] == "Bash|Read|Write|Edit|Grep|Glob|MultiEdit"
        assert entry["hooks"][0]["timeout"] == 10
        assert "if" not in entry, "global secret hook must not gain agent-scoped if conditions"


# ─── AC3: non-interference ──────────────────────────────────────────────────


class TestNonInterference:  # -k non_interference (see method name prefixes below)
    def test_non_interference_read_only_commands_do_not_match_any_mutation_rule(self) -> None:
        for command in NON_INTERFERENCE_COMMANDS:
            for rule in REQUIRED_CANONICAL_MUTATION_RULES:
                assert not _bash_rule_matches(rule, command), (
                    f"non-interference violation: {command!r} matches {rule!r}"
                )

    def _run_guard(self, subcommand: str, payload: dict[str, Any] | None) -> subprocess.CompletedProcess:
        stdin_data = json.dumps(payload) if payload is not None else ""
        return subprocess.run(
            [sys.executable, str(GUARD_SCRIPT_PATH), subcommand],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_non_interference_deny_blocks_pr_reviewer_pretooluse(self) -> None:
        result = self._run_guard(
            "deny",
            {"hook_event_name": "PreToolUse", "agent_type": "pr-reviewer"},
        )
        assert result.returncode == 2
        assert result.stderr.strip() != ""

    def test_non_interference_deny_ignores_non_pr_reviewer_agent_type(self) -> None:
        for other_agent in ["implementation-worker", "issue-author", "test-runner", None]:
            payload: dict[str, Any] = {"hook_event_name": "PreToolUse"}
            if other_agent is not None:
                payload["agent_type"] = other_agent
            result = self._run_guard("deny", payload)
            assert result.returncode == 0, (
                f"non-pr-reviewer agent_type {other_agent!r} must not be denied"
            )

    def test_non_interference_deny_ignores_non_pretooluse_event(self) -> None:
        result = self._run_guard(
            "deny",
            {"hook_event_name": "SessionStart", "agent_type": "pr-reviewer"},
        )
        assert result.returncode == 0

    def test_non_interference_deny_malformed_stdin_does_not_block(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT_PATH), "deny"],
            input="not-json{{{",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0

    def test_non_interference_observe_identity_silent_without_probe_env(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT_PATH), "observe-identity"],
            input=json.dumps({"hook_event_name": "SessionStart", "agent_type": "pr-reviewer"}),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_non_interference_observe_identity_emits_sanitized_marker_with_probe_env(self) -> None:
        import os

        env = dict(os.environ)
        env["LOOP_PR_REVIEWER_RUNTIME_PROBE"] = "1"
        result = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT_PATH), "observe-identity"],
            input=json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "agent_type": "pr-reviewer",
                    "session_id": "should-not-appear",
                    "transcript_path": "/should/not/appear",
                }
            ),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0
        assert "reviewer-identity-observed" in result.stdout
        assert "agent_type=pr-reviewer" in result.stdout
        assert "should-not-appear" not in result.stdout
        assert "/should/not/appear" not in result.stdout

    def test_non_interference_observe_reference_read_matches_exact_canonical_path_only(self) -> None:
        import os

        env = dict(os.environ)
        env["LOOP_PR_REVIEWER_RUNTIME_PROBE"] = "1"

        matching = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT_PATH), "observe-reference-read"],
            input=json.dumps(
                {
                    "tool_name": "Read",
                    "tool_input": {
                        "file_path": str(
                            REPO_ROOT
                            / ".claude/skills/pr-review-judge/references/allowed-paths-gate.md"
                        )
                    },
                    "agent_type": "pr-reviewer",
                }
            ),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert matching.returncode == 0
        assert "reviewer-reference-read-ok" in matching.stdout

        non_matching = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT_PATH), "observe-reference-read"],
            input=json.dumps(
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/some/other/file.md"},
                    "agent_type": "pr-reviewer",
                }
            ),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert non_matching.returncode == 0
        assert non_matching.stdout == ""

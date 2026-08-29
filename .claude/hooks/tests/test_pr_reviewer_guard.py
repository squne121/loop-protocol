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

PR #2385 review fix_delta (P0/P1-1/P1-2/P1-3):
- P0: frontmatter restructured to a single `matcher: "Bash"` group with 9
  hook handler objects, each carrying its own `if` (official Claude Code
  hooks schema places `if` on the individual hook handler, not on the
  outer matcher-group object).
- P1-1: the guard's `deny` subcommand now inspects the actual
  `tool_input.command` via an anchored regex allowlist and fails open
  (allow) when it cannot confidently classify the command as a canonical
  mutation attempt, since the frontmatter `if` alone is a best-effort
  filter that Claude Code fires conservatively on ambiguous input.
- P1-2: `git worktree` denial is scoped (guard-side) to
  add/remove/move/prune/repair/lock/unlock; `git worktree list` is a
  harmless read-only identity-check operation and is never denied.
- P1-3: the reference-read positive control requires an exact
  `agent_type == "pr-reviewer"` AND an exact resolved-path match (not a
  suffix/substring match).
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

# Representative real `tool_input.command` strings that the guard's own
# anchored regex (P1-1) must classify as a canonical mutation attempt and
# deny for a `pr-reviewer` agent_type, keyed by the frontmatter `if` rule
# that would fire first.
GUARD_DENIED_COMMAND_EXAMPLES = {
    "Bash(git commit *)": "git commit -m 'x' --allow-empty",
    "Bash(git push *)": "git push origin HEAD",
    "Bash(git worktree *)": "git worktree remove some-worktree",
    "Bash(gh pr review *)": "gh pr review 1 --comment --body 'x'",
    "Bash(gh pr comment *)": "gh pr comment 1 --body 'x'",
    "Bash(gh pr merge *)": "gh pr merge 1 --squash",
    "Bash(gh issue edit *)": "gh issue edit 1 --add-label x",
    "Bash(gh issue comment *)": "gh issue comment 1 --body 'x'",
    "Bash(gh issue close *)": "gh issue close 1",
}

# P1-2: `git worktree list` is read-only and must never be denied, even
# though the (deliberately broad) frontmatter `if: "Bash(git worktree *)"`
# still fires on it -- the guard's own anchored regex is the actual
# enforcement boundary and excludes `list`.
GUARD_ALLOWED_WORKTREE_COMMAND = "git worktree list"

# P1-1: ambiguous/ununderstandable commands the frontmatter `if` may
# conservatively fire the hook on, which the guard must still fail open on.
GUARD_ALLOWED_AMBIGUOUS_COMMANDS = [
    "git status",
    "$(echo git commit -m x)",
    "eval \"git push origin HEAD\"",
]

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
        """P0 (PR #2385 review, confirmed against official Claude Code hooks
        docs): `if` must live on each individual hook handler object inside
        a matcher group's `hooks[]` array, not on the outer matcher-group
        object. This asserts a single `matcher: "Bash"` PreToolUse group
        containing exactly 9 hook handler objects, each with its own
        `if`."""
        text = PR_REVIEWER_MD_PATH.read_text(encoding="utf-8")
        frontmatter = _parse_frontmatter(text)

        hooks = frontmatter.get("hooks")
        assert isinstance(hooks, dict), f"hooks frontmatter missing/malformed: {hooks!r}"
        pre_tool_use = hooks.get("PreToolUse")
        assert isinstance(pre_tool_use, list) and pre_tool_use, "PreToolUse hooks missing"

        bash_groups = [entry for entry in pre_tool_use if entry.get("matcher") == "Bash"]
        assert len(bash_groups) == 1, (
            f"expected exactly one matcher: Bash PreToolUse group, found "
            f"{len(bash_groups)}: {pre_tool_use!r}"
        )
        bash_group = bash_groups[0]
        assert "if" not in bash_group, (
            "matcher-group object itself must not carry 'if' -- 'if' belongs "
            "on each individual hook handler (official schema, P0)"
        )

        inner_hooks = bash_group.get("hooks")
        assert isinstance(inner_hooks, list) and len(inner_hooks) == 9, (
            f"matcher: Bash group must wire exactly 9 hook handlers "
            f"(one per canonical mutation command family): {inner_hooks!r}"
        )

        observed_rules: dict[str, dict[str, Any]] = {}
        for hook_def in inner_hooks:
            rule = hook_def.get("if")
            assert isinstance(rule, str), f"hook handler missing 'if' condition: {hook_def!r}"
            observed_rules[rule] = hook_def

        assert set(observed_rules.keys()) == REQUIRED_CANONICAL_MUTATION_RULES, (
            f"canonical mutation command families mismatch: {sorted(observed_rules.keys())}"
        )

        for rule, hook_def in observed_rules.items():
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

    def test_non_interference_deny_blocks_pr_reviewer_pretooluse_for_each_family(self) -> None:
        """P1-1: the guard must actually inspect tool_input.command -- a
        deny only fires when the real command matches one of the anchored
        canonical mutation patterns."""
        for rule, command in GUARD_DENIED_COMMAND_EXAMPLES.items():
            result = self._run_guard(
                "deny",
                {
                    "hook_event_name": "PreToolUse",
                    "agent_type": "pr-reviewer",
                    "tool_input": {"command": command},
                },
            )
            assert result.returncode == 2, f"expected deny for {rule!r} example {command!r}"
            assert result.stderr.strip() != ""

    def test_non_interference_deny_fails_open_without_matching_command(self) -> None:
        """P1-1: agent_type == pr-reviewer + PreToolUse alone is NOT enough
        -- without a tool_input.command that matches the anchored allowlist,
        the guard must fail open (allow), since blind-deny-on-identity-alone
        was the P1-1 bug."""
        result = self._run_guard(
            "deny",
            {"hook_event_name": "PreToolUse", "agent_type": "pr-reviewer"},
        )
        assert result.returncode == 0

    def test_non_interference_deny_fails_open_on_ambiguous_commands(self) -> None:
        """P1-1: commands the frontmatter `if` may conservatively fire the
        hook on, but which do not literally match the anchored allowlist,
        must fail open."""
        for command in GUARD_ALLOWED_AMBIGUOUS_COMMANDS:
            result = self._run_guard(
                "deny",
                {
                    "hook_event_name": "PreToolUse",
                    "agent_type": "pr-reviewer",
                    "tool_input": {"command": command},
                },
            )
            assert result.returncode == 0, f"expected fail-open allow for {command!r}"

    def test_non_interference_deny_allows_git_worktree_list(self) -> None:
        """P1-2: `git worktree list` is read-only and must never be denied,
        even for a `pr-reviewer` PreToolUse event."""
        result = self._run_guard(
            "deny",
            {
                "hook_event_name": "PreToolUse",
                "agent_type": "pr-reviewer",
                "tool_input": {"command": GUARD_ALLOWED_WORKTREE_COMMAND},
            },
        )
        assert result.returncode == 0

    def test_non_interference_deny_ignores_non_pr_reviewer_agent_type(self) -> None:
        for other_agent in ["implementation-worker", "issue-author", "test-runner", None]:
            payload: dict[str, Any] = {
                "hook_event_name": "PreToolUse",
                "tool_input": {"command": "git commit -m 'x' --allow-empty"},
            }
            if other_agent is not None:
                payload["agent_type"] = other_agent
            result = self._run_guard("deny", payload)
            assert result.returncode == 0, (
                f"non-pr-reviewer agent_type {other_agent!r} must not be denied"
            )

    def test_non_interference_deny_ignores_non_pretooluse_event(self) -> None:
        result = self._run_guard(
            "deny",
            {
                "hook_event_name": "SessionStart",
                "agent_type": "pr-reviewer",
                "tool_input": {"command": "git commit -m 'x' --allow-empty"},
            },
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
        assert "agent_type=pr-reviewer" in matching.stdout

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

    def test_non_interference_observe_reference_read_rejects_suffix_only_match(self) -> None:
        """P1-3: a path that merely ends with the canonical reference path
        (but is not exactly it, e.g. an unrelated prefix directory) must NOT
        emit the success marker."""
        import os

        env = dict(os.environ)
        env["LOOP_PR_REVIEWER_RUNTIME_PROBE"] = "1"

        suffix_only_path = str(
            Path("/some/unrelated/prefix-dir")
            / ".claude/skills/pr-review-judge/references/allowed-paths-gate.md"
        )
        result = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT_PATH), "observe-reference-read"],
            input=json.dumps(
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": suffix_only_path},
                    "agent_type": "pr-reviewer",
                }
            ),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_non_interference_observe_reference_read_requires_exact_agent_type(self) -> None:
        """P1-3: exact-path match alone is not enough -- a missing or
        non-`pr-reviewer` agent_type must never emit the success marker
        (previously defaulted to `agent_type=unknown` and still emitted)."""
        import os

        env = dict(os.environ)
        env["LOOP_PR_REVIEWER_RUNTIME_PROBE"] = "1"

        exact_path = str(
            REPO_ROOT / ".claude/skills/pr-review-judge/references/allowed-paths-gate.md"
        )
        for agent_type in [None, "unknown", "implementation-worker"]:
            payload: dict[str, Any] = {
                "tool_name": "Read",
                "tool_input": {"file_path": exact_path},
            }
            if agent_type is not None:
                payload["agent_type"] = agent_type
            result = subprocess.run(
                [sys.executable, str(GUARD_SCRIPT_PATH), "observe-reference-read"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            assert result.returncode == 0
            assert result.stdout == "", f"unexpected marker for agent_type={agent_type!r}"

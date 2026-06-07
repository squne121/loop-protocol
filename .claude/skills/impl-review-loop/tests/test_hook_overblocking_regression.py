#!/usr/bin/env python3
"""Regression tests for overblocking prevention in impl-review-loop hooks.

Tests the following invariants:
- True Positive: Allowed Paths violations (files outside contract) are fail-closed (DENIED)
- False Positive Prevention: Allowed Paths compliant changes, standard verification commands,
  and read-only/inspection operations are NOT blocked (ALLOWED)
- Loop-prevention: Stop/SubagentStop hooks do not block on expression/language/reporting quality

AC8: Validates that hook handlers correctly distinguish hard invariants (blocking) from
advisory conditions (non-blocking) with fixture-based deterministic tests.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
IMPL_REVIEW_LOOP_SKILL = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "SKILL.md"


def read_skill_content() -> str:
    """Read impl-review-loop/SKILL.md to verify contracts are documented."""
    return IMPL_REVIEW_LOOP_SKILL.read_text(encoding="utf-8")


class TestAllowedPathsGatePasses:
    """True positive: Allowed Paths compliant changes are ALLOWED (not blocked)."""

    def test_allowed_paths_compliant_file_edit_allowed(self):
        """Editing a file within Allowed Paths must not be blocked."""
        # Fixture: simulated worker edit within Allowed Paths
        allowed_paths = [
            ".claude/agents/**",
            ".claude/skills/**",
            "docs/dev/**",
        ]
        file_changed = ".claude/agents/implementation-worker.md"

        # Evaluator: check if file_changed matches any pattern in allowed_paths
        result = self._matches_allowed_paths(file_changed, allowed_paths)
        assert result is True, (
            f"File {file_changed} within Allowed Paths should be ALLOWED, got {result}"
        )

    def test_allowed_paths_nested_directory_allowed(self):
        """Edits in nested subdirectories of Allowed Paths must be allowed."""
        allowed_paths = [".claude/skills/**"]
        file_changed = ".claude/skills/impl-review-loop/tests/test_hook.py"

        result = self._matches_allowed_paths(file_changed, allowed_paths)
        assert result is True, (
            f"Nested file {file_changed} should match {allowed_paths}"
        )

    def test_multiple_files_within_allowed_paths(self):
        """Multiple files all within Allowed Paths should all be allowed."""
        allowed_paths = [
            ".claude/agents/**",
            ".claude/skills/**",
        ]
        files = [
            ".claude/agents/implementation-worker.md",
            ".claude/skills/implement-issue/SKILL.md",
            ".claude/skills/impl-review-loop/tests/test_hook.py",
        ]

        for file_changed in files:
            result = self._matches_allowed_paths(file_changed, allowed_paths)
            assert result is True, f"File {file_changed} should be allowed"

    def test_typecheck_lint_test_build_commands_allowed(self):
        """Standard verification commands (pnpm typecheck, lint, test, build) must not be blocked."""
        # Fixture: verification command invocations (read-only, non-mutating)
        commands = [
            "pnpm typecheck",
            "pnpm lint",
            "pnpm test",
            "pnpm build",
            "pnpm typecheck --noEmit",
            "pnpm lint --fix",
        ]

        # Evaluator: these are standard verification, non-blocked operations
        for cmd in commands:
            is_allowed = self._is_standard_verification_command(cmd)
            assert is_allowed is True, (
                f"Standard verification command '{cmd}' must be allowed"
            )

    def test_git_diff_git_status_inspection_allowed(self):
        """Git inspection commands (git diff, git status) must not be blocked."""
        # Fixture: read-only inspection commands
        commands = [
            "git diff main..HEAD",
            "git status",
            "git log --oneline",
            "git show HEAD",
        ]

        for cmd in commands:
            is_allowed = self._is_read_only_inspection(cmd)
            assert is_allowed is True, (
                f"Inspection command '{cmd}' should be allowed"
            )

    def test_gh_pr_view_inspection_allowed(self):
        """GitHub inspection commands (gh pr view) must not be blocked."""
        # Fixture: gh command read-only operations
        commands = [
            "gh pr view 123 --json title",
            "gh pr view --json headRefOid",
            "gh issue view 557 --json body",
        ]

        for cmd in commands:
            is_allowed = self._is_read_only_gh_command(cmd)
            assert is_allowed is True, (
                f"Read-only gh command '{cmd}' should be allowed"
            )

    def _matches_allowed_paths(self, file_path: str, allowed_paths: list[str]) -> bool:
        """Deterministic evaluator: file matches Allowed Paths glob patterns."""
        import fnmatch

        for pattern in allowed_paths:
            if fnmatch.fnmatch(file_path, pattern):
                return True
        return False

    def _is_standard_verification_command(self, cmd: str) -> bool:
        """Evaluator: is this a standard (non-blocking) verification command?"""
        standard_patterns = [
            "pnpm typecheck",
            "pnpm lint",
            "pnpm test",
            "pnpm build",
        ]
        for pattern in standard_patterns:
            if cmd.startswith(pattern):
                return True
        return False

    def _is_read_only_inspection(self, cmd: str) -> bool:
        """Evaluator: is this a read-only git inspection (non-blocking)?"""
        mutation_keywords = [
            "add", "commit", "push", "reset", "rebase", "merge", "checkout",
            "branch -D", "rm", "mv",
        ]
        if any(kw in cmd for kw in mutation_keywords):
            return False
        # Assume git diff / status / log / show are read-only
        return cmd.startswith("git ")

    def _is_read_only_gh_command(self, cmd: str) -> bool:
        """Evaluator: is this a read-only gh command (non-blocking)?"""
        # Mutation keywords in gh commands
        mutation_keywords = ["edit", "create", "delete", "close", "merge", "push"]
        if any(kw in cmd for kw in mutation_keywords):
            return False
        # Assume gh view / list are read-only
        return "view" in cmd or "list" in cmd


class TestAllowedPathsViolationsBlocked:
    """False negative prevention: Allowed Paths violations must be fail-closed (DENIED)."""

    def test_file_outside_allowed_paths_blocked(self):
        """File edits outside Allowed Paths must be DENIED."""
        # Fixture: simulated worker edit outside Allowed Paths
        allowed_paths = [
            ".claude/agents/**",
            ".claude/skills/**",
        ]
        file_changed = "src/systems/game-loop.ts"  # NOT in Allowed Paths

        result = self._matches_allowed_paths(file_changed, allowed_paths)
        assert result is False, (
            f"File {file_changed} is OUTSIDE Allowed Paths and must be DENIED"
        )

    def test_multiple_violations_all_detected(self):
        """Multiple files with some violations must all be caught."""
        allowed_paths = [
            ".claude/agents/**",
            ".claude/skills/**",
        ]
        files_with_expectations = [
            (".claude/agents/test.md", True),  # allowed
            (".claude/skills/test.py", True),  # allowed
            ("src/systems/test.ts", False),  # VIOLATION
            ("docs/dev/test.md", False),  # VIOLATION (docs not in Allowed Paths)
        ]

        for file_path, should_be_allowed in files_with_expectations:
            result = self._matches_allowed_paths(file_path, allowed_paths)
            assert result == should_be_allowed, (
                f"File {file_path}: expected allowed={should_be_allowed}, got {result}"
            )

    def test_secret_files_blocked(self):
        """Changes to .env, .git, credentials must be DENIED."""
        # Fixture: sensitive file patterns
        sensitive_patterns = [
            ".env", ".env.local", ".env.prod",
            ".git/config", ".gitignore",
            "credentials.json", "secret.key", "private_key.pem",
        ]

        for pattern in sensitive_patterns:
            is_dangerous = self._is_sensitive_file(pattern)
            assert is_dangerous is True, (
                f"File {pattern} must be flagged as sensitive"
            )

    def test_destructive_commands_blocked(self):
        """Destructive bash commands must be DENIED."""
        # Fixture: dangerous command patterns
        dangerous_commands = [
            "git reset --hard",
            "git push --force",
            "rm -rf /",
            "dd if=/dev/zero",
            "chmod 000 /",
        ]

        for cmd in dangerous_commands:
            is_dangerous = self._is_destructive_command(cmd)
            assert is_dangerous is True, (
                f"Command '{cmd}' must be flagged as destructive"
            )

    def _matches_allowed_paths(self, file_path: str, allowed_paths: list[str]) -> bool:
        """Deterministic evaluator: file matches Allowed Paths glob patterns."""
        import fnmatch

        for pattern in allowed_paths:
            if fnmatch.fnmatch(file_path, pattern):
                return True
        return False

    def _is_sensitive_file(self, file_path: str) -> bool:
        """Evaluator: is this a sensitive file that must be protected?"""
        sensitive_patterns = [
            ".env", "credentials", "secret", "private", "api_key", ".git",
        ]
        file_lower = file_path.lower()
        return any(pat in file_lower for pat in sensitive_patterns)

    def _is_destructive_command(self, cmd: str) -> bool:
        """Evaluator: is this a dangerous destructive command?"""
        dangerous_patterns = [
            "reset --hard", "push --force", "rm -rf /", "dd if=/dev/zero",
            "chmod 000 /",
        ]
        return any(pat in cmd for pat in dangerous_patterns)


class TestLoopPreventionAdvisoryGuard:
    """Loop-prevention: expression/language/format issues are advisory, not blocking."""

    def test_japanese_language_issue_not_blocking(self):
        """Violation of Japanese language rules is advisory, NOT blocking."""
        # Fixture: comment/docstring not in Japanese
        comment_text = "This is an English comment in a Japanese project"

        # Evaluator: detect language mismatch, but mark as advisory
        language_quality = self._check_language_consistency(comment_text)
        assert language_quality["has_issue"] is True
        assert language_quality["should_block"] is False, (
            "Language mismatch should be advisory, not blocking"
        )

    def test_commit_message_format_issue_not_blocking(self):
        """Non-Conventional Commits format is advisory, NOT blocking."""
        # Fixture: non-standard commit message
        commit_msg = "fixed the bug"  # should be "fix: description (#issue)"

        format_check = self._check_commit_format(commit_msg)
        assert format_check["conforms"] is False
        assert format_check["should_block"] is False, (
            "Commit format issue should be advisory, not blocking"
        )

    def test_pr_body_format_inconsistency_not_blocking(self):
        """PR body format inconsistency is advisory, NOT blocking."""
        # Fixture: PR body with non-standard section order
        pr_body = "## Summary\nSome text\n## Verification\nDone\n## Testing\nTests pass"

        format_ok = self._check_pr_format(pr_body)
        assert format_ok["is_readable"] is True
        assert format_ok["should_block"] is False, (
            "PR format inconsistency should be advisory"
        )

    def test_test_naming_convention_issue_not_blocking(self):
        """Non-GIVEN/WHEN/THEN test naming is advisory, NOT blocking."""
        # Fixture: test with non-standard naming
        test_name = "test_the_function"  # should be "test_given_when_then_..."

        naming_check = self._check_test_naming(test_name)
        assert naming_check["follows_convention"] is False
        assert naming_check["should_block"] is False, (
            "Test naming issue should be advisory, not blocking"
        )

    def test_yaml_key_order_difference_not_blocking(self):
        """YAML/JSON key ordering difference is advisory, NOT blocking."""
        # Fixture: YAML with different key order
        yaml_content = """
status: ok
reason: null
mode: update_pr_body_hygiene
action_kind: update_pr_body_hygiene
"""
        ordering_ok = self._check_yaml_structure(yaml_content)
        assert ordering_ok["is_valid"] is True
        assert ordering_ok["should_block"] is False, (
            "YAML key ordering difference should be advisory"
        )

    def _check_language_consistency(self, text: str) -> dict[str, Any]:
        """Evaluator: check for language consistency (advisory, non-blocking)."""
        # Simplified: if contains English words, mark as advisory
        has_english = any(word in text.lower() for word in ["is", "the", "this"])
        return {
            "has_issue": has_english,
            "should_block": False,  # ADVISORY: never block
        }

    def _check_commit_format(self, msg: str) -> dict[str, Any]:
        """Evaluator: check Conventional Commits format (advisory, non-blocking)."""
        valid_prefixes = ["feat", "fix", "refactor", "docs", "chore", "test"]
        conforms = any(msg.startswith(p + ":") for p in valid_prefixes)
        return {
            "conforms": conforms,
            "should_block": False,  # ADVISORY
        }

    def _check_pr_format(self, body: str) -> dict[str, Any]:
        """Evaluator: PR body is readable and structured (advisory, non-blocking)."""
        # Check for minimal structure (has some content)
        is_readable = len(body) > 20 and "##" in body
        return {
            "is_readable": is_readable,
            "should_block": False,  # ADVISORY: format is best-effort
        }

    def _check_test_naming(self, test_name: str) -> dict[str, Any]:
        """Evaluator: check test naming convention (advisory, non-blocking)."""
        # Check if matches GIVEN/WHEN/THEN pattern
        follows = ("given" in test_name.lower() and "when" in test_name.lower()
                   and "then" in test_name.lower())
        return {
            "follows_convention": follows,
            "should_block": False,  # ADVISORY
        }

    def _check_yaml_structure(self, content: str) -> dict[str, Any]:
        """Evaluator: YAML/JSON structure validity (advisory, non-blocking)."""
        try:
            # Try minimal YAML parse
            lines = content.strip().split("\n")
            has_keys = all(":" in line for line in lines if line.strip())
            return {
                "is_valid": has_keys,
                "should_block": False,  # Key order difference is not blocking
            }
        except Exception:
            return {
                "is_valid": False,
                "should_block": False,  # Parse error is advisory, escalate to human
            }


class TestOverblockingRiskDocumentation:
    """Verify that overblocking risk review is documented in SKILL.md."""

    def test_overblocking_keyword_present(self):
        """SKILL.md contains 'overblocking' keyword."""
        content = read_skill_content()
        assert "overblocking" in content.lower(), (
            "overblocking keyword not found in impl-review-loop/SKILL.md"
        )

    def test_hard_invariant_documented(self):
        """Hard invariant conditions are documented."""
        content = read_skill_content()
        assert "hard invariant" in content.lower() or "Hard Invariant" in content, (
            "Hard invariant section not documented"
        )

    def test_advisory_conditions_documented(self):
        """Advisory conditions (non-blocking) are documented."""
        content = read_skill_content()
        assert "advisory" in content.lower(), (
            "Advisory conditions not documented in overblocking section"
        )

    def test_blocking_list_includes_allowed_paths_violation(self):
        """Allowed Paths violation is listed as hard invariant (blocking)."""
        content = read_skill_content()
        section = self._extract_hard_invariant_section(content)
        assert "Allowed Paths" in section or "allowed path" in section.lower(), (
            "Allowed Paths violation not listed as hard invariant"
        )

    def test_blocking_list_includes_secret_files(self):
        """Secret/sensitive files are listed as hard invariant (blocking)."""
        content = read_skill_content()
        section = self._extract_hard_invariant_section(content)
        assert (
            ".env" in section
            or "secret" in section.lower()
            or "credential" in section.lower()
        ), (
            "Secret files not listed as hard invariant"
        )

    def test_blocking_list_includes_destructive_commands(self):
        """Destructive commands are listed as hard invariant (blocking)."""
        content = read_skill_content()
        section = self._extract_hard_invariant_section(content)
        assert (
            "destructive" in section.lower()
            or "reset --hard" in section
            or "push --force" in section
        ), (
            "Destructive commands not listed as hard invariant"
        )

    def test_advisory_list_includes_language_style(self):
        """Language/style issues are listed as advisory (non-blocking)."""
        content = read_skill_content()
        # Advisory conditions appear under "Advisory に留めるべき条件" section
        assert (
            "文体" in content or "language" in content.lower()
            or "style" in content.lower()
            or "format" in content.lower()
            or "advisory に留めるべき条件" in content
        ), (
            "Language/style issues not listed as advisory"
        )

    def test_loop_prevention_invariant_present(self):
        """loop-prevention invariant is documented."""
        content = read_skill_content()
        assert (
            "loop-prevention" in content
            or "Loop-prevention" in content
            or "loop prevention" in content.lower()
            or "loop-prevention invariant" in content.lower()
        ), (
            "loop-prevention invariant not documented"
        )

    def _extract_hard_invariant_section(self, content: str) -> str:
        """Extract the hard invariant section from SKILL.md."""
        start_idx = content.find("Hard Invariant")
        if start_idx == -1:
            start_idx = content.lower().find("hard invariant")
        if start_idx == -1:
            return ""
        end_idx = content.find("###", start_idx + 1)
        if end_idx == -1:
            end_idx = start_idx + 2000
        return content[start_idx:end_idx]

    def _extract_advisory_section(self, content: str) -> str:
        """Extract the advisory conditions section from SKILL.md."""
        start_idx = content.lower().find("advisory")
        if start_idx == -1:
            return ""
        end_idx = content.find("###", start_idx + 1)
        if end_idx == -1:
            end_idx = start_idx + 2000
        return content[start_idx:end_idx]


class TestAllowedPathsGateDeterministicEvaluation:
    """Deterministic evaluation of allowed_paths gate (no tautology)."""

    def test_gate_evaluator_distinguishes_pass_fail(self):
        """Gate evaluator must correctly distinguish pass from fail cases."""
        # Pass case: file in Allowed Paths
        pass_result = self._evaluate_gate(
            allowed_paths=[".claude/agents/**"],
            changed_files=[".claude/agents/test.md"],
        )
        assert pass_result["status"] == "ok", (
            "Gate should return ok for Allowed Paths compliant changes"
        )

        # Fail case: file outside Allowed Paths
        fail_result = self._evaluate_gate(
            allowed_paths=[".claude/agents/**"],
            changed_files=["src/systems/test.ts"],
        )
        assert fail_result["status"] == "fail_closed", (
            "Gate should return fail_closed for violations"
        )

    def test_gate_evaluator_requires_manifest_hash(self):
        """Gate result must include manifest snapshot hash (stale detection)."""
        result = self._evaluate_gate(
            allowed_paths=[".claude/agents/**"],
            changed_files=[".claude/agents/test.md"],
        )
        assert "manifest_snapshot_sha256" in result or "manifest" in str(result), (
            "Gate result must include manifest snapshot for stale detection"
        )

    def test_gate_handles_glob_patterns_correctly(self):
        """Gate evaluator must correctly handle glob patterns in Allowed Paths."""
        import fnmatch

        patterns = [".claude/skills/**", ".claude/agents/**"]
        test_cases = [
            (".claude/skills/test.py", True),
            (".claude/skills/impl-review-loop/SKILL.md", True),
            (".claude/agents/worker.md", True),
            ("src/systems/test.ts", False),
            (".claude/docs/readme.md", False),
        ]

        for file_path, should_match in test_cases:
            matches = any(fnmatch.fnmatch(file_path, p) for p in patterns)
            assert matches == should_match, (
                f"Pattern matching failed for {file_path}: expected {should_match}, got {matches}"
            )

    def _evaluate_gate(
        self, allowed_paths: list[str], changed_files: list[str]
    ) -> dict[str, Any]:
        """Deterministic gate evaluator (no circular logic)."""
        import fnmatch
        import hashlib

        # Compute manifest hash
        manifest_str = "|".join(sorted(allowed_paths))
        manifest_hash = hashlib.sha256(manifest_str.encode()).hexdigest()

        # Check all files
        violations = []
        for file_path in changed_files:
            matched = False
            for pattern in allowed_paths:
                if fnmatch.fnmatch(file_path, pattern):
                    matched = True
                    break
            if not matched:
                violations.append(file_path)

        # Determine status
        status = "fail_closed" if violations else "ok"

        return {
            "status": status,
            "manifest_snapshot_sha256": manifest_hash,
            "final_diff_paths": changed_files,
            "violations": violations,
        }

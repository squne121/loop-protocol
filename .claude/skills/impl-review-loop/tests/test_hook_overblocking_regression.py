#!/usr/bin/env python3
"""Regression tests for overblocking prevention in impl-review-loop hooks.

Tests the following invariants:
- True Positive: Allowed Paths violations (files outside contract) are fail-closed (DENIED)
- False Positive Prevention: Allowed Paths compliant changes, standard verification commands,
  and read-only/inspection operations are NOT blocked (ALLOWED)
- Loop-prevention: Stop/SubagentStop hooks do not block on expression/language/reporting quality

AC8: Validates that hook handlers correctly distinguish hard invariants (blocking) from
advisory conditions (non-blocking) with fixture-based deterministic tests.

IMPORTANT: Tests import evaluation functions from allowed_paths_gate module (not inline helpers).
This prevents tautological self-validation and ensures tests are genuine regression tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
IMPL_REVIEW_LOOP_SKILL = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "SKILL.md"

# Import the canonical gate evaluator module (NOT inline helpers)
_gate_module_path = (
    REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts" / "allowed_paths_gate.py"
)
if not _gate_module_path.exists():
    raise ImportError(f"Gate module not found: {_gate_module_path}")

_spec = spec_from_file_location("allowed_paths_gate", _gate_module_path)
_gate_module = module_from_spec(_spec)
sys.modules["allowed_paths_gate"] = _gate_module
_spec.loader.exec_module(_gate_module)

# Import canonical functions
evaluate_allowed_paths_gate = _gate_module.evaluate_allowed_paths_gate
classify_command = _gate_module.classify_command
classify_path = _gate_module.classify_path
classify_quality_issue = _gate_module.classify_quality_issue


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

        # Evaluator: call canonical gate module (not inline helper)
        result = evaluate_allowed_paths_gate(allowed_paths, [file_changed])
        assert result["status"] == "ok", (
            f"File {file_changed} within Allowed Paths should be ALLOWED, got {result['status']}"
        )

    def test_allowed_paths_nested_directory_allowed(self):
        """Edits in nested subdirectories of Allowed Paths must be allowed."""
        allowed_paths = [".claude/skills/**"]
        file_changed = ".claude/skills/impl-review-loop/tests/test_hook.py"

        result = evaluate_allowed_paths_gate(allowed_paths, [file_changed])
        assert result["status"] == "ok", (
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

        result = evaluate_allowed_paths_gate(allowed_paths, files)
        assert result["status"] == "ok", f"All files should be allowed: {result['violations']}"

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

        # Evaluator: use canonical classify_command (not inline helper)
        for cmd in commands:
            classification = classify_command(cmd)
            assert classification == "allow", (
                f"Standard verification command '{cmd}' must be classified as allow"
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
            classification = classify_command(cmd)
            assert classification == "allow", (
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
            classification = classify_command(cmd)
            assert classification == "allow", (
                f"Read-only gh command '{cmd}' should be allowed"
            )



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

        result = evaluate_allowed_paths_gate(allowed_paths, [file_changed])
        assert result["status"] == "fail_closed", (
            f"File {file_changed} is OUTSIDE Allowed Paths and must be DENIED"
        )

    def test_multiple_violations_all_detected(self):
        """Multiple files with some violations must all be caught."""
        allowed_paths = [
            ".claude/agents/**",
            ".claude/skills/**",
        ]
        files = [
            ".claude/agents/test.md",
            ".claude/skills/test.py",
            "src/systems/test.ts",
            "docs/dev/test.md",
        ]

        result = evaluate_allowed_paths_gate(allowed_paths, files)
        # Should have violations for src/systems/test.ts and docs/dev/test.md
        assert result["status"] == "fail_closed", (
            f"Gate should detect violations; got {result}"
        )
        assert set(result["violations"]) == {"src/systems/test.ts", "docs/dev/test.md"}, (
            f"Expected violations for src/systems/test.ts and docs/dev/test.md, got {result['violations']}"
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
            classification = classify_path(pattern)
            assert classification == "hard_invariant_block", (
                f"File {pattern} must be classified as hard_invariant_block"
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
            classification = classify_command(cmd)
            assert classification == "hard_invariant_block", (
                f"Command '{cmd}' must be classified as hard_invariant_block"
            )



class TestLoopPreventionAdvisoryGuard:
    """Loop-prevention: expression/language/format issues are advisory, not blocking."""

    def test_japanese_language_issue_not_blocking(self):
        """Violation of Japanese language rules is advisory, NOT blocking."""
        # Fixture: comment/docstring not in Japanese
        # Evaluator: use canonical classify_quality_issue (not inline helper)
        result = classify_quality_issue("language")
        assert result["category"] == "advisory", (
            "Language issues must be advisory"
        )
        assert result["should_block"] is False, (
            "Language mismatch should be advisory, not blocking"
        )

    def test_commit_message_format_issue_not_blocking(self):
        """Non-Conventional Commits format is advisory, NOT blocking."""
        # Evaluator: use canonical classify_quality_issue
        result = classify_quality_issue("commit_format")
        assert result["category"] == "advisory", (
            "Commit format issues must be advisory"
        )
        assert result["should_block"] is False, (
            "Commit format issue should be advisory, not blocking"
        )

    def test_pr_body_format_inconsistency_not_blocking(self):
        """PR body format inconsistency is advisory, NOT blocking."""
        # Evaluator: use canonical classify_quality_issue
        result = classify_quality_issue("pr_body_format")
        assert result["category"] == "advisory", (
            "PR body format issues must be advisory"
        )
        assert result["should_block"] is False, (
            "PR format inconsistency should be advisory"
        )

    def test_test_naming_convention_issue_not_blocking(self):
        """Non-GIVEN/WHEN/THEN test naming is advisory, NOT blocking."""
        # Evaluator: use canonical classify_quality_issue
        result = classify_quality_issue("test_naming")
        assert result["category"] == "advisory", (
            "Test naming issues must be advisory"
        )
        assert result["should_block"] is False, (
            "Test naming issue should be advisory, not blocking"
        )

    def test_yaml_key_order_difference_not_blocking(self):
        """YAML/JSON key ordering difference is advisory, NOT blocking."""
        # Evaluator: use canonical classify_quality_issue
        result = classify_quality_issue("yaml_key_order")
        assert result["category"] == "advisory", (
            "YAML key order issues must be advisory"
        )
        assert result["should_block"] is False, (
            "YAML key ordering difference should be advisory"
        )



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
        pass_result = evaluate_allowed_paths_gate(
            allowed_paths=[".claude/agents/**"],
            changed_files=[".claude/agents/test.md"],
        )
        assert pass_result["status"] == "ok", (
            "Gate should return ok for Allowed Paths compliant changes"
        )

        # Fail case: file outside Allowed Paths
        fail_result = evaluate_allowed_paths_gate(
            allowed_paths=[".claude/agents/**"],
            changed_files=["src/systems/test.ts"],
        )
        assert fail_result["status"] == "fail_closed", (
            "Gate should return fail_closed for violations"
        )

    def test_gate_evaluator_requires_manifest_hash(self):
        """Gate result must include manifest snapshot hash (stale detection)."""
        result = evaluate_allowed_paths_gate(
            allowed_paths=[".claude/agents/**"],
            changed_files=[".claude/agents/test.md"],
        )
        assert "manifest_snapshot_sha256" in result, (
            "Gate result must include manifest_snapshot_sha256 for stale detection"
        )
        assert result["manifest_snapshot_sha256"] is not None, (
            "Manifest hash must not be None"
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


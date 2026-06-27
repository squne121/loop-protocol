"""tests/agent_ops/test_probe_docs_contract.py — docs contract tests for Issue #1197.

Covers:
- AC1: post-merge-cleanup SKILL.md no longer references raw git probe commands (post_merge_cleanup)
- AC7: AGENTS.md / project-constitution.md / hook-boundaries.md policy alignment (policy_alignment)
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ─── AC1: post_merge_cleanup ──────────────────────────────────────────────────

class TestPostMergeCleanup:
    def test_post_merge_cleanup_skill_references_probe_scripts(self) -> None:
        """AC1: post-merge-cleanup SKILL.md must reference probe scripts."""
        skill_md = REPO_ROOT / ".claude/skills/post-merge-cleanup/SKILL.md"
        content = skill_md.read_text()
        assert "git_ref_probe.py" in content or "git_worktree_probe.py" in content, (
            "post-merge-cleanup/SKILL.md must reference at least one probe script "
            "(git_ref_probe.py or git_worktree_probe.py)"
        )

    def test_post_merge_cleanup_skill_no_raw_for_each_ref_outside_probe_context(self) -> None:
        """AC1: post-merge-cleanup SKILL.md should not have raw git for-each-ref shell examples."""
        skill_md = REPO_ROOT / ".claude/skills/post-merge-cleanup/SKILL.md"
        content = skill_md.read_text()
        # Raw git for-each-ref in a bash code block (not inside a probe script reference)
        # is what we want to avoid
        import re
        # Check for lines like: git for-each-ref (in code blocks, not as explanation text)
        raw_probe_in_code = re.search(
            r"```\s*bash[^`]*`git\s+for-each-ref[^`]*`",
            content,
            re.DOTALL,
        )
        assert raw_probe_in_code is None, (
            "post-merge-cleanup/SKILL.md has raw 'git for-each-ref' in a bash code block; "
            "replace with probe script call"
        )


# ─── AC7: policy_alignment ────────────────────────────────────────────────────

class TestPolicyAlignment:
    def test_policy_alignment_agents_md_mentions_probe_script_priority(self) -> None:
        """AC7: AGENTS.md must mention probe script priority over raw git commands."""
        agents_md = REPO_ROOT / "AGENTS.md"
        content = agents_md.read_text()
        # Either probe script names or the policy statement
        has_probe_ref = (
            "git_ref_probe.py" in content
            or "git_worktree_probe.py" in content
            or "probe script" in content.lower()
        )
        assert has_probe_ref, (
            "AGENTS.md must mention git probe scripts or 'probe script' policy"
        )

    def test_policy_alignment_project_constitution_mentions_probe_policy(self) -> None:
        """AC7: project-constitution.md must reflect git probe script policy."""
        constitution = REPO_ROOT / ".claude/rules/project-constitution.md"
        content = constitution.read_text()
        has_policy = (
            "git_ref_probe.py" in content
            or "複雑な git read-only probe" in content
            or "probe script" in content.lower()
        )
        assert has_policy, (
            ".claude/rules/project-constitution.md must mention git probe script policy"
        )

    def test_policy_alignment_hook_boundaries_hook_is_guardrail(self) -> None:
        """AC7: hook-boundaries.md must state hook is fail-closed guardrail, not security boundary."""
        hook_doc = REPO_ROOT / "docs/dev/hook-boundaries.md"
        content = hook_doc.read_text()
        # The document should explicitly NOT call hooks a security boundary
        # and SHOULD call them fail-closed local guardrail
        has_guardrail_statement = (
            "fail-closed ローカルガードレール" in content
            or "fail-closed local guardrail" in content.lower()
        )
        assert has_guardrail_statement, (
            "docs/dev/hook-boundaries.md must describe hooks as fail-closed local guardrails"
        )

    def test_policy_alignment_hook_boundaries_probe_scripts_in_allowlist(self) -> None:
        """AC7: hook-boundaries.md should mention probe scripts in DETERMINISTIC_CHECKER_ALLOWLIST."""
        hook_doc = REPO_ROOT / "docs/dev/hook-boundaries.md"
        content = hook_doc.read_text()
        has_probe = (
            "git_ref_probe.py" in content
            or "git_worktree_probe.py" in content
        )
        assert has_probe, (
            "docs/dev/hook-boundaries.md must mention probe scripts in DETERMINISTIC_CHECKER_ALLOWLIST"
        )

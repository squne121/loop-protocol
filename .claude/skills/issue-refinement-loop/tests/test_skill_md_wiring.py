#!/usr/bin/env python3
"""
Test that SKILL.md contains required wiring for plan_refinement_loop.py output consumption.

AC7: SKILL.md should not contain prose re-judgments of investigation_policy,
web_research_policy, scope_signal_guard, or follow_up_materialization.
Instead, it should consume the planner output.
"""

import re
from pathlib import Path


def load_skill_md() -> str:
    """Load the SKILL.md file."""
    skill_path = (
        Path(__file__).parent.parent / "SKILL.md"
    )
    assert skill_path.exists(), f"SKILL.md not found: {skill_path}"
    return skill_path.read_text(encoding="utf-8")


class TestSkillMdWiring:
    """Test SKILL.md wiring for planner output consumption."""

    def test_skill_md_mentions_plan_refinement_loop(self):
        """AC7: SKILL.md should mention plan_refinement_loop.py."""
        skill_md = load_skill_md()
        assert (
            "plan_refinement_loop.py" in skill_md
        ), "SKILL.md should mention plan_refinement_loop.py"

    def test_skill_md_mentions_refinement_loop_plan_v1(self):
        """AC7: SKILL.md should mention REFINEMENT_LOOP_PLAN_V1."""
        skill_md = load_skill_md()
        assert (
            "REFINEMENT_LOOP_PLAN_V1" in skill_md
        ), "SKILL.md should mention REFINEMENT_LOOP_PLAN_V1"

    def test_skill_md_mentions_schema_validation(self):
        """AC7: SKILL.md should mention JSON schema validation."""
        skill_md = load_skill_md()
        assert (
            "schema" in skill_md.lower() or "validation" in skill_md.lower()
        ), "SKILL.md should mention schema validation"

    def test_skill_md_mentions_fail_closed_handling(self):
        """AC7: SKILL.md should mention fail_closed handling."""
        skill_md = load_skill_md()
        assert (
            "fail_closed" in skill_md
        ), "SKILL.md should mention fail_closed handling"

    def test_skill_md_does_not_repedge_investigation_logic(self):
        """AC7: SKILL.md Step 0f has no prose re-judgment logic"""
        skill_md = load_skill_md()

        # Step 0f section extraction (heading-based slicing)
        if "#### Step 0f:" not in skill_md:
            # Step 0f is gone - OK
            return

        step_0f = skill_md.split("#### Step 0f:")[1]
        # Find next heading (####, ###, or ##)
        next_heading = re.search(r'\n(####? )', step_0f)
        if next_heading:
            step_0f = step_0f[:next_heading.start()]

        # Forbidden fragments indicating prose re-judgment
        forbidden_fragments = [
            "true_if_any",
            "false_only_if",
            "investigation_policy.codebase_required = true",
            "investigation_policy.codebase_required = false",
            "web_research_policy.required = true",
            "web_research_policy.required = false",
            "policy_derivation:",  # Old YAML schema literal
        ]

        found = [f for f in forbidden_fragments if f in step_0f]
        assert not found, (
            f"SKILL.md Step 0f contains prose re-judgment logic: {found}\n\n"
            f"Step 0f should call plan_refinement_loop.py instead.\n\n"
            f"Step 0f excerpt:\n{step_0f[:500]}"
        )

    def test_skill_md_step_0f_references_planner(self):
        """AC7: Step 0f references plan_refinement_loop.py or REFINEMENT_LOOP_PLAN_V1"""
        skill_md = load_skill_md()
        if "#### Step 0f:" not in skill_md:
            # Step 0f deleted - OK (handled by automated planner)
            return

        step_0f = skill_md.split("#### Step 0f:")[1]
        next_heading = re.search(r'\n(####? )', step_0f)
        if next_heading:
            step_0f = step_0f[:next_heading.start()]

        has_planner_ref = (
            "plan_refinement_loop.py" in step_0f or
            "REFINEMENT_LOOP_PLAN_V1" in step_0f
        )
        assert has_planner_ref, (
            "Step 0f should reference plan_refinement_loop.py or REFINEMENT_LOOP_PLAN_V1 "
            f"to show it uses the planner output.\n\nStep 0f excerpt:\n{step_0f[:500]}"
        )

    def test_skill_md_has_wiring_for_planner_invocation(self):
        """AC7: SKILL.md should have wiring for planner invocation."""
        skill_md = load_skill_md()
        # Look for evidence of planner invocation (could be shell script or reference)
        has_invocation_hint = any(
            phrase in skill_md.lower()
            for phrase in [
                "plan_refinement_loop",
                "planner",
                "stdin",
                "json input",
            ]
        )
        assert (
            has_invocation_hint
        ), "SKILL.md should have wiring for planner invocation"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

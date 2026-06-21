#!/usr/bin/env python3
"""
Tests for fail_closed override rewrite constraints.

Covers:
- AC1: fail_closed override produces required_sections in planner output
- AC2: fail_closed override produces required_contract_keys and rewrite_constraints
- AC3: FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 fields present in rewrite_constraints
- AC5: missing sections cause fail_closed, not Review progression
- AC6: human_decision_reframe is not validation bypass
- AC7: allowed_reason_codes / never_override_reason_codes override policy
- AC8: FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 schema emitted by planner
- AC9b: pre-mutation dry-run / post-mutation fresh check concept (routing tests)
- AC10a: max_rewrite_attempts / no_progress_route in constraints
- AC10b: no-progress detection routing to human_judgment_required
- AC11: terminal result fields (checked_body_sha256 etc.)
- AC12a: Write is in disallowedTools of issue-author.md
"""

import json
import subprocess
from pathlib import Path
from typing import Any


TESTS_DIR = Path(__file__).parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
FIXTURES_DIR = TESTS_DIR / "fixtures"
# Resolve the .claude/agents directory relative to repo root
# Tests are at .claude/skills/issue-refinement-loop/tests/
AGENTS_DIR = TESTS_DIR.parent.parent.parent / "agents"


def make_input(body: str, issue_number: int = 1) -> dict[str, Any]:
    """Build a minimal valid REFINEMENT_LOOP_PLANNER_INPUT_V1 from body text."""
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": f"Test Issue #{issue_number}",
            "body": body,
            "labels": [],
        },
        "comments": None,
        "known_context": None,
    }


def load_fixture(fixture_name: str, fixture_type: str = "malformed") -> str:
    """Load a fixture from tests/fixtures/."""
    path = FIXTURES_DIR / fixture_type / f"{fixture_name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    # Fall back to parent fixtures dir
    path2 = TESTS_DIR.parent / "fixtures" / fixture_type / f"{fixture_name}.md"
    if path2.exists():
        return path2.read_text(encoding="utf-8")
    return ""


def run_planner(input_data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Run plan_refinement_loop.py and return (output_dict, exit_code)."""
    script = SCRIPTS_DIR / "plan_refinement_loop.py"
    result = subprocess.run(
        ["python3", str(script)],
        input=json.dumps(input_data, ensure_ascii=False),
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout), result.returncode


# ---------------------------------------------------------------------------
# AC1 / AC2 / AC8: FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 fields
# ---------------------------------------------------------------------------


class TestFailClosedRewriteConstraintsV1Present:
    """AC1/AC2/AC8: planner emits FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 on fail_closed."""

    def test_fail_closed_override_rewrite_includes_required_sections(self):
        """AC1: fail_closed output contains required_sections list."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, exit_code = run_planner(make_input(body))

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True
        rewrite_constraints = output["fail_closed"].get("rewrite_constraints", {})
        assert rewrite_constraints, "rewrite_constraints must be present on fail_closed"
        assert "required_sections" in rewrite_constraints, (
            "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 must include required_sections"
        )

    def test_fail_closed_includes_required_contract_keys(self):
        """AC2: fail_closed output contains required_contract_keys."""
        body = load_fixture("missing_contract_keys", "malformed")
        if not body:
            # Minimal body with no contract at all
            body = "## Outcome\n\nSome outcome.\n"
        output, exit_code = run_planner(make_input(body))

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True
        rewrite_constraints = output["fail_closed"].get("rewrite_constraints", {})
        assert "required_contract_keys" in rewrite_constraints, (
            "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 must include required_contract_keys"
        )

    def test_fail_closed_includes_rewrite_constraints_field(self):
        """AC2: fail_closed output contains rewrite_constraints nested field."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, exit_code = run_planner(make_input(body))

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True
        rc = output["fail_closed"].get("rewrite_constraints", {})
        assert "rewrite_constraints" in rc, (
            "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 must include nested rewrite_constraints field"
        )
        nested = rc["rewrite_constraints"]
        assert "freeform_rewrite_forbidden" in nested
        assert nested["freeform_rewrite_forbidden"] is True, (
            "freeform_rewrite_forbidden must be True"
        )

    def test_fail_closed_rewrite_constraints_schema_version(self):
        """AC8: FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 schema_version is present."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"].get("rewrite_constraints", {})
        assert rc.get("schema_version") == "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1", (
            "schema_version must be FAIL_CLOSED_REWRITE_CONSTRAINTS_V1"
        )


# ---------------------------------------------------------------------------
# AC3: Structured fields preserved (not freeform)
# ---------------------------------------------------------------------------


class TestRewriteConstraintsStructuredFields:
    """AC3: required_sections / required_contract_keys fields are structured, not freeform."""

    def test_required_sections_is_a_list(self):
        """AC3: required_sections is a list (not a freeform string)."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        assert isinstance(rc["required_sections"], list), (
            "required_sections must be a list"
        )

    def test_required_contract_keys_is_a_list(self):
        """AC3: required_contract_keys is a list (not a freeform string)."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        assert isinstance(rc["required_contract_keys"], list), (
            "required_contract_keys must be a list"
        )

    def test_must_add_sections_matches_required_sections(self):
        """AC3: rewrite_constraints.must_add_sections equals required_sections."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        assert rc["rewrite_constraints"]["must_add_sections"] == rc["required_sections"], (
            "must_add_sections must match required_sections for structured repair"
        )


# ---------------------------------------------------------------------------
# AC5: Missing sections block Review
# ---------------------------------------------------------------------------


class TestMissingSectionsBlockReview:
    """AC5: Planner returns fail_closed when required sections are missing."""

    def test_missing_outcome_causes_fail_closed(self):
        """AC5: Absence of Outcome section sets fail_closed.required=True."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, exit_code = run_planner(make_input(body))

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True
        assert "missing_required_section" in output["fail_closed"]["reason_codes"], (
            "missing_required_section must be in reason_codes"
        )

    def test_fail_closed_override_rewrite_triggers_on_missing_section(self):
        """AC5: Planner fail_closed on missing required section; not a pass."""
        body = load_fixture("missing_required_section_with_contract", "malformed")
        if not body:
            # Construct inline fixture: has contract but no Outcome
            body = (
                "```yaml\ncontract_schema_version: v1\nissue_kind: implementation\n```\n\n"
                "## Acceptance Criteria\n\n- [ ] AC1: Some AC\n\n"
                "## Verification Commands\n\n```bash\n$ echo ok\n```\n"
            )
        output, exit_code = run_planner(make_input(body))

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True

    def test_missing_sections_after_rewrite_keeps_fail_closed_requirement(self):
        """AC5: Even after rewrite attempt, missing sections keep fail_closed=True.

        This simulates a scenario where issue-author didn't fix all sections:
        the planner is re-invoked and must still return fail_closed.
        """
        # Body with Acceptance Criteria but still no Outcome
        body_after_partial_rewrite = (
            "```yaml\ncontract_schema_version: v1\nissue_kind: implementation\n```\n\n"
            "## Background\n\nSome background.\n\n"
            "## Acceptance Criteria\n\n- [ ] AC1: Still missing Outcome\n\n"
            "## Verification Commands\n\n```bash\n$ echo ok\n```\n"
        )
        output, exit_code = run_planner(make_input(body_after_partial_rewrite))

        assert exit_code == 0
        # The issue_kind is known (implementation) so template-based check runs.
        # If template check finds Outcome is missing, fail_closed must still be true.
        assert output["fail_closed"]["required"] is True


# ---------------------------------------------------------------------------
# AC6: human_decision_reframe is not validation bypass
# ---------------------------------------------------------------------------


class TestHumanDecisionReframeDefinition:
    """AC6: human_decision_reframe meaning in references (tested via planner output structure)."""

    def test_rewrite_constraints_always_present_on_fail_closed(self):
        """AC6: FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 is always in fail_closed output.

        If human_decision_reframe were a bypass, rewrite_constraints would be absent.
        Its presence enforces the 'constraint intake' definition.
        """
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        assert output["fail_closed"]["required"] is True
        assert "rewrite_constraints" in output["fail_closed"], (
            "rewrite_constraints must always be present — human_decision_reframe is not a bypass"
        )


# ---------------------------------------------------------------------------
# AC7: override_policy allowed_reason_codes / never_override_reason_codes
# ---------------------------------------------------------------------------


class TestOverridePolicy:
    """AC7: allowed_reason_codes and never_override_reason_codes in FAIL_CLOSED_REWRITE_CONSTRAINTS_V1."""

    def test_allowed_reason_codes_includes_missing_required_section(self):
        """AC7: missing_required_section is in allowed_reason_codes."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        allowed = rc["override_policy"]["allowed_reason_codes"]
        assert "missing_required_section" in allowed, (
            "missing_required_section must be in allowed_reason_codes"
        )

    def test_allowed_reason_codes_includes_missing_required_contract_key(self):
        """AC7: missing_required_contract_key is in allowed_reason_codes."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        allowed = rc["override_policy"]["allowed_reason_codes"]
        assert "missing_required_contract_key" in allowed, (
            "missing_required_contract_key must be in allowed_reason_codes"
        )

    def test_never_override_includes_unknown_issue_kind(self):
        """AC7: unknown_issue_kind is in never_override_reason_codes."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        never = rc["override_policy"]["never_override_reason_codes"]
        assert "unknown_issue_kind" in never, (
            "unknown_issue_kind must be in never_override_reason_codes"
        )

    def test_never_override_includes_checker_internal_error(self):
        """AC7: checker_internal_error is in never_override_reason_codes."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        never = rc["override_policy"]["never_override_reason_codes"]
        assert "checker_internal_error" in never, (
            "checker_internal_error must be in never_override_reason_codes"
        )

    def test_unknown_issue_kind_appears_in_non_overridable_in_current_result(self):
        """AC7: When fail_closed is due to unknown_issue_kind, it appears in non_overridable_in_current_result."""
        body = load_fixture("never_override_unknown_issue_kind", "malformed")
        if not body:
            # Inline fallback: unknown issue_kind
            body = (
                "```yaml\ncontract_schema_version: v1\n"
                "issue_kind: completely_unknown_xyz\ngoal_ref: test\n```\n\n"
                "## Outcome\n\nSome outcome.\n\n"
                "## Acceptance Criteria\n\n- [ ] AC1\n\n"
                "## Verification Commands\n\n```bash\n$ echo ok\n```\n"
            )
        output, exit_code = run_planner(make_input(body))

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True
        assert "unknown_issue_kind" in output["fail_closed"]["reason_codes"], (
            "unknown_issue_kind must be in reason_codes for unrecognized issue_kind"
        )
        rc = output["fail_closed"]["rewrite_constraints"]
        non_overridable = rc["override_policy"]["non_overridable_in_current_result"]
        assert "unknown_issue_kind" in non_overridable, (
            "unknown_issue_kind must be in non_overridable_in_current_result"
        )

    def test_missing_section_appears_in_overridable_in_current_result(self):
        """AC7: When fail_closed is due to missing_required_section, it appears in overridable_in_current_result."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        assert output["fail_closed"]["required"] is True
        assert "missing_required_section" in output["fail_closed"]["reason_codes"]
        rc = output["fail_closed"]["rewrite_constraints"]
        overridable = rc["override_policy"]["overridable_in_current_result"]
        assert "missing_required_section" in overridable, (
            "missing_required_section must be in overridable_in_current_result"
        )


# ---------------------------------------------------------------------------
# AC9b: pre-mutation dry-run / post-mutation fresh check (routing concept)
# ---------------------------------------------------------------------------


class TestPrePostMutationCheckerRouting:
    """AC9b: Tests that verify the 2-stage checker contract is expressed in the output."""

    def test_issue_author_runs_contract_checker(self):
        """AC4/AC9b: After fail_closed, issue-author must run checker (verified via reference doc)."""
        # Verify the reference document exists and mentions the 2-stage checker
        ref_doc = TESTS_DIR.parent / "references" / "ac-vc-reflection.md"
        assert ref_doc.exists(), f"ac-vc-reflection.md must exist: {ref_doc}"
        content = ref_doc.read_text(encoding="utf-8")
        assert "pre-mutation dry-run checker" in content, (
            "ac-vc-reflection.md must document pre-mutation dry-run checker"
        )
        assert "post-mutation fresh checker" in content, (
            "ac-vc-reflection.md must document post-mutation fresh checker"
        )

    def test_rewrite_runs_contract_checker(self):
        """AC4/AC9b: rewrite process must include contract checker re-run."""
        ref_doc = TESTS_DIR.parent / "references" / "ac-vc-reflection.md"
        content = ref_doc.read_text(encoding="utf-8")
        # The reference must mention that checker runs after issue-author rewrite
        assert "checker" in content.lower(), (
            "ac-vc-reflection.md must reference the checker step after rewrite"
        )

    def test_pre_mutation_dry_run_documented_in_references(self):
        """AC9b: pre-mutation dry-run concept is documented."""
        ref_doc = TESTS_DIR.parent / "references" / "ac-vc-reflection.md"
        content = ref_doc.read_text(encoding="utf-8")
        assert "pre-mutation" in content, (
            "ac-vc-reflection.md must mention pre-mutation dry-run"
        )

    def test_post_mutation_fresh_check_documented_in_references(self):
        """AC9b: post-mutation fresh check concept is documented."""
        ref_doc = TESTS_DIR.parent / "references" / "ac-vc-reflection.md"
        content = ref_doc.read_text(encoding="utf-8")
        assert "post-mutation" in content, (
            "ac-vc-reflection.md must mention post-mutation fresh check"
        )


# ---------------------------------------------------------------------------
# AC10a / AC10b: max_rewrite_attempts / no-progress detection
# ---------------------------------------------------------------------------


class TestMaxRewriteAttemptsAndNoProgress:
    """AC10: max_rewrite_attempts and no_progress_route in FAIL_CLOSED_REWRITE_CONSTRAINTS_V1."""

    def test_max_rewrite_attempts_in_constraints(self):
        """AC10a: max_rewrite_attempts is present in FAIL_CLOSED_REWRITE_CONSTRAINTS_V1."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        assert "max_rewrite_attempts" in rc, (
            "max_rewrite_attempts must be in FAIL_CLOSED_REWRITE_CONSTRAINTS_V1"
        )
        assert rc["max_rewrite_attempts"] == 2, (
            "max_rewrite_attempts must be 2"
        )

    def test_no_progress_route_in_constraints(self):
        """AC10a: no_progress_route is present and set to human_judgment_required."""
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        assert "no_progress_route" in rc, (
            "no_progress_route must be in FAIL_CLOSED_REWRITE_CONSTRAINTS_V1"
        )
        assert rc["no_progress_route"] == "human_judgment_required", (
            "no_progress_route must be 'human_judgment_required'"
        )

    def test_no_progress_after_two_rewrites_routes_human_judgment(self):
        """AC10b: If body_sha256 unchanged across 2 rewrites, route is human_judgment_required.

        This test simulates the no-progress detection at the planner level:
        the planner emits the routing destination; the orchestrator is responsible
        for enforcing it.
        """
        body = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body))

        rc = output["fail_closed"]["rewrite_constraints"]
        # The no_progress_route tells the orchestrator where to go on no-progress
        assert rc["no_progress_route"] == "human_judgment_required", (
            "no-progress detection must route to human_judgment_required"
        )

    def test_no_progress_routes_human_judgment(self):
        """AC10b: Planner specifies human_judgment_required as no-progress destination."""
        body = "## Outcome\n\nSome outcome.\n"  # minimal valid
        # Even a good body with no fail_closed: verify the constant is correct
        body_bad = load_fixture("missing_outcome_section", "malformed")
        output, _ = run_planner(make_input(body_bad))
        assert output["fail_closed"]["required"] is True
        rc = output["fail_closed"]["rewrite_constraints"]
        assert rc["no_progress_route"] == "human_judgment_required"


# ---------------------------------------------------------------------------
# AC11: Terminal result fields (checked_body_sha256, checker_exit_code, etc.)
# ---------------------------------------------------------------------------


class TestTerminalResultFields:
    """AC11: Terminal result field names are documented in references."""

    def test_terminal_result_fields_documented_in_issue_author(self):
        """AC11: issue-author.md documents checked_body_sha256, checker_exit_code, etc."""
        agent_doc = AGENTS_DIR / "issue-author.md"
        assert agent_doc.exists(), f"issue-author.md must exist: {agent_doc}"
        content = agent_doc.read_text(encoding="utf-8")
        assert "checked_body_sha256" in content, (
            "issue-author.md must document checked_body_sha256"
        )
        assert "checker_exit_code" in content, (
            "issue-author.md must document checker_exit_code"
        )
        assert "missing_sections" in content, (
            "issue-author.md must document missing_sections"
        )
        assert "missing_contract_keys" in content, (
            "issue-author.md must document missing_contract_keys"
        )


# ---------------------------------------------------------------------------
# AC12a: Write is in disallowedTools of issue-author.md
# ---------------------------------------------------------------------------


class TestIssueAuthorDisallowedTools:
    """AC12: issue-author.md must have Write in disallowedTools frontmatter."""

    def test_issue_author_write_is_in_disallowed_tools(self):
        """AC12a: disallowedTools in issue-author.md includes Write."""
        agent_doc = AGENTS_DIR / "issue-author.md"
        assert agent_doc.exists(), f"issue-author.md must exist: {agent_doc}"
        content = agent_doc.read_text(encoding="utf-8")

        # Find the frontmatter section (between --- markers)
        parts = content.split("---")
        assert len(parts) >= 3, "issue-author.md must have YAML frontmatter"
        frontmatter = parts[1]

        # Check disallowedTools block contains Write
        in_disallowed = False
        for line in frontmatter.splitlines():
            stripped = line.strip()
            if stripped.startswith("disallowedTools:"):
                in_disallowed = True
                continue
            if in_disallowed:
                if stripped.startswith("- "):
                    if stripped == "- Write":
                        return  # Found it
                elif stripped and not stripped.startswith("#"):
                    break  # End of the list

        # If we get here, Write was not found in disallowedTools
        assert False, (
            "Write must be in disallowedTools in issue-author.md frontmatter"
        )


# ---------------------------------------------------------------------------
# AC3: FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 in plan_refinement_loop.py (AC8 symbol check)
# ---------------------------------------------------------------------------


class TestPlannerContainsConstraintsV1:
    """AC8: plan_refinement_loop.py contains FAIL_CLOSED_REWRITE_CONSTRAINTS_V1."""

    def test_planner_script_contains_fail_closed_rewrite_constraints_v1(self):
        """AC8: FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 symbol is in the planner script."""
        script = SCRIPTS_DIR / "plan_refinement_loop.py"
        assert script.exists(), f"plan_refinement_loop.py must exist: {script}"
        content = script.read_text(encoding="utf-8")
        assert "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1" in content, (
            "plan_refinement_loop.py must contain FAIL_CLOSED_REWRITE_CONSTRAINTS_V1"
        )

    def test_planner_script_contains_override_policy(self):
        """AC7: plan_refinement_loop.py contains override_policy / allowed_reason_codes."""
        script = SCRIPTS_DIR / "plan_refinement_loop.py"
        content = script.read_text(encoding="utf-8")
        assert "override_policy" in content, (
            "plan_refinement_loop.py must contain override_policy"
        )
        assert "allowed_reason_codes" in content, (
            "plan_refinement_loop.py must contain allowed_reason_codes"
        )
        assert "never_override_reason_codes" in content, (
            "plan_refinement_loop.py must contain never_override_reason_codes"
        )

    def test_planner_script_contains_max_rewrite_attempts(self):
        """AC10a: plan_refinement_loop.py contains max_rewrite_attempts."""
        script = SCRIPTS_DIR / "plan_refinement_loop.py"
        content = script.read_text(encoding="utf-8")
        assert "max_rewrite_attempts" in content, (
            "plan_refinement_loop.py must contain max_rewrite_attempts"
        )

    def test_planner_script_contains_no_progress_route(self):
        """AC10a: plan_refinement_loop.py contains no_progress_route."""
        script = SCRIPTS_DIR / "plan_refinement_loop.py"
        content = script.read_text(encoding="utf-8")
        assert "no_progress_route" in content, (
            "plan_refinement_loop.py must contain no_progress_route"
        )

    def test_planner_script_contains_human_judgment_required(self):
        """AC10a: plan_refinement_loop.py returns human_judgment_required as no_progress_route."""
        script = SCRIPTS_DIR / "plan_refinement_loop.py"
        content = script.read_text(encoding="utf-8")
        assert "human_judgment_required" in content, (
            "plan_refinement_loop.py must contain human_judgment_required"
        )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])



# ---------------------------------------------------------------------------
# AC4 / AC5: NaN / Infinity strict JSON (issue #1067)
# ---------------------------------------------------------------------------


class TestAC4AC5StrictJsonNanRejection:
    """AC4/AC5: json.dumps uses allow_nan=False; json.loads rejects NaN/Infinity."""

    def test_planner_output_does_not_contain_nan(self):
        """AC4: planner output JSON is valid strict JSON (no NaN/Infinity)."""
        import math
        body = load_fixture("missing_outcome_section", "malformed") or "## Outcome\n\nTest.\n"
        output, exit_code = run_planner(make_input(body))
        # Re-serialize and parse to detect NaN
        serialized = json.dumps(output)
        reparsed = json.loads(serialized)

        def check_no_nan(obj):
            if isinstance(obj, float):
                assert not math.isnan(obj) and not math.isinf(obj), (
                    f"NaN/Infinity found in planner output: {obj}"
                )
            elif isinstance(obj, dict):
                for v in obj.values():
                    check_no_nan(v)
            elif isinstance(obj, list):
                for item in obj:
                    check_no_nan(item)
        check_no_nan(reparsed)

    def test_canonical_json_rejects_nan(self):
        """AC4: _canonical_json with allow_nan=False raises on NaN input."""
        import sys
        sys.path.insert(0, str(SCRIPTS_DIR))
        import importlib
        import plan_refinement_loop as planner_mod
        import math
        import pytest
        with pytest.raises((ValueError, TypeError)):
            planner_mod._canonical_json({"value": math.nan})

    def test_strict_json_loads_rejects_nan_string(self):
        """AC5: _strict_json_loads rejects bare NaN token (Python json quirk)."""
        import sys
        sys.path.insert(0, str(SCRIPTS_DIR))
        import plan_refinement_loop as planner_mod
        import pytest
        # Python's json.loads accepts NaN as a float by default
        # Our strict version must reject it
        with pytest.raises((ValueError, json.JSONDecodeError)):
            planner_mod._strict_json_loads('{"value": NaN}')

    def test_strict_json_loads_accepts_valid_json(self):
        """AC5: _strict_json_loads accepts normal JSON without error."""
        import sys
        sys.path.insert(0, str(SCRIPTS_DIR))
        import plan_refinement_loop as planner_mod
        result = planner_mod._strict_json_loads('{"key": "value", "n": 42}')
        assert result == {"key": "value", "n": 42}

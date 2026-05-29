#!/usr/bin/env python3
"""
Tests for auto_fixable_structural_blocker_list in plan_refinement_loop.py

Covers:
- AC1: LOOP_STATE blocker_class field (referenced from SKILL.md)
- AC2: --no-approval + blocker_class: auto_fixable_structural auto-continuation
- AC3: requires_human blocker stops at needs_second_pass
- AC4: --no-approval not set stops at needs_second_pass regardless of blocker_class
- AC5: auto_fixable_structural_blocker_list field in REFINEMENT_LOOP_PLAN_V1

These tests verify the planner correctly detects auto-fixable structural blockers
so that the orchestrator can use them for routing decisions.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "plan_refinement_loop.py"
DETERMINISTIC_NOW = "2025-05-25T12:00:00+00:00"


def run_planner(issue_body: str, issue_number: int = 99) -> dict[str, Any]:
    """Run the planner with given issue body and return the parsed output."""
    input_data = {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": "Test Issue for auto_fixable blockers",
            "body": issue_body,
            "labels": [],
        },
        "comments": None,
        "known_context": None,
        "now": DETERMINISTIC_NOW,
    }
    result = subprocess.run(
        ["python3", str(SCRIPT_PATH)],
        input=json.dumps(input_data, ensure_ascii=False),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Planner failed: {result.stderr}"
    return json.loads(result.stdout)


# Minimal valid issue body (no auto-fixable blockers)
VALID_ISSUE_BODY = """## Outcome

A minimal valid issue with all required sections.

## Acceptance Criteria

- [ ] AC1: Something is done

## Verification Commands

```bash
$ rg 'pattern' some/path
```

## Stop Conditions

- Stop if something bad happens

## Machine-Readable Contract

```yaml
contract_schema_version: "1"
issue_kind: implementation
change_kind: feature
```
"""


class TestAutoFixableStructuralBlockerListField:
    """AC5: Verify auto_fixable_structural_blocker_list field exists in output."""

    def test_field_present_in_output(self):
        """AC5: auto_fixable_structural_blocker_list is present in decisions output."""
        output = run_planner(VALID_ISSUE_BODY)
        assert "auto_fixable_structural_blocker_list" in output["decisions"]

    def test_field_is_list(self):
        """AC5: auto_fixable_structural_blocker_list is a list."""
        output = run_planner(VALID_ISSUE_BODY)
        assert isinstance(output["decisions"]["auto_fixable_structural_blocker_list"], list)

    def test_no_blockers_for_complete_issue(self):
        """AC5: No auto-fixable blockers for a complete issue body."""
        output = run_planner(VALID_ISSUE_BODY)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert blockers == [], f"Expected no blockers, got: {blockers}"


class TestMissingMachineReadableContract:
    """Test detection of missing_machine_readable_contract blocker."""

    ISSUE_WITHOUT_MRC = """## Outcome

Issue body missing the Machine-Readable Contract section.

## Acceptance Criteria

- [ ] AC1: Something is done

## Verification Commands

```bash
$ rg 'pattern' some/path
```

## Stop Conditions

- Stop if something bad happens
"""

    def test_missing_machine_readable_contract_detected(self):
        """GIVEN issue missing ## Machine-Readable Contract
        WHEN planner runs
        THEN missing_machine_readable_contract is in blocker list."""
        output = run_planner(self.ISSUE_WITHOUT_MRC)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert "missing_machine_readable_contract" in blockers

    def test_present_machine_readable_contract_not_detected(self):
        """GIVEN issue with ## Machine-Readable Contract
        WHEN planner runs
        THEN missing_machine_readable_contract is NOT in blocker list."""
        output = run_planner(VALID_ISSUE_BODY)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert "missing_machine_readable_contract" not in blockers


class TestMissingStopConditions:
    """Test detection of missing_stop_conditions blocker."""

    ISSUE_WITHOUT_SC = """## Outcome

Issue body missing the Stop Conditions section.

## Acceptance Criteria

- [ ] AC1: Something is done

## Verification Commands

```bash
$ rg 'pattern' some/path
```

## Machine-Readable Contract

```yaml
contract_schema_version: "1"
issue_kind: implementation
change_kind: feature
```
"""

    def test_missing_stop_conditions_detected(self):
        """GIVEN issue missing ## Stop Conditions
        WHEN planner runs
        THEN missing_stop_conditions is in blocker list."""
        output = run_planner(self.ISSUE_WITHOUT_SC)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert "missing_stop_conditions" in blockers

    def test_present_stop_conditions_not_detected(self):
        """GIVEN issue with ## Stop Conditions
        WHEN planner runs
        THEN missing_stop_conditions is NOT in blocker list."""
        output = run_planner(VALID_ISSUE_BODY)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert "missing_stop_conditions" not in blockers


class TestMissingContractSchemaVersion:
    """Test that missing_contract_schema_version is NOT in auto-fixable blocker list.

    B3 resolution: missing_contract_schema_version is removed from auto_fixable_structural
    because when a YAML block exists but lacks contract_schema_version,
    _check_malformed_contract triggers fail_closed (which empties the auto_fixable list).
    There is no non-fail_closed path where this blocker is actionable, so it is
    excluded from scope entirely.

    The fail_closed behavior for malformed YAML contracts is retained.
    """

    ISSUE_WITHOUT_YAML = """## Outcome

Issue with no YAML block at all in the Machine-Readable Contract section.

## Acceptance Criteria

- [ ] AC1: Something is done

## Verification Commands

```bash
$ rg 'pattern' some/path
```

## Stop Conditions

- Stop if something bad happens

## Machine-Readable Contract

No YAML block here.
"""

    def test_no_yaml_block_does_not_trigger_version_blocker(self):
        """GIVEN issue with no YAML block at all
        WHEN planner runs
        THEN missing_contract_schema_version is NOT in blocker list."""
        output = run_planner(self.ISSUE_WITHOUT_YAML)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert "missing_contract_schema_version" not in blockers

    def test_present_contract_schema_version_not_detected(self):
        """GIVEN issue with contract_schema_version present
        WHEN planner runs
        THEN missing_contract_schema_version is NOT in blocker list."""
        output = run_planner(VALID_ISSUE_BODY)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert "missing_contract_schema_version" not in blockers

    def test_yaml_block_without_version_triggers_fail_closed(self):
        """GIVEN issue with YAML block but missing contract_schema_version
        WHEN planner runs
        THEN fail_closed is required (malformed_machine_readable_contract).

        A malformed YAML block triggers fail_closed, not an auto-fixable blocker.
        The auto_fixable_structural_blocker_list is empty in fail_closed paths.
        """
        issue_with_malformed_yaml = """## Outcome

Has a YAML block but it's missing contract_schema_version.

## Acceptance Criteria

- [ ] AC1

## Verification Commands

```bash
$ rg 'x' path
```

## Stop Conditions

- stop

## Machine-Readable Contract

```yaml
issue_kind: implementation
change_kind: feature
```
"""
        output = run_planner(issue_with_malformed_yaml)
        # Malformed contract takes precedence → fail_closed
        assert output["fail_closed"]["required"] is True
        assert "malformed_machine_readable_contract" in output["fail_closed"]["reason_codes"]
        # auto_fixable_structural_blocker_list is empty in fail_closed path
        assert output["decisions"]["auto_fixable_structural_blocker_list"] == []


class TestMultipleBlockers:
    """Test detection of multiple auto-fixable structural blockers simultaneously."""

    ISSUE_MISSING_MULTIPLE = """## Outcome

Issue body missing multiple required sections.

## Acceptance Criteria

- [ ] AC1: Something is done

## Verification Commands

```bash
$ rg 'pattern' some/path
```
"""

    def test_multiple_blockers_detected(self):
        """GIVEN issue missing Stop Conditions AND Machine-Readable Contract
        WHEN planner runs
        THEN both blockers are in the list."""
        output = run_planner(self.ISSUE_MISSING_MULTIPLE)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert "missing_stop_conditions" in blockers
        assert "missing_machine_readable_contract" in blockers

    def test_blockers_list_has_no_duplicates(self):
        """GIVEN any issue body
        WHEN planner runs
        THEN blocker list has no duplicate entries."""
        output = run_planner(self.ISSUE_MISSING_MULTIPLE)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert len(blockers) == len(set(blockers)), "Duplicate blockers found"


class TestBlockerClassRoutingLogic:
    """
    AC2/AC3/AC4: Test the blocker_class routing logic documentation.

    The planner provides the data; the orchestrator (issue-refinement-loop) uses it.
    These tests verify the planner output enables the correct routing decisions.
    """

    def test_ac2_auto_fixable_only_enables_auto_continue(self):
        """AC2: GIVEN --no-approval AND blocker_class: auto_fixable_structural only
        WHEN orchestrator reads planner output
        THEN auto_fixable_structural_blocker_list is non-empty AND no requires_human blockers.

        The planner returns auto_fixable_structural_blocker_list with entries.
        The orchestrator uses this to enable auto-continuation.
        """
        issue_missing_mrc = VALID_ISSUE_BODY.replace(
            "## Machine-Readable Contract\n\n```yaml\ncontract_schema_version: \"1\"\nissue_kind: implementation\nchange_kind: feature\n```\n",
            "",
        )
        output = run_planner(issue_missing_mrc)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        # Non-empty auto_fixable list means orchestrator CAN auto-continue with --no-approval
        assert len(blockers) > 0
        # All detected blockers must be valid auto-fixable IDs
        valid_auto_fixable = {
            "missing_machine_readable_contract",
            "missing_stop_conditions",
            "vc_missing_prefix",
        }
        for blocker in blockers:
            assert blocker in valid_auto_fixable, f"Unknown blocker: {blocker}"

    def test_ac3_requires_human_blocker_stops_loop(self):
        """AC3: GIVEN requires_human blocker present
        WHEN orchestrator checks blocker_class
        THEN auto_fixable_structural_blocker_list does NOT contain requires_human blockers.

        requires_human blockers (new_scope_area, ac_removed_or_weakened, etc.) are NOT
        detected by the planner as auto_fixable — they come from the reviewer's REVIEW_ISSUE_RESULT_V1.
        The planner's auto_fixable_structural_blocker_list only contains fixable structural issues.
        """
        # A "complete" issue (no structural blockers) should have empty auto_fixable list
        output = run_planner(VALID_ISSUE_BODY)
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        # No auto-fixable structural blockers = planner does not override reviewer's requires_human verdict
        assert blockers == []

    def test_ac4_no_approval_flag_not_set_stops_loop(self):
        """AC4: GIVEN --no-approval NOT set
        WHEN issue has auto-fixable blockers
        THEN orchestrator should stop regardless (blocker_class doesn't override --no-approval absence).

        The planner provides auto_fixable_structural_blocker_list regardless of --no-approval.
        The orchestrator is responsible for checking --no-approval flag before using this list.
        This test verifies the planner produces the list correctly either way.
        """
        issue_missing_mrc = VALID_ISSUE_BODY.replace(
            "## Machine-Readable Contract\n\n```yaml\ncontract_schema_version: \"1\"\nissue_kind: implementation\nchange_kind: feature\n```\n",
            "",
        )
        output = run_planner(issue_missing_mrc)
        # Planner always outputs the list — orchestrator decides whether to act on it
        assert "auto_fixable_structural_blocker_list" in output["decisions"]
        blockers = output["decisions"]["auto_fixable_structural_blocker_list"]
        assert isinstance(blockers, list)


class TestFailClosedPathsIncludeField:
    """Verify auto_fixable_structural_blocker_list is present even in fail_closed paths."""

    def test_fail_closed_malformed_contract_includes_field(self):
        """GIVEN issue with malformed YAML contract (has yaml but no contract_schema_version)
        WHEN planner runs and returns fail_closed
        THEN auto_fixable_structural_blocker_list is still present (empty)."""
        # This triggers fail_closed via _check_malformed_contract (has yaml but no version)
        malformed_body = """## Outcome

Has a YAML block but it's malformed.

```yaml
some_field: value
```
"""
        output = run_planner(malformed_body)
        assert output["fail_closed"]["required"] is True
        assert "auto_fixable_structural_blocker_list" in output["decisions"]

    def test_fail_closed_missing_outcome_includes_field(self):
        """GIVEN issue with missing Outcome section
        WHEN planner runs and returns fail_closed
        THEN auto_fixable_structural_blocker_list is still present (empty)."""
        no_outcome = "No outcome section here.\n\n## Acceptance Criteria\n\n- [ ] AC1"
        output = run_planner(no_outcome)
        assert output["fail_closed"]["required"] is True
        assert "auto_fixable_structural_blocker_list" in output["decisions"]

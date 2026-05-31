#!/usr/bin/env python3
"""
Tests for parent delivery-rollup issue_kind handling in plan_refinement_loop.py.

AC9 Outline:
  - parent delivery-rollup without Outcome → pass (no fail_closed)
  - implementation without Outcome → fail_closed (reason: missing_required_section)
  - fenced code 内に issue_kind: parent があるだけの implementation → parent 扱いしない
"""

import json
import subprocess
from pathlib import Path
from typing import Any


SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "plan_refinement_loop.py"
)
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _make_input(body: str, issue_number: int = 999) -> dict[str, Any]:
    """Build a minimal planner input dict."""
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": "Test Issue",
            "body": body,
            "labels": [],
        },
        "comments": None,
        "known_context": None,
    }


def _run_planner(body: str, issue_number: int = 999) -> tuple[dict[str, Any], int]:
    """Run the planner with given issue body. Returns (output_dict, exit_code)."""
    input_data = _make_input(body, issue_number)
    result = subprocess.run(
        ["python3", str(SCRIPT_PATH)],
        input=json.dumps(input_data, ensure_ascii=False),
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout), result.returncode


# ---------------------------------------------------------------------------
# AC9-1: parent delivery-rollup without Outcome → pass (no fail_closed)
# ---------------------------------------------------------------------------

class TestParentDeliveryRollupWithoutOutcome:
    """
    GIVEN an issue with issue_kind: parent and parent_mode: delivery-rollup
    AND the issue body does NOT contain ## Outcome
    WHEN the planner runs
    THEN fail_closed.required is False
    """

    def test_uses_fixture_file(self):
        """AC9-1 (fixture-based): parent delivery-rollup without Outcome does not fail_closed."""
        fixture_path = FIXTURES_DIR / "positive" / "parent_delivery_rollup_without_outcome.md"
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"
        body = fixture_path.read_text(encoding="utf-8")

        assert "## Outcome" not in body, "Fixture must not have ## Outcome section"

        output, exit_code = _run_planner(body, issue_number=100)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is False, (
            "parent delivery-rollup without Outcome must NOT fail_closed"
        )
        assert "missing_required_section" not in output["fail_closed"]["reason_codes"]
        assert "missing_required_parent_section" not in output["fail_closed"]["reason_codes"]

    def test_inline_minimal_parent_delivery_rollup_without_outcome(self):
        """AC9-1 (inline): minimal parent delivery-rollup body without Outcome passes."""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: "test"
change_kind: workflow
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Summary

Test summary.

## Goal

Test goal.

## Desired Destination

Test destination.

## Current Validated Scope

Test scope.

## Decisions Fixed

- 2026-05-31: test decision

## Quality Decision Record

- `Status`: N/A
- `Decision Date`: 未記録
- `Does this prove the parent goal complete?`: N/A
- `Reason`: N/A
- `Evidence`: N/A
- `Next Action`: なし

## Parent Closure Rule

- `delivery-rollup`: child issue で close する

## Child Issues

- [ ] #1 — test child

## Remaining Parent Gaps

- [ ] なし

## Phase Handoff Contract

- `Desired Destination`

## Acceptance Criteria

- [ ] test AC
"""
        assert "## Outcome" not in body

        output, exit_code = _run_planner(body, issue_number=101)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is False, (
            "parent delivery-rollup without Outcome must not fail_closed"
        )


# ---------------------------------------------------------------------------
# AC9-2: implementation without Outcome → fail_closed
# ---------------------------------------------------------------------------

class TestImplementationWithoutOutcome:
    """
    GIVEN an issue with issue_kind: implementation
    AND the issue body does NOT contain ## Outcome
    WHEN the planner runs
    THEN fail_closed.required is True with reason missing_required_section
    """

    def test_implementation_without_outcome_fails_closed(self):
        """AC9-2: implementation issue missing Outcome → fail_closed."""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: code
```

## Parent Issue

none

## Parent Goal Ref

- Goal: test goal
- Desired Destination: test destination

## Current Validated Scope

- src/test.ts

## Remaining Parent Gaps

なし

## In Scope

- src/test.ts

## Out of Scope

- other stuff

## Acceptance Criteria

- [ ] test AC

## Verification Commands

- `pnpm test`

## Allowed Paths

- src/test.ts

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合

## Required Skills

なし
"""
        assert "## Outcome" not in body

        output, exit_code = _run_planner(body, issue_number=102)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True, (
            "implementation issue without Outcome must fail_closed"
        )
        assert "missing_required_section" in output["fail_closed"]["reason_codes"], (
            "reason_code must be missing_required_section"
        )


# ---------------------------------------------------------------------------
# AC9-3: fenced code 内に issue_kind: parent があるだけの implementation → parent 扱いしない
# ---------------------------------------------------------------------------

class TestFencedCodeIssueKindIsNotParsed:
    """
    GIVEN an issue body where 'issue_kind: parent' appears ONLY inside a fenced code block
    (not in Machine-Readable Contract section's YAML)
    AND there is no ## Outcome section
    WHEN the planner runs
    THEN the issue is NOT treated as parent (fail_closed.required is True)

    AC4: Only the YAML under ## Machine-Readable Contract is parsed for issue_kind.
    """

    def test_fenced_code_issue_kind_parent_is_not_parent(self):
        """AC9-3: issue_kind: parent in fenced code outside MRC section → not parent."""
        body = """\
## Some Description Section

This issue demonstrates how the planner must not be fooled by:

```yaml
issue_kind: parent
parent_mode: delivery-rollup
```

appearing in an unrelated section.

## In Scope

- feature implementation

## Acceptance Criteria

- [ ] test AC

## Verification Commands

- `pnpm test`
"""
        assert "## Outcome" not in body
        assert "## Machine-Readable Contract" not in body

        output, exit_code = _run_planner(body, issue_number=103)

        assert exit_code == 0
        # No Machine-Readable Contract section → issue_kind is None
        # Falls back to Outcome check → Outcome missing → fail_closed
        assert output["fail_closed"]["required"] is True, (
            "fenced code with issue_kind: parent should NOT be treated as parent issue"
        )

    def test_mrc_section_with_issue_kind_parent_in_fenced_code_but_no_mrc_yaml(self):
        """AC9-3 variant: issue_kind: parent in explanation text of MRC, not parsed YAML."""
        body = """\
## Machine-Readable Contract

This section normally contains a YAML block. Below is an EXAMPLE showing
what issue_kind: parent looks like — but this is just explanatory text,
not a parseable YAML block.

For reference:
    issue_kind: parent
    parent_mode: delivery-rollup

(No actual fenced yaml block here.)

## Summary

Test summary.

## Acceptance Criteria

- [ ] test AC
"""
        assert "## Outcome" not in body

        output, exit_code = _run_planner(body, issue_number=104)

        assert exit_code == 0
        # No yaml fenced block in MRC section → _extract_machine_contract returns None
        # Falls back to Outcome check → fail_closed
        assert output["fail_closed"]["required"] is True, (
            "Plain text 'issue_kind: parent' without fenced yaml must not be treated as parent"
        )


# ---------------------------------------------------------------------------
# AC9: parent delivery-rollup with missing required parent section → fail_closed
# (AC7 verification)
# ---------------------------------------------------------------------------

class TestParentDeliveryRollupMissingParentSection:
    """
    GIVEN an issue with issue_kind: parent and parent_mode: delivery-rollup
    AND a required parent template section (e.g., ## Summary) is missing
    WHEN the planner runs
    THEN fail_closed.required is True with reason missing_required_parent_section
    """

    def test_missing_summary_fails_closed_with_parent_reason(self):
        """AC7: parent delivery-rollup missing required parent section → fail_closed."""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: "test"
change_kind: workflow
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Goal

Test goal (but Summary is missing).

## Desired Destination

Test destination.

## Current Validated Scope

Test scope.

## Decisions Fixed

- 2026-05-31: test

## Quality Decision Record

- `Status`: N/A

## Parent Closure Rule

- `delivery-rollup`: child issues closed.

## Child Issues

- [ ] #1 — test

## Remaining Parent Gaps

- [ ] なし

## Phase Handoff Contract

- `Desired Destination`

## Acceptance Criteria

- [ ] test AC
"""
        assert "## Outcome" not in body
        assert "## Summary" not in body

        output, exit_code = _run_planner(body, issue_number=105)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True, (
            "parent delivery-rollup missing required parent section must fail_closed"
        )
        assert "missing_required_parent_section" in output["fail_closed"]["reason_codes"], (
            "reason_code must be missing_required_parent_section (AC7)"
        )
        # Must NOT use implementation reason_code
        assert "missing_required_section" not in output["fail_closed"]["reason_codes"], (
            "parent delivery-rollup must not use missing_required_section reason_code"
        )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

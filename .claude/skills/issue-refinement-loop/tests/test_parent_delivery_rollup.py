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


# ---------------------------------------------------------------------------
# Blocker 1: fenced code 内の偽 MRC セクションは parent と認識されてはならない
# AC9 regression: fenced code in issue body contains a fake ## Machine-Readable Contract
# ---------------------------------------------------------------------------

class TestFencedCodeFakeMRCSectionNotRecognized:
    """
    GIVEN an issue body where ## Machine-Readable Contract and ```yaml appear
    ONLY inside a fenced code block (the entire fake MRC is fenced)
    AND the real body has NO ## Outcome section
    AND no real ## Machine-Readable Contract heading outside fences
    WHEN the planner runs
    THEN the issue is NOT treated as parent (not a delivery-rollup)
    AND fail_closed.required is True with reason missing_required_section
    """

    def test_fake_mrc_inside_fenced_code_does_not_override_outcome_check(self):
        """Blocker 1 / AC9 regression: ## MRC inside fenced code is not parsed.

        Uses a 4-backtick outer fence (````markdown) so the inner 3-backtick
        ```yaml block does not accidentally close the outer fence.
        This is the valid Markdown way to nest fenced blocks.
        """
        body = """\
## Some Section

Here is an example of what a parent issue looks like. Do NOT be misled:

````markdown
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: "fake"
change_kind: workflow
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Outcome

This outcome is also inside the fenced block and must not be detected.
````

This issue has no real ## Outcome and no real ## Machine-Readable Contract.

## Acceptance Criteria

- [ ] test
"""
        output, exit_code = _run_planner(body, issue_number=200)

        assert exit_code == 0
        # No real Machine-Readable Contract → no issue_kind detected → Outcome fallback
        # → missing Outcome → fail_closed
        assert output["fail_closed"]["required"] is True, (
            "Fake MRC inside fenced code must not be recognized — should fail_closed"
        )
        assert "missing_required_section" in output["fail_closed"]["reason_codes"], (
            "Should fail with missing_required_section (not treated as parent delivery-rollup)"
        )

    def test_fenced_mrc_heading_only_no_real_mrc(self):
        """Blocker 1 AC9 regression: fenced block has ## Machine-Readable Contract heading."""
        body = """\
## Introduction

The following is a documentation example:

```
## Machine-Readable Contract

contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
```

## Acceptance Criteria

- [ ] test
"""
        output, exit_code = _run_planner(body, issue_number=201)

        assert exit_code == 0
        # No real MRC section (heading was inside fence) → falls back to Outcome check
        # No Outcome → fail_closed
        assert output["fail_closed"]["required"] is True, (
            "MRC heading inside fenced code must not be recognized as a real heading"
        )


# ---------------------------------------------------------------------------
# Blocker 2: template load failure → fail_closed (not fail-open)
# ---------------------------------------------------------------------------

class TestTemplateLoadFailure:
    """
    GIVEN an issue with a known issue_kind (e.g. implementation)
    AND the template file is inaccessible or unreadable
    WHEN load_required_section_labels() is called
    THEN the result has error=template_required_sections_unavailable
    """

    def test_template_load_result_distinguishes_success_from_error(self):
        """Blocker 2: TemplateLoadResult.error is None on success."""
        import sys
        import importlib
        from pathlib import Path

        # Import the script module directly for unit testing
        script_path = SCRIPT_PATH
        import importlib.util
        spec = importlib.util.spec_from_file_location("plan_refinement_loop", script_path)
        mod = importlib.util.module_from_spec(spec)
        # Register module in sys.modules before exec to allow dataclass string annotations
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        # Test with a non-existent template path
        result = mod.load_required_section_labels(Path("/nonexistent/template.yml"))
        assert result.error is not None, (
            "load_required_section_labels with missing file must return error"
        )
        assert result.error == "template_required_sections_unavailable", (
            f"Expected template_required_sections_unavailable, got {result.error}"
        )
        assert result.required_labels == [], (
            "required_labels must be empty on error"
        )

    def test_template_load_result_success_has_none_error(self):
        """Blocker 2: TemplateLoadResult.error is None when template loads successfully."""
        import sys
        import importlib.util
        from pathlib import Path

        script_path = SCRIPT_PATH
        spec = importlib.util.spec_from_file_location("plan_refinement_loop", script_path)
        mod = importlib.util.module_from_spec(spec)
        # Register module in sys.modules before exec to allow dataclass string annotations
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        # Use a real template file from the repo
        repo_root = mod._find_repo_root()
        template_path = repo_root / ".github" / "ISSUE_TEMPLATE" / "implementation.yml"
        if not template_path.exists():
            import pytest
            pytest.skip("implementation.yml template not found")

        result = mod.load_required_section_labels(template_path)
        assert result.error is None, (
            f"Expected error=None on success, got error={result.error}"
        )
        assert len(result.required_labels) > 0, (
            "implementation template should have required labels"
        )


# ---------------------------------------------------------------------------
# Blocker 3: unknown issue_kind → fail_closed (not fall-through to Outcome check)
# ---------------------------------------------------------------------------

class TestUnknownIssueKind:
    """
    GIVEN an issue with an issue_kind not in {implementation, parent, research}
    WHEN the planner runs
    THEN fail_closed.required is True with reason unknown_issue_kind
    """

    def test_unknown_issue_kind_fails_closed(self):
        """Blocker 3: issue_kind not in allowlist → fail_closed."""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: ../../workflows/ci
goal_ref: "path traversal attempt"
change_kind: workflow
```

## Outcome

This should still fail because issue_kind is not in the allowlist.

## Acceptance Criteria

- [ ] test
"""
        output, exit_code = _run_planner(body, issue_number=300)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True, (
            "Unknown issue_kind must produce fail_closed"
        )
        assert "unknown_issue_kind" in output["fail_closed"]["reason_codes"], (
            "reason_code must be unknown_issue_kind for non-allowlisted issue_kind"
        )

    def test_path_traversal_issue_kind_not_resolved(self):
        """Blocker 3: resolve_issue_template returns None for non-allowlisted issue_kind."""
        import sys
        import importlib.util
        from pathlib import Path

        script_path = SCRIPT_PATH
        spec = importlib.util.spec_from_file_location("plan_refinement_loop", script_path)
        mod = importlib.util.module_from_spec(spec)
        # Register module in sys.modules before exec to allow dataclass string annotations
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        repo_root = mod._find_repo_root()

        # Path traversal attempt
        result = mod.resolve_issue_template("../../workflows/ci", repo_root)
        assert result is None, (
            "resolve_issue_template must return None for non-allowlisted issue_kind"
        )

        # Another traversal attempt
        result2 = mod.resolve_issue_template("../something", repo_root)
        assert result2 is None, (
            "resolve_issue_template must return None for path traversal attempts"
        )

        # Valid issue_kind should still work
        result3 = mod.resolve_issue_template("implementation", repo_root)
        # May be None if file doesn't exist, but should NOT raise
        # Just check it doesn't crash and is either Path or None
        assert result3 is None or hasattr(result3, "exists"), (
            "Valid issue_kind should return Path or None"
        )


# ---------------------------------------------------------------------------
# Blocker 5: #483 実本文相当の fixture で fail_closed.required == False
# ---------------------------------------------------------------------------

class TestParent483FixtureDeliveryRollup:
    """
    GIVEN a fixture equivalent to #483 (parent delivery-rollup, no ## Outcome)
    WHEN the planner runs
    THEN fail_closed.required is False (the original bug is fixed)

    This tests that the planner correctly handles real-world parent delivery-rollup
    issues that have no ## Outcome section but are otherwise complete.
    """

    def test_parent_483_equivalent_fixture_does_not_fail_closed(self):
        """Blocker 5: #483-equivalent parent delivery-rollup fixture → fail_closed.required=False."""
        fixture_path = (
            FIXTURES_DIR / "positive" / "parent_483_delivery_rollup_without_outcome.md"
        )
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

        body = fixture_path.read_text(encoding="utf-8")

        # Verify fixture properties
        assert "## Outcome" not in body, "Fixture must not have ## Outcome section"
        assert "issue_kind: parent" in body, "Fixture must have issue_kind: parent"
        assert "parent_mode: delivery-rollup" in body, "Fixture must be delivery-rollup"
        assert "## Summary" in body, "Fixture must have ## Summary"
        assert "## Goal" in body, "Fixture must have ## Goal"
        assert "## Parent Closure Rule" in body, "Fixture must have ## Parent Closure Rule"
        assert "## Acceptance Criteria" in body, "Fixture must have ## Acceptance Criteria"

        output, exit_code = _run_planner(body, issue_number=483)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is False, (
            "#483-equivalent parent delivery-rollup without Outcome must NOT fail_closed — "
            f"got reason_codes: {output['fail_closed']['reason_codes']}"
        )
        assert "missing_required_section" not in output["fail_closed"]["reason_codes"]
        assert "missing_required_parent_section" not in output["fail_closed"]["reason_codes"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

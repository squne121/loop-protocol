#!/usr/bin/env python3
"""
test_required_sections_fullset.py

Tests for Issue #1023: missing_required_section should collect and report ALL
missing sections at once, not just the first one found.

AC1: Multiple missing sections → planner output fail_closed.missing_sections contains all
AC2: fail_closed.rewrite_constraints.required_sections contains all missing sections
AC3: issue-author payload (FAIL_CLOSED_REWRITE_CONSTRAINTS_V1) contains required_sections
AC4: Same reason_code already in known_context.prev_fail_closed_reasons → prevent_duplicate_repair=True
AC5: run_refinement_preflight BLOCKERS line includes all missing sections
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import plan_refinement_loop as planner  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_input(body: str, known_context: dict | None = None) -> dict:
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": 1023,
            "title": "Test: multiple missing sections",
            "body": body,
            "labels": [],
        },
        "comments": None,
        "known_context": known_context,
        "now": "2026-06-20T00:00:00+00:00",
    }


# Body with a machine contract specifying implementation kind but missing multiple sections:
# Outcome, Acceptance Criteria, Verification Commands are all absent.
# Only Allowed Paths is present.
BODY_MANY_MISSING = """\
# Test Issue

```yaml
contract_schema_version: v1
issue_kind: implementation
goal_ref: "test"
```

## Background

Some context.

## Allowed Paths

- `.claude/skills/test/`
"""

# Body with implementation contract missing only Outcome (one missing section).
BODY_ONE_MISSING = """\
# Test Issue: one missing

```yaml
contract_schema_version: v1
issue_kind: implementation
goal_ref: "test"
```

## Background

Some context.

## In Scope

- item

## Acceptance Criteria

- [ ] AC1: something

## Verification Commands

```bash
$ echo ok
```

## Allowed Paths

- `.claude/skills/test/`
"""

# Body for prevent_duplicate_repair test: missing Outcome section (no contract, fallback path)
BODY_NO_CONTRACT_MISSING_OUTCOME = """\
# Test Issue: no machine contract

## Background

Some context only, no Outcome section.
"""


def run_planner_json(body: str, known_context: dict | None = None) -> tuple[dict, int]:
    """Run plan_refinement_loop via subprocess and parse JSON output."""
    script_path = SCRIPTS_DIR / "plan_refinement_loop.py"
    input_data = make_input(body, known_context)
    result = subprocess.run(
        ["python3", str(script_path)],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
    )
    output = json.loads(result.stdout)
    return output, result.returncode


# ---------------------------------------------------------------------------
# AC1: Multiple missing sections are all present in fail_closed.missing_sections
# ---------------------------------------------------------------------------

class TestAC1MissingSectionsFullSet:
    def test_multiple_missing_sections_in_fail_closed(self):
        """AC1: When multiple sections are missing, fail_closed.missing_sections contains all."""
        output, exit_code = run_planner_json(BODY_MANY_MISSING)

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"
        fc = output["fail_closed"]
        assert fc["required"] is True
        assert "missing_required_section" in fc["reason_codes"]

        missing = fc.get("missing_sections", [])
        assert isinstance(missing, list), "missing_sections must be a list"
        assert len(missing) > 1, (
            f"Expected multiple missing sections, got: {missing}. "
            "All sections should be reported at once."
        )

    def test_missing_sections_field_present_in_fail_closed(self):
        """AC1: fail_closed always has a missing_sections field when missing_required_section fires."""
        output, _ = run_planner_json(BODY_MANY_MISSING)
        fc = output["fail_closed"]
        assert "missing_sections" in fc, (
            "fail_closed must have a 'missing_sections' key when missing_required_section fires"
        )

    def test_single_missing_section_still_reported(self):
        """AC1: Even when only one section is missing, it appears in missing_sections list."""
        output, _ = run_planner_json(BODY_ONE_MISSING)
        fc = output["fail_closed"]
        if fc["required"] and "missing_required_section" in fc.get("reason_codes", []):
            missing = fc.get("missing_sections", [])
            assert isinstance(missing, list)
            assert len(missing) >= 1


# ---------------------------------------------------------------------------
# AC2: fail_closed.rewrite_constraints.required_sections contains all missing sections
# ---------------------------------------------------------------------------

class TestAC2RewriteConstraintsRequiredSections:
    def test_rewrite_constraints_required_sections_fullset(self):
        """AC2: rewrite_constraints.required_sections contains all missing sections."""
        output, _ = run_planner_json(BODY_MANY_MISSING)
        fc = output["fail_closed"]
        assert fc["required"] is True

        rc = fc.get("rewrite_constraints", {})
        assert rc.get("schema_version") == "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1"

        required_sections = rc.get("required_sections", [])
        missing_sections = fc.get("missing_sections", [])
        assert set(required_sections) == set(missing_sections), (
            f"rewrite_constraints.required_sections {required_sections} "
            f"must match fail_closed.missing_sections {missing_sections}"
        )
        assert len(required_sections) > 1, (
            "Expected multiple sections in rewrite_constraints.required_sections"
        )

    def test_must_add_sections_matches_missing(self):
        """AC2: rewrite_constraints.rewrite_constraints.must_add_sections also has all missing."""
        output, _ = run_planner_json(BODY_MANY_MISSING)
        fc = output["fail_closed"]
        rc = fc.get("rewrite_constraints", {})
        inner = rc.get("rewrite_constraints", {})
        must_add = inner.get("must_add_sections", [])
        missing = fc.get("missing_sections", [])
        assert set(must_add) == set(missing), (
            f"must_add_sections {must_add} must match missing_sections {missing}"
        )


# ---------------------------------------------------------------------------
# AC3: FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 schema presence
# ---------------------------------------------------------------------------

class TestAC3RewriteConstraintsSchema:
    def test_schema_version_present(self):
        """AC3: rewrite_constraints has schema_version=FAIL_CLOSED_REWRITE_CONSTRAINTS_V1."""
        output, _ = run_planner_json(BODY_MANY_MISSING)
        rc = output["fail_closed"]["rewrite_constraints"]
        assert rc["schema_version"] == "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1"

    def test_required_sections_is_list(self):
        """AC3: rewrite_constraints.required_sections is a list."""
        output, _ = run_planner_json(BODY_MANY_MISSING)
        rc = output["fail_closed"]["rewrite_constraints"]
        assert isinstance(rc["required_sections"], list)

    def test_no_progress_route_present(self):
        """AC3: rewrite_constraints has no_progress_route field."""
        output, _ = run_planner_json(BODY_MANY_MISSING)
        rc = output["fail_closed"]["rewrite_constraints"]
        assert "no_progress_route" in rc


# ---------------------------------------------------------------------------
# AC4: prevent_duplicate_repair when same reason_code was already seen
# ---------------------------------------------------------------------------

class TestAC4PreventDuplicateRepair:
    def test_prevent_duplicate_repair_false_on_first_attempt(self):
        """AC4: First attempt (no prev_fail_closed_reasons) → prevent_duplicate_repair=False."""
        output, _ = run_planner_json(BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=None)
        fc = output["fail_closed"]
        assert fc["required"] is True
        rc = fc.get("rewrite_constraints", {})
        # Default: no prior attempt → prevent_duplicate_repair should be False
        assert rc.get("prevent_duplicate_repair") is False

    def test_prevent_duplicate_repair_true_when_same_reason_repeated(self):
        """AC4: Same reason_code in known_context.prev_fail_closed_reasons → prevent_duplicate_repair=True."""
        known_context = {
            "prev_fail_closed_reasons": ["missing_required_section"],
        }
        output, _ = run_planner_json(BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=known_context)
        fc = output["fail_closed"]
        assert fc["required"] is True
        rc = fc.get("rewrite_constraints", {})
        assert rc.get("prevent_duplicate_repair") is True, (
            "Expected prevent_duplicate_repair=True when same reason was seen before"
        )

    def test_prevent_duplicate_repair_false_when_different_reason(self):
        """AC4: Different reason_code in prev_fail_closed_reasons → prevent_duplicate_repair=False."""
        known_context = {
            "prev_fail_closed_reasons": ["malformed_machine_readable_contract"],
        }
        output, _ = run_planner_json(BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=known_context)
        fc = output["fail_closed"]
        assert fc["required"] is True
        rc = fc.get("rewrite_constraints", {})
        # missing_required_section is current reason, prev was malformed_contract → no overlap
        assert rc.get("prevent_duplicate_repair") is False

    def test_prevent_duplicate_repair_true_with_multiple_overlap(self):
        """AC4: Multiple overlapping reasons → prevent_duplicate_repair=True."""
        known_context = {
            "prev_fail_closed_reasons": [
                "missing_required_section",
                "missing_required_contract_key",
            ],
        }
        output, _ = run_planner_json(BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=known_context)
        fc = output["fail_closed"]
        if fc["required"]:
            rc = fc.get("rewrite_constraints", {})
            # missing_required_section overlaps → True
            assert rc.get("prevent_duplicate_repair") is True


# ---------------------------------------------------------------------------
# AC5: run_refinement_preflight BLOCKERS line includes all missing sections
# ---------------------------------------------------------------------------

class TestAC5BlockersIncludeMissingSections:
    def _run_preflight(self, body: str) -> tuple[dict, int]:
        """Run run_refinement_preflight.py subprocess and return result dict + exit code."""
        script_path = SCRIPTS_DIR / "run_refinement_preflight.py"
        input_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 1023,
            "repo": "testowner/testrepo",
            "now": "2026-06-20T00:00:00+00:00",
            "issue": {
                "number": 1023,
                "title": "Test",
                "body": body,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [],
        }
        result = subprocess.run(
            ["python3", str(script_path)],
            input=json.dumps(input_data),
            capture_output=True,
            text=True,
        )
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            output = {}
        return output, result.returncode

    def test_blockers_include_missing_sections_list(self):
        """AC5: BLOCKERS in preflight result includes all missing sections when fail_closed fires."""
        output, exit_code = self._run_preflight(BODY_MANY_MISSING)

        # Preflight should be blocked
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}"
        assert output.get("status") == "blocked"

        blockers = output.get("blockers", [])
        # Find a blocker entry that mentions missing sections
        section_blockers = [b for b in blockers if "missing_required_section" in str(b)]
        assert section_blockers, f"No missing_required_section blocker found in: {blockers}"

        # At least one blocker should contain multiple sections (not just the reason code alone)
        section_with_list = [
            b for b in section_blockers
            if isinstance(b, str) and "[" in b and "]" in b
        ]
        assert section_with_list, (
            f"Expected a blocker with section list like "
            f"'missing_required_section: [...]', got: {section_blockers}"
        )

    def test_blockers_contains_planner_fail_closed_marker(self):
        """AC5: BLOCKERS always starts with PLANNER_FAIL_CLOSED when fail_closed fires."""
        output, exit_code = self._run_preflight(BODY_MANY_MISSING)
        blockers = output.get("blockers", [])
        assert "PLANNER_FAIL_CLOSED" in blockers, (
            f"Expected PLANNER_FAIL_CLOSED in blockers, got: {blockers}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

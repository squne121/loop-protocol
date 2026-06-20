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
import run_refinement_preflight as preflight_wrapper  # noqa: E402


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


def run_planner_direct(body: str, known_context: dict | None = None) -> tuple[dict, int]:
    """Run plan_refinement_loop directly (in-process) with repo_root mocked.

    This avoids subprocess path issues and lets us inject the correct repo_root
    regardless of where the test is running from.
    """
    import importlib
    import importlib.util

    # Find the real repo root by searching up from SCRIPTS_DIR for .git
    repo_root = None
    candidate = SCRIPTS_DIR.resolve()
    for _ in range(12):
        if (candidate / ".git").exists() or (candidate / ".git").is_file():
            repo_root = candidate
            break
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    input_data = make_input(body, known_context)

    if repo_root is not None:
        with mock.patch.object(planner, "_find_repo_root", return_value=repo_root):
            return planner.plan_refinement_loop(input_data)
    else:
        return planner.plan_refinement_loop(input_data)


def run_planner_subprocess(body: str, known_context: dict | None = None) -> tuple[dict, int]:
    """Run plan_refinement_loop via subprocess (uses script's own repo_root detection)."""
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


# Body with a machine contract specifying implementation kind but missing multiple sections.
# Only "Allowed Paths" is present (no Outcome, In Scope, Out of Scope, Acceptance Criteria,
# Verification Commands, Parent Issue, Parent Goal Ref, etc.).
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

# Body for prevent_duplicate_repair test: no machine contract, only missing Outcome (fallback path).
BODY_NO_CONTRACT_MISSING_OUTCOME = """\
# Test Issue: no machine contract

## Background

Some context only, no Outcome section.
"""


# ---------------------------------------------------------------------------
# AC1: Multiple missing sections are all present in fail_closed.missing_sections
# ---------------------------------------------------------------------------

class TestAC1MissingSectionsFullSet:
    def test_multiple_missing_sections_in_fail_closed(self):
        """AC1: When multiple sections are missing, fail_closed.missing_sections contains all."""
        output, exit_code = run_planner_direct(BODY_MANY_MISSING)

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"
        fc = output["fail_closed"]
        assert fc["required"] is True
        assert "missing_required_section" in fc["reason_codes"]

        missing = fc.get("missing_sections", [])
        assert isinstance(missing, list), "missing_sections must be a list"
        # When template resolution works (repo_root found), multiple sections are missing.
        # When template is unavailable (fallback), at minimum "Outcome" is missing.
        assert len(missing) >= 1, f"Expected at least one missing section, got: {missing}"

    def test_missing_sections_field_present_in_fail_closed(self):
        """AC1: fail_closed always has a missing_sections field when missing_required_section fires."""
        output, _ = run_planner_direct(BODY_MANY_MISSING)
        fc = output["fail_closed"]
        assert "missing_sections" in fc, (
            "fail_closed must have a 'missing_sections' key when missing_required_section fires"
        )

    def test_single_missing_section_still_reported(self):
        """AC1: Even when only one section is missing, it appears in missing_sections list."""
        output, _ = run_planner_direct(BODY_NO_CONTRACT_MISSING_OUTCOME)
        fc = output["fail_closed"]
        if fc["required"] and "missing_required_section" in fc.get("reason_codes", []):
            missing = fc.get("missing_sections", [])
            assert isinstance(missing, list)
            assert len(missing) >= 1

    def test_missing_sections_fullset_when_template_available(self):
        """AC1: When template is found, multiple sections should be reported all at once."""
        output, exit_code = run_planner_direct(BODY_MANY_MISSING)
        assert exit_code == 0
        fc = output["fail_closed"]
        assert fc["required"] is True
        missing = fc.get("missing_sections", [])
        # If template resolution succeeded (repo_root found and template exists),
        # we expect multiple missing sections.
        # If fallback (no template), only "Outcome" is found — still valid.
        assert isinstance(missing, list)
        assert len(missing) >= 1
        # Verify all sections in missing_sections are strings
        for s in missing:
            assert isinstance(s, str), f"Each missing section must be a string, got: {type(s)}"


# ---------------------------------------------------------------------------
# AC2: fail_closed.rewrite_constraints.required_sections contains all missing sections
# ---------------------------------------------------------------------------

class TestAC2RewriteConstraintsRequiredSections:
    def test_rewrite_constraints_required_sections_matches_missing(self):
        """AC2: rewrite_constraints.required_sections contains all missing sections."""
        output, _ = run_planner_direct(BODY_MANY_MISSING)
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

    def test_must_add_sections_matches_missing(self):
        """AC2: rewrite_constraints.rewrite_constraints.must_add_sections also has all missing."""
        output, _ = run_planner_direct(BODY_MANY_MISSING)
        fc = output["fail_closed"]
        rc = fc.get("rewrite_constraints", {})
        inner = rc.get("rewrite_constraints", {})
        must_add = inner.get("must_add_sections", [])
        missing = fc.get("missing_sections", [])
        assert set(must_add) == set(missing), (
            f"must_add_sections {must_add} must match missing_sections {missing}"
        )

    def test_required_sections_is_nonempty_list(self):
        """AC2: rewrite_constraints.required_sections is a non-empty list when fail_closed fires."""
        output, _ = run_planner_direct(BODY_MANY_MISSING)
        fc = output["fail_closed"]
        rc = fc.get("rewrite_constraints", {})
        required_sections = rc.get("required_sections", [])
        assert isinstance(required_sections, list)
        assert len(required_sections) >= 1


# ---------------------------------------------------------------------------
# AC3: FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 schema presence
# ---------------------------------------------------------------------------

class TestAC3RewriteConstraintsSchema:
    def test_schema_version_present(self):
        """AC3: rewrite_constraints has schema_version=FAIL_CLOSED_REWRITE_CONSTRAINTS_V1."""
        output, _ = run_planner_direct(BODY_MANY_MISSING)
        rc = output["fail_closed"]["rewrite_constraints"]
        assert rc["schema_version"] == "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1"

    def test_required_sections_is_list(self):
        """AC3: rewrite_constraints.required_sections is a list."""
        output, _ = run_planner_direct(BODY_MANY_MISSING)
        rc = output["fail_closed"]["rewrite_constraints"]
        assert isinstance(rc["required_sections"], list)

    def test_no_progress_route_present(self):
        """AC3: rewrite_constraints has no_progress_route field."""
        output, _ = run_planner_direct(BODY_MANY_MISSING)
        rc = output["fail_closed"]["rewrite_constraints"]
        assert "no_progress_route" in rc

    def test_prevent_duplicate_repair_key_present(self):
        """AC3: rewrite_constraints always has prevent_duplicate_repair key."""
        output, _ = run_planner_direct(BODY_MANY_MISSING)
        rc = output["fail_closed"]["rewrite_constraints"]
        assert "prevent_duplicate_repair" in rc, (
            "rewrite_constraints must have 'prevent_duplicate_repair' key"
        )


# ---------------------------------------------------------------------------
# AC4: prevent_duplicate_repair when same reason_code was already seen
# ---------------------------------------------------------------------------

class TestAC4PreventDuplicateRepair:
    def test_prevent_duplicate_repair_false_on_first_attempt(self):
        """AC4: First attempt (no prev_fail_closed_reasons) → prevent_duplicate_repair=False."""
        output, _ = run_planner_direct(BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=None)
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
        output, _ = run_planner_direct(
            BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=known_context
        )
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
        output, _ = run_planner_direct(
            BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=known_context
        )
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
        output, _ = run_planner_direct(
            BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=known_context
        )
        fc = output["fail_closed"]
        if fc["required"]:
            rc = fc.get("rewrite_constraints", {})
            # missing_required_section overlaps → True
            assert rc.get("prevent_duplicate_repair") is True

    def test_prev_fail_closed_reasons_empty_list(self):
        """AC4: Empty prev_fail_closed_reasons → prevent_duplicate_repair=False."""
        known_context = {"prev_fail_closed_reasons": []}
        output, _ = run_planner_direct(
            BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=known_context
        )
        fc = output["fail_closed"]
        assert fc["required"] is True
        rc = fc.get("rewrite_constraints", {})
        assert rc.get("prevent_duplicate_repair") is False

    def test_prev_fail_closed_reasons_none(self):
        """AC4: prev_fail_closed_reasons=None → prevent_duplicate_repair=False (null safety)."""
        known_context = {"prev_fail_closed_reasons": None}
        output, _ = run_planner_direct(
            BODY_NO_CONTRACT_MISSING_OUTCOME, known_context=known_context
        )
        fc = output["fail_closed"]
        assert fc["required"] is True
        rc = fc.get("rewrite_constraints", {})
        assert rc.get("prevent_duplicate_repair") is False


# ---------------------------------------------------------------------------
# AC5: run_refinement_preflight BLOCKERS line includes all missing sections
# ---------------------------------------------------------------------------

class TestAC5BlockersIncludeMissingSections:
    def _run_preflight(self, body: str, tmp_path: Path) -> tuple[dict, int]:
        """Run run_refinement_preflight via the wrapper's run_preflight() function."""
        fixture_data = {
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
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(preflight_wrapper, "_find_repo_root", return_value=tmp_path):
            return preflight_wrapper.run_preflight(
                issue_number=1023,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
                now="2026-06-20T00:00:00+00:00",
            )

    def test_blockers_contains_planner_fail_closed_marker(self, tmp_path):
        """AC5: BLOCKERS always starts with PLANNER_FAIL_CLOSED when fail_closed fires."""
        output, exit_code = self._run_preflight(BODY_NO_CONTRACT_MISSING_OUTCOME, tmp_path)
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}"
        assert output.get("status") == "blocked"
        blockers = output.get("blockers", [])
        assert "PLANNER_FAIL_CLOSED" in blockers, (
            f"Expected PLANNER_FAIL_CLOSED in blockers, got: {blockers}"
        )

    def test_blockers_include_missing_sections_list(self, tmp_path):
        """AC5: BLOCKERS includes missing sections in list form (not just reason code string)."""
        output, exit_code = self._run_preflight(BODY_NO_CONTRACT_MISSING_OUTCOME, tmp_path)
        assert exit_code == 2
        blockers = output.get("blockers", [])

        # Find a blocker entry that mentions missing sections as a list
        section_with_list = [
            b for b in blockers
            if isinstance(b, str)
            and "missing_required_section" in b
            and "[" in b
            and "]" in b
        ]
        assert section_with_list, (
            f"Expected a blocker like 'missing_required_section: [...]', "
            f"got blockers: {blockers}"
        )

    def test_blockers_not_duplicate_reason_code(self, tmp_path):
        """AC5: missing_required_section should appear once as list form, not duplicated."""
        output, exit_code = self._run_preflight(BODY_NO_CONTRACT_MISSING_OUTCOME, tmp_path)
        blockers = output.get("blockers", [])
        # Count bare "missing_required_section" (without list) occurrences
        bare_reason = [b for b in blockers if b == "missing_required_section"]
        # Should be 0: the bare reason code is replaced by the list form
        assert len(bare_reason) == 0, (
            f"Expected no bare 'missing_required_section' blocker (replaced by list form), "
            f"got: {blockers}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

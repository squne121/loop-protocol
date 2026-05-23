"""Tests for plan_child_materialization.py.

Uses #254-equivalent fixtures to verify that C254 child lines are classified
into missing / existing_open / existing_closed / stale_body_only (and ambiguous)
correctly.

GIVEN a delivery-rollup parent issue body
WHEN plan_child_materialization.build_plan() is called
THEN each child entry receives the correct status classification.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Resolve the scripts directory so we can import without installing.
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import plan_child_materialization as pmc  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Simulated #254 parent issue body with three child types:
#:  - C254-1: has a matching open issue #281 without placeholder → existing_open
#:  - C254-3: has placeholder （未起票） with no issue ref → missing
#:  - C254-5: has both placeholder AND a real open issue ref #285 → stale_body_only
FIXTURE_PARENT_BODY_254 = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Outcome

#254 の child issues が全て close されたとき、本 Issue を close する。

## Child Issues

- C254-1 docs: docs/product/game-thesis.md を追加 #281
- C254-3 docs: docs/dev/runtime-verification-policy.md を追加（未起票）
- C254-5 feat: SDD 採否 ADR を追加（未起票） #285

## Background

Some background text that mentions C254-9 and #999 but should NOT be parsed.
"""


def _make_issue_info(number: int, state: str = "OPEN", state_reason=None) -> pmc.ExistingIssueInfo:
    return pmc.ExistingIssueInfo(
        number=number,
        state=state,
        state_reason=state_reason,
        url=f"https://github.com/squne121/loop-protocol/issues/{number}",
    )


def _build_dry_run(body: str, parent_issue: int = 254, repo: str = "") -> pmc.Plan:
    """Build a plan in dry-run mode (no GitHub API calls)."""
    return pmc.build_plan(body, parent_issue=parent_issue, repo=repo, dry_run=True)


def _build_live(
    body: str,
    parent_issue: int = 254,
    repo: str = "squne121/loop-protocol",
    view_side_effects: dict | None = None,
    search_return: list | None = None,
) -> pmc.Plan:
    """Build a plan in live mode with mocked gh calls.

    view_side_effects: {issue_number: ExistingIssueInfo | None}
        If None, the API call "failed" for that issue.
    search_return: list of candidate dicts returned by _search_dedupe_candidates.
    """
    if view_side_effects is None:
        view_side_effects = {}
    if search_return is None:
        search_return = []

    def fake_view(r, n, g="gh"):
        return view_side_effects.get(n)

    def fake_search(r, key, g="gh"):
        return search_return

    with patch.object(pmc, "_view_issue", side_effect=fake_view), \
         patch.object(pmc, "_search_dedupe_candidates", return_value=search_return):
        return pmc.build_plan(body, parent_issue=parent_issue, repo=repo, dry_run=False)


def _child_by_id(plan: pmc.Plan, child_id: str) -> pmc.ChildEntry:
    for child in plan.children:
        if child.child_id == child_id:
            return child
    raise KeyError(f"child_id={child_id!r} not found in plan")


# ---------------------------------------------------------------------------
# Tests: parent_mode extraction
# ---------------------------------------------------------------------------


class TestParentModeExtraction:
    def test_extracts_delivery_rollup_from_body(self) -> None:
        """GIVEN a body with parent_mode: delivery-rollup
        WHEN _extract_parent_mode is called
        THEN it returns 'delivery-rollup'.
        """
        body = "```yaml\nparent_mode: delivery-rollup\n```"
        assert pmc._extract_parent_mode(body) == "delivery-rollup"

    def test_returns_unknown_when_absent(self) -> None:
        """GIVEN a body without parent_mode
        WHEN _extract_parent_mode is called
        THEN it returns 'unknown' (not 'delivery-rollup').
        """
        body = "No machine-readable contract here"
        assert pmc._extract_parent_mode(body) == "unknown"

    def test_parent_mode_unknown_produces_human_escalation(self) -> None:
        """GIVEN a body without parent_mode (broken Machine-Readable Contract)
        WHEN build_plan is called
        THEN children that are parsed get action=human_escalation.
        """
        body = """\
## Child Issues

- C254-3 docs: something（未起票）
"""
        plan = _build_dry_run(body, parent_issue=254)
        # Should have parsed the child
        assert len(plan.children) > 0
        child = _child_by_id(plan, "C254-3")
        assert child.action == "human_escalation"


# ---------------------------------------------------------------------------
# Tests: closure_mode extraction
# ---------------------------------------------------------------------------


class TestClosureModeExtraction:
    def test_extracts_child_complete(self) -> None:
        """GIVEN a body with closure_mode: child-complete
        WHEN _extract_closure_mode is called
        THEN it returns 'child-complete'.
        """
        body = "```yaml\nclosure_mode: child-complete\n```"
        assert pmc._extract_closure_mode(body) == "child-complete"

    def test_returns_unknown_when_absent(self) -> None:
        """GIVEN a body without closure_mode
        WHEN _extract_closure_mode is called
        THEN it returns 'unknown'.
        """
        body = "No machine-readable contract here"
        assert pmc._extract_closure_mode(body) == "unknown"


# ---------------------------------------------------------------------------
# Tests: section scoping — ## Child Issues boundary
# ---------------------------------------------------------------------------


class TestChildIssueSectionScoping:
    def test_only_parses_child_issues_section(self) -> None:
        """GIVEN a body with Cxxx-N patterns outside ## Child Issues
        WHEN _parse_child_lines is called
        THEN only lines from ## Child Issues section are returned.
        """
        body = """\
## Background

Some text mentions C254-9 for context.

## Child Issues

- C254-3 docs: something（未起票）

## Remaining Parent Gaps

- C254-7 is referenced here but NOT a child line
"""
        results = pmc._parse_child_lines(body)
        child_ids = [r["child_id"] for r in results]
        assert "C254-3" in child_ids
        assert "C254-9" not in child_ids
        assert "C254-7" not in child_ids

    def test_section_stops_at_next_heading(self) -> None:
        """GIVEN a ## Child Issues section followed by another heading
        WHEN _parse_child_lines is called
        THEN lines after the next heading are not parsed.
        """
        body = """\
## Child Issues

- C254-1 docs: something #281

## Verification Commands

- C254-99 should not be parsed
"""
        results = pmc._parse_child_lines(body)
        child_ids = [r["child_id"] for r in results]
        assert "C254-1" in child_ids
        assert "C254-99" not in child_ids

    def test_no_child_issues_section_returns_empty(self) -> None:
        """GIVEN a body without ## Child Issues heading
        WHEN _parse_child_lines is called
        THEN results is empty.
        """
        body = """\
## Outcome

- C254-3 is mentioned here but there's no Child Issues section
"""
        results = pmc._parse_child_lines(body)
        assert results == []


# ---------------------------------------------------------------------------
# Tests: child line parsing
# ---------------------------------------------------------------------------


class TestParseChildLines:
    def test_detects_placeholder_line(self) -> None:
        """GIVEN a child line with （未起票）
        WHEN _parse_child_lines is called
        THEN is_placeholder is True.
        """
        body = "## Child Issues\n\n- C254-3 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-3"
        assert results[0]["is_placeholder"] is True
        assert results[0]["raw_issue_refs"] == []

    def test_detects_issue_ref_line(self) -> None:
        """GIVEN a child line with an issue reference like #281
        WHEN _parse_child_lines is called
        THEN raw_issue_refs contains 281.
        """
        body = "## Child Issues\n\n- C254-1 docs: something #281\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-1"
        assert results[0]["is_placeholder"] is False
        assert 281 in results[0]["raw_issue_refs"]

    def test_detects_stale_line_with_both_placeholder_and_ref(self) -> None:
        """GIVEN a line with both （未起票） and #285
        WHEN _parse_child_lines is called
        THEN is_placeholder is True AND raw_issue_refs contains 285.
        """
        body = "## Child Issues\n\n- C254-5 feat: something（未起票） #285\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["is_placeholder"] is True
        assert 285 in results[0]["raw_issue_refs"]

    def test_ignores_non_child_lines_in_section(self) -> None:
        """GIVEN a body with no Cxxx-N lines in Child Issues section
        WHEN _parse_child_lines is called
        THEN results is empty.
        """
        body = "## Child Issues\n\nSome text without child references.\n"
        assert pmc._parse_child_lines(body) == []

    def test_checkbox_form_parsed(self) -> None:
        """GIVEN a child line with checkbox form '- [ ] #N — CX-M: ...'
        WHEN _parse_child_lines is called
        THEN child_id is correctly extracted.
        """
        body = "## Child Issues\n\n- [ ] #281 — C254-1: docs: something\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-1"


# ---------------------------------------------------------------------------
# Tests: classify_child — live mode with mock
# ---------------------------------------------------------------------------


class TestClassifyChildMissing:
    """Classification: missing."""

    def test_missing_when_placeholder_no_ref(self) -> None:
        """GIVEN a child line with （未起票） and no issue ref
        WHEN classified
        THEN status is 'missing' and action is 'create_issue'.
        """
        parsed = {
            "child_id": "C254-3",
            "rest": "docs: something（未起票）",
            "is_placeholder": True,
            "raw_issue_refs": [],
            "line_number": 5,
            "raw_line": "- C254-3 docs: something（未起票）",
        }
        warnings: list[str] = []
        entry = pmc._classify_child(
            parsed,
            parent_issue=254,
            parent_mode="delivery-rollup",
            repo="squne121/loop-protocol",
            dry_run=False,
            issue_lookup_warnings=warnings,
        )
        assert entry.status == "missing"
        assert entry.action == "create_issue"
        assert entry.existing_issue is None
        assert entry.dedupe_key == "delivery-rollup:254:C254-3"

    def test_missing_when_no_placeholder_and_no_ref(self) -> None:
        """GIVEN a child line with no placeholder and no issue ref
        WHEN classified
        THEN status is 'missing' (bare description).
        """
        parsed = {
            "child_id": "C254-7",
            "rest": "docs: some description without ref",
            "is_placeholder": False,
            "raw_issue_refs": [],
            "line_number": 6,
            "raw_line": "- C254-7 docs: some description without ref",
        }
        warnings: list[str] = []
        entry = pmc._classify_child(
            parsed,
            parent_issue=254,
            parent_mode="delivery-rollup",
            repo="",
            dry_run=True,
            issue_lookup_warnings=warnings,
        )
        assert entry.status == "missing"
        assert entry.action == "create_issue"


class TestClassifyChildExistingOpen:
    """Classification: existing_open."""

    def test_existing_open_when_ref_matches_open_issue(self) -> None:
        """GIVEN a child line referencing a known open issue #281
        WHEN classified (live mode, issue is OPEN)
        THEN status is 'existing_open' and action is 'no_op'.
        """
        parsed = {
            "child_id": "C254-1",
            "rest": "docs: something #281",
            "is_placeholder": False,
            "raw_issue_refs": [281],
            "line_number": 4,
            "raw_line": "- C254-1 docs: something #281",
        }
        warnings: list[str] = []
        with patch.object(
            pmc, "_view_issue", return_value=_make_issue_info(281, "OPEN")
        ), patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo="squne121/loop-protocol",
                dry_run=False,
                issue_lookup_warnings=warnings,
            )
        assert entry.status == "existing_open"
        assert entry.action == "no_op"
        assert entry.existing_issue is not None
        assert entry.existing_issue.number == 281

    def test_existing_open_dedupe_key_format(self) -> None:
        """GIVEN a child classified as existing_open
        WHEN checking dedupe_key
        THEN it follows 'delivery-rollup:<parent>:<child_id>' format.
        """
        parsed = {
            "child_id": "C254-1",
            "rest": "docs: something #281",
            "is_placeholder": False,
            "raw_issue_refs": [281],
            "line_number": 4,
            "raw_line": "- C254-1 docs: something #281",
        }
        warnings: list[str] = []
        with patch.object(
            pmc, "_view_issue", return_value=_make_issue_info(281, "OPEN")
        ), patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo="squne121/loop-protocol",
                dry_run=False,
                issue_lookup_warnings=warnings,
            )
        assert entry.dedupe_key == "delivery-rollup:254:C254-1"


class TestClassifyChildExistingClosed:
    """Classification: existing_closed — closed child must NOT be ambiguous."""

    def test_closed_child_is_existing_closed_not_ambiguous(self) -> None:
        """GIVEN a child line referencing a closed issue
        WHEN classified (live mode, issue is CLOSED)
        THEN status is 'existing_closed' (not 'ambiguous') and action is 'no_op'.

        Regression: Blocker 1 — closed child was previously classified as ambiguous.
        """
        parsed = {
            "child_id": "C254-1",
            "rest": "docs: something #281",
            "is_placeholder": False,
            "raw_issue_refs": [281],
            "line_number": 4,
            "raw_line": "- C254-1 docs: something #281",
        }
        warnings: list[str] = []
        with patch.object(
            pmc, "_view_issue", return_value=_make_issue_info(281, "CLOSED", "COMPLETED")
        ), patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo="squne121/loop-protocol",
                dry_run=False,
                issue_lookup_warnings=warnings,
            )
        assert entry.status == "existing_closed", (
            "closed child must be 'existing_closed', not 'ambiguous'"
        )
        assert entry.action == "no_op"
        assert entry.existing_issue is not None
        assert entry.existing_issue.state == "CLOSED"


class TestClassifyChildStaleBodyOnly:
    """Classification: stale_body_only."""

    def test_stale_body_only_when_placeholder_and_open_ref(self) -> None:
        """GIVEN a child line with （未起票） AND a reference to an open issue #285
        WHEN classified (live mode)
        THEN status is 'stale_body_only' and action is 'reuse_and_update_parent'.
        """
        parsed = {
            "child_id": "C254-5",
            "rest": "feat: SDD 採否 ADR を追加（未起票） #285",
            "is_placeholder": True,
            "raw_issue_refs": [285],
            "line_number": 6,
            "raw_line": "- C254-5 feat: SDD 採否 ADR を追加（未起票） #285",
        }
        warnings: list[str] = []
        with patch.object(
            pmc, "_view_issue", return_value=_make_issue_info(285, "OPEN")
        ), patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo="squne121/loop-protocol",
                dry_run=False,
                issue_lookup_warnings=warnings,
            )
        assert entry.status == "stale_body_only"
        assert entry.action == "reuse_and_update_parent"
        assert entry.existing_issue is not None
        assert entry.existing_issue.number == 285


class TestClassifyChildAmbiguous:
    """Classification: ambiguous."""

    def test_ambiguous_when_view_issue_fails(self) -> None:
        """GIVEN a child line referencing an issue where gh issue view fails
        WHEN classified (live mode)
        THEN status is 'ambiguous', action is 'human_escalation',
             and a warning is recorded.

        Regression: Blocker 2 — lookup failure must not be a silent fallback.
        """
        parsed = {
            "child_id": "C254-9",
            "rest": "feat: something #999",
            "is_placeholder": False,
            "raw_issue_refs": [999],
            "line_number": 7,
            "raw_line": "- C254-9 feat: something #999",
        }
        warnings: list[str] = []
        with patch.object(pmc, "_view_issue", return_value=None), \
             patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo="squne121/loop-protocol",
                dry_run=False,
                issue_lookup_warnings=warnings,
            )
        assert entry.status == "ambiguous"
        assert entry.action == "human_escalation"
        assert len(warnings) > 0, "A warning must be recorded when issue view fails"


class TestParentModeUnknown:
    """Classification when parent_mode is unknown — Blocker 4."""

    def test_unknown_parent_mode_produces_human_escalation(self) -> None:
        """GIVEN parent_mode='unknown' (broken contract)
        WHEN classify_child is called
        THEN action is 'human_escalation'.

        Regression: Blocker 4 — unknown parent_mode must not default to delivery-rollup.
        """
        parsed = {
            "child_id": "C254-3",
            "rest": "docs: something（未起票）",
            "is_placeholder": True,
            "raw_issue_refs": [],
            "line_number": 5,
            "raw_line": "- C254-3 docs: something（未起票）",
        }
        warnings: list[str] = []
        entry = pmc._classify_child(
            parsed,
            parent_issue=254,
            parent_mode="unknown",
            repo="",
            dry_run=True,
            issue_lookup_warnings=warnings,
        )
        assert entry.action == "human_escalation"

    def test_build_plan_with_missing_parent_mode(self) -> None:
        """GIVEN a body without parent_mode key
        WHEN build_plan is called (dry_run)
        THEN parent_mode is 'unknown' in the plan.

        Regression: Blocker 4 — must return 'unknown', not 'delivery-rollup'.
        """
        body = """\
## Outcome

No machine-readable contract here.

## Child Issues

- C254-3 docs: something（未起票）
"""
        plan = _build_dry_run(body, parent_issue=254)
        assert plan.parent_mode == "unknown"


# ---------------------------------------------------------------------------
# Tests: full build_plan with #254 fixture (dry-run)
# ---------------------------------------------------------------------------


class TestBuildPlan254FixtureDryRun:
    """Integration-level tests using the #254-equivalent fixture (dry-run mode)."""

    def test_parent_mode_is_delivery_rollup(self) -> None:
        """GIVEN the #254 fixture body
        WHEN build_plan is called (dry_run)
        THEN parent_mode is 'delivery-rollup'.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.parent_mode == "delivery-rollup"

    def test_closure_mode_is_child_complete(self) -> None:
        """GIVEN the #254 fixture body with closure_mode: child-complete
        WHEN build_plan is called (dry_run)
        THEN closure_mode is 'child-complete'.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.closure_mode == "child-complete"

    def test_parent_issue_number(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.parent_issue == 254

    def test_body_sha256_present(self) -> None:
        """GIVEN the #254 fixture body
        WHEN build_plan is called
        THEN body_sha256 is a non-empty hex string.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert len(plan.body_sha256) == 64
        assert all(c in "0123456789abcdef" for c in plan.body_sha256)

    def test_schema_version_is_2(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.schema_version == 2

    def test_issue_lookup_complete_in_dry_run(self) -> None:
        """In dry-run mode, issue_lookup.complete is True (trivially)."""
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.issue_lookup.complete is True

    def test_c254_3_is_missing(self) -> None:
        """GIVEN C254-3 has （未起票） and no issue ref
        WHEN classified via build_plan (dry_run)
        THEN status is 'missing'.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        child = _child_by_id(plan, "C254-3")
        assert child.status == "missing"
        assert child.existing_issue is None
        assert child.action == "create_issue"

    def test_three_children_detected(self) -> None:
        """GIVEN the fixture has exactly 3 child lines in ## Child Issues
        WHEN build_plan is called (dry_run)
        THEN plan.children has length 3.

        Note: C254-9 mentioned in Background section must NOT be parsed.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert len(plan.children) == 3

    def test_background_child_id_not_parsed(self) -> None:
        """GIVEN C254-9 is mentioned in ## Background (not ## Child Issues)
        WHEN build_plan is called
        THEN C254-9 is NOT in plan.children.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        child_ids = [c.child_id for c in plan.children]
        assert "C254-9" not in child_ids

    def test_required_issue_creations_contains_missing(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert "C254-3" in plan.required_issue_creations


# ---------------------------------------------------------------------------
# Tests: full build_plan with live mode (mocked gh)
# ---------------------------------------------------------------------------


class TestBuildPlan254FixtureLive:
    """Integration-level tests using the #254-equivalent fixture (live mode, mocked)."""

    def _make_view_effects(self) -> dict:
        return {
            281: _make_issue_info(281, "OPEN"),
            285: _make_issue_info(285, "OPEN"),
        }

    def test_c254_1_is_existing_open(self) -> None:
        """GIVEN C254-1 references open issue #281
        WHEN classified via build_plan (live, mocked)
        THEN status is 'existing_open'.
        """
        plan = _build_live(FIXTURE_PARENT_BODY_254, view_side_effects=self._make_view_effects())
        child = _child_by_id(plan, "C254-1")
        assert child.status == "existing_open"
        assert child.action == "no_op"

    def test_c254_5_is_stale_body_only(self) -> None:
        plan = _build_live(FIXTURE_PARENT_BODY_254, view_side_effects=self._make_view_effects())
        child = _child_by_id(plan, "C254-5")
        assert child.status == "stale_body_only"
        assert child.action == "reuse_and_update_parent"

    def test_closed_referenced_issue_is_existing_closed(self) -> None:
        """GIVEN C254-1 references a CLOSED issue #281
        WHEN classified via build_plan (live, mocked)
        THEN status is 'existing_closed', NOT 'ambiguous'.

        Regression: Blocker 1 — closed children must not be ambiguous.
        """
        view_effects = {
            281: _make_issue_info(281, "CLOSED", "COMPLETED"),
            285: _make_issue_info(285, "OPEN"),
        }
        plan = _build_live(FIXTURE_PARENT_BODY_254, view_side_effects=view_effects)
        child = _child_by_id(plan, "C254-1")
        assert child.status == "existing_closed", (
            "closed referenced issue must be 'existing_closed', NOT 'ambiguous'"
        )
        assert child.action == "no_op"

    def test_required_issue_edits_contains_stale(self) -> None:
        plan = _build_live(FIXTURE_PARENT_BODY_254, view_side_effects=self._make_view_effects())
        assert len(plan.required_issue_edits) > 0

    def test_parent_body_updates_for_stale_child(self) -> None:
        """GIVEN C254-5 is stale_body_only
        WHEN build_plan is called (live, mocked)
        THEN parent_body_updates contains a safe patch entry for C254-5.
        """
        plan = _build_live(FIXTURE_PARENT_BODY_254, view_side_effects=self._make_view_effects())
        stale_updates = [u for u in plan.parent_body_updates if "C254-5" in u.old_line]
        assert len(stale_updates) == 1
        upd = stale_updates[0]
        assert "（未起票）" not in upd.new_line
        assert "#285" in upd.new_line
        # Verify safe patch fields
        assert upd.section == "Child Issues"
        assert upd.line_number > 0
        assert upd.expected_match_count >= 1

    def test_parent_body_updates_expected_match_count_unique(self) -> None:
        """GIVEN each stale child line appears exactly once in the body
        WHEN build_plan is called
        THEN expected_match_count is 1 for each update.
        """
        plan = _build_live(FIXTURE_PARENT_BODY_254, view_side_effects=self._make_view_effects())
        for upd in plan.parent_body_updates:
            assert upd.expected_match_count == 1, (
                f"expected_match_count must be 1 for unique lines, got {upd.expected_match_count} "
                f"for: {upd.old_line!r}"
            )

    def test_parent_body_updates_duplicate_match_detected(self) -> None:
        """GIVEN a body with a duplicate stale child line
        WHEN build_plan is called
        THEN expected_match_count reflects the actual count (>1).

        Regression: Blocker 7 — duplicate matches must be detectable.
        """
        # Duplicate the stale line
        duplicate_body = FIXTURE_PARENT_BODY_254 + (
            "- C254-5 feat: SDD 採否 ADR を追加（未起票） #285\n"
        )
        view_effects = self._make_view_effects()
        plan = _build_live(duplicate_body, view_side_effects=view_effects)
        # With a duplicate stale line for C254-5 in the body, we expect TWO children
        c254_5_updates = [u for u in plan.parent_body_updates if "C254-5" in u.old_line]
        # The duplicate body causes expected_match_count > 1 for the first occurrence
        # OR we get two separate update entries; either way consumer can detect the issue
        total_matches = sum(u.expected_match_count for u in c254_5_updates)
        assert total_matches > 1, (
            "duplicate stale lines must result in expected_match_count > 1 to be detectable"
        )


# ---------------------------------------------------------------------------
# Regression: Blocker 2 — gh issue list failure must not silently make all refs ambiguous
# ---------------------------------------------------------------------------


class TestIssueLookupFailure:
    def test_view_issue_failure_records_warning_not_fatal(self) -> None:
        """GIVEN gh issue view fails for one issue
        WHEN build_plan is called
        THEN that child is ambiguous with a warning recorded in issue_lookup.warnings.

        Regression: Blocker 2 — per-issue failure is isolated (not plan-level).
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- C254-9 feat: something #999
"""
        with patch.object(pmc, "_view_issue", return_value=None), \
             patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            plan = pmc.build_plan(
                body, parent_issue=254, repo="squne121/loop-protocol", dry_run=False
            )
        child = _child_by_id(plan, "C254-9")
        assert child.status == "ambiguous"
        assert child.action == "human_escalation"
        assert len(plan.issue_lookup.warnings) > 0
        # Plan itself remains complete (per-issue failure, not plan-level)
        assert plan.issue_lookup.complete is True


# ---------------------------------------------------------------------------
# Regression: Blocker 8 — --body-file uses existing_unverified, not ambiguous
# ---------------------------------------------------------------------------


class TestDryRunBodyFile:
    def test_body_file_mode_uses_existing_unverified(self) -> None:
        """GIVEN --body-file mode (dry_run=True) and a child with an issue ref
        WHEN build_plan is called
        THEN status is 'existing_unverified' (NOT 'ambiguous').

        Regression: Blocker 8 — body-file mode must not classify all refs as ambiguous.
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- C254-1 docs: something #281
"""
        plan = _build_dry_run(body, parent_issue=254)
        child = _child_by_id(plan, "C254-1")
        assert child.status == "existing_unverified", (
            "dry-run mode must use 'existing_unverified', not 'ambiguous'"
        )

    def test_body_file_mode_stale_is_stale_not_ambiguous(self) -> None:
        """GIVEN --body-file mode and a stale child (placeholder + ref)
        WHEN build_plan is called
        THEN status is 'stale_body_only' (not 'ambiguous').
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- C254-5 feat: something（未起票） #285
"""
        plan = _build_dry_run(body, parent_issue=254)
        child = _child_by_id(plan, "C254-5")
        assert child.status == "stale_body_only"


# ---------------------------------------------------------------------------
# Tests: V2 schema fields in plan
# ---------------------------------------------------------------------------


class TestPlanV2Schema:
    """Verify CHILD_MATERIALIZATION_PLAN_V2 schema fields — Blocker 5."""

    def test_schema_has_closure_mode(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert hasattr(plan, "closure_mode")
        assert plan.closure_mode == "child-complete"

    def test_schema_has_repo(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254, repo="squne121/loop-protocol")
        assert plan.repo == "squne121/loop-protocol"

    def test_schema_has_source_fields(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254, parent_issue=254)
        assert plan.source_issue_number == 254
        assert len(plan.body_sha256) == 64

    def test_schema_has_issue_lookup(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert hasattr(plan, "issue_lookup")
        assert hasattr(plan.issue_lookup, "complete")
        assert hasattr(plan.issue_lookup, "strategy")
        assert hasattr(plan.issue_lookup, "warnings")

    def test_child_has_existing_issue_not_number(self) -> None:
        """Children must have 'existing_issue' (ExistingIssueInfo), not plain int."""
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        child = _child_by_id(plan, "C254-1")
        # existing_issue is an ExistingIssueInfo object (or None), not an int
        if child.existing_issue is not None:
            assert hasattr(child.existing_issue, "number")
            assert hasattr(child.existing_issue, "state")

    def test_child_has_existing_issue_candidates(self) -> None:
        """Each child must have an existing_issue_candidates list."""
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        for child in plan.children:
            assert hasattr(child, "existing_issue_candidates")
            assert isinstance(child.existing_issue_candidates, list)


# ---------------------------------------------------------------------------
# Tests: YAML serialization
# ---------------------------------------------------------------------------


class TestPlanToYaml:
    def test_output_starts_with_schema_key_v2(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert yaml_output.startswith("CHILD_MATERIALIZATION_PLAN_V2:")

    def test_output_contains_missing_status(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "missing" in yaml_output

    def test_output_contains_existing_unverified_for_dry_run(self) -> None:
        """In dry-run mode, existing refs are serialized as 'existing_unverified'."""
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "existing_unverified" in yaml_output

    def test_output_contains_stale_body_only_status(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "stale_body_only" in yaml_output

    def test_output_contains_dedupe_key(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "delivery-rollup:254:C254-3" in yaml_output

    def test_output_contains_closure_mode(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "closure_mode: child-complete" in yaml_output

    def test_output_contains_issue_lookup(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "issue_lookup:" in yaml_output
        assert "complete:" in yaml_output

    def test_parent_body_updates_have_safe_patch_fields(self) -> None:
        """Serialized parent_body_updates must include safe patch fields."""
        view_effects = {
            281: _make_issue_info(281, "OPEN"),
            285: _make_issue_info(285, "OPEN"),
        }
        plan = _build_live(FIXTURE_PARENT_BODY_254, view_side_effects=view_effects)
        yaml_output = pmc.plan_to_yaml(plan)
        if plan.parent_body_updates:
            assert "section:" in yaml_output
            assert "line_number:" in yaml_output
            assert "old_line:" in yaml_output
            assert "new_line:" in yaml_output
            assert "expected_match_count:" in yaml_output

    def test_empty_plan_no_warnings(self) -> None:
        """GIVEN an empty body (no child lines in Child Issues section)
        WHEN plan_to_yaml is called
        THEN the output is still valid YAML (with a warning).
        """
        plan = _build_dry_run("## Outcome\n\nNo child lines here.\n")
        yaml_output = pmc.plan_to_yaml(plan)
        assert "CHILD_MATERIALIZATION_PLAN_V2:" in yaml_output
        # A warning should be present about no children found
        assert "warnings:" in yaml_output


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_bullet_form_child_lines_parsed(self) -> None:
        """GIVEN child lines prefixed with '- '
        WHEN _parse_child_lines is called (within ## Child Issues)
        THEN they are parsed correctly.
        """
        body = "## Child Issues\n\n- C254-2 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-2"

    def test_non_bullet_child_lines_parsed(self) -> None:
        """GIVEN child lines without bullet prefix
        WHEN _parse_child_lines is called (within ## Child Issues)
        THEN they are parsed correctly.
        """
        body = "## Child Issues\n\nC254-4 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-4"

    def test_line_number_is_tracked(self) -> None:
        """GIVEN a body with Child Issues section
        WHEN _parse_child_lines is called
        THEN each result has a line_number >= 1.
        """
        body = "## Child Issues\n\n- C254-3 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert results[0]["line_number"] >= 1

    def test_raw_line_is_tracked(self) -> None:
        """GIVEN a body with a child line
        WHEN _parse_child_lines is called
        THEN raw_line contains the original line text.
        """
        body = "## Child Issues\n\n- C254-3 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert "C254-3" in results[0]["raw_line"]

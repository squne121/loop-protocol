"""Tests for plan_child_materialization.py.

Uses #254-equivalent fixtures to verify that C254 child lines are classified
into missing / existing / stale_body_only (and ambiguous) correctly.

GIVEN a delivery-rollup parent issue body
WHEN plan_child_materialization.build_plan() is called
THEN each child entry receives the correct status classification.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Resolve the scripts directory so we can import without installing.
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import plan_child_materialization as pmc  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Simulated #254 parent issue body with three child types:
#:  - C254-1: has a matching open issue #281 without placeholder → existing
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
"""

#: Open issues available in the "repository" for fixture tests.
FIXTURE_OPEN_ISSUES = [
    {"number": 281, "title": "docs: docs/product/game-thesis.md を追加"},
    {"number": 285, "title": "feat: SDD 採否 ADR を追加"},
]

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _build(body: str, parent_issue: int = 254, open_issues=None) -> pmc.Plan:
    if open_issues is None:
        open_issues = FIXTURE_OPEN_ISSUES
    return pmc.build_plan(body, parent_issue=parent_issue, open_issues=open_issues)


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

    def test_returns_default_when_absent(self) -> None:
        """GIVEN a body without parent_mode
        WHEN _extract_parent_mode is called
        THEN it returns the default 'delivery-rollup'.
        """
        body = "No machine-readable contract here"
        assert pmc._extract_parent_mode(body) == "delivery-rollup"


# ---------------------------------------------------------------------------
# Tests: child line parsing
# ---------------------------------------------------------------------------


class TestParseChildLines:
    def test_detects_placeholder_line(self) -> None:
        """GIVEN a child line with （未起票）
        WHEN _parse_child_lines is called
        THEN is_placeholder is True.
        """
        body = "- C254-3 docs: something（未起票）\n"
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
        body = "- C254-1 docs: something #281\n"
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
        body = "- C254-5 feat: something（未起票） #285\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["is_placeholder"] is True
        assert 285 in results[0]["raw_issue_refs"]

    def test_ignores_non_child_lines(self) -> None:
        """GIVEN a body with no Cxxx-N lines
        WHEN _parse_child_lines is called
        THEN results is empty.
        """
        body = "## Outcome\n\nSome text without child references.\n"
        assert pmc._parse_child_lines(body) == []


# ---------------------------------------------------------------------------
# Tests: classify_child — three required statuses
# ---------------------------------------------------------------------------


class TestClassifyChildMissing:
    """AC3 requirement: missing classification."""

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
        }
        entry = pmc._classify_child(parsed, open_issues=[], parent_issue=254)
        assert entry.status == "missing"
        assert entry.action == "create_issue"
        assert entry.existing_issue_number is None
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
        }
        entry = pmc._classify_child(parsed, open_issues=[], parent_issue=254)
        assert entry.status == "missing"
        assert entry.action == "create_issue"


class TestClassifyChildExisting:
    """AC3 requirement: existing classification."""

    def test_existing_when_ref_matches_open_issue(self) -> None:
        """GIVEN a child line referencing a known open issue #281
        WHEN classified
        THEN status is 'existing' and action is 'no_op'.
        """
        parsed = {
            "child_id": "C254-1",
            "rest": "docs: something #281",
            "is_placeholder": False,
            "raw_issue_refs": [281],
        }
        open_issues = [{"number": 281, "title": "docs: something"}]
        entry = pmc._classify_child(parsed, open_issues=open_issues, parent_issue=254)
        assert entry.status == "existing"
        assert entry.action == "no_op"
        assert entry.existing_issue_number == 281

    def test_existing_dedupe_key_format(self) -> None:
        """GIVEN a child classified as existing
        WHEN checking dedupe_key
        THEN it follows 'delivery-rollup:<parent>:<child_id>' format.
        """
        parsed = {
            "child_id": "C254-1",
            "rest": "docs: something #281",
            "is_placeholder": False,
            "raw_issue_refs": [281],
        }
        open_issues = [{"number": 281, "title": "docs: something"}]
        entry = pmc._classify_child(parsed, open_issues=open_issues, parent_issue=254)
        assert entry.dedupe_key == "delivery-rollup:254:C254-1"


class TestClassifyChildStaleBodyOnly:
    """AC3 requirement: stale_body_only classification."""

    def test_stale_body_only_when_placeholder_and_open_ref(self) -> None:
        """GIVEN a child line with （未起票） AND a reference to an open issue #285
        WHEN classified
        THEN status is 'stale_body_only' and action is 'reuse_and_update_parent'.
        """
        parsed = {
            "child_id": "C254-5",
            "rest": "feat: SDD 採否 ADR を追加（未起票） #285",
            "is_placeholder": True,
            "raw_issue_refs": [285],
        }
        open_issues = [{"number": 285, "title": "feat: SDD 採否 ADR を追加"}]
        entry = pmc._classify_child(parsed, open_issues=open_issues, parent_issue=254)
        assert entry.status == "stale_body_only"
        assert entry.action == "reuse_and_update_parent"
        assert entry.existing_issue_number == 285

    def test_ambiguous_when_ref_not_open(self) -> None:
        """GIVEN a child line referencing an issue not in open_issues
        WHEN classified
        THEN status is 'ambiguous' and action is 'human_escalation'.
        """
        parsed = {
            "child_id": "C254-9",
            "rest": "feat: something #999",
            "is_placeholder": False,
            "raw_issue_refs": [999],
        }
        entry = pmc._classify_child(parsed, open_issues=[], parent_issue=254)
        assert entry.status == "ambiguous"
        assert entry.action == "human_escalation"


# ---------------------------------------------------------------------------
# Tests: full build_plan with #254 fixture
# ---------------------------------------------------------------------------


class TestBuildPlan254Fixture:
    """Integration-level tests using the #254-equivalent fixture."""

    def test_parent_mode_is_delivery_rollup(self) -> None:
        """GIVEN the #254 fixture body
        WHEN build_plan is called
        THEN parent_mode is 'delivery-rollup'.
        """
        plan = _build(FIXTURE_PARENT_BODY_254)
        assert plan.parent_mode == "delivery-rollup"

    def test_parent_issue_number(self) -> None:
        plan = _build(FIXTURE_PARENT_BODY_254)
        assert plan.parent_issue == 254

    def test_c254_1_is_existing(self) -> None:
        """GIVEN C254-1 references open issue #281
        WHEN classified via build_plan
        THEN status is 'existing'.
        """
        plan = _build(FIXTURE_PARENT_BODY_254)
        child = _child_by_id(plan, "C254-1")
        assert child.status == "existing"
        assert child.existing_issue_number == 281
        assert child.action == "no_op"

    def test_c254_3_is_missing(self) -> None:
        """GIVEN C254-3 has （未起票） and no issue ref
        WHEN classified via build_plan
        THEN status is 'missing'.
        """
        plan = _build(FIXTURE_PARENT_BODY_254)
        child = _child_by_id(plan, "C254-3")
        assert child.status == "missing"
        assert child.existing_issue_number is None
        assert child.action == "create_issue"

    def test_c254_5_is_stale_body_only(self) -> None:
        """GIVEN C254-5 has both （未起票） and #285 (open issue)
        WHEN classified via build_plan
        THEN status is 'stale_body_only'.
        """
        plan = _build(FIXTURE_PARENT_BODY_254)
        child = _child_by_id(plan, "C254-5")
        assert child.status == "stale_body_only"
        assert child.existing_issue_number == 285
        assert child.action == "reuse_and_update_parent"

    def test_required_issue_creations_contains_missing(self) -> None:
        """GIVEN the #254 fixture
        WHEN build_plan is called
        THEN required_issue_creations contains C254-3.
        """
        plan = _build(FIXTURE_PARENT_BODY_254)
        assert "C254-3" in plan.required_issue_creations

    def test_required_issue_edits_contains_stale(self) -> None:
        """GIVEN the #254 fixture
        WHEN build_plan is called
        THEN required_issue_edits is non-empty (stale child needs parent body update).
        """
        plan = _build(FIXTURE_PARENT_BODY_254)
        assert len(plan.required_issue_edits) > 0

    def test_parent_body_updates_for_stale_child(self) -> None:
        """GIVEN C254-5 is stale_body_only
        WHEN build_plan is called
        THEN parent_body_updates contains a replace entry for C254-5.
        """
        plan = _build(FIXTURE_PARENT_BODY_254)
        stale_updates = [u for u in plan.parent_body_updates if "C254-5" in u.replace]
        assert len(stale_updates) == 1
        assert "（未起票）" not in stale_updates[0].with_
        assert "#285" in stale_updates[0].with_

    def test_three_children_detected(self) -> None:
        """GIVEN the fixture has exactly 3 child lines
        WHEN build_plan is called
        THEN plan.children has length 3.
        """
        plan = _build(FIXTURE_PARENT_BODY_254)
        assert len(plan.children) == 3

    def test_no_warnings_on_well_formed_body(self) -> None:
        """GIVEN a well-formed fixture body
        WHEN build_plan is called
        THEN no warnings are emitted.
        """
        plan = _build(FIXTURE_PARENT_BODY_254)
        assert plan.warnings == []


# ---------------------------------------------------------------------------
# Tests: YAML serialization
# ---------------------------------------------------------------------------


class TestPlanToYaml:
    def test_output_starts_with_schema_key(self) -> None:
        plan = _build(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert yaml_output.startswith("CHILD_MATERIALIZATION_PLAN_V1:")

    def test_output_contains_missing_status(self) -> None:
        plan = _build(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "missing" in yaml_output

    def test_output_contains_existing_status(self) -> None:
        plan = _build(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "existing" in yaml_output

    def test_output_contains_stale_body_only_status(self) -> None:
        plan = _build(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "stale_body_only" in yaml_output

    def test_output_contains_dedupe_key(self) -> None:
        plan = _build(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "delivery-rollup:254:C254-3" in yaml_output

    def test_empty_plan_no_warnings(self) -> None:
        """GIVEN an empty body (no child lines)
        WHEN plan_to_yaml is called
        THEN the output is still valid YAML (with a warning).
        """
        plan = _build("## Outcome\n\nNo child lines here.\n")
        yaml_output = pmc.plan_to_yaml(plan)
        assert "CHILD_MATERIALIZATION_PLAN_V1:" in yaml_output
        # A warning should be present about no children found
        assert "warnings:" in yaml_output


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_open_issues_list_classifies_ref_as_ambiguous(self) -> None:
        """GIVEN an empty open_issues list and a child with an issue ref
        WHEN build_plan is called
        THEN the child is classified as 'ambiguous'.
        """
        body = "- C254-1 docs: something #281\n"
        plan = pmc.build_plan(body, parent_issue=254, open_issues=[])
        child = _child_by_id(plan, "C254-1")
        assert child.status == "ambiguous"

    def test_bullet_form_child_lines_parsed(self) -> None:
        """GIVEN child lines prefixed with '- '
        WHEN _parse_child_lines is called
        THEN they are parsed correctly.
        """
        body = "- C254-2 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-2"

    def test_non_bullet_child_lines_parsed(self) -> None:
        """GIVEN child lines without bullet prefix
        WHEN _parse_child_lines is called
        THEN they are parsed correctly.
        """
        body = "C254-4 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-4"

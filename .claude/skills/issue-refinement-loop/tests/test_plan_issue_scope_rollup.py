"""
Tests for plan_issue_scope_rollup.py

Covers:
- AC1: script exists and pytest fixtures are present
- AC6: same_skill_family only -> confidence must be medium or low (not high)
- Basic: empty input -> candidates[] is empty
- confidence=high: shared_dedupe_key -> high
- confidence=high: exact_allowed_path_overlap + same_parent_issue -> high
- confidence=high: exact_allowed_path_overlap + same_failure_mode_marker -> high
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Resolve the script path relative to this test file
SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import plan_issue_scope_rollup as rollup  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def empty_issues_json(tmp_path: Path) -> str:
    """Fixture: path to a JSON file containing an empty issues list."""
    p = tmp_path / "issues_empty.json"
    p.write_text("[]", encoding="utf-8")
    return str(p)


@pytest.fixture()
def empty_prs_json(tmp_path: Path) -> str:
    """Fixture: path to a JSON file containing an empty PRs list."""
    p = tmp_path / "prs_empty.json"
    p.write_text("[]", encoding="utf-8")
    return str(p)


@pytest.fixture()
def issues_same_skill_family_json(tmp_path: Path) -> str:
    """Fixture: two issues sharing the same skill family but different Allowed Paths."""
    issues = [
        {
            "number": 100,
            "title": "実装: issue-refinement-loop に Step A を追加する",
            "body": (
                "## Allowed Paths\n"
                "- `.claude/skills/issue-refinement-loop/SKILL.md`\n\n"
                "## Outcome\nStep A が追加される。\n"
            ),
        },
        {
            "number": 101,
            "title": "実装: issue-refinement-loop に Step B を追加する",
            "body": (
                "## Allowed Paths\n"
                "- `.claude/skills/issue-refinement-loop/references/new-ref.md`\n\n"
                "## Outcome\nnew-ref.md が作成される。\n"
            ),
        },
    ]
    p = tmp_path / "issues_same_skill_family.json"
    p.write_text(json.dumps(issues), encoding="utf-8")
    return str(p)


@pytest.fixture()
def issues_shared_dedupe_key_json(tmp_path: Path) -> str:
    """Fixture: two issues with the same dedupe_key."""
    issues = [
        {
            "number": 200,
            "title": "実装: scope rollup preflight を追加する",
            "body": (
                "## Allowed Paths\n"
                "- `.claude/skills/issue-refinement-loop/scripts/plan_issue_scope_rollup.py`\n\n"
                "## Source\ndedupe_key: \"scope-rollup-preflight-v1\"\n"
            ),
        },
        {
            "number": 201,
            "title": "実装: scope rollup preflight を impl-review-loop にも追加する",
            "body": (
                "## Allowed Paths\n"
                "- `.claude/skills/impl-review-loop/steps/preparation.md`\n\n"
                "## Source\ndedupe_key: \"scope-rollup-preflight-v1\"\n"
            ),
        },
    ]
    p = tmp_path / "issues_shared_dedupe_key.json"
    p.write_text(json.dumps(issues), encoding="utf-8")
    return str(p)


@pytest.fixture()
def issues_allowed_path_and_parent_json(tmp_path: Path) -> str:
    """Fixture: two issues with exact allowed path overlap AND same parent issue."""
    issues = [
        {
            "number": 300,
            "title": "実装: SKILL.md に Step 0d を追加する",
            "body": (
                "## Machine-Readable Contract\n"
                "parent_issue: #250\n\n"
                "## Allowed Paths\n"
                "- `.claude/skills/issue-refinement-loop/SKILL.md`\n\n"
                "## Outcome\nStep 0d が追加される。\n"
            ),
        },
        {
            "number": 301,
            "title": "実装: SKILL.md に Step 0e を追加する",
            "body": (
                "## Machine-Readable Contract\n"
                "parent_issue: #250\n\n"
                "## Allowed Paths\n"
                "- `.claude/skills/issue-refinement-loop/SKILL.md`\n\n"
                "## Outcome\nStep 0e が追加される。\n"
            ),
        },
    ]
    p = tmp_path / "issues_allowed_path_and_parent.json"
    p.write_text(json.dumps(issues), encoding="utf-8")
    return str(p)


@pytest.fixture()
def issues_allowed_path_and_failure_mode_json(tmp_path: Path) -> str:
    """Fixture: two issues with exact allowed path overlap AND same failure mode marker."""
    issues = [
        {
            "number": 400,
            "title": "fix: scope rollup 失敗モード A を修正する",
            "body": (
                "failure_mode_marker: \"scope-overlap-bug\"\n\n"
                "## Allowed Paths\n"
                "- `.claude/skills/issue-refinement-loop/SKILL.md`\n\n"
                "## Outcome\n失敗モード A が修正される。\n"
            ),
        },
        {
            "number": 401,
            "title": "fix: scope rollup 失敗モード B を修正する",
            "body": (
                "failure_mode_marker: \"scope-overlap-bug\"\n\n"
                "## Allowed Paths\n"
                "- `.claude/skills/issue-refinement-loop/SKILL.md`\n\n"
                "## Outcome\n失敗モード B が修正される。\n"
            ),
        },
    ]
    p = tmp_path / "issues_allowed_path_and_failure_mode.json"
    p.write_text(json.dumps(issues), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# Basic tests
# ---------------------------------------------------------------------------


class TestEmptyInput:
    """GIVEN empty issues and PRs, WHEN plan is generated, THEN candidates is empty."""

    def test_candidates_empty(self, empty_issues_json: str, empty_prs_json: str) -> None:
        plan = rollup.run(empty_issues_json, empty_prs_json)
        assert plan["candidates"] == []

    def test_schema_version(self, empty_issues_json: str, empty_prs_json: str) -> None:
        plan = rollup.run(empty_issues_json, empty_prs_json)
        assert plan["schema_version"] == 2

    def test_source(self, empty_issues_json: str, empty_prs_json: str) -> None:
        plan = rollup.run(empty_issues_json, empty_prs_json)
        assert plan["source"] == "plan_issue_scope_rollup"

    def test_completeness_full(self, empty_issues_json: str, empty_prs_json: str) -> None:
        plan = rollup.run(empty_issues_json, empty_prs_json)
        assert plan["input"]["completeness"] == "full"
        assert plan["input"]["warnings"] == []


# ---------------------------------------------------------------------------
# AC6 adversarial test: same_skill_family only must NOT be high
# ---------------------------------------------------------------------------


class TestSameSkillFamilyOnlyConfidence:
    """
    AC6: GIVEN two issues sharing same_skill_family ONLY (no dedupe_key, no shared Allowed Paths
    exact match, no same_parent_issue, no same_failure_mode_marker),
    WHEN plan is generated,
    THEN confidence must be medium or low (never high).
    """

    def test_same_skill_family_only_not_high(
        self,
        issues_same_skill_family_json: str,
        empty_prs_json: str,
    ) -> None:
        """Adversarial: same_skill_family only -> confidence must not be high."""
        plan = rollup.run(
            issues_same_skill_family_json,
            empty_prs_json,
            current_issue_number=100,
        )
        for candidate in plan["candidates"]:
            signals = candidate["signals"]
            # Only same_skill_family should be present (no dedupe_key, paths differ)
            if signals == [rollup.SIGNAL_SAME_SKILL_FAMILY]:
                assert candidate["confidence"] != rollup.CONFIDENCE_HIGH, (
                    f"Candidate #{candidate['number']} has same_skill_family only "
                    f"but confidence is {candidate['confidence']!r} (must not be high)"
                )

    def test_same_skill_family_only_is_low(
        self,
        issues_same_skill_family_json: str,
        empty_prs_json: str,
    ) -> None:
        """GIVEN same_skill_family only, THEN confidence is low."""
        plan = rollup.run(
            issues_same_skill_family_json,
            empty_prs_json,
            current_issue_number=100,
        )
        for candidate in plan["candidates"]:
            signals = candidate["signals"]
            if signals == [rollup.SIGNAL_SAME_SKILL_FAMILY]:
                assert candidate["confidence"] == rollup.CONFIDENCE_LOW, (
                    f"Expected low, got {candidate['confidence']!r}"
                )


# ---------------------------------------------------------------------------
# confidence=high tests
# ---------------------------------------------------------------------------


class TestConfidenceHigh:
    """GIVEN high-confidence signals, WHEN plan is generated, THEN confidence is high."""

    def test_shared_dedupe_key_is_high(
        self,
        issues_shared_dedupe_key_json: str,
        empty_prs_json: str,
    ) -> None:
        """GIVEN shared_dedupe_key, THEN confidence is high."""
        plan = rollup.run(
            issues_shared_dedupe_key_json,
            empty_prs_json,
            current_issue_number=200,
        )
        assert plan["candidates"], "Expected at least one candidate"
        for candidate in plan["candidates"]:
            if rollup.SIGNAL_SHARED_DEDUPE_KEY in candidate["signals"]:
                assert candidate["confidence"] == rollup.CONFIDENCE_HIGH, (
                    f"Expected high for shared_dedupe_key, got {candidate['confidence']!r}"
                )

    def test_allowed_path_overlap_plus_same_parent_is_high(
        self,
        issues_allowed_path_and_parent_json: str,
        empty_prs_json: str,
    ) -> None:
        """GIVEN exact_allowed_path_overlap + same_parent_issue, THEN confidence is high."""
        plan = rollup.run(
            issues_allowed_path_and_parent_json,
            empty_prs_json,
            current_issue_number=300,
        )
        assert plan["candidates"], "Expected at least one candidate"
        for candidate in plan["candidates"]:
            signals = set(candidate["signals"])
            if (
                rollup.SIGNAL_EXACT_ALLOWED_PATH_OVERLAP in signals
                and rollup.SIGNAL_SAME_PARENT_ISSUE in signals
            ):
                assert candidate["confidence"] == rollup.CONFIDENCE_HIGH, (
                    f"Expected high for overlap+parent, got {candidate['confidence']!r}"
                )

    def test_allowed_path_overlap_plus_failure_mode_is_high(
        self,
        issues_allowed_path_and_failure_mode_json: str,
        empty_prs_json: str,
    ) -> None:
        """GIVEN exact_allowed_path_overlap + same_failure_mode_marker, THEN confidence is high."""
        plan = rollup.run(
            issues_allowed_path_and_failure_mode_json,
            empty_prs_json,
            current_issue_number=400,
        )
        assert plan["candidates"], "Expected at least one candidate"
        for candidate in plan["candidates"]:
            signals = set(candidate["signals"])
            if (
                rollup.SIGNAL_EXACT_ALLOWED_PATH_OVERLAP in signals
                and rollup.SIGNAL_SAME_FAILURE_MODE_MARKER in signals
            ):
                assert candidate["confidence"] == rollup.CONFIDENCE_HIGH, (
                    f"Expected high for overlap+failure_mode, got {candidate['confidence']!r}"
                )


# ---------------------------------------------------------------------------
# determine_confidence unit tests
# ---------------------------------------------------------------------------


class TestDetermineConfidence:
    """Unit tests for the _determine_confidence helper."""

    def test_shared_dedupe_key_alone(self) -> None:
        assert rollup._determine_confidence([rollup.SIGNAL_SHARED_DEDUPE_KEY]) == rollup.CONFIDENCE_HIGH

    def test_overlap_plus_parent(self) -> None:
        assert (
            rollup._determine_confidence(
                [rollup.SIGNAL_EXACT_ALLOWED_PATH_OVERLAP, rollup.SIGNAL_SAME_PARENT_ISSUE]
            )
            == rollup.CONFIDENCE_HIGH
        )

    def test_overlap_plus_failure_mode(self) -> None:
        assert (
            rollup._determine_confidence(
                [rollup.SIGNAL_EXACT_ALLOWED_PATH_OVERLAP, rollup.SIGNAL_SAME_FAILURE_MODE_MARKER]
            )
            == rollup.CONFIDENCE_HIGH
        )

    def test_same_skill_family_only_is_low(self) -> None:
        """AC6: same_skill_family only -> low (not high)."""
        result = rollup._determine_confidence([rollup.SIGNAL_SAME_SKILL_FAMILY])
        assert result == rollup.CONFIDENCE_LOW
        assert result != rollup.CONFIDENCE_HIGH

    def test_overlap_alone_is_medium(self) -> None:
        assert (
            rollup._determine_confidence([rollup.SIGNAL_EXACT_ALLOWED_PATH_OVERLAP])
            == rollup.CONFIDENCE_MEDIUM
        )

    def test_same_parent_alone_is_medium(self) -> None:
        assert (
            rollup._determine_confidence([rollup.SIGNAL_SAME_PARENT_ISSUE])
            == rollup.CONFIDENCE_MEDIUM
        )

    def test_skill_family_plus_overlap_is_medium(self) -> None:
        assert (
            rollup._determine_confidence(
                [rollup.SIGNAL_SAME_SKILL_FAMILY, rollup.SIGNAL_EXACT_ALLOWED_PATH_OVERLAP]
            )
            == rollup.CONFIDENCE_MEDIUM
        )

    def test_empty_signals_is_low(self) -> None:
        assert rollup._determine_confidence([]) == rollup.CONFIDENCE_LOW


# ---------------------------------------------------------------------------
# Output schema tests
# ---------------------------------------------------------------------------


class TestOutputSchema:
    """WHEN plan is generated, THEN output conforms to ISSUE_SCOPE_ROLLUP_PLAN_V2 schema."""

    def test_required_top_level_fields(
        self, empty_issues_json: str, empty_prs_json: str
    ) -> None:
        plan = rollup.run(empty_issues_json, empty_prs_json)
        for field in ("schema_version", "repo", "generated_at", "source", "body_sha256", "input", "candidates"):
            assert field in plan, f"Missing required field: {field}"

    def test_input_fields(self, empty_issues_json: str, empty_prs_json: str) -> None:
        plan = rollup.run(empty_issues_json, empty_prs_json)
        assert "completeness" in plan["input"]
        assert "warnings" in plan["input"]

    def test_candidate_fields(
        self,
        issues_shared_dedupe_key_json: str,
        empty_prs_json: str,
    ) -> None:
        plan = rollup.run(
            issues_shared_dedupe_key_json,
            empty_prs_json,
            current_issue_number=200,
        )
        for c in plan["candidates"]:
            for field in ("kind", "number", "confidence", "dedupe_key", "signals", "suggested_action"):
                assert field in c, f"Candidate missing field: {field}"

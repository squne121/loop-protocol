"""
Tests for plan_issue_scope_rollup.py

Covers:
- AC1: script exists and pytest fixtures are present
- AC6: same_skill_family only -> confidence must be medium or low (not high)
- Basic: empty input -> candidates[] is empty
- confidence=high: shared_dedupe_key -> high
- confidence=high: exact_allowed_path_overlap + same_parent_issue -> high
- confidence=high: exact_allowed_path_overlap + same_failure_mode_marker -> high
- B1: current_issue not found -> no fallback to first issue (completeness:partial, candidates:[])
- B3: Allowed Paths parser handles backtick spans and Japanese annotations
- B4: security keyword does not trigger on "issue-author", "token budget", etc.
- B5: PR files field is used as primary path signal
- B5: closed issues appear in candidates with non_mergeable_reasons
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
# Helper: run() now returns (plan, exit_code) — unwrap for convenience
# ---------------------------------------------------------------------------


def _run(*args, **kwargs):
    """Unwrap the (plan, exit_code) tuple returned by rollup.run()."""
    plan, _ = rollup.run(*args, **kwargs)
    return plan


def _run_with_code(*args, **kwargs):
    """Return both plan and exit_code."""
    return rollup.run(*args, **kwargs)


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
        plan = _run(empty_issues_json, empty_prs_json)
        assert plan["candidates"] == []

    def test_schema_version(self, empty_issues_json: str, empty_prs_json: str) -> None:
        plan = _run(empty_issues_json, empty_prs_json)
        assert plan["schema_version"] == 2

    def test_source(self, empty_issues_json: str, empty_prs_json: str) -> None:
        plan = _run(empty_issues_json, empty_prs_json)
        assert plan["source"] == "plan_issue_scope_rollup"

    def test_completeness_full(self, empty_issues_json: str, empty_prs_json: str) -> None:
        plan = _run(empty_issues_json, empty_prs_json)
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
        plan = _run(
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
        plan = _run(
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
        plan = _run(
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
        plan = _run(
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
        plan = _run(
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
        plan = _run(empty_issues_json, empty_prs_json)
        for field in ("schema_version", "repo", "generated_at", "source", "body_sha256", "input", "candidates"):
            assert field in plan, f"Missing required field: {field}"

    def test_input_fields(self, empty_issues_json: str, empty_prs_json: str) -> None:
        plan = _run(empty_issues_json, empty_prs_json)
        assert "completeness" in plan["input"]
        assert "warnings" in plan["input"]

    def test_candidate_fields(
        self,
        issues_shared_dedupe_key_json: str,
        empty_prs_json: str,
    ) -> None:
        plan = _run(
            issues_shared_dedupe_key_json,
            empty_prs_json,
            current_issue_number=200,
        )
        for c in plan["candidates"]:
            for field in ("kind", "number", "confidence", "dedupe_key", "signals", "suggested_action"):
                assert field in c, f"Candidate missing field: {field}"


# ---------------------------------------------------------------------------
# B1 adversarial: current_issue not found must NOT fallback to first issue
# ---------------------------------------------------------------------------


class TestCurrentIssueNotFoundNoFallback:
    """
    B1: GIVEN current_issue_number is specified but absent from issues_json,
    WHEN plan is generated,
    THEN completeness is 'partial', candidates is [], and exit_code is 2.
    Must NOT fall back to the first issue in the list.
    """

    def test_current_issue_not_found_does_not_fallback_to_first_issue(
        self, tmp_path: Path, empty_prs_json: str
    ) -> None:
        """
        GIVEN issues list contains issue #10,
        AND current_issue_number is 999 (not present),
        THEN candidates must be [] — no fallback to #10.
        """
        issues = [
            {
                "number": 10,
                "title": "実装: some feature",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/issue-refinement-loop/SKILL.md`\n"
                ),
            },
            {
                "number": 11,
                "title": "実装: another feature",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/issue-refinement-loop/SKILL.md`\n"
                ),
            },
        ]
        issues_path = tmp_path / "issues.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")

        plan, exit_code = _run_with_code(
            str(issues_path),
            empty_prs_json,
            current_issue_number=999,
        )

        assert plan["candidates"] == [], (
            "candidates must be empty when current issue is not found — no fallback"
        )
        assert plan["input"]["completeness"] == "partial"
        assert rollup._CURRENT_ISSUE_NOT_FOUND in plan["input"]["warnings"]
        assert exit_code == 2


# ---------------------------------------------------------------------------
# B3 adversarial: Allowed Paths parser with backtick and Japanese annotations
# ---------------------------------------------------------------------------


class TestAllowedPathsBacktickParsing:
    """
    B3: GIVEN bullet lines use backtick code spans and/or Japanese annotations,
    THEN paths are correctly normalised.
    """

    def test_allowed_paths_with_backtick_and_japanese_annotation_are_normalized(
        self, tmp_path: Path, empty_prs_json: str
    ) -> None:
        """
        GIVEN: "- `.claude/skills/foo.py`（新規作成）"
        THEN: extracted path == ".claude/skills/foo.py"
        """
        issues = [
            {
                "number": 500,
                "title": "実装: foo を追加する",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/foo.py`（新規作成）\n"
                    "- `.claude/skills/bar.md`\n"
                ),
            },
            {
                "number": 501,
                "title": "実装: foo を別から追加する",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/foo.py`（更新）\n"
                ),
            },
        ]
        issues_path = tmp_path / "issues.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")

        plan = _run(
            str(issues_path),
            empty_prs_json,
            current_issue_number=500,
        )

        # The two issues share .claude/skills/foo.py — they must detect overlap
        assert plan["candidates"], "Expected candidates from shared path"
        candidate = plan["candidates"][0]
        signals = set(candidate["signals"])
        assert (
            rollup.SIGNAL_EXACT_ALLOWED_PATH_OVERLAP in signals
            or rollup.SIGNAL_ALLOWED_PATH_INTERSECTION in signals
        ), f"Expected a path-overlap signal, got: {signals}"

    def test_allowed_path_extract_backtick_span(self) -> None:
        """Unit: _extract_path_from_bullet strips backtick span and annotation."""
        line = "- `.claude/skills/foo.py`（新規作成）"
        result = rollup._extract_path_from_bullet(line)
        assert result == ".claude/skills/foo.py", f"Got: {result!r}"

    def test_allowed_path_extract_plain_line(self) -> None:
        """Unit: _extract_path_from_bullet works on plain lines too."""
        line = "- .claude/skills/bar.md"
        result = rollup._extract_path_from_bullet(line)
        assert result == ".claude/skills/bar.md", f"Got: {result!r}"

    def test_allowed_path_extract_windows_separator_normalised(self) -> None:
        """Unit: backslashes are normalised to forward slashes."""
        line = "- `.claude\\skills\\baz.py`"
        result = rollup._extract_path_from_bullet(line)
        assert result == ".claude/skills/baz.py", f"Got: {result!r}"


# ---------------------------------------------------------------------------
# B4 adversarial: security keyword must not trigger on false positives
# ---------------------------------------------------------------------------


class TestSecurityKeywordWordBoundary:
    """
    B4: "auth" and "token" are removed from SECURITY_RE to avoid false positives.
    Compound words like "issue-author", "authored", "token_budget" must not match.
    """

    def test_issue_author_does_not_trigger_security_auth_keyword(self) -> None:
        """
        GIVEN title/body contains "issue-author" or "authored PR",
        THEN _is_security_related returns False.
        Note: the body must not incidentally contain other security keywords.
        """
        item = {
            "number": 600,
            "title": "issue-author skill update",
            "body": "This PR was authored by the issue-author SubAgent. No access-control changes.",
        }
        assert not rollup._is_security_related(item), (
            "'issue-author' / 'authored' must not trigger security detection"
        )

    def test_token_budget_does_not_trigger_security_token_keyword(self) -> None:
        """
        GIVEN title/body contains "トークン消費" or "token efficiency",
        THEN _is_security_related returns False.
        """
        item = {
            "number": 601,
            "title": "OUTPUT_BUDGET_V1 token efficiency improvement",
            "body": "Reduce トークン消費 by trimming verbose output. token budget matters.",
        }
        assert not rollup._is_security_related(item), (
            "'token' / 'トークン' must not trigger security detection"
        )

    def test_real_security_keyword_triggers(self) -> None:
        """GIVEN title contains 'authentication', THEN _is_security_related returns True."""
        item = {
            "number": 602,
            "title": "fix: authentication bypass vulnerability",
            "body": "Addresses an authentication issue in the login flow.",
        }
        assert rollup._is_security_related(item)

    def test_security_match_evidence_populated(self) -> None:
        """GIVEN security keyword is present, THEN security_match_evidence is set on candidate."""
        item = {
            "number": 603,
            "title": "fix: credential leak in logs",
            "body": "Credentials were accidentally logged.",
        }
        evidence = rollup._security_match_evidence(item)
        assert "credential" in evidence, f"Expected 'credential' in evidence: {evidence}"

    def test_security_match_evidence_absent_for_non_security(self) -> None:
        """GIVEN no security keywords, THEN _security_match_evidence returns []."""
        item = {
            "number": 604,
            "title": "refactor: clean up issue-author script",
            "body": "Just renaming variables and adding token efficiency checks.",
        }
        evidence = rollup._security_match_evidence(item)
        assert evidence == [], f"Expected no evidence, got: {evidence}"


# ---------------------------------------------------------------------------
# B5: PR files field is used as primary path signal
# ---------------------------------------------------------------------------


class TestPRFilesAsPrimaryPathSignal:
    """
    B5: GIVEN a PR has a `files` field,
    THEN its file paths are used as the path signal (not Allowed Paths from body).
    """

    def test_pr_files_are_used_as_primary_path_signal(
        self, tmp_path: Path
    ) -> None:
        """
        GIVEN current issue has Allowed Paths containing foo.py,
        AND a PR has files=[{path: ".claude/skills/foo.py"}],
        THEN the PR appears as a candidate with a path-overlap signal.
        """
        issues = [
            {
                "number": 700,
                "title": "実装: foo を追加する",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/foo.py`\n"
                ),
            },
        ]
        prs = [
            {
                "number": 701,
                "title": "feat: add foo",
                "body": "Adds foo.",
                "state": "OPEN",
                "files": [{"path": ".claude/skills/foo.py"}],
            },
        ]
        issues_path = tmp_path / "issues.json"
        prs_path = tmp_path / "prs.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")
        prs_path.write_text(json.dumps(prs), encoding="utf-8")

        plan = _run(str(issues_path), str(prs_path), current_issue_number=700)

        assert plan["candidates"], "Expected the PR to appear as a candidate"
        pr_candidates = [c for c in plan["candidates"] if c["kind"] == "pr"]
        assert pr_candidates, "Expected at least one PR candidate"
        signals = set(pr_candidates[0]["signals"])
        assert (
            rollup.SIGNAL_EXACT_ALLOWED_PATH_OVERLAP in signals
            or rollup.SIGNAL_ALLOWED_PATH_INTERSECTION in signals
        ), f"Expected path-overlap signal from PR files, got: {signals}"


# ---------------------------------------------------------------------------
# B5: closed issues appear with non_mergeable_reasons
# ---------------------------------------------------------------------------


class TestClosedCandidateReported:
    """
    B5: GIVEN --state all is used (closed issues included),
    WHEN a candidate is closed with stateReason=not_planned,
    THEN candidate has non_mergeable_reasons containing 'closed_not_planned'.
    """

    def test_closed_not_planned_candidate_is_reported(
        self, tmp_path: Path, empty_prs_json: str
    ) -> None:
        """
        GIVEN issue #801 is closed (not_planned) and shares a path with #800,
        THEN it appears in candidates with non_mergeable_reasons=['closed_not_planned'].
        """
        issues = [
            {
                "number": 800,
                "title": "実装: bar を追加する",
                "state": "OPEN",
                "stateReason": None,
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/bar.md`\n"
                ),
            },
            {
                "number": 801,
                "title": "実装: bar の別バージョン（クローズ済み）",
                "state": "CLOSED",
                "stateReason": "not_planned",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/bar.md`\n"
                ),
            },
        ]
        issues_path = tmp_path / "issues.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")

        plan = _run(str(issues_path), empty_prs_json, current_issue_number=800)

        assert plan["candidates"], "Expected at least one candidate"
        closed_candidates = [
            c for c in plan["candidates"] if c.get("number") == 801
        ]
        assert closed_candidates, "Expected closed issue #801 to appear in candidates"
        nmr = closed_candidates[0].get("non_mergeable_reasons", [])
        assert "closed_not_planned" in nmr, (
            f"Expected 'closed_not_planned' in non_mergeable_reasons, got: {nmr}"
        )

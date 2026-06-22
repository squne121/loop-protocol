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


# ---------------------------------------------------------------------------
# #550: structured scope_context generalises collision detection
# ---------------------------------------------------------------------------


@pytest.fixture()
def issues_547_549_same_schema_disjoint_anchor_json(tmp_path: Path) -> str:
    """Fixture modelled on the real #547 / #549 collision.

    Both issues touch the SAME schema file but target disjoint sub-file anchors:
    - #547 extends the `phase_instance_id` pattern
    - #549 adds `/required` to `secret_policy`

    PREMISE CORRECTION (#550 review Blocker 2): the original Issue framing claimed the
    false-positive came from the property name `secret_policy` being detected as the
    keyword `secret`. That premise is FALSE under the current SECURITY_RE — `_` is a
    word character, so `\\bsecret\\b` does NOT match `secret_policy`
    (see test_secret_policy_property_name_is_not_detected_as_secret_keyword below).

    The realistic false-positive trigger is a *standalone* security keyword appearing
    in the body (here `permission`, and a standalone `secret` value mention). This
    fixture reproduces that: a genuine SECURITY_RE match is present, yet escalation must
    NOT fire because the two issues target disjoint sub-file anchors.
    """
    schema_path = "- `.claude/skills/some-skill/schema/contract.schema.json`\n"
    issues = [
        {
            "number": 547,
            "title": "実装: phase_instance_id を CI-native 形式に拡張する",
            "body": (
                "## Allowed Paths\n"
                + schema_path
                + "\n## Outcome\n"
                "`phase_instance_id` の `/properties/phase_instance_id/pattern` を拡張する。\n"
                "permission 関連の挙動は変更しない。\n"
            ),
        },
        {
            "number": 549,
            "title": "実装: secret_policy に root required を追加する",
            "body": (
                "## Allowed Paths\n"
                + schema_path
                + "\n## Outcome\n"
                "`secret_policy` に `/required` を追加する。\n"
                # Standalone 'permission' / 'secret' words DO match SECURITY_RE — this is
                # the real trigger, not the `secret_policy` property name itself.
                "permission boundary は据え置き。secret value の取り扱いは変更しない。\n"
            ),
        },
    ]
    p = tmp_path / "issues_547_549.json"
    p.write_text(json.dumps(issues), encoding="utf-8")
    return str(p)


@pytest.fixture()
def issues_same_anchor_conflict_json(tmp_path: Path) -> str:
    """Fixture: two issues both modifying the SAME sub-file anchor (`/required`)."""
    schema_path = "- `.claude/skills/some-skill/schema/contract.schema.json`\n"
    issues = [
        {
            "number": 910,
            "title": "実装: schema の root required に field A を追加する",
            "body": (
                "## Allowed Paths\n"
                + schema_path
                + "\n## Outcome\n"
                "`/required` に field A を追加する。\n"
            ),
        },
        {
            "number": 911,
            "title": "実装: schema の root required から field B を削除する",
            "body": (
                "## Allowed Paths\n"
                + schema_path
                + "\n## Outcome\n"
                "`/required` から field B を削除する。\n"
            ),
        },
    ]
    p = tmp_path / "issues_same_anchor_conflict.json"
    p.write_text(json.dumps(issues), encoding="utf-8")
    return str(p)


@pytest.fixture()
def issues_exact_same_file_no_anchor_json(tmp_path: Path) -> str:
    """Fixture: exact same Allowed Paths but NO machine-readable sub-file anchors.

    Disjointness cannot be proven, so the classifier must fail-safe to `uncertain`.
    """
    schema_path = "- `.claude/skills/some-skill/schema/contract.schema.json`\n"
    issues = [
        {
            "number": 920,
            "title": "実装: schema を更新する A",
            "body": (
                "## Allowed Paths\n"
                + schema_path
                + "\n## Outcome\nスキーマを更新する。\n"
            ),
        },
        {
            "number": 921,
            "title": "実装: schema を更新する B",
            "body": (
                "## Allowed Paths\n"
                + schema_path
                + "\n## Outcome\nスキーマを別の観点で更新する。\n"
            ),
        },
    ]
    p = tmp_path / "issues_exact_same_file_no_anchor.json"
    p.write_text(json.dumps(issues), encoding="utf-8")
    return str(p)


class TestScopeContextGeneralisation:
    """#550: collision detection is driven by structured scope_context, not keyword match."""

    def test_scope_rollup_treats_schema_property_keyword_as_metadata_not_domain_specific_risk(
        self,
        issues_547_549_same_schema_disjoint_anchor_json: str,
        empty_prs_json: str,
    ) -> None:
        """AC4: #547/#549-style — same schema file, disjoint anchors, shared keyword.

        The shared security-adjacent keyword and schema property names must be recorded
        for audit (domain_flags / security_match_evidence) but MUST NOT cause
        human_review_required. The conflict is classified as same_file_disjoint_anchor.
        """
        plan = _run(
            issues_547_549_same_schema_disjoint_anchor_json,
            empty_prs_json,
            current_issue_number=547,
        )
        candidates = [c for c in plan["candidates"] if c.get("number") == 549]
        assert candidates, "Expected #549 to appear as a candidate of #547"
        c = candidates[0]

        # The escalation must NOT fire from a keyword / property-name match alone.
        assert c["suggested_action"] != rollup.ACTION_HUMAN_REVIEW_REQUIRED, (
            f"Keyword/property-name match must not escalate; got {c['suggested_action']!r}"
        )

        sc = c["scope_context"]
        assert sc["conflict_type"] == rollup.CONFLICT_SAME_FILE_DISJOINT_ANCHOR, (
            f"Expected same_file_disjoint_anchor, got {sc['conflict_type']!r}"
        )
        assert sc["escalation_required"] is False

        # Audit trail is preserved: domain_flags classifies the change areas, and the
        # security keyword is still recorded in security_match_evidence (AC7 additive).
        assert rollup.DOMAIN_SCHEMA in sc["domain_flags"] or rollup.DOMAIN_METADATA in sc["domain_flags"], (
            f"Expected schema/metadata in domain_flags for audit, got {sc['domain_flags']!r}"
        )
        assert "permission" in c.get("security_match_evidence", []), (
            "Security keyword must still be recorded for audit even when not escalated"
        )

    def test_scope_rollup_escalates_uncertain_or_boundary_affecting_changes(
        self,
        issues_same_anchor_conflict_json: str,
        issues_exact_same_file_no_anchor_json: str,
        empty_prs_json: str,
    ) -> None:
        """AC5: genuine conflicts escalate.

        Two escalation paths are covered:
        1. boundary-affecting: both issues modify the SAME sub-file anchor (`/required`)
           -> same_anchor_conflicting_operation -> human_review_required
        2. uncertain: exact same file with no machine-readable anchors to prove
           disjointness -> uncertain -> human_review_required (fail-safe)
        """
        # Path 1: same sub-file anchor -> conflicting operation
        plan_conflict = _run(
            issues_same_anchor_conflict_json,
            empty_prs_json,
            current_issue_number=910,
        )
        conflict_candidates = [c for c in plan_conflict["candidates"] if c.get("number") == 911]
        assert conflict_candidates, "Expected #911 to appear as a candidate of #910"
        cc = conflict_candidates[0]
        assert cc["scope_context"]["conflict_type"] == rollup.CONFLICT_SAME_ANCHOR_CONFLICTING_OP, (
            f"Expected same_anchor_conflicting_operation, got {cc['scope_context']['conflict_type']!r}"
        )
        assert cc["scope_context"]["escalation_required"] is True
        assert cc["suggested_action"] == rollup.ACTION_HUMAN_REVIEW_REQUIRED, (
            f"Same-anchor conflict must escalate; got {cc['suggested_action']!r}"
        )

        # Path 2: exact same file, no anchors -> uncertain -> proceed_with_coordination (not escalate)
        plan_uncertain = _run(
            issues_exact_same_file_no_anchor_json,
            empty_prs_json,
            current_issue_number=920,
        )
        uncertain_candidates = [c for c in plan_uncertain["candidates"] if c.get("number") == 921]
        assert uncertain_candidates, "Expected #921 to appear as a candidate of #920"
        uc = uncertain_candidates[0]
        assert uc["scope_context"]["conflict_type"] == rollup.CONFLICT_UNCERTAIN, (
            f"Expected uncertain, got {uc['scope_context']['conflict_type']!r}"
        )
        assert uc["scope_context"]["escalation_required"] is False, (
            "uncertain conflict_type must have escalation_required=False"
        )
        assert uc["suggested_action"] == rollup.ACTION_PROCEED_WITH_COORDINATION, (
            f"Uncertain conflict must return proceed_with_coordination; got {uc['suggested_action']!r}"
        )

    def test_scope_context_and_ordering_constraint_are_additive_fields(
        self,
        issues_547_549_same_schema_disjoint_anchor_json: str,
        empty_prs_json: str,
    ) -> None:
        """AC1/AC2/AC3/AC7: new fields are present and legacy fields are preserved."""
        plan = _run(
            issues_547_549_same_schema_disjoint_anchor_json,
            empty_prs_json,
            current_issue_number=547,
        )
        assert plan["candidates"], "Expected candidates"
        c = plan["candidates"][0]
        # AC1/AC2: scope_context with domain_flags
        assert "scope_context" in c
        assert "domain_flags" in c["scope_context"]
        # AC3: ordering_constraint is a separate top-level candidate field
        assert "ordering_constraint" in c
        assert c["ordering_constraint"] != c["suggested_action"], (
            "ordering_constraint must be a distinct concern from suggested_action"
        )
        # AC7: legacy fields preserved (additive extension)
        for legacy_field in ("kind", "number", "confidence", "signals", "suggested_action", "dedupe_key"):
            assert legacy_field in c, f"Legacy field {legacy_field!r} must be preserved"

    def test_secret_policy_property_name_is_not_detected_as_secret_keyword(self) -> None:
        """#550 review Blocker 2: correct the false `secret_policy` -> `secret` premise.

        SECURITY_RE uses word boundaries and `_` is a word character, so the property
        name `secret_policy` does NOT match the keyword `secret`. The original Issue
        premise ("secret_policy was detected as secret") is therefore FALSE; the real
        trigger is a *standalone* security keyword.
        """
        # The property name alone is NOT detected.
        for not_detected in ("secret_policy", "add secret_policy to required", "`secret_policy`"):
            item = {"number": 1, "title": "", "body": not_detected}
            assert rollup._security_match_evidence(item) == [], (
                f"{not_detected!r} must NOT match SECURITY_RE (property name, not keyword)"
            )
        # A standalone keyword IS detected — this is the real false-positive trigger.
        item = {"number": 2, "title": "", "body": "the secret value is logged; permission changed"}
        evidence = rollup._security_match_evidence(item)
        assert "secret" in evidence and "permission" in evidence, (
            f"Standalone security keywords must be detected, got {evidence!r}"
        )

    def test_standalone_security_keyword_in_disjoint_anchor_case_is_recorded_but_not_escalated(
        self,
        issues_547_549_same_schema_disjoint_anchor_json: str,
        empty_prs_json: str,
    ) -> None:
        """#550 review Blocker 2 (realistic): a genuine SECURITY_RE match present, yet
        disjoint anchors mean no escalation. Proves keyword detection != escalation.
        """
        plan = _run(
            issues_547_549_same_schema_disjoint_anchor_json,
            empty_prs_json,
            current_issue_number=547,
        )
        c = next(c for c in plan["candidates"] if c.get("number") == 549)
        # A real standalone keyword WAS detected (audit trail present)...
        assert "secret" in c.get("security_match_evidence", []), (
            f"Standalone 'secret' must be recorded for audit, got {c.get('security_match_evidence')!r}"
        )
        # ...but it does NOT drive escalation (disjoint sub-file anchors).
        assert c["scope_context"]["conflict_type"] == rollup.CONFLICT_SAME_FILE_DISJOINT_ANCHOR
        assert c["scope_context"]["escalation_required"] is False
        assert c["suggested_action"] != rollup.ACTION_HUMAN_REVIEW_REQUIRED

    def test_prefix_overlap_records_anchor_paths_and_is_not_classified_none(
        self, tmp_path: Path, empty_prs_json: str
    ) -> None:
        """#550 review Blocker 4: a prefix (parent->child) path overlap must NOT collapse
        scope_context to conflict_type: none. The overlap pair is recorded in anchor_paths.
        """
        issues = [
            {
                "number": 1000,
                "title": "実装: skill dir 全体を更新する",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/foo`\n"
                    "\n## Outcome\nディレクトリを更新する。\n"
                ),
            },
            {
                "number": 1001,
                "title": "実装: skill の SKILL.md を更新する",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/foo/SKILL.md`\n"
                    "\n## Outcome\nSKILL.md を更新する。\n"
                ),
            },
        ]
        issues_path = tmp_path / "issues_prefix.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")

        plan = _run(str(issues_path), empty_prs_json, current_issue_number=1000)
        candidates = [c for c in plan["candidates"] if c.get("number") == 1001]
        assert candidates, "Expected prefix-overlap candidate"
        c = candidates[0]
        assert rollup.SIGNAL_ALLOWED_PATH_PREFIX_OVERLAP in c["signals"]
        sc = c["scope_context"]
        assert sc["conflict_type"] == rollup.CONFLICT_PREFIX_OVERLAP_UNCERTAIN, (
            f"Prefix overlap must not be 'none'; got {sc['conflict_type']!r}"
        )
        assert sc["anchor_paths"], "Prefix overlap pair must be recorded in anchor_paths"
        assert any("->" in p for p in sc["anchor_paths"]), (
            f"Expected a parent->child pair in anchor_paths, got {sc['anchor_paths']!r}"
        )


# ---------------------------------------------------------------------------
# #607: proceed_with_coordination replaces uncertain escalation
# ---------------------------------------------------------------------------


class TestProceedWithCoordination:
    """#607: uncertain conflict type returns proceed_with_coordination, not human_review_required."""

    def test_same_allowed_paths_no_anchor_should_proceed_with_coordination(
        self, tmp_path: Path, empty_prs_json: str
    ) -> None:
        """GIVEN exact same Allowed Paths AND no machine-readable sub-file anchors,
        WHEN plan is generated,
        THEN suggested_action is proceed_with_coordination (not human_review_required).
        """
        schema_path = "- `.claude/skills/some-skill/SKILL.md`\n"
        issues = [
            {
                "number": 1100,
                "title": "実装: SKILL.md を更新する X",
                "body": (
                    "## Allowed Paths\n"
                    + schema_path
                    + "\n## Outcome\nSKILL.md を更新する。\n"
                ),
            },
            {
                "number": 1101,
                "title": "実装: SKILL.md を更新する Y",
                "body": (
                    "## Allowed Paths\n"
                    + schema_path
                    + "\n## Outcome\nSKILL.md を別の観点で更新する。\n"
                ),
            },
        ]
        issues_path = tmp_path / "issues_same_paths_no_anchor.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")

        plan = _run(str(issues_path), empty_prs_json, current_issue_number=1100)
        candidates = [c for c in plan["candidates"] if c.get("number") == 1101]
        assert candidates, "Expected #1101 to appear as a candidate of #1100"
        c = candidates[0]

        assert c["scope_context"]["conflict_type"] == rollup.CONFLICT_UNCERTAIN, (
            f"Expected uncertain, got {c['scope_context']['conflict_type']!r}"
        )
        assert c["scope_context"]["escalation_required"] is False, (
            "uncertain conflict must have escalation_required=False"
        )
        assert c["suggested_action"] == rollup.ACTION_PROCEED_WITH_COORDINATION, (
            f"Expected proceed_with_coordination, got {c['suggested_action']!r}"
        )

    def test_permission_keyword_only_should_not_escalate(self) -> None:
        """GIVEN body contains standalone 'permission' (SECURITY_RE match) AND
        scope_context has conflict_type=uncertain AND escalation_required=False,
        WHEN _suggested_action is called,
        THEN action is proceed_with_coordination (not human_review_required).

        This exercises the actual word-boundary `permission` keyword path (unlike
        'permissionMode' which does NOT match \\bpermission\\b). The test verifies that
        security_match_evidence containing "permission" does NOT drive escalation — only
        escalation_required (genuine structural conflict) drives human_review_required.
        This reproduces the #569/#597 false-positive: an unrelated Issue containing
        standalone 'permission' caused human_escalation due to keyword+same-paths match.
        """
        # Directly test _suggested_action with a scope_context that includes the
        # standalone 'permission' evidence but CONFLICT_UNCERTAIN (no shared anchors).
        # CONFLICT_UNCERTAIN must be evaluated before escalation_required.
        action = rollup._suggested_action(
            item={},
            signals=[],
            confidence=rollup.CONFIDENCE_MEDIUM,
            scope_context={
                "conflict_type": rollup.CONFLICT_UNCERTAIN,
                "security_match_evidence": ["permission"],
                "escalation_required": False,
            },
        )
        assert action == rollup.ACTION_PROCEED_WITH_COORDINATION, (
            f"permission keyword in security_match_evidence must not escalate; "
            f"got {action!r}"
        )

    def test_permission_keyword_word_boundary_triggers_security_match_evidence(
        self, tmp_path: Path, empty_prs_json: str
    ) -> None:
        """Verify that standalone 'permission' (not 'permissionMode') is detected by
        SECURITY_RE and recorded in security_match_evidence — but does NOT escalate.

        Integration path: body with standalone 'permission' + same Allowed Paths + no
        anchors -> CONFLICT_UNCERTAIN -> proceed_with_coordination.
        """
        shared_path = "- `.claude/skills/impl-review-loop/steps/preparation.md`\n"
        issues = [
            {
                "number": 1200,
                "title": "実装: preparation.md に proceed_with_coordination を追加する",
                "body": (
                    "## Allowed Paths\n"
                    + shared_path
                    + "\n## Outcome\nキーワードベース停止を廃止する。\n"
                ),
            },
            {
                "number": 1201,
                "title": "実装: preparation.md の permission 設定を更新する",
                "body": (
                    "## Allowed Paths\n"
                    + shared_path
                    + "\n## Outcome\npermission チェックの挙動を変更する。\n"
                ),
            },
        ]
        issues_path = tmp_path / "issues_permission_keyword_integration.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")

        plan = _run(str(issues_path), empty_prs_json, current_issue_number=1200)
        candidates = [c for c in plan["candidates"] if c.get("number") == 1201]
        assert candidates, "Expected #1201 to appear as a candidate of #1200"
        c = candidates[0]

        # The standalone 'permission' keyword IS detected by SECURITY_RE
        assert "permission" in c.get("security_match_evidence", []), (
            "Standalone 'permission' must be recorded in security_match_evidence for audit"
        )
        # But it must NOT cause escalation — CONFLICT_UNCERTAIN -> proceed_with_coordination
        assert c["scope_context"]["conflict_type"] == rollup.CONFLICT_UNCERTAIN, (
            f"Expected uncertain (same paths, no anchors), got {c['scope_context']['conflict_type']!r}"
        )
        assert c["suggested_action"] != rollup.ACTION_HUMAN_REVIEW_REQUIRED, (
            f"permission keyword alone must not escalate; got {c['suggested_action']!r}"
        )
        assert c["suggested_action"] == rollup.ACTION_PROCEED_WITH_COORDINATION, (
            f"Expected proceed_with_coordination, got {c['suggested_action']!r}"
        )

    def test_same_anchor_conflicting_op_should_escalate(
        self, tmp_path: Path, empty_prs_json: str
    ) -> None:
        """GIVEN both issues target the same sub-file anchor,
        WHEN plan is generated,
        THEN suggested_action is human_review_required (genuine structural conflict).
        """
        shared_path = "- `.claude/skills/some-skill/schema/contract.schema.json`\n"
        issues = [
            {
                "number": 1300,
                "title": "実装: schema の `/required` に field A を追加する",
                "body": (
                    "## Allowed Paths\n"
                    + shared_path
                    + "\n## Outcome\n`/required` に field A を追加する。\n"
                ),
            },
            {
                "number": 1301,
                "title": "実装: schema の `/required` から field B を削除する",
                "body": (
                    "## Allowed Paths\n"
                    + shared_path
                    + "\n## Outcome\n`/required` から field B を削除する。\n"
                ),
            },
        ]
        issues_path = tmp_path / "issues_same_anchor_escalate.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")

        plan = _run(str(issues_path), empty_prs_json, current_issue_number=1300)
        candidates = [c for c in plan["candidates"] if c.get("number") == 1301]
        assert candidates, "Expected #1301 to appear as a candidate of #1300"
        c = candidates[0]

        assert c["scope_context"]["conflict_type"] == rollup.CONFLICT_SAME_ANCHOR_CONFLICTING_OP, (
            f"Expected same_anchor_conflicting_operation, got {c['scope_context']['conflict_type']!r}"
        )
        assert c["scope_context"]["escalation_required"] is True
        assert c["suggested_action"] == rollup.ACTION_HUMAN_REVIEW_REQUIRED, (
            f"Genuine structural conflict must escalate; got {c['suggested_action']!r}"
        )

    def test_prefix_overlap_uncertain_should_not_escalate(
        self, tmp_path: Path, empty_prs_json: str
    ) -> None:
        """GIVEN prefix-only path overlap (no exact intersection) AND no shared anchors,
        WHEN plan is generated,
        THEN conflict_type is prefix_overlap_uncertain AND suggested_action is NOT human_review_required.
        """
        issues = [
            {
                "number": 1400,
                "title": "実装: skill dir を更新する",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/some-skill`\n"
                    "\n## Outcome\nディレクトリ全体を更新する。\n"
                ),
            },
            {
                "number": 1401,
                "title": "実装: skill の README を更新する",
                "body": (
                    "## Allowed Paths\n"
                    "- `.claude/skills/some-skill/README.md`\n"
                    "\n## Outcome\nREADME.md を更新する。\n"
                ),
            },
        ]
        issues_path = tmp_path / "issues_prefix_overlap.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")

        plan = _run(str(issues_path), empty_prs_json, current_issue_number=1400)
        candidates = [c for c in plan["candidates"] if c.get("number") == 1401]
        assert candidates, "Expected #1401 to appear as a candidate of #1400"
        c = candidates[0]

        assert c["scope_context"]["conflict_type"] == rollup.CONFLICT_PREFIX_OVERLAP_UNCERTAIN, (
            f"Expected prefix_overlap_uncertain, got {c['scope_context']['conflict_type']!r}"
        )
        assert c["suggested_action"] != rollup.ACTION_HUMAN_REVIEW_REQUIRED, (
            f"Prefix overlap without shared anchors must not escalate; got {c['suggested_action']!r}"
        )

    def test_uncertain_conflict_takes_precedence_over_stale_escalation_flag(self) -> None:
        """GIVEN conflict_type is CONFLICT_UNCERTAIN AND escalation_required is True
        (stale / erroneously-set flag),
        WHEN _suggested_action is called,
        THEN action is proceed_with_coordination — CONFLICT_UNCERTAIN is evaluated first.

        This guards against a regression where escalation_required=True could override
        the CONFLICT_UNCERTAIN routing even though CONFLICT_UNCERTAIN is definitionally
        NOT a genuine structural conflict.
        """
        action = rollup._suggested_action(
            item={},
            signals=[],
            confidence=rollup.CONFIDENCE_HIGH,
            scope_context={
                "conflict_type": rollup.CONFLICT_UNCERTAIN,
                "escalation_required": True,
            },
        )
        assert action == rollup.ACTION_PROCEED_WITH_COORDINATION, (
            f"CONFLICT_UNCERTAIN must take precedence over stale escalation_required=True; "
            f"got {action!r}"
        )


# ---------------------------------------------------------------------------
# AC2: self_validation.payload_sha256 stability tests (Issue #820)
# ---------------------------------------------------------------------------


class TestSelfValidation:
    """AC2: self_validation block is present in run() output.
    payload_sha256 covers the plan without self_validation (self-reference excluded).
    """

    def test_run_output_contains_self_validation_block(
        self, empty_issues_json: str, empty_prs_json: str
    ) -> None:
        """AC1/AC2: GIVEN run() is called, THEN output contains self_validation block
        with payload_sha256 field.
        """
        plan, _ = _run_with_code(empty_issues_json, empty_prs_json)
        assert "self_validation" in plan, "plan must contain self_validation block"
        sv = plan["self_validation"]
        assert "payload_sha256" in sv, "self_validation must contain payload_sha256"
        assert isinstance(sv["payload_sha256"], str)
        assert len(sv["payload_sha256"]) == 64, "sha256 hex digest must be 64 chars"

    def test_payload_sha256_is_stable_for_same_inputs(
        self, empty_issues_json: str, empty_prs_json: str
    ) -> None:
        """AC2: GIVEN a plan dict (without self_validation), THEN payload_sha256 is
        deterministic (byte-stable) when applied to the same dict twice.

        Note: run() always produces a fresh generated_at timestamp, so two separate
        run() calls will produce different payload_sha256 values even for the same
        logical inputs. The stability guarantee applies to _compute_payload_sha256()
        itself (same dict -> same hash), not to successive run() calls.
        """
        import plan_issue_scope_rollup as rollup_module
        # Build a fixed payload and verify that hashing it twice gives the same result
        fixed_payload = {
            "schema_version": 2,
            "repo": "test/repo",
            "generated_at": "2026-06-13T00:00:00Z",  # fixed timestamp
            "source": "plan_issue_scope_rollup",
            "body_sha256": "abc123",
            "input": {"completeness": "full", "warnings": []},
            "candidates": [],
        }
        hash1 = rollup_module._compute_payload_sha256(fixed_payload)
        hash2 = rollup_module._compute_payload_sha256(fixed_payload)
        assert hash1 == hash2, (
            "payload_sha256 must be deterministic (same dict -> same hash)"
        )
        assert len(hash1) == 64

    def test_payload_sha256_changes_when_candidates_change(
        self,
        empty_issues_json: str,
        empty_prs_json: str,
        issues_shared_dedupe_key_json: str,
    ) -> None:
        """AC2: GIVEN different candidates, THEN payload_sha256 differs."""
        plan_empty, _ = _run_with_code(empty_issues_json, empty_prs_json, invocation_id="id-1")
        plan_with_candidates, _ = _run_with_code(
            issues_shared_dedupe_key_json, empty_prs_json,
            current_issue_number=200, invocation_id="id-2"
        )
        assert (
            plan_empty["self_validation"]["payload_sha256"]
            != plan_with_candidates["self_validation"]["payload_sha256"]
        ), "payload_sha256 must differ when candidates differ"

    def test_self_validation_contains_script_file_sha256(
        self, empty_issues_json: str, empty_prs_json: str
    ) -> None:
        """AC3: GIVEN run() output, THEN self_validation.script_file_sha256 is present."""
        plan, _ = _run_with_code(empty_issues_json, empty_prs_json)
        sv = plan["self_validation"]
        assert "script_file_sha256" in sv, "self_validation must contain script_file_sha256"
        assert isinstance(sv["script_file_sha256"], str)
        assert len(sv["script_file_sha256"]) == 64

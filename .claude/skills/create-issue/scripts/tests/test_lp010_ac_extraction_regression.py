#!/usr/bin/env python3
"""Regression tests for LP010 AC-number extraction (Issue #1704).

Background: `validate_issue_body.py`'s `_extract_ac_numbers()` used a
hyphen-bullet-only regex (`r'- \\[[^\\]]*\\]\\s+AC(\\d+)\\b'`). Any Issue body
whose `## Acceptance Criteria` section used asterisk (`*`) or plus (`+`)
task-list markers produced an EMPTY extracted AC set. Comparing that
(wrongly empty) set against a Verification Commands section with zero
`# AC<N>` references vacuously "matched" (both empty), so LP010 silently
PASSED bodies that `check_issue_contract.py`'s C5_ac_vc_number_alignment
correctly FAILED (Issue #1415, 2026-07-24).

GIVEN/WHEN/THEN:
  - AC1: `_extract_ac_numbers()` recognises "-"/"*"/"+" task-list markers,
    "[ ]"/"[x]"/"[X]" checkbox states, and 1-4 spaces after the marker.
  - AC2: an all-asterisk-bullet AC section (AC1-AC15) with a VC section
    that has zero `# AC<N>` references FAILS LP010 (not a vacuous pass).
  - AC3: a mixed hyphen/asterisk AC section FAILS LP010 when the VC
    section is genuinely missing a reference for one of the ACs.
  - AC4: an "AC<N>"-shaped token in prose, a URL, a filename, inline code,
    or a fenced code sample is never extracted as an AC definition.
  - AC5: on the same all-asterisk/zero-marker fixture, `check_issue_contract.py`
    (C5_ac_vc_number_alignment) and `validate_issue_body.py` (LP010) agree
    (both FAIL) -- a fixture-level parity check, not a general parity claim.
  - AC8: the Issue #1415 historical reproduction fixture is pinned in-repo
    (no GitHub live-body fetch), with `source_issue` / `source_observed_at`
    / `source_body_sha256` recorded.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# __file__ is at: <repo>/.claude/skills/create-issue/scripts/tests/test_lp010_ac_extraction_regression.py
# parents: [0]=tests, [1]=scripts, [2]=create-issue, [3]=skills, [4]=.claude, [5]=<repo root>
_REPO_ROOT = Path(__file__).resolve().parents[5]
_CREATE_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"

sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS))
sys.path.insert(0, str(_REVIEW_ISSUE_SCRIPTS))

from validate_issue_body import (  # noqa: E402
    _extract_ac_numbers,
    _extract_vc_ac_numbers,
    validate_issue_body,
)
from check_issue_contract import check_c5_ac_vc_alignment  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _asterisk_only_ac_section(count: int) -> str:
    """AC1..AC<count>, each as a "* [ ] AC<n>: ..." bullet (no VC refs)."""
    return "\n".join(f"* [ ] AC{n}: item number {n}" for n in range(1, count + 1))


# Issue #1415 reproduction: AC1-AC15, all asterisk-bullet, VC has zero
# `# AC<N>` markers. Used by AC2, AC5, AC8.
FIXTURE_ZERO_MARKER_ASTERISK_BODY = f"""
## Acceptance Criteria

{_asterisk_only_ac_section(15)}

## Verification Commands

```bash
$ echo "no per-AC markers here"
```
"""


# AC3: mixed hyphen/asterisk bullets, VC genuinely missing a reference
# for AC2 (the asterisk-bulleted item). Pre-#1704, `_extract_ac_numbers()`
# would have silently dropped AC2 from the extracted set (hyphen-only
# regex), making `ac_numbers == {"AC1"} == vc_numbers` -- a false PASS
# that hid the real missing-VC-reference gap.
FIXTURE_MIXED_HYPHEN_ASTERISK_BODY = """
## Acceptance Criteria

- [ ] AC1: hyphen-bulleted item
* [ ] AC2: asterisk-bulleted item

## Verification Commands

```bash
$ echo "covers AC1 only"  # AC1
```
"""


# AC4: "AC<N>"-shaped tokens appear in prose, a URL, a filename, inline
# code, and a fenced code sample -- none of these are AC *definitions*.
# Only AC1 (hyphen bullet, checkbox at line start) is a real definition.
FIXTURE_PROSE_AND_CODE_NOISE_BODY = """
## Acceptance Criteria

- [ ] AC1: see also AC2 for background, url https://example.com/AC3,
      file AC4.py, and inline code `- [ ] AC5` describing the old format.

```
- [ ] AC9: this looks like a definition but is inside a fenced code block
* [ ] AC10: same, asterisk bullet, still fenced
```

## Verification Commands

```bash
$ echo "covers AC1 only"  # AC1
```
"""


# ---------------------------------------------------------------------------
# AC1: mixed marker recognition
# ---------------------------------------------------------------------------


class TestLp010ExtractAcNumbersRecognizesMixedMarkers:
    def test_lp010_extract_ac_numbers_recognizes_mixed_markers(self):
        """GIVEN an AC section using hyphen, asterisk, and plus bullet
        markers (with 1-4 spaces after the marker and any of
        [ ]/[x]/[X] checkbox states) WHEN `_extract_ac_numbers()` runs
        THEN it extracts all three AC numbers (not just the hyphen one)."""
        body = """
## Acceptance Criteria

- [ ] AC1: hyphen bullet, single space
*  [x] AC2: asterisk bullet, two spaces, checked
+    [X] AC3: plus bullet, four spaces, upper-case checked

## Verification Commands

```bash
$ echo "1"  # AC1
$ echo "2"  # AC2
$ echo "3"  # AC3
```
"""
        assert _extract_ac_numbers(body) == {"AC1", "AC2", "AC3"}


# ---------------------------------------------------------------------------
# AC2: all-asterisk-bullet + zero VC markers must FAIL, not vacuously pass
# ---------------------------------------------------------------------------


class TestLp010FailsWhenAllAsteriskBulletAndVcHasZeroAcMarkers:
    def test_lp010_fails_when_all_asterisk_bullet_and_vc_has_zero_ac_markers(self):
        """GIVEN an AC section entirely composed of "* [ ] AC<n>" bullets
        (AC1-AC15) and a Verification Commands section with zero
        `# AC<N>` references WHEN `validate_issue_body()` runs THEN LP010
        is present in `errors` (previously: 0 errors, a vacuous empty-set
        "match")."""
        ac_numbers = _extract_ac_numbers(FIXTURE_ZERO_MARKER_ASTERISK_BODY)
        vc_numbers = _extract_vc_ac_numbers(FIXTURE_ZERO_MARKER_ASTERISK_BODY)
        assert ac_numbers == {f"AC{n}" for n in range(1, 16)}
        assert vc_numbers == set()

        result = validate_issue_body(FIXTURE_ZERO_MARKER_ASTERISK_BODY)
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) > 0
        assert lp010_errors[0].expected == sorted(ac_numbers)
        assert lp010_errors[0].actual == []


# ---------------------------------------------------------------------------
# AC3: mixed hyphen/asterisk bullets
# ---------------------------------------------------------------------------


class TestLp010FailsOnMixedHyphenAsteriskBullet:
    def test_lp010_fails_on_mixed_hyphen_asterisk_bullet(self):
        """GIVEN an AC section mixing "- [ ] AC1" and "* [ ] AC2" bullets,
        where the VC section only references AC1 (genuinely missing
        AC2) WHEN `validate_issue_body()` runs THEN LP010 is present in
        `errors` -- pre-#1704 the hyphen-only regex would have dropped
        AC2 from the extracted set entirely and vacuously matched."""
        ac_numbers = _extract_ac_numbers(FIXTURE_MIXED_HYPHEN_ASTERISK_BODY)
        assert ac_numbers == {"AC1", "AC2"}

        result = validate_issue_body(FIXTURE_MIXED_HYPHEN_ASTERISK_BODY)
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) > 0
        assert lp010_errors[0].expected == ["AC1", "AC2"]
        assert lp010_errors[0].actual == ["AC1"]


# ---------------------------------------------------------------------------
# AC4: no false extraction from prose / URL / filename / inline / fenced code
# ---------------------------------------------------------------------------


class TestLp010DoesNotExtractAcNumberFromProseOrCode:
    def test_lp010_does_not_extract_ac_number_from_prose_or_code(self):
        """GIVEN an AC section where AC2-AC5 appear only in prose, a URL,
        a filename, and inline code (attached to the AC1 definition
        line), and AC9/AC10 appear as definition-shaped lines but INSIDE
        a fenced code block, WHEN `_extract_ac_numbers()` runs THEN only
        AC1 (the real definition) is extracted."""
        ac_numbers = _extract_ac_numbers(FIXTURE_PROSE_AND_CODE_NOISE_BODY)
        assert ac_numbers == {"AC1"}

        result = validate_issue_body(FIXTURE_PROSE_AND_CODE_NOISE_BODY)
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) == 0


# ---------------------------------------------------------------------------
# AC5: fixture-level C5 / LP010 agreement (zero-marker fixture)
# ---------------------------------------------------------------------------


class TestC5Lp010FixtureLevelAgreementOnZeroMarkerFixture:
    def test_c5_lp010_fixture_level_agreement_on_zero_marker_fixture(self):
        """GIVEN the same all-asterisk/zero-VC-marker fixture used in AC2
        WHEN both `check_issue_contract.py`'s C5_ac_vc_number_alignment
        and `validate_issue_body.py`'s LP010 run THEN both FAIL on this
        specific fixture (fixture-level agreement only -- this test does
        NOT assert general parity between the two checkers; C5's broad
        AC-number regex and lack of extra-VC-ref detection are tracked
        separately in #1712)."""
        c5_status, _c5_messages = check_c5_ac_vc_alignment(FIXTURE_ZERO_MARKER_ASTERISK_BODY)
        assert c5_status == "fail"

        result = validate_issue_body(FIXTURE_ZERO_MARKER_ASTERISK_BODY)
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) > 0


# ---------------------------------------------------------------------------
# AC8: Issue #1415 historical fixture, repo-pinned (no live GitHub fetch)
# ---------------------------------------------------------------------------

# Provenance metadata for the repo-pinned Issue #1415 reproduction fixture.
# This fixture reproduces the AC-bullet-format / VC-marker-absence
# characteristics documented in Issue #1704's Background section (the
# 2026-07-24 refinement-loop run that first surfaced the C5/LP010
# discrepancy for Issue #1415), NOT a verbatim byte-for-byte copy of a
# live GitHub body fetch -- the whole point of AC8 is that this fixture
# must NOT depend on a live GitHub read at test time.
ISSUE_1415_FIXTURE_SOURCE_ISSUE = 1415
ISSUE_1415_FIXTURE_SOURCE_OBSERVED_AT = "2026-07-24T00:00:00Z"
ISSUE_1415_HISTORICAL_FIXTURE_BODY = (
    "## Acceptance Criteria\n\n"
    + _asterisk_only_ac_section(15)
    + "\n\n## Verification Commands\n\n"
    "```bash\n"
    "$ echo \"historical Issue #1415 VC section had no per-AC markers\"\n"
    "```\n"
)
ISSUE_1415_FIXTURE_SOURCE_BODY_SHA256 = (
    "sha256:" + hashlib.sha256(ISSUE_1415_HISTORICAL_FIXTURE_BODY.encode("utf-8")).hexdigest()
)


class TestIssue1415HistoricalFixtureIsRepoPinned:
    def test_issue_1415_historical_fixture_is_repo_pinned(self):
        """GIVEN the repo-pinned Issue #1415 historical reproduction
        fixture (source_issue / source_observed_at / source_body_sha256
        recorded as module constants, no GitHub API call at test time)
        WHEN `validate_issue_body()` runs on it THEN LP010 is present in
        `errors` (the same false-negative class as AC2)."""
        # Provenance recorded, not fetched.
        assert ISSUE_1415_FIXTURE_SOURCE_ISSUE == 1415
        assert ISSUE_1415_FIXTURE_SOURCE_OBSERVED_AT == "2026-07-24T00:00:00Z"
        recomputed_sha256 = (
            "sha256:"
            + hashlib.sha256(ISSUE_1415_HISTORICAL_FIXTURE_BODY.encode("utf-8")).hexdigest()
        )
        assert recomputed_sha256 == ISSUE_1415_FIXTURE_SOURCE_BODY_SHA256

        result = validate_issue_body(ISSUE_1415_HISTORICAL_FIXTURE_BODY)
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) > 0

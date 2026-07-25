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
  - AC8: the Issue #1415 minimal-reproduction fixture is pinned in-repo
    (no GitHub live-body fetch), with `fixture_scope: minimal_reproduction`
    / `source_issue` / `fixture_sha256` (hand-computed literal, not a
    self-recomputation) recorded (PR #1717 review B4).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

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
# AC8: Issue #1415 minimal-reproduction fixture, repo-pinned (no live GitHub fetch)
# ---------------------------------------------------------------------------

# Fixture-scope metadata (PR #1717 review B4). This fixture is a MINIMAL
# REPRODUCTION of the AC-bullet-format / VC-marker-absence characteristics
# documented in Issue #1704's Background section (the 2026-07-24
# refinement-loop run that first surfaced the C5/LP010 discrepancy for
# Issue #1415) -- it is NOT a verbatim byte-for-byte copy of a live
# GitHub body fetch, so it must never be named/labelled as a "historical
# snapshot" or a "source body sha256" (that would falsely imply
# provenance it does not have). `fixture_sha256` below is a hand-computed
# literal constant (not recomputed at test time from the fixture body),
# so an accidental future edit to the fixture body is caught by this
# constant going stale rather than silently self-validating.
fixture_scope = "minimal_reproduction"
source_issue = 1415
ISSUE_1415_MINIMAL_REPRODUCTION_BODY = (
    "## Acceptance Criteria\n\n"
    + _asterisk_only_ac_section(15)
    + "\n\n## Verification Commands\n\n"
    "```bash\n"
    "$ echo \"historical Issue #1415 VC section had no per-AC markers\"\n"
    "```\n"
)
# Hand-computed literal constant -- do NOT replace with a runtime
# hashlib.sha256(...) recomputation from ISSUE_1415_MINIMAL_REPRODUCTION_BODY
# (that would defeat the whole point of pinning: it would always match
# itself and could never catch a fixture-body edit).
fixture_sha256 = (
    "sha256:b8aca9e9f28ac7ffa53f06025eb42f32d7bc7c9ee2c01801aeb60ba75cf187fa"
)


class TestIssue1415HistoricalFixtureIsRepoPinned:
    def test_issue_1415_historical_fixture_is_repo_pinned(self):
        """GIVEN the repo-pinned Issue #1415 minimal-reproduction fixture
        (`fixture_scope: minimal_reproduction` / `source_issue` /
        `fixture_sha256` recorded as module constants -- NOT the
        `historical_snapshot` / `source_body_sha256` naming this test
        previously used, since the fixture is a minimal reproduction of
        Issue #1415's characteristics rather than a byte-for-byte copy of
        a live GitHub body fetch; PR #1717 review B4 -- no GitHub API
        call at test time, and `fixture_sha256` a hand-computed literal
        -- NOT recomputed from the fixture body) WHEN
        `validate_issue_body()` runs on it THEN LP010 is present in
        `errors` (the same false-negative class as AC2). Test class/
        method name is intentionally unchanged from the original so
        Issue #1704's `-k test_issue_1415_historical_fixture_is_repo_pinned`
        Verification Command AC8 still resolves; only the internal field
        naming was corrected (B4)."""
        assert fixture_scope == "minimal_reproduction"
        assert source_issue == 1415
        assert (
            hashlib.sha256(
                ISSUE_1415_MINIMAL_REPRODUCTION_BODY.encode("utf-8")
            ).hexdigest()
            == fixture_sha256.removeprefix("sha256:")
        )

        result = validate_issue_body(ISSUE_1415_MINIMAL_REPRODUCTION_BODY)
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) > 0



# ---------------------------------------------------------------------------
# PR #1717 review required_tests: bullet x checkbox-state parameterized
# matrix (positive) -- B1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bullet", ["-", "*", "+"])
@pytest.mark.parametrize("checkbox", ["[ ]", "[x]", "[X]"])
@pytest.mark.parametrize("spaces", [1, 2, 3, 4])
class TestLp010AcDefinitionMatrixPositive:
    def test_bullet_checkbox_spacing_combination_is_extracted(
        self, bullet: str, checkbox: str, spaces: int
    ):
        """GIVEN a single AC definition line built from every combination
        of bullet marker ("-"/"*"/"+") x checkbox state ("[ ]"/"[x]"/"[X]")
        x 1-4 spaces after the marker WHEN `_extract_ac_numbers()` runs
        THEN AC1 is extracted (PR #1717 required_tests matrix)."""
        line = f"{bullet}{' ' * spaces}{checkbox} AC1: item"
        body = f"## Acceptance Criteria\n\n{line}\n\n## Verification Commands\n\n```bash\n$ echo 1  # AC1\n```\n"
        assert _extract_ac_numbers(body) == {"AC1"}


# ---------------------------------------------------------------------------
# PR #1717 review required_tests: invalid checkbox state negative matrix
# (non-extraction) -- B1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "- [] AC1: empty brackets",
        "- [foo] AC1: word content",
        "- [xx] AC1: doubled x",
        "- [✓] AC1: checkmark glyph",
        "- [ ]AC1: no space after checkbox",
    ],
    ids=[
        "empty_brackets",
        "word_content",
        "doubled_x",
        "checkmark_glyph",
        "no_space_after_checkbox",
    ],
)
class TestLp010AcDefinitionMatrixNegativeInvalidCheckboxState:
    def test_invalid_checkbox_state_line_is_not_extracted(self, line: str):
        """GIVEN an AC-definition-shaped line whose checkbox content is
        NOT a GFM-valid "[ ]"/"[x]"/"[X]" state (empty brackets, arbitrary
        word content, doubled "x", a checkmark glyph) or that omits the
        required space after the checkbox, WHEN `_extract_ac_numbers()`
        runs THEN AC1 is NOT extracted (PR #1717 review B1 required_tests
        negative matrix)."""
        body = f"## Acceptance Criteria\n\n{line}\n\n## Verification Commands\n\n```bash\n$ echo 1\n```\n"
        assert _extract_ac_numbers(body) == set()


# ---------------------------------------------------------------------------
# PR #1717 review required_tests: fence-grammar matrix (GFM SSOT
# delegation) -- B2
# ---------------------------------------------------------------------------


class TestLp010AcExtractionFenceGrammarMatrix:
    def test_tilde_fence_hides_ac_shaped_lines(self):
        """GIVEN AC-shaped lines inside a tilde (~~~) fence WHEN
        `_extract_ac_numbers()` runs THEN they are not extracted."""
        body = (
            "## Acceptance Criteria\n\n"
            "- [ ] AC1: real definition\n\n"
            "~~~\n"
            "- [ ] AC9: inside tilde fence\n"
            "~~~\n\n"
            "## Verification Commands\n\n```bash\n$ echo 1  # AC1\n```\n"
        )
        assert _extract_ac_numbers(body) == {"AC1"}

    def test_four_backtick_fence_with_inner_three_backtick_line(self):
        """GIVEN a 4-backtick fence containing a literal 3-backtick line
        (which would otherwise look like a closing fence to a naive
        toggle scanner) WHEN `_extract_ac_numbers()` runs THEN the
        AC-shaped line inside stays hidden and the real AC1 definition
        after the fence is still extracted."""
        body = (
            "## Acceptance Criteria\n\n"
            "````\n"
            "- [ ] AC9: inside 4-backtick fence\n"
            "```\n"
            "- [ ] AC10: still inside (inner 3-backtick did not close it)\n"
            "````\n\n"
            "- [ ] AC1: real definition after the fence\n\n"
            "## Verification Commands\n\n```bash\n$ echo 1  # AC1\n```\n"
        )
        assert _extract_ac_numbers(body) == {"AC1"}

    def test_short_closer_does_not_close_fence(self):
        """GIVEN a 4-backtick opening fence closed by only 3 backticks
        (too short per GFM: closer must be >= opener length) WHEN
        `_extract_ac_numbers()` runs THEN the AC-shaped line remains
        hidden inside the (still-open) fence, and content after the
        would-be-short-closer is also treated as fenced."""
        body = (
            "## Acceptance Criteria\n\n"
            "````\n"
            "- [ ] AC9: inside fence\n"
            "```\n"
            "- [ ] AC1: this looks real but the fence never actually closed\n"
            "````\n\n"
            "## Verification Commands\n\n```bash\n$ echo 1\n```\n"
        )
        assert _extract_ac_numbers(body) == set()

    def test_fence_char_type_mismatch_does_not_close(self):
        """GIVEN a backtick-opened fence that a tilde line attempts to
        close (different fence character type) WHEN
        `_extract_ac_numbers()` runs THEN the tilde line does not close
        the fence and the AC-shaped content stays hidden."""
        body = (
            "## Acceptance Criteria\n\n"
            "```\n"
            "- [ ] AC9: inside fence\n"
            "~~~\n"
            "- [ ] AC10: char-type mismatch did not close the backtick fence\n"
            "```\n\n"
            "- [ ] AC1: real definition after the fence\n\n"
            "## Verification Commands\n\n```bash\n$ echo 1  # AC1\n```\n"
        )
        assert _extract_ac_numbers(body) == {"AC1"}

    def test_unclosed_fence_hides_ac_shaped_lines_to_eof(self):
        """GIVEN an opening fence that is never closed before EOF WHEN
        `_extract_ac_numbers()` runs THEN the AC-shaped line inside
        remains hidden (unclosed fence is treated as code to EOF, not
        prose)."""
        body = (
            "## Acceptance Criteria\n\n"
            "- [ ] AC1: real definition\n\n"
            "```bash\n"
            "- [ ] AC9: unclosed fence, runs to end of section\n"
        )
        assert _extract_ac_numbers(body) == {"AC1"}

    def test_trailing_text_after_closer_is_not_a_valid_close(self):
        """GIVEN a closing-fence-shaped line with trailing non-space text
        (e.g. "``` extra") WHEN `_extract_ac_numbers()` runs THEN that
        line does NOT close the fence (GFM: closer must have no trailing
        content), so the AC-shaped content remains hidden."""
        body = (
            "## Acceptance Criteria\n\n"
            "```\n"
            "- [ ] AC9: inside fence\n"
            "``` extra trailing text\n"
            "- [ ] AC10: still inside, fake closer had trailing text\n"
            "```\n\n"
            "- [ ] AC1: real definition after the fence\n\n"
            "## Verification Commands\n\n```bash\n$ echo 1  # AC1\n```\n"
        )
        assert _extract_ac_numbers(body) == {"AC1"}

    def test_four_space_indented_code_block_is_not_extracted(self):
        """GIVEN an AC-shaped line indented by 4 spaces (GFM indented code
        block convention) WHEN `_extract_ac_numbers()` runs THEN it is
        NOT extracted -- `_AC_DEFINITION_LINE_RE`'s `{0,3}` leading-space
        anchor (GFM list-marker indent) excludes it regardless of how
        `iter_markdown_blocks()` classifies the surrounding block."""
        body = (
            "## Acceptance Criteria\n\n"
            "- [ ] AC1: real definition\n\n"
            "    - [ ] AC9: four-space indented, not a real definition\n\n"
            "## Verification Commands\n\n```bash\n$ echo 1  # AC1\n```\n"
        )
        assert _extract_ac_numbers(body) == {"AC1"}


# ---------------------------------------------------------------------------
# PR #1717 review required_tests: ASCII-only AC identifier (full-width
# digit non-extraction) -- B1
# ---------------------------------------------------------------------------


class TestLp010AcNumberAsciiOnly:
    def test_full_width_digit_ac_number_is_not_extracted(self):
        """GIVEN an AC definition line whose number uses full-width
        (Unicode Nd, not ASCII) digits ("AC１２") WHEN
        `_extract_ac_numbers()` runs THEN it is NOT extracted --
        `_AC_DEFINITION_LINE_RE` uses `[0-9]+`, not `\\d+`, so
        non-ASCII decimal digit forms never match (PR #1717 review B1)."""
        body = (
            "## Acceptance Criteria\n\n"
            "- [ ] AC１２: full-width digits, not ASCII\n\n"
            "## Verification Commands\n\n```bash\n$ echo 1\n```\n"
        )
        assert _extract_ac_numbers(body) == set()


# ---------------------------------------------------------------------------
# PR #1717 review required_tests: empty-set guard negative (both AC and VC
# genuinely empty must not be reported as a mismatch) -- B3
# ---------------------------------------------------------------------------


class TestLp010EmptySetGuardNegative:
    def test_table_row_shaped_line_does_not_trigger_vacuous_mismatch(self):
        """GIVEN an Acceptance Criteria section that contains ONLY a
        Markdown table row shaped like a checkbox ("| [ ] | unchecked |")
        and no real AC-definition bullet, and a Verification Commands
        section with zero `# AC<N>` references, WHEN
        `validate_issue_body()` runs THEN no LP010 error is reported --
        `ac_numbers == vc_numbers == set()` is a legitimate empty/empty
        match, not a vacuous one (PR #1717 review B3; the previous
        `_has_checkbox_shaped_lines()` guard mis-matched this table row
        as checkbox-shaped and produced a contradictory
        expected=[]/actual=[] mismatch)."""
        body = (
            "## Acceptance Criteria\n\n"
            "| status | description |\n"
            "| --- | --- |\n"
            "| [ ] | unchecked |\n\n"
            "## Verification Commands\n\n```bash\n$ echo \"no AC markers\"\n```\n"
        )
        ac_numbers = _extract_ac_numbers(body)
        vc_numbers = _extract_vc_ac_numbers(body)
        assert ac_numbers == set()
        assert vc_numbers == set()

        result = validate_issue_body(body)
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) == 0

"""Instruction-wiring regression tests for enumerated/exhaustive claim evidence
coverage (Issue #2765).

Background: PR #2763's source-review head `ce73605b6a442f6b88eafe30a3feda4bf62ac3dc`
was reviewed while an aggregate `TestRedaction -q -> 6 passed` count was treated as
if it directly proved all four token-format variants required by Issue #2728's AC3,
when in fact three of the four variants were not covered by any executable
assertion. PR #2763 was subsequently fixed (commit
`9d6d6b7a932b752059a016f0a301f2ccfcd72336`) and merged
(`c7dc65a7510afb172dc6fab19cc914c279cf25c7`); the *current* state of PR #2763 is not
a failure reproducer.

These tests are static text/contract-wiring checks against the repository's own
tracked skill files. They do NOT automate the LLM's semantic judgment during an
actual PR review -- they only guard against regression of the instruction wiring
that a live review depends on:

- `pr-review-judge/SKILL.md` reaches the new evidence-coverage Procedure step,
  in declared order, before the verdict-decision step, and instructs the
  literal `PR_REVIEW_JUDGE_EVIDENCE_COVERAGE_RULE_APPLIED` marker to be emitted
  when that step is actually reached (AC7 runtime-verification precondition).
- `references/ac-evidence-checks.md` declares the finite/exhaustive-claim
  trigger condition (excluding illustrative examples), the
  case-identity-AND-relevant-assertion-AND-PASS rule, the skip/xfail
  restriction, the no-new-heavyweight-gate boundary, the #2757 responsibility
  split, and the pinned historical regression SHAs (AC1, AC2, AC4, AC5, AC6).
- `references/safety-claim-gate.md` declares claim-to-evidence coverage that
  only applies the enumerated-test rule to test evidence, without excluding
  legitimate non-test evidence kinds (AC3).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "SKILL.md"
AC_EVIDENCE_CHECKS_PATH = (
    REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "references" / "ac-evidence-checks.md"
)
SAFETY_CLAIM_GATE_PATH = (
    REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "references" / "safety-claim-gate.md"
)

MARKER = "PR_REVIEW_JUDGE_EVIDENCE_COVERAGE_RULE_APPLIED"

PRE_FIX_HEAD_SHA = "ce73605b6a442f6b88eafe30a3feda4bf62ac3dc"
FIX_COMMIT_SHA = "9d6d6b7a932b752059a016f0a301f2ccfcd72336"
FINAL_MERGE_HEAD_SHA = "c7dc65a7510afb172dc6fab19cc914c279cf25c7"


def _read(path: Path) -> str:
    assert path.exists(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


def _slice_section(text: str, heading: str) -> str:
    """Slice out the body of a single Markdown section, from ``heading`` up to
    (but excluding) the next level-2 (``## ``) heading, or end-of-text if there
    is no following level-2 heading.

    Deliberately a plain ``str.index``-based slice, not a generic Markdown
    parser or AST analyzer -- this repo's convention is to keep contract
    checks as simple literal text assertions (Issue #2765 fix_delta).
    """
    start = text.index(heading)
    next_heading_index = text.find("\n## ", start + len(heading))
    if next_heading_index == -1:
        return text[start:]
    return text[start:next_heading_index]


# ---------------------------------------------------------------------------
# SKILL.md: Procedure wiring + ordered runtime marker (AC7 precondition)
# ---------------------------------------------------------------------------


def test_skill_md_references_evidence_coverage_step():
    text = _read(SKILL_PATH)
    assert "Enumerated / Exhaustive Claim Evidence Coverage" in text
    assert "ac-evidence-checks.md" in text
    assert "safety-claim-gate.md" in text
    assert "case identity" in text


def test_skill_md_declares_runtime_marker_at_evidence_coverage_step():
    text = _read(SKILL_PATH)
    assert MARKER in text, (
        f"SKILL.md must instruct emission of the literal marker {MARKER!r} when the "
        "evidence-coverage Procedure step is reached (Issue #2765 AC7)."
    )
    # The marker instruction must live strictly *inside* the "### 4.6b)" step --
    # not merely "before ### 5) verdict" (that older bound would also pass if the
    # marker were moved into an unrelated later step such as "### 4.7) Clean-Room
    # Review", which sits between 4.6b and 5). Bound it tightly to
    # 4.6b_heading < marker < 4.7_heading < verdict_heading (PR #2774 review
    # finding, fix_delta P1-2).
    marker_index = text.index(MARKER)
    coverage_heading_index = text.index("### 4.6b)")
    next_step_heading_index = text.index("### 4.7)", coverage_heading_index)
    verdict_step_index = text.index("### 5) verdict", next_step_heading_index)
    assert coverage_heading_index < marker_index < next_step_heading_index, (
        "marker instruction must sit strictly within the '### 4.6b)' evidence-coverage "
        "step, before the next Procedure step ('### 4.7)') begins -- moving the "
        "marker into a later step (e.g. Clean-Room Review) must fail this test"
    )
    assert next_step_heading_index < verdict_step_index, (
        "'### 4.7)' must still precede '### 5) verdict' in declared order"
    )


def test_skill_md_evidence_coverage_step_precedes_verdict_decision():
    """Regression guard for Procedure declared-order (AC7's
    procedure_steps_executed_in_declared_order assumption)."""
    text = _read(SKILL_PATH)
    coverage_index = text.index("Enumerated / Exhaustive Claim Evidence Coverage")
    verdict_index = text.index("### 5) verdict")
    output_contract_index = text.index("## Output Contract")
    assert coverage_index < verdict_index < output_contract_index


def test_skill_md_blocker_rule_is_unconditional_on_ci_status():
    text = _read(SKILL_PATH)
    assert "current-head CI が green でも" in text


# ---------------------------------------------------------------------------
# ac-evidence-checks.md: finite/exhaustive trigger + case-level evidence rule
# ---------------------------------------------------------------------------


def test_ac_evidence_checks_declares_finite_exhaustive_trigger_excluding_examples():
    text = _read(AC_EVIDENCE_CHECKS_PATH)
    assert re.search(r"全称|exhaustive|有限.*列挙|finite.*enumerat", text)
    # illustrative-example exclusion must be explicit, not merely absent
    assert "例示列挙" in text
    assert "対象外" in text


def test_ac_evidence_checks_rejects_aggregate_pass_as_sole_evidence():
    text = _read(AC_EVIDENCE_CHECKS_PATH)
    assert "aggregate" in text
    assert "coverage evidence として受理しない" in text


def test_ac_evidence_checks_requires_case_identity_and_relevant_assertion_and_pass():
    """Pin the actual AND-semantics and blocker wiring of the enumerated-claim
    rule, not merely the presence of three unrelated keywords anywhere in the
    file (PR #2774 review finding, fix_delta P1-1).

    A prior version of this test only asserted that "case identity",
    "relevant executable assertion", and "coverage の証明にならない" each
    occurred *somewhere* in the file. That would keep passing even if a future
    edit silently weakened the rule from AND to OR, dropped the PASS
    requirement, or dropped the REQUEST_CHANGES blocker consequence -- exactly
    the kind of semantic regression Issue #2765 exists to prevent. This
    version scopes all assertions to the actual
    "## Enumerated / Exhaustive Claim Evidence Coverage" section body and pins
    the specific phrases that carry the AND / PASS / blocker semantics.
    """
    text = _read(AC_EVIDENCE_CHECKS_PATH)
    section = _slice_section(
        text, "## Enumerated / Exhaustive Claim Evidence Coverage"
    )

    # (a) The two conditions are combined with AND ("両方（AND）"), not OR.
    # If a future edit downgrades this to "OR" (or drops the AND phrase
    # entirely), this exact phrase disappears and the assertion fails.
    assert "両方（AND）" in section, (
        "the two per-case conditions (case identity, relevant executable "
        "assertion PASS) must be explicitly combined with AND, not OR"
    )
    # Guard against a literal OR-downgraded rewrite of the connecting clause.
    assert "case identity OR relevant executable assertion" not in section
    assert re.search(r"case identity\b[^\n]{0,40}\bOR\b[^\n]{0,60}relevant executable assertion", section) is None

    # (b) "relevant executable assertion" must explicitly require PASS -- not
    # merely be *mentioned* -- so dropping the PASS requirement text breaks
    # this assertion even though the bare phrase "relevant executable
    # assertion" might still appear elsewhere.
    assert "relevant executable assertion が PASS" in section, (
        "the relevant executable assertion condition must explicitly require "
        "PASS, not merely existence/mention of the assertion"
    )

    # named node / parameter ID alone must be explicitly insufficient
    assert "coverage の証明にならない" in section

    # (c) Missing either condition for even one enumerated case must be an
    # unconditional REQUEST_CHANGES blocker (independent of current-head CI
    # status). Pin the specific sentence that ties "missing a case" to the
    # blocker verdict so deleting this consequence breaks the test.
    assert re.search(
        r"いずれか一方でも欠落するケースが1つでもあれば[^\n]*REQUEST_CHANGES[^\n]*blocker",
        section,
    ), (
        "missing case identity or relevant-assertion-PASS for any single "
        "enumerated case must be documented as an unconditional "
        "REQUEST_CHANGES blocker"
    )


def test_ac_evidence_checks_restricts_skip_and_xfail_as_direct_evidence():
    text = _read(AC_EVIDENCE_CHECKS_PATH)
    assert "skip" in text
    assert "xfail" in text
    assert "明示的に許可" in text


def test_ac_evidence_checks_pins_historical_regression_shas_and_disclaims_current_pr():
    text = _read(AC_EVIDENCE_CHECKS_PATH)
    assert PRE_FIX_HEAD_SHA in text
    assert FIX_COMMIT_SHA in text
    assert FINAL_MERGE_HEAD_SHA in text
    assert "現在の PR #2763 はこの failure の reproducer ではない" in text


def test_ac_evidence_checks_does_not_introduce_new_heavyweight_gate():
    text = _read(AC_EVIDENCE_CHECKS_PATH)
    assert "新しい" in text
    assert "persistent database" in text


def test_ac_evidence_checks_separates_responsibility_from_2757():
    text = _read(AC_EVIDENCE_CHECKS_PATH)
    assert "2757" in text
    assert "責務" in text


# ---------------------------------------------------------------------------
# safety-claim-gate.md: claim-to-evidence coverage (not test-only)
# ---------------------------------------------------------------------------


def test_safety_claim_gate_declares_claim_to_evidence_coverage():
    text = _read(SAFETY_CLAIM_GATE_PATH)
    assert "claim-to-evidence" in text


def test_safety_claim_gate_applies_enumerated_test_rule_only_to_test_evidence():
    text = _read(SAFETY_CLAIM_GATE_PATH)
    assert "Evidence が **test** の場合のみ" in text
    assert "ac-evidence-checks.md" in text


def test_safety_claim_gate_does_not_exclude_legitimate_non_test_evidence():
    text = _read(SAFETY_CLAIM_GATE_PATH)
    for evidence_kind in (
        "source/configuration inspection",
        "policy/config diff",
        "permission declaration",
        "runtime artifact",
        "CI/CheckRun",
        "deterministic validator output",
    ):
        assert evidence_kind in text, f"missing legitimate non-test evidence kind: {evidence_kind}"
    assert "不当に排除しない" in text

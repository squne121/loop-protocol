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
    # The marker instruction must live in the same paragraph as the coverage rule
    # itself, not in an unrelated section (guards against a regression where the
    # marker is moved somewhere disconnected from the actual rule application).
    marker_index = text.index(MARKER)
    coverage_heading_index = text.index("Enumerated / Exhaustive Claim Evidence Coverage")
    verdict_step_index = text.index("### 5) verdict")
    assert coverage_heading_index < marker_index < verdict_step_index, (
        "marker instruction must sit within the evidence-coverage step and precede "
        "the verdict-decision step in Procedure declared order"
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
    text = _read(AC_EVIDENCE_CHECKS_PATH)
    assert "case identity" in text
    assert "relevant executable assertion" in text
    # named node / parameter ID alone must be explicitly insufficient
    assert "coverage の証明にならない" in text


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

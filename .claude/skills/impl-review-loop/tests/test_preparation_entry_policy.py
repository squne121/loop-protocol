"""
test_preparation_entry_policy.py

AC9: SKILL.md and steps/preparation.md must not contain conflicting routing
rules for stale prior contract review handling (Issue #2272).
"""

from __future__ import annotations

from pathlib import Path

_STEPS_DIR = Path(__file__).resolve().parent.parent / "steps"
_SKILL_MD = Path(__file__).resolve().parent.parent / "SKILL.md"


def test_ac9_no_conflicting_routing_rules():
    preparation_text = (_STEPS_DIR / "preparation.md").read_text(encoding="utf-8")
    skill_text = _SKILL_MD.read_text(encoding="utf-8")

    # Both documents must reference the single unified entry contract by
    # name so a reader can find the SSOT instead of two independent, silent
    # rule sets.
    assert "ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1" in preparation_text
    assert "ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1" in skill_text

    # The stale_contract_review subsection must carry the explicit
    # precedence/supersession annotation reconciling it with the
    # Root-Owned Entry Transition route (no longer an unqualified terminal
    # stop for every invocation path).
    assert "Root-Owned Entry Transition との関係" in preparation_text
    assert "重ねて `intake_gate_failed` の terminal stop として再適用しない" in preparation_text

    # The reconciling annotation must appear strictly after the
    # stale_contract_review subreason heading (i.e. it qualifies that exact
    # rule, not some unrelated section).
    stale_heading = "#### 2. `stale_contract_review`（陳腐化した契約レビュー）"
    reconciliation_marker = "Root-Owned Entry Transition との関係"
    assert stale_heading in preparation_text
    assert preparation_text.index(reconciliation_marker) > preparation_text.index(
        stale_heading
    )

    # preparation.md must document precedence of the Root-Owned Synchronous
    # Entry Transition section over the legacy Intake Gate for stale review
    # handling.
    precedence_marker = "Root-Owned Synchronous Entry Transition"
    assert precedence_marker in preparation_text
    assert preparation_text.index(precedence_marker) < preparation_text.index(
        stale_heading
    )

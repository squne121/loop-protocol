"""
test_reviewer_gate_required_evidence_v2.py

Issue #1856 (AC6): decision-matrix.md の reviewer_gate.required_evidence から
TEST_VERDICT_MACHINE が必須項目として外れ、CI_CHECK_RUN_SCOPED のみが必須で
あることを構造的に検証する。

current 状態（cutover 前）は required_evidence に TEST_VERDICT_MACHINE を
含むため、単純な `rg TEST_VERDICT_MACHINE` の有無チェックでは極性が反転する
（advisory コメントとして残る TEST_VERDICT_MACHINE の言及にヒットしてしまう）。
本テストは `reviewer_gate:` ブロックを抽出し、その中の required_evidence
リスト（コメントアウトされていない有効な YAML list item のみ）を構造的に
解析して判定する。
"""

from __future__ import annotations

import pathlib
import re

_DECISION_MATRIX_MD = (
    pathlib.Path(__file__).parents[2] / "references" / "decision-matrix.md"
)


def _reviewer_gate_block() -> str:
    text = _DECISION_MATRIX_MD.read_text(encoding="utf-8")
    assert _DECISION_MATRIX_MD.is_file(), (
        f"decision-matrix.md not found at {_DECISION_MATRIX_MD}"
    )
    match = re.search(
        r"  reviewer_gate:\n(.*?)\n(?=  \w|```)",
        text,
        re.DOTALL,
    )
    assert match is not None, "reviewer_gate: block not found in decision-matrix.md"
    return match.group(1)


def _active_required_evidence_items(block: str) -> list[str]:
    """Return required_evidence list items that are NOT commented out.

    A line is considered an active list item when it matches
    `      - <name>` (two-space-indented dash) without a leading '#'.
    Lines like `      # - TEST_VERDICT_MACHINE` are advisory-only comments
    and are excluded.
    """
    req_match = re.search(
        r"    required_evidence:\n(.*?)\n(?=    \w)",
        block,
        re.DOTALL,
    )
    assert req_match is not None, "required_evidence: sub-block not found"
    req_block = req_match.group(1)

    items = []
    for line in req_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            item = stripped[2:].split("#", 1)[0].strip()
            items.append(item)
    return items


def test_required_evidence_does_not_include_test_verdict_machine():
    block = _reviewer_gate_block()
    active_items = _active_required_evidence_items(block)
    assert "TEST_VERDICT_MACHINE" not in active_items, (
        f"TEST_VERDICT_MACHINE must not be an active required_evidence item "
        f"(Issue #1856 AC6); got active_items={active_items}"
    )


def test_required_evidence_includes_ci_check_run_scoped():
    block = _reviewer_gate_block()
    active_items = _active_required_evidence_items(block)
    assert "CI_CHECK_RUN_SCOPED" in active_items, (
        f"CI_CHECK_RUN_SCOPED must remain an active required_evidence item; "
        f"got active_items={active_items}"
    )


def test_test_verdict_machine_still_mentioned_as_advisory_only():
    """TEST_VERDICT_MACHINE reference must survive as a commented-out advisory note."""
    block = _reviewer_gate_block()
    assert "TEST_VERDICT_MACHINE" in block
    assert "advisory" in block

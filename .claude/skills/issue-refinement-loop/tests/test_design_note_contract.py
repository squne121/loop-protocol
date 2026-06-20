"""
Tests for drift safety of issue-refinement-loop derived design note.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

import pytest
import yaml


def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists():
            return parent
    # Fallback for non-git invocation contexts
    return current.parents[7]


REPO_ROOT = _repo_root()
DESIGN_NOTE = REPO_ROOT / "docs/dev/workflows/issue-refinement-loop-design.md"
SKILL_MD = REPO_ROOT / ".claude/skills/issue-refinement-loop/SKILL.md"
LOOP_STATE_REFERENCE = REPO_ROOT / ".claude/skills/issue-refinement-loop/references/loop-state.md"

REQUIRED_CLAIMS = {
    "max_iterations_default": "max_iterations_default",
    "iteration_limit_termination": "iteration_limit_termination",
    "review_result_contract": "review_result_contract",
    "loop_state_reference": "loop_state_reference",
}


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_claims(design_note_text: str) -> Dict[str, object]:
    match = re.search(
        r"## DERIVED_RUNTIME_CLAIMS_V1\n\s*```yaml\n(.*?)```",
        design_note_text,
        re.DOTALL,
    )
    assert match, "DERIVED_RUNTIME_CLAIMS_V1 block is missing"
    claims_yaml = match.group(1).strip()
    claims_doc = yaml.safe_load(claims_yaml)
    assert isinstance(claims_doc, dict), "claims yaml must be a mapping"
    assert "claims" in claims_doc, "claims key is required"
    claims = claims_doc["claims"]
    assert isinstance(claims, dict), "claims must be a map"
    return claims


def _extract_default_max_iterations(skill_text: str) -> int:
    match = re.search(r"default_max_iterations:\s*(\d+)", skill_text)
    assert match, "default_max_iterations is not found in issue-refinement-loop SKILL.md"
    return int(match.group(1))


def _find_copy_window(
    canonical_text: str,
    note_text: str,
    min_lines: int = 12,
    min_chars: int = 600,
):
    def normalize_for_copy(src: str):
        lines = []
        for line in src.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r"^#{1,6}\s+", stripped):
                continue
            if re.fullmatch(r"[\-`*#>\s]+", stripped):
                continue
            if len(stripped) <= 18:
                continue
            if re.fullmatch(r"[./][\w._/\-]+", stripped):
                continue
            lines.append(stripped)
        return lines

    can_lines = normalize_for_copy(canonical_text)
    note_norm = "\n".join(normalize_for_copy(note_text))
    if len(note_norm) < min_chars:
        return None

    for idx in range(0, max(0, len(can_lines) - min_lines + 1)):
        window = can_lines[idx : idx + min_lines]
        if not window:
            continue
        window_text = "\n".join(window)
        if len(window_text) < min_chars:
            continue
        if window_text in note_norm:
            return window_text
    return None


def test_design_note_frontmatter_declares_derived_note_contract():
    design_note = _read_file(DESIGN_NOTE)
    claims = _extract_claims(design_note)

    for key in REQUIRED_CLAIMS:
        assert key in claims, f"missing claim: {key}"
        assert isinstance(claims[key], dict), f"claim {key} must be a mapping"
        assert "canonical_source" in claims[key], f"claim {key} must include canonical_source"
        assert "canonical_selector" in claims[key], f"claim {key} must include canonical_selector"
        assert "expected_value" in claims[key], f"claim {key} must include expected_value"


def test_design_note_max_iterations_matches_skill_loop_policy():
    skill_text = _read_file(SKILL_MD)
    design_note = _read_file(DESIGN_NOTE)
    claims = _extract_claims(design_note)
    skill_default = _extract_default_max_iterations(skill_text)

    assert claims["max_iterations_default"]["expected_value"] == skill_default


def test_design_note_iteration_limit_termination_matches_skill_policy():
    skill_text = _read_file(SKILL_MD)
    design_note = _read_file(DESIGN_NOTE)
    claims = _extract_claims(design_note)

    expected = claims["iteration_limit_termination"]["expected_value"]
    normalized_expected = " ".join(expected.split())
    assert normalized_expected == "iteration + 1 >= max_iterations -> human_escalation"

    normalized_skill = " ".join(skill_text.split())
    assert "needs-fix" in normalized_skill
    assert re.search(r"iteration\s*\+\s*1\s*>=\s*max_iterations", normalized_skill)
    assert "human_escalation" in normalized_skill


def test_design_note_review_result_contract_matches_skill_compact_contract():
    skill_text = _read_file(SKILL_MD)
    design_note = _read_file(DESIGN_NOTE)
    claims = _extract_claims(design_note)

    assert claims["review_result_contract"]["expected_value"] == "ISSUE_REVIEW_RESULT_COMPACT_V1"
    assert "ISSUE_REVIEW_RESULT_COMPACT_V1" in skill_text


def test_design_note_not_loaded_during_runtime():
    note = _read_file(DESIGN_NOTE)
    assert "normal loop execution (runtime)" in note
    assert "routine issue refinement" in note
    assert "any SubAgent execution within the loop itself" in note


def test_design_note_loop_state_reference_exists():
    note = _read_file(DESIGN_NOTE)
    claim = _extract_claims(note)["loop_state_reference"]
    expected = claim["expected_value"]
    assert expected in (".claude/skills/issue-refinement-loop/references/loop-state.md", "references/loop-state.md")

    loop_state_doc = _read_file(LOOP_STATE_REFERENCE)
    assert "LOOP_STATE" in loop_state_doc


def test_design_note_rejects_long_copied_blocks_from_canonical_sources():
    note = _read_file(DESIGN_NOTE)
    skill_text = _read_file(SKILL_MD)

    assert _find_copy_window(skill_text, note) is None

    copied = []
    for line in skill_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^#{1,6}\s+", stripped):
            continue
        if len(stripped) <= 18:
            continue
        copied.append(stripped)

    assert len(copied) >= 15, "not enough canonical lines available for negative fixture"
    negative_block = "\n".join(copied[:15])
    bad_note = (
        note + "\n\n" + "# negative fixture\n\n" + positive_negative_payload(negative_block)
    )
    # negative fixture must fail long-copy detection
    assert _find_copy_window(skill_text, bad_note) is not None


def positive_negative_payload(block: str) -> str:
    # ensure long copied text appears exactly and clearly exceeds block threshold
    if len(block) < 600:
        block = (block + "\n") * 10
    return block


def test_negative_fixture_with_default_1_fails():
    design_note = _read_file(DESIGN_NOTE)
    bad_note = design_note.replace(
        "expected_value: 3",
        "expected_value: 1",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_claims_consistency(bad_note)


def test_negative_fixture_with_needs_second_pass_fails():
    design_note = _read_file(DESIGN_NOTE)
    bad_note = design_note.replace(
        "iteration + 1 >= max_iterations -> human_escalation",
        "iteration + 1 >= max_iterations -> needs_second_pass",
    )
    with pytest.raises(AssertionError):
        _assert_claims_consistency(bad_note)


def _assert_claims_consistency(design_note_text: str) -> None:
    skill_text = _read_file(SKILL_MD)
    claims = _extract_claims(design_note_text)
    skill_default = _extract_default_max_iterations(skill_text)

    assert claims["max_iterations_default"]["expected_value"] == skill_default
    normalized_expected = " ".join(claims["iteration_limit_termination"]["expected_value"].split())
    assert normalized_expected == "iteration + 1 >= max_iterations -> human_escalation"
    normalized_skill = " ".join(skill_text.split())
    assert re.search(r"iteration\s*\+\s*1\s*>=\s*max_iterations", normalized_skill)
    assert "human_escalation" in normalized_skill
    assert (
        claims["review_result_contract"]["expected_value"]
        == "ISSUE_REVIEW_RESULT_COMPACT_V1"
    )

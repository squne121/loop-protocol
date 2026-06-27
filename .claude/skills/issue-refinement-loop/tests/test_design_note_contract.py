"""Tests for drift safety of issue-refinement-loop derived design note."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List

import pytest
import yaml


def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "docs" / "dev" / "workflows" / "issue-refinement-loop-design.md").exists():
            return parent
    raise RuntimeError("Unable to resolve repo root from test path")


REPO_ROOT = _repo_root()
DESIGN_NOTE = REPO_ROOT / "docs/dev/workflows/issue-refinement-loop-design.md"
SKILL_MD = REPO_ROOT / ".claude/skills/issue-refinement-loop/SKILL.md"
LOOP_STATE_REFERENCE = REPO_ROOT / ".claude/skills/issue-refinement-loop/references/loop-state.md"

REQUIRED_CLAIMS = {
    "max_iterations_default": "loop_policy.default_max_iterations",
    "iteration_limit_termination": "needs-fix continuation rule",
    "review_result_contract": "Step 2 result contract",
    "loop_state_reference": "LOOP_STATE",
}

_RELEVANT_COPY_MIN_LINES = 12
_RELEVANT_COPY_MIN_CHARS = 600
_COPY_SKIP_SHORT_LINE_LENGTH = 20


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_frontmatter(md_text: str) -> Dict[str, object]:
    match = re.search(r"^---\n(.*?)\n---\n", md_text, flags=re.DOTALL | re.MULTILINE)
    assert match, "design note frontmatter is missing"
    frontmatter = yaml.safe_load(match.group(1))
    assert isinstance(frontmatter, dict), "frontmatter must be a mapping"
    return frontmatter


def _extract_claims(design_note_text: str) -> Dict[str, object]:
    marker = "## DERIVED_RUNTIME_CLAIMS_V1"
    assert marker in design_note_text, "DERIVED_RUNTIME_CLAIMS_V1 block is missing"
    claim_section = design_note_text.split(marker, 1)[1]
    match = re.search(r"```[ \t]*yaml[ \t]*\n(.*?)```", claim_section, re.DOTALL)
    assert match, "DERIVED_RUNTIME_CLAIMS_V1 YAML block is missing"
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


def _normalize_lines(src: str) -> List[str]:
    lines: List[str] = []
    for raw_line in src.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^#{1,6}\s+", line):
            continue
        if re.fullmatch(r"[-`*#>\s]+", line):
            continue
        if len(line) <= _COPY_SKIP_SHORT_LINE_LENGTH:
            continue
        if re.fullmatch(r"[./][\\w._/-]+", line):
            continue
        if line.startswith("|") and line.endswith("|"):
            continue
        lines.append(line)
    return lines


def _find_long_copied_windows(canonical_text: str, note_text: str) -> List[str]:
    canonical_lines = _normalize_lines(canonical_text)
    if len(canonical_lines) < _RELEVANT_COPY_MIN_LINES:
        return []

    note_norm = "\n".join(_normalize_lines(note_text))
    if len(note_norm) < _RELEVANT_COPY_MIN_CHARS:
        return []

    windows: List[str] = []
    for idx in range(0, len(canonical_lines) - _RELEVANT_COPY_MIN_LINES + 1):
        block_lines = canonical_lines[idx : idx + _RELEVANT_COPY_MIN_LINES]
        block_text = "\n".join(block_lines)
        if len(block_text) < _RELEVANT_COPY_MIN_CHARS:
            continue
        if block_text in note_norm:
            windows.append(block_text)
    return windows


def _extract_canonical_sources(design_note_text: str) -> List[str]:
    frontmatter = _extract_frontmatter(design_note_text)
    canonical_sources = frontmatter.get("canonical_sources")
    assert isinstance(canonical_sources, list), "canonical_sources must be a list"
    assert canonical_sources, "canonical_sources must not be empty"
    return canonical_sources


def _assert_claims_consistency(design_note_text: str) -> None:
    skill_text = _read_file(SKILL_MD)
    claims = _extract_claims(design_note_text)

    assert "max_iterations_default" in claims, "max_iterations_default claim missing"
    assert "iteration_limit_termination" in claims, "iteration_limit_termination claim missing"
    assert "review_result_contract" in claims, "review_result_contract claim missing"
    assert "loop_state_reference" in claims, "loop_state_reference claim missing"

    assert claims["max_iterations_default"].get("canonical_selector") == REQUIRED_CLAIMS["max_iterations_default"]
    assert claims[(
        "iteration_limit_termination"
    )].get("canonical_selector") == REQUIRED_CLAIMS["iteration_limit_termination"]
    assert claims["review_result_contract"].get("canonical_selector") == REQUIRED_CLAIMS["review_result_contract"]
    assert claims["loop_state_reference"].get("canonical_selector") == REQUIRED_CLAIMS["loop_state_reference"]

    assert claims["max_iterations_default"]["expected_value"] == _extract_default_max_iterations(skill_text)
    assert " ".join(str(claims["iteration_limit_termination"]["expected_value"]).split()) == (
        "iteration + 1 >= max_iterations -> human_escalation"
    )
    assert (
        claims["review_result_contract"]["expected_value"]
        == "ISSUE_REVIEW_RESULT_COMPACT_V1"
    )
    assert claims["loop_state_reference"]["expected_value"].endswith(
        "references/loop-state.md"
    )
    assert "needs_second_pass" not in design_note_text
    normalized_skill = " ".join(skill_text.split())
    assert "iteration + 1 >= max_iterations" in normalized_skill
    assert "human_escalation" in normalized_skill


def test_design_note_frontmatter_declares_derived_note_contract():
    design_note = _read_file(DESIGN_NOTE)
    claims = _extract_claims(design_note)
    canonical_sources = _extract_canonical_sources(design_note)

    for key in REQUIRED_CLAIMS:
        assert key in claims, f"missing claim: {key}"
        claim = claims[key]
        assert isinstance(claim, dict), f"claim {key} must be a mapping"
        assert claim.get("canonical_source") in canonical_sources
        assert isinstance(claim.get("canonical_selector"), str)
        assert claim.get("canonical_selector") == REQUIRED_CLAIMS[key]
        assert claim.get("expected_value") is not None


def test_design_note_max_iterations_matches_skill_loop_policy():
    design_note = _read_file(DESIGN_NOTE)
    claims = _extract_claims(design_note)
    skill_default = _extract_default_max_iterations(_read_file(SKILL_MD))

    assert claims["max_iterations_default"]["expected_value"] == skill_default


def test_design_note_iteration_limit_termination_matches_skill_policy():
    design_note = _read_file(DESIGN_NOTE)
    claims = _extract_claims(design_note)
    assert (
        " ".join(str(claims["iteration_limit_termination"]["expected_value"]).split())
        == "iteration + 1 >= max_iterations -> human_escalation"
    )
    assert "needs_second_pass" not in _read_file(DESIGN_NOTE)


def test_design_note_review_result_contract_matches_skill_compact_contract():
    design_note = _read_file(DESIGN_NOTE)
    claims = _extract_claims(design_note)
    skill_text = _read_file(SKILL_MD)

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
    assert expected in (
        ".claude/skills/issue-refinement-loop/references/loop-state.md",
        "references/loop-state.md",
    )
    assert "LOOP_STATE" in _read_file(LOOP_STATE_REFERENCE)


def test_design_note_rejects_long_copied_blocks_from_canonical_sources():
    design_note = _read_file(DESIGN_NOTE)
    frontmatter = _extract_frontmatter(design_note)
    canonical_sources = frontmatter["canonical_sources"]
    assert isinstance(canonical_sources, Iterable)

    for relpath in canonical_sources:
        source_path = REPO_ROOT / relpath
        assert source_path.exists(), f"canonical source missing: {relpath}"
        copied_blocks = _find_long_copied_windows(
            _read_file(source_path),
            design_note,
        )
        assert not copied_blocks, (
            f"long copied block from canonical source detected: {relpath}\n"
            f"copy candidates: {copied_blocks[:1]}"
        )


def _negative_fixture_from_canonical_source(source_path: Path, design_note: str) -> str:
    source_lines = _normalize_lines(_read_file(source_path))
    assert len(source_lines) >= _RELEVANT_COPY_MIN_LINES, (
        f"not enough source lines in {source_path} for negative fixture"
    )
    copied_block = "\n".join(source_lines[:_RELEVANT_COPY_MIN_LINES])
    if len(copied_block) < _RELEVANT_COPY_MIN_CHARS:
        copied_block = (copied_block + "\n") * 10
    return f"{design_note}\n\n# negative fixture\n\n```text\n{copied_block}\n```"


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
        1,
    )
    with pytest.raises(AssertionError):
        _assert_claims_consistency(bad_note)


def test_long_copy_negative_fixture_is_detected():
    design_note = _read_file(DESIGN_NOTE)
    bad_note = _negative_fixture_from_canonical_source(SKILL_MD, design_note)
    copied = _find_long_copied_windows(_read_file(SKILL_MD), bad_note)
    assert copied, "negative fixture should contain long copied block"


def test_design_note_contract_consistency_guard():
    _assert_claims_consistency(_read_file(DESIGN_NOTE))

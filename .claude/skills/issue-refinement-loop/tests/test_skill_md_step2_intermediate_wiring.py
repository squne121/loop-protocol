#!/usr/bin/env python3
"""
test_skill_md_step2_intermediate_wiring.py

Issue #1755 AC1: SKILL.md の Step 2 セクション（`### Step 2:` 見出しから次の
`##`/`###` 見出し直前まで、`#### Step 2a` を含む）内で、
`review_compact.validate_intermediate_v1`（child intermediate grammar）を指す
固有パターンが `review_compact.validate_v2`（parent-owned V2 final grammar）
を指すパターンより先に出現し、かつ bare な `review_compact.validate`
トークン（`_v2` にも `_intermediate_v1` にも一致しない旧 V1 final grammar
呼び出し）が Step 2 セクション内に存在しないことを検証する。
"""

import re
from pathlib import Path

SKILL_MD_PATH = Path(__file__).parent.parent / "SKILL.md"

_STEP2_HEADING_RE = re.compile(r"^### Step 2:", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"\n(### Step 3|## )")

_INTERMEDIATE_TOKEN_RE = re.compile(r"validate_intermediate_v1|_intermediate_v1")
_V2_TOKEN_RE = re.compile(r"review_compact\.validate_v2")
_BARE_TOKEN_RE = re.compile(r"review_compact\.validate(?!_v2|_intermediate_v1)")


def _load_skill_md() -> str:
    assert SKILL_MD_PATH.exists(), f"SKILL.md not found: {SKILL_MD_PATH}"
    return SKILL_MD_PATH.read_text(encoding="utf-8")


def _extract_step2_section(skill_md: str) -> str:
    """Extract the Step 2 section (from `### Step 2:` up to, but not
    including, the next `### Step 3` or `## ` heading). This intentionally
    includes `#### Step 2a` as required by AC1."""
    match = _STEP2_HEADING_RE.search(skill_md)
    assert match, "SKILL.md must contain a '### Step 2:' heading"
    rest = skill_md[match.start():]
    next_heading = _NEXT_HEADING_RE.search(rest)
    return rest[: next_heading.start()] if next_heading else rest


def test_step2_section_exists():
    """AC1: Step 2 section must be extractable (non-empty)."""
    section = _extract_step2_section(_load_skill_md())
    assert len(section) > 0
    assert "#### Step 2a" in section, (
        "Step 2 section must include Step 2a (AC1 scope explicitly requires this)"
    )


def test_step2_section_references_validate_intermediate_v1():
    """AC1: the Step 2 section must reference review_compact.validate_intermediate_v1."""
    section = _extract_step2_section(_load_skill_md())
    assert _INTERMEDIATE_TOKEN_RE.search(section), (
        "Step 2 section must reference review_compact.validate_intermediate_v1 "
        "(or _intermediate_v1) -- the child intermediate grammar validator"
    )


def test_intermediate_v1_pattern_precedes_v2_pattern():
    """AC1: `_intermediate_v1` (or `validate_intermediate_v1`) must occur
    BEFORE `review_compact.validate_v2` in the Step 2 section, reflecting
    the correct validator-first wiring order (child intermediate validation
    happens before the parent-owned V2 final envelope emission)."""
    section = _extract_step2_section(_load_skill_md())

    intermediate_match = _INTERMEDIATE_TOKEN_RE.search(section)
    v2_match = _V2_TOKEN_RE.search(section)

    assert intermediate_match, "Step 2 section missing _intermediate_v1 / validate_intermediate_v1 pattern"
    assert v2_match, "Step 2 section missing review_compact.validate_v2 pattern"

    assert intermediate_match.start() < v2_match.start(), (
        "review_compact.validate_intermediate_v1 must appear BEFORE "
        "review_compact.validate_v2 in the Step 2 section (Issue #1755 AC1).\n"
        f"intermediate at offset {intermediate_match.start()}, "
        f"v2 at offset {v2_match.start()}"
    )


def test_step2_section_has_no_bare_review_compact_validate_token():
    """AC1: the Step 2 section must NOT contain a bare `review_compact.validate`
    token (one that is not immediately followed by `_v2` or `_intermediate_v1`).
    This is the legacy V1 final-grammar command name and must not be called
    directly against child raw stdout (Issue #1755)."""
    section = _extract_step2_section(_load_skill_md())
    bare_matches = list(_BARE_TOKEN_RE.finditer(section))
    assert not bare_matches, (
        "Step 2 section must not contain a bare 'review_compact.validate' token "
        f"(found {len(bare_matches)} occurrence(s)): "
        + ", ".join(
            section[max(0, m.start() - 40): m.start() + 40] for m in bare_matches
        )
    )


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])

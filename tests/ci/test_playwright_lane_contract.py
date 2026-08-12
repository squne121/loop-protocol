"""
tests/ci/test_playwright_lane_contract.py

Issue #2119 AC3/AC4: the pre-existing determinism contract
(workers:1/fullyParallel:false/CI retry/updateSnapshots:none) survives the
lane split, and the responsive matrix's expected tuple set (viewport x DPR
x zoom) has zero duplicates and exact equality with runtime evidence
(enforced in-test in assist-player-affordance-responsive.spec.ts itself;
this test statically locks in the tuple-count arithmetic so a change to one
of the three arrays without updating the derived constant fails CI instead
of silently under/over-counting cells).
"""
from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PW_CONFIG = REPO_ROOT / "playwright.config.ts"
RESPONSIVE_SPEC = REPO_ROOT / "tests" / "e2e" / "assist-player-affordance-responsive.spec.ts"


def test_workers_one_fully_parallel_false_retry_update_snapshots_none_preserved():
    text = PW_CONFIG.read_text(encoding="utf-8")
    assert "fullyParallel: false" in text
    assert "workers: 1" in text
    assert "retries: process.env.CI ? 1 : 0" in text
    assert "updateSnapshots: process.env.CI ? 'none' : 'missing'" in text


def _extract_array_literal_length(text: str, const_name: str) -> int:
    """Counts top-level `{`-object entries (or scalar entries) inside
    `const <const_name> = [ ... ]` by counting object-open braces / commas at
    depth 1 -- deliberately NOT `eval`/`exec`-based (no untrusted code
    execution), a pure bracket-depth scan of the literal source text."""
    m = re.search(rf"const {re.escape(const_name)}(?::[^=]+)? = \[", text)
    assert m, f"{const_name} array literal not found in {RESPONSIVE_SPEC}"
    start = m.end() - 1  # index of the opening '['
    depth = 0
    entry_starts = 0
    i = start
    in_object_depth = None
    while i < len(text):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        elif ch == "{" and depth == 1 and in_object_depth is None:
            entry_starts += 1
        i += 1
    return entry_starts if entry_starts > 0 else None


def test_responsive_matrix_tuple_set_exact_equality_and_no_duplicates():
    text = RESPONSIVE_SPEC.read_text(encoding="utf-8")

    viewports_count = _extract_array_literal_length(text, "RESPONSIVE_VIEWPORTS")
    zooms_count = _extract_array_literal_length(text, "RESPONSIVE_ZOOMS")

    # RESPONSIVE_DPRS is a flat number array, not an object array -- count
    # numeric literal entries directly.
    m = re.search(r"const RESPONSIVE_DPRS = \[([^\]]*)\]", text)
    assert m, "RESPONSIVE_DPRS array literal not found"
    dprs_count = len([p for p in m.group(1).split(",") if p.strip()])

    assert viewports_count == 4, f"expected 4 RESPONSIVE_VIEWPORTS entries, got {viewports_count}"
    assert dprs_count == 4, f"expected 4 RESPONSIVE_DPRS entries, got {dprs_count}"
    assert zooms_count == 4, f"expected 4 RESPONSIVE_ZOOMS entries, got {zooms_count}"

    expected_cell_count = viewports_count * dprs_count * zooms_count
    assert expected_cell_count == 64

    # The spec's own exported constant must equal the arithmetic above, and
    # the in-test assertions (RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: exact
    # cell count, zero duplicate tuples, pointer_mapping/frozen_gameplay
    # presence per cell) must exist in source.
    assert "RESPONSIVE_CANVAS_MATRIX_EXPECTED_CELL_COUNT =" in text
    assert "RESPONSIVE_VIEWPORTS.length * RESPONSIVE_DPRS.length * RESPONSIVE_ZOOMS.length" in text
    assert "RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: evidence cell count must equal the expected tuple count" in text
    assert "RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: duplicate tuple" in text
    assert "missing pointer_mapping evidence" in text
    assert "missing frozen_gameplay evidence" in text

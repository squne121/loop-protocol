"""
tests/ci/test_playwright_lane_contract.py

Issue #2119 AC3/AC4: the pre-existing determinism contract
(workers:1/fullyParallel:false/CI retry/updateSnapshots:none) survives the
lane split, and the responsive matrix's expected tuple set (viewport x DPR
x zoom) has zero duplicates and exact equality with both (a) the spec's own
literal source arrays and (b) real CI runtime evidence, both compared
against an independently-authored frozen fixture (PR #2137 human review
issuecomment-5273090534 P1 fix: the prior version of this test only counted
array elements and self-verified against the spec file's own source text --
replacing one viewport with a different resolution while keeping 4 entries
would previously have stayed green; this version parses and compares the
actual tuple VALUES against tests/ci/fixtures/responsive_canvas_matrix_contract_v1.json).
"""
from __future__ import annotations

import glob
import json
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PW_CONFIG = REPO_ROOT / "playwright.config.ts"
RESPONSIVE_SPEC = REPO_ROOT / "tests" / "e2e" / "assist-player-affordance-responsive.spec.ts"
CONTRACT_FIXTURE = REPO_ROOT / "tests" / "ci" / "fixtures" / "responsive_canvas_matrix_contract_v1.json"
ARTIFACTS_DIR = REPO_ROOT / ".claude" / "artifacts"


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


def _parse_viewports(text: str) -> list[dict]:
    m = re.search(r"const RESPONSIVE_VIEWPORTS(?::[^=]+)? = \[(.*?)\]\n", text, re.S)
    assert m, "RESPONSIVE_VIEWPORTS array literal not found"
    entries = re.findall(
        r"\{\s*width:\s*(\d+),\s*height:\s*(\d+),\s*label:\s*'([^']+)'\s*\}",
        m.group(1),
    )
    assert entries, "no RESPONSIVE_VIEWPORTS object entries parsed"
    return [
        {"width": int(w), "height": int(h), "label": label}
        for (w, h, label) in entries
    ]


def _parse_dprs(text: str) -> list[float]:
    m = re.search(r"const RESPONSIVE_DPRS = \[([^\]]*)\]", text)
    assert m, "RESPONSIVE_DPRS array literal not found"
    return [float(p.strip()) for p in m.group(1).split(",") if p.strip()]


def _parse_zooms(text: str) -> list[dict]:
    m = re.search(r"const RESPONSIVE_ZOOMS(?::[^=]+)? = \[(.*?)\]\n", text, re.S)
    assert m, "RESPONSIVE_ZOOMS array literal not found"
    entries = re.findall(
        r"\{\s*factor:\s*([\d.]+),\s*label:\s*'([^']+)'\s*\}",
        m.group(1),
    )
    assert entries, "no RESPONSIVE_ZOOMS object entries parsed"
    return [{"factor": float(f), "label": label} for (f, label) in entries]


def _load_contract_fixture() -> dict:
    assert CONTRACT_FIXTURE.is_file(), f"missing frozen fixture: {CONTRACT_FIXTURE}"
    return json.loads(CONTRACT_FIXTURE.read_text(encoding="utf-8"))


def test_responsive_matrix_tuple_set_exact_equality_and_no_duplicates():
    text = RESPONSIVE_SPEC.read_text(encoding="utf-8")
    fixture = _load_contract_fixture()

    parsed_viewports = _parse_viewports(text)
    parsed_dprs = _parse_dprs(text)
    parsed_zooms = _parse_zooms(text)

    # RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: the spec's own literal tuple
    # VALUES (not just element counts) must exactly equal the independently
    # authored frozen fixture -- replacing one viewport/DPR/zoom entry with a
    # different value while keeping the same element count must fail.
    assert parsed_viewports == fixture["expected_viewports"], (
        f"RESPONSIVE_VIEWPORTS values diverge from frozen fixture: "
        f"{parsed_viewports} != {fixture['expected_viewports']}"
    )
    assert parsed_dprs == fixture["expected_device_scale_factors"], (
        f"RESPONSIVE_DPRS values diverge from frozen fixture: "
        f"{parsed_dprs} != {fixture['expected_device_scale_factors']}"
    )
    assert parsed_zooms == fixture["expected_zooms"], (
        f"RESPONSIVE_ZOOMS values diverge from frozen fixture: "
        f"{parsed_zooms} != {fixture['expected_zooms']}"
    )

    assert len(parsed_viewports) == len(set(v["label"] for v in parsed_viewports)) == 4
    assert len(parsed_dprs) == 4
    assert len(parsed_zooms) == len(set(z["label"] for z in parsed_zooms)) == 4

    expected_cell_count = len(parsed_viewports) * len(parsed_dprs) * len(parsed_zooms)
    assert expected_cell_count == fixture["expected_cell_count"] == 64

    # The spec's own exported constant must equal the arithmetic above, and
    # the in-test assertions (RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: exact
    # cell count, zero duplicate tuples, pointer_mapping/frozen_gameplay
    # presence per cell) must exist in source (defense in depth: the E2E run
    # itself fails fast on the same invariant before any CI artifact exists).
    assert "RESPONSIVE_CANVAS_MATRIX_EXPECTED_CELL_COUNT =" in text
    assert "RESPONSIVE_VIEWPORTS.length * RESPONSIVE_DPRS.length * RESPONSIVE_ZOOMS.length" in text
    assert "RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: evidence cell count must equal the expected tuple count" in text
    assert "RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: duplicate tuple" in text
    assert "missing pointer_mapping evidence" in text
    assert "missing frozen_gameplay evidence" in text


def test_responsive_matrix_runtime_evidence_matches_frozen_fixture_exactly():
    """AC4 (runtime half): when a real responsive-canvas-runtime-evidence.json
    CI artifact is available, its observed (viewport, DPR, zoom) tuple set
    must exactly equal the frozen fixture's expected tuple set -- missing or
    extra tuples fail, duplicate tuples fail. This intentionally validates
    against the frozen fixture (not the spec file's own source), per PR #2137
    human review issuecomment-5273090534 P1 (avoid self-verification)."""
    fixture = _load_contract_fixture()
    candidates = (
        glob.glob(str(ARTIFACTS_DIR / "**" / "responsive-canvas-runtime-evidence*.json"), recursive=True)
        if ARTIFACTS_DIR.is_dir()
        else []
    )
    if not candidates:
        import pytest

        pytest.skip(
            f"no real responsive-canvas-runtime-evidence.json CI artifact found under "
            f"{ARTIFACTS_DIR} -- generated by the e2e-responsive-matrix provider job. "
            f"SKIP (not a fabricated PASS) per Runtime Verification Applicability "
            f"fallback_success_is_pass: false; the static tuple-value contract above "
            f"(spec source vs frozen fixture) already runs unconditionally."
        )

    expected_tuples = {
        (v["label"], dpr, z["label"])
        for v in fixture["expected_viewports"]
        for dpr in fixture["expected_device_scale_factors"]
        for z in fixture["expected_zooms"]
    }

    for evidence_path in candidates:
        doc = json.loads(pathlib.Path(evidence_path).read_text(encoding="utf-8"))
        assert doc.get("schema") == "RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1", evidence_path
        entries = doc.get("evidence", [])

        observed_tuples: list[tuple] = []
        for entry in entries:
            assert entry.get("pointer_mapping"), f"{evidence_path}: missing pointer_mapping evidence"
            assert entry.get("frozen_gameplay"), f"{evidence_path}: missing frozen_gameplay evidence"
            assert entry.get("head_sha"), f"{evidence_path}: missing head_sha evidence"
            observed_tuples.append((entry.get("viewport"), entry.get("declared_dpr"), entry.get("browser_zoom")))

        observed_set = set(observed_tuples)
        assert len(observed_tuples) == len(observed_set), (
            f"{evidence_path}: duplicate observed tuples: "
            f"{[t for t in observed_tuples if observed_tuples.count(t) > 1]}"
        )

        missing = expected_tuples - observed_set
        extra = observed_set - expected_tuples
        assert not missing, f"{evidence_path}: missing expected tuples: {missing}"
        assert not extra, f"{evidence_path}: unexpected extra tuples: {extra}"

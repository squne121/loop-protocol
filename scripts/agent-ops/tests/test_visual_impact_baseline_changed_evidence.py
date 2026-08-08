"""test_visual_impact_baseline_changed_evidence.py (Issue #2019 AC13)

GIVEN a `baseline_changed` disposition, WHEN evaluated, THEN "the PNG
changed" alone never passes: it requires canonical update-then-verify
success on the SAME head, expected/actual/diff availability, and evidence
manifest digest + binding.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2019_baseline_changed"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)


def _base_evidence(**overrides) -> "rvi.EvidenceObservation":
    defaults = dict(
        baseline_diff_present=True,
        canonical_verify_success=True,
        evidence_manifest_surface_matches=True,
        evidence_manifest_contract_matches=True,
        evidence_manifest_head_matches=True,
        canonical_update_then_verify_success=True,
        expected_actual_diff_available=True,
        evidence_manifest_digest="sha256:" + "a" * 64,
    )
    defaults.update(overrides)
    return rvi.EvidenceObservation(**defaults)


def test_png_changed_alone_without_canonical_lane_success_fails():
    evidence = _base_evidence(canonical_update_then_verify_success=False)
    ok, reason = rvi.evaluate_baseline_changed(evidence)
    assert ok is False
    assert reason == "canonical_update_then_verify_lane_did_not_succeed_on_current_head"


def test_no_baseline_diff_but_claiming_baseline_changed_fails():
    evidence = _base_evidence(baseline_diff_present=False)
    ok, reason = rvi.evaluate_baseline_changed(evidence)
    assert ok is False
    assert "no baseline diff observed" in reason


def test_missing_expected_actual_diff_fails():
    evidence = _base_evidence(expected_actual_diff_available=False)
    ok, reason = rvi.evaluate_baseline_changed(evidence)
    assert ok is False
    assert reason == "expected_actual_diff_not_available"


def test_missing_evidence_manifest_digest_fails():
    evidence = _base_evidence(evidence_manifest_digest=None)
    ok, reason = rvi.evaluate_baseline_changed(evidence)
    assert ok is False
    assert reason == "evidence_manifest_digest_missing"


def test_evidence_manifest_binding_mismatch_fails():
    evidence = _base_evidence(evidence_manifest_head_matches=False)
    ok, reason = rvi.evaluate_baseline_changed(evidence)
    assert ok is False
    assert reason == "evidence_manifest_binding_mismatch"


def test_full_canonical_evidence_passes():
    evidence = _base_evidence()
    ok, reason = rvi.evaluate_baseline_changed(evidence)
    assert ok is True
    assert reason == "ok"

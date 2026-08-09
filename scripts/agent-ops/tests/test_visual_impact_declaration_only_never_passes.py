"""test_visual_impact_declaration_only_never_passes.py (Issue #2019 AC12)

GIVEN a VISUAL_IMPACT_DECLARATION_V1 claiming `verified_unchanged` with NO
supporting trusted evidence (baseline diff / canonical VRT success /
evidence manifest binding), WHEN evaluated THEN the disposition never
passes. Declaration self-report alone is never sufficient.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2019_declaration_only"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)


def _no_evidence() -> "rvi.EvidenceObservation":
    return rvi.EvidenceObservation(
        baseline_diff_present=False,
        canonical_verify_success=False,
        evidence_manifest_surface_matches=False,
        evidence_manifest_contract_matches=False,
        evidence_manifest_head_matches=False,
    )


def test_declaration_only_verified_unchanged_never_passes():
    ok, reason = rvi.evaluate_verified_unchanged(_no_evidence())
    assert ok is False
    assert reason != "ok"


def test_declaration_only_baseline_changed_never_passes():
    ok, reason = rvi.evaluate_baseline_changed(_no_evidence())
    assert ok is False
    assert reason != "ok"


def test_canonical_verify_success_alone_without_manifest_binding_fails():
    """Even a genuinely-successful canonical VRT run does not pass if the
    evidence manifest cannot be bound to this exact surface/contract/head
    (prevents replaying an unrelated success as evidence)."""
    evidence = rvi.EvidenceObservation(
        baseline_diff_present=False,
        canonical_verify_success=True,
        evidence_manifest_surface_matches=False,
        evidence_manifest_contract_matches=True,
        evidence_manifest_head_matches=True,
    )
    ok, reason = rvi.evaluate_verified_unchanged(evidence)
    assert ok is False
    assert reason == "evidence_manifest_binding_mismatch"


def test_baseline_diff_present_contradicts_verified_unchanged_claim():
    evidence = rvi.EvidenceObservation(
        baseline_diff_present=True,
        canonical_verify_success=True,
        evidence_manifest_surface_matches=True,
        evidence_manifest_contract_matches=True,
        evidence_manifest_head_matches=True,
    )
    ok, reason = rvi.evaluate_verified_unchanged(evidence)
    assert ok is False
    assert reason.startswith("baseline_diff_present")


def test_full_trusted_evidence_passes_verified_unchanged():
    evidence = rvi.EvidenceObservation(
        baseline_diff_present=False,
        canonical_verify_success=True,
        evidence_manifest_surface_matches=True,
        evidence_manifest_contract_matches=True,
        evidence_manifest_head_matches=True,
    )
    ok, reason = rvi.evaluate_verified_unchanged(evidence)
    assert ok is True
    assert reason == "ok"

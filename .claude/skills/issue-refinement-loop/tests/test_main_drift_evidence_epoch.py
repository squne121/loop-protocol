from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("plan_refinement_loop_main_drift", Path(__file__).resolve().parents[1] / "scripts" / "plan_refinement_loop.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = MODULE; SPEC.loader.exec_module(MODULE)
SHA_A, SHA_B = "a" * 40, "b" * 40

def _context(**extra):
    value = {"current_base_sha": SHA_B, "evidence_base_sha": SHA_A, "allowed_paths_snapshot_base_sha": SHA_B,
        "allowed_paths": ["docs/dev/"], "latest_main_net_diff": ["docs/dev/workflow.md"], "expected_head_sha": SHA_A,
        "observed_head_sha": SHA_A, "expected_old_sha": SHA_B, "observed_old_sha": SHA_B}
    value.update(extra); return value

def test_given_drift_when_refinement_classifies_then_old_evidence_is_not_reusable():
    result = MODULE.classify_refinement_evidence_epoch(_context())
    assert result["route"] == "scope_clean_reconciliation"
    assert result["reusable_evidence"] == {"snapshot": None, "ci": None, "review": None}
    assert result["mutation_owner"] == "refinement"

def test_given_stale_scope_snapshot_when_refinement_classifies_then_it_stops():
    assert MODULE.classify_refinement_evidence_epoch(_context(allowed_paths_snapshot_base_sha=SHA_A))["reason_code"] == "stale_allowed_paths_snapshot"

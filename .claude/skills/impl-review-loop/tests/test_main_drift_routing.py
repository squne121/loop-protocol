from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from route_loop_verdict_v2 import ROUTE_FAIL_CLOSED, ROUTE_SCOPE_CLEAN_RECONCILIATION, route_loop_verdict_v2

SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def _verdict(): return {"verdict": "APPROVE", "reviewed_head_sha": SHA_A, "blockers": [], "warnings": []}
def _behind(): return {"head_sha": SHA_A, "mergeable": "MERGEABLE", "merge_state_status": "BEHIND"}
def _drift(**extra):
    value = {"current_base_sha": SHA_B, "evidence_base_sha": SHA_C, "allowed_paths_snapshot_base_sha": SHA_B,
        "allowed_paths": [".claude/skills/impl-review-loop/"], "latest_main_net_diff": [".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"],
        "expected_old_sha": SHA_B, "observed_old_sha": SHA_B}
    value.update(extra); return value


def test_given_scope_clean_drift_when_routed_then_reconciliation_reverifies_without_spending_iteration():
    decision = route_loop_verdict_v2(_verdict(), _behind(), _drift())
    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.rerun_required == {"snapshot": True, "ci": True, "review": True}
    assert decision.selected_action["evidence_epoch"]["implementation_iteration_delta"] == 0


def test_given_eligible_behind_drift_when_routed_then_fast_path_avoids_reconciliation():
    decision = route_loop_verdict_v2(_verdict(), _behind(), _drift(behind_fast_path_eligible=True))
    assert decision.route == "route_to_update_branch"
    assert decision.selected_action["main_drift_strategy"] == "behind_fast_path"


def test_given_semantic_ambiguity_when_routed_then_it_stops_without_action():
    decision = route_loop_verdict_v2(_verdict(), _behind(), _drift(semantic_ambiguity=True))
    assert decision.route == ROUTE_FAIL_CLOSED and decision.selected_action is None

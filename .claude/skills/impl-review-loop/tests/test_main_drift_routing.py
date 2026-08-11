from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from route_loop_verdict_v2 import (  # noqa: E402
    ROUTE_FAIL_CLOSED,
    ROUTE_SCOPE_CLEAN_RECONCILIATION,
    build_step5_live_mergeability,
    route_loop_verdict_v2,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
STEP5_MERGEABILITY = (
    Path(__file__).resolve().parents[1]
    / "steps"
    / "step-5-mergeability-handling.md"
)


def _verdict() -> dict[str, object]:
    return {
        "verdict": "APPROVE",
        "reviewed_head_sha": SHA_A,
        "blockers": [],
        "warnings": [],
    }


def _live(
    merge_state_status: str,
    main_drift: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "head_sha": SHA_A,
        "mergeable": "MERGEABLE",
        "merge_state_status": merge_state_status,
    }
    value["main_drift"] = _fresh_drift() if main_drift is None else main_drift
    return value


def _live_without_main_drift(merge_state_status: str) -> dict[str, object]:
    """main_drift remains an optional key on the public routing boundary."""
    return {
        "head_sha": SHA_A,
        "mergeable": "MERGEABLE",
        "merge_state_status": merge_state_status,
    }


def _drift(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "current_base_sha": SHA_B,
        "evidence_base_sha": SHA_C,
        "allowed_paths_snapshot_base_sha": SHA_B,
        "allowed_paths": [".claude/skills/impl-review-loop/"],
        "latest_main_net_diff": [
            ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
        ],
        "expected_old_sha": SHA_B,
        "observed_old_sha": SHA_B,
    }
    value.update(extra)
    return value


def _fresh_drift(**extra: object) -> dict[str, object]:
    value = _drift(evidence_base_sha=SHA_B)
    value.update(extra)
    return value


def test_given_scope_clean_drift_when_routed_then_reconciliation_reverifies_without_spending_iteration():
    decision = route_loop_verdict_v2(_verdict(), _live("BEHIND", _drift()))

    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.rerun_required == {"snapshot": True, "ci": True, "review": True}
    assert decision.selected_action["evidence_epoch"]["implementation_iteration_delta"] == 0


def test_given_eligible_behind_drift_when_routed_then_fast_path_avoids_reconciliation():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(behind_fast_path_eligible=True)),
    )

    assert decision.route == "route_to_update_branch"
    assert decision.selected_action["main_drift_strategy"] == "behind_fast_path"


@pytest.mark.parametrize("merge_state_status", ["CLEAN", "HAS_HOOKS", "BLOCKED", "DRAFT", "UNSTABLE"])
def test_given_scope_clean_drift_in_any_nonbehind_state_when_routed_then_old_evidence_is_invalidated(
    merge_state_status: str,
):
    decision = route_loop_verdict_v2(
        _verdict(),
        _live(merge_state_status, _drift(behind_fast_path_eligible=True)),
    )

    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.selected_action["reusable_evidence"] == {
        "snapshot": None,
        "ci": None,
        "review": None,
    }
    assert decision.selected_action["evidence_epoch"]["base_sha"] == SHA_B
    assert decision.selected_action["evidence_epoch"]["implementation_iteration_delta"] == 0


@pytest.mark.parametrize("merge_state_status", ["CLEAN", "HAS_HOOKS", "BLOCKED", "DRAFT", "UNSTABLE"])
def test_given_step5_control_plane_live_facts_when_stale_evidence_exists_then_nonbehind_state_cannot_bypass_rebind(
    merge_state_status: str,
):
    """Step 5 の production input 経路は stale evidence を必ず router に渡す。"""
    live_mergeability = build_step5_live_mergeability(
        _live(merge_state_status),
        _drift(behind_fast_path_eligible=True),
    )

    decision = route_loop_verdict_v2(_verdict(), live_mergeability)

    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.route != "approved"
    assert decision.selected_action["reusable_evidence"] == {
        "snapshot": None,
        "ci": None,
        "review": None,
    }
    assert decision.rerun_required == {"snapshot": True, "ci": True, "review": True}


def test_given_normal_step5_when_documented_then_live_main_drift_facts_are_built_before_routing():
    body = STEP5_MERGEABILITY.read_text(encoding="utf-8")

    assert "毎回の通常 production routing 前" in body
    assert "build_step5_live_mergeability(" in body
    for key in (
        "current_base_sha",
        "evidence_base_sha",
        "allowed_paths_snapshot_base_sha",
        "latest_main_net_diff",
        "expected_old_sha",
        "observed_old_sha",
    ):
        assert key in body


def test_given_missing_main_drift_when_approve_then_pre_2102_routing_is_preserved():
    """main_drift is an optional key (Design Invariant: the public
    two-input routing boundary is stable). Callers that omit it entirely
    keep the pre-#2102 CLEAN/MERGEABLE -> approved behavior; only a
    PRESENT-but-malformed main_drift payload fails closed."""
    decision = route_loop_verdict_v2(_verdict(), _live_without_main_drift("CLEAN"))

    assert decision.route == "approved"


@pytest.mark.parametrize(
    "main_drift, reason_code",
    [
        ({"current_base_sha": SHA_B}, "main_drift_context_invalid"),
        (_fresh_drift(allowed_paths="not-a-path-list"), "main_drift_context_invalid"),
    ],
)
def test_given_invalid_step5_main_drift_facts_when_approve_then_router_fails_closed(
    main_drift: dict[str, object],
    reason_code: str,
):
    live_mergeability = {
        "head_sha": SHA_A,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "main_drift": main_drift,
    }

    decision = route_loop_verdict_v2(_verdict(), live_mergeability)

    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.fail_closed is True
    assert decision.reason_code == reason_code
    assert decision.route != "approved"


def test_given_semantic_ambiguity_when_routed_then_it_stops_without_action():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("CLEAN", _drift(semantic_ambiguity=True)),
    )

    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.selected_action is None



def test_given_drift_rebind_attempts_below_budget_when_routed_then_epoch_carries_next_attempt_count():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(drift_rebind_attempts=1)),
    )

    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.selected_action["evidence_epoch"]["drift_rebind_attempts"] == 2
    assert decision.selected_action["evidence_epoch"]["max_drift_rebind_attempts"] == 2


def test_given_drift_rebind_attempts_at_budget_when_routed_then_fail_closed_not_human_escalation():
    """Issue #2102 P0-B: bounded outer drift-rebind budget, independent of
    LOOP_STATE.iteration / implementation_iteration_delta. A churning main
    must not livelock the loop, but exhausting the bound is a deterministic
    machine stop, not a semantic-judgment human_escalation route."""
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(drift_rebind_attempts=2)),
    )

    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.fail_closed is True
    assert decision.reason_code == "concurrent_base_churn_budget_exhausted"
    assert decision.route != "route_human_escalation"


def test_given_negative_drift_rebind_attempts_when_routed_then_context_invalid():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(drift_rebind_attempts=-1)),
    )

    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.reason_code == "main_drift_context_invalid"

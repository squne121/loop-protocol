"""
Focused pytest for the `already_satisfied` terminal route (Issue #2607).

Covers AC2 (four-condition gate), AC3 (base_ac_satisfied is caller-supplied,
never PR-head-derived), AC4 (meaningful_pr_delta is a boolean fact, never a
blockers[] free-text heuristic), and the AC11 8-case matrix:

  1. PR exists + base_ac_satisfied=true + meaningful_pr_delta=false -> already_satisfied
  2. PR未作成 + base_ac_satisfied=true -> no-op early-exit (preparation.md choke point)
  3. base_ac_satisfied=false -> ordinary implementation route (both routing
     surfaces: route_loop_verdict_v2() continue_loop, and the early-exit
     choke point's dispatch_step1=True)
  4. meaningful_pr_delta=true -> no `pr.action: close` recommendation
  5. evidence_base_sha stale/mismatch (or main_drift absent) -> not already_satisfied
  6. an ordinary REQUEST_CHANGES unrelated to already_satisfied -> existing
     continue_loop is preserved (no already_satisfied_evidence key at all)
  7. existing conflict/fail-closed/main-drift regressions are preserved
  8. already_satisfied never performs an auto-close mutation itself
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

IMPL_REVIEW_LOOP_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = IMPL_REVIEW_LOOP_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402

SHA_MAIN = "a" * 40
SHA_OTHER = "b" * 40
SHA_STALE = "c" * 40


def _reviewer_verdict(
    *, verdict: str = "REQUEST_CHANGES", reviewed_head_sha: str = SHA_MAIN,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "reviewed_head_sha": reviewed_head_sha,
        "blockers": blockers if blockers is not None else [],
    }


def _main_drift(*, current_base_sha: str = SHA_OTHER, head_sha: str = SHA_MAIN) -> dict[str, Any]:
    """A main_drift payload that classifies as `fresh` (current_base_sha ==
    its own evidence_base_sha) so it never interferes with already_satisfied
    evaluation -- only main_drift["current_base_sha"] is used as the
    already_satisfied freshness comparator (Issue #2102 reuse)."""
    return {
        "current_base_sha": current_base_sha,
        "evidence_base_sha": current_base_sha,
        "allowed_paths_snapshot_base_sha": current_base_sha,
        "allowed_paths": ["some/path.py"],
        "latest_main_net_diff": [],
        "expected_old_sha": head_sha,
        "observed_old_sha": head_sha,
    }


def _live_mergeability(
    *,
    head_sha: str = SHA_MAIN,
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    already_satisfied_evidence: dict[str, Any] | None = None,
    main_drift: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "head_sha": head_sha,
        "mergeable": mergeable,
        "merge_state_status": merge_state_status,
    }
    if already_satisfied_evidence is not None:
        payload["already_satisfied_evidence"] = already_satisfied_evidence
    if main_drift is not None:
        payload["main_drift"] = main_drift
    return payload


def _evidence(
    *, base_ac_satisfied: Any = True, meaningful_pr_delta: Any = False,
    evidence_base_sha: Any = SHA_OTHER,
) -> dict[str, Any]:
    return {
        "base_ac_satisfied": base_ac_satisfied,
        "meaningful_pr_delta": meaningful_pr_delta,
        "evidence_base_sha": evidence_base_sha,
    }


# ---------------------------------------------------------------------------
# preparation.md's early-exit choke point (Issue #2607, PR未作成 path).
#
# No new production script is in this Issue's Allowed Paths (only SKILL.md /
# route_loop_verdict_v2.py / preparation.md / step-5 docs / these two test
# files). This pure function mirrors, 1:1, the single common choke point
# documented in preparation.md's "0-a-1. Already-Satisfied Early-Exit choke
# point" section -- it is loaded directly (via importlib, by absolute path)
# from test_already_satisfied_early_exit_e2e_runtime_only.py so both the
# unit-level coverage here and the real-subprocess-driven e2e coverage there
# exercise the exact same decision function (single source of truth).
# ---------------------------------------------------------------------------


def resolve_already_satisfied_early_exit_decision(
    *,
    next_action_route: str,
    product_spec_routing_action: str,
    pr_exists: bool,
    base_ac_satisfied: bool,
) -> dict[str, Any]:
    """Pure decision function for preparation.md's `0-a-1` choke point.

    Fires (dispatch_step1=False) iff `pr_exists` is False AND
    `base_ac_satisfied` is True -- regardless of `next_action_route` /
    `product_spec_routing_action` (AC5: single common choke point,
    independent of which upstream branch value is currently driving Step 1
    continuation).
    """
    if pr_exists or not base_ac_satisfied:
        return {
            "early_exit": False,
            "dispatch_step1": True,
            "reason": "pr_already_exists" if pr_exists else "base_ac_not_satisfied",
            "upstream_route": next_action_route,
            "upstream_product_spec_routing_action": product_spec_routing_action,
        }
    return {
        "early_exit": True,
        "dispatch_step1": False,
        "reason": "already_satisfied_no_pr_created",
        "result": {
            "status": "no_change_required",
            "termination_reason": "already_satisfied",
            "merge_ready": False,
        },
        "recommendation": {
            "pr": {"action": "none", "reason": "no_pr_created"},
            "issue": {
                "action": "close",
                "state_reason": "completed",
                "reason": "requirement_already_delivered",
            },
        },
        "upstream_route": next_action_route,
        "upstream_product_spec_routing_action": product_spec_routing_action,
    }


# ---------------------------------------------------------------------------
# AC2: four-condition gate.
# ---------------------------------------------------------------------------


def test_already_satisfied_requires_all_four_conditions():
    """AC2: only REQUEST_CHANGES + base_ac_satisfied=true +
    meaningful_pr_delta=false + evidence_base_sha == current main HEAD
    selects already_satisfied; any single condition flipped falls back to
    continue_loop."""
    all_true = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(),
            main_drift=_main_drift(),
        ),
    )
    assert all_true.route == "already_satisfied"
    assert all_true.fail_closed is False

    # Condition 1 flipped: verdict is not REQUEST_CHANGES (APPROVE, with a
    # matching head so it doesn't get routed to stale-head re-review first).
    not_request_changes = route_loop_verdict_v2(
        _reviewer_verdict(verdict="APPROVE"),
        _live_mergeability(
            already_satisfied_evidence=_evidence(),
            main_drift=_main_drift(),
        ),
    )
    assert not_request_changes.route != "already_satisfied"

    # Condition 2 flipped: base_ac_satisfied=false.
    base_not_satisfied = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(base_ac_satisfied=False),
            main_drift=_main_drift(),
        ),
    )
    assert base_not_satisfied.route == "continue_loop"

    # Condition 3 flipped: meaningful_pr_delta=true.
    meaningful_delta = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(meaningful_pr_delta=True),
            main_drift=_main_drift(),
        ),
    )
    assert meaningful_delta.route == "continue_loop"

    # Condition 4 flipped: evidence_base_sha does not match main HEAD.
    stale_evidence = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(evidence_base_sha=SHA_STALE),
            main_drift=_main_drift(),
        ),
    )
    assert stale_evidence.route == "continue_loop"


# ---------------------------------------------------------------------------
# AC3: base_ac_satisfied is a caller-supplied fact, never PR-head-derived.
# ---------------------------------------------------------------------------


def test_base_ac_satisfied_derived_from_independent_base_checkout_not_pr_head():
    """AC3: already_satisfied routing is reachable with a non-empty
    blockers[] list and is never routed to ROUTE_STALE_HEAD_REREVIEW -- both
    the "blockers must be empty" rule and the
    reviewed_head_sha != live head_sha stale-head-rereview check are
    APPROVE-branch-only (structurally unreachable from the REQUEST_CHANGES
    dispatch already_satisfied is evaluated in). base_ac_satisfied comes
    solely from the caller-supplied already_satisfied_evidence -- not from
    any PR-head TEST_VERDICT proxy. reviewer_verdict cannot even carry a
    test_verdict field (rejected as a legacy field), so PR-head test
    results have no channel into this router at all."""
    with_blockers_present = route_loop_verdict_v2(
        _reviewer_verdict(blockers=["unrelated review note"]),
        _live_mergeability(
            already_satisfied_evidence=_evidence(),
            main_drift=_main_drift(),
        ),
    )
    assert with_blockers_present.route == "already_satisfied"
    assert with_blockers_present.route != "route_stale_head_rereview"

    # A reviewer_verdict carrying a legacy test_verdict field fails closed
    # entirely (schema_invalid), proving there is no accepted channel for a
    # PR-head test result to leak into base_ac_satisfied evaluation.
    poisoned = route_loop_verdict_v2(
        {**_reviewer_verdict(), "test_verdict": "PASS"},
        _live_mergeability(
            already_satisfied_evidence=_evidence(),
            main_drift=_main_drift(),
        ),
    )
    assert poisoned.route == "fail_closed"
    assert poisoned.reason_code == "schema_invalid_legacy_field_present:test_verdict"


# ---------------------------------------------------------------------------
# AC4: meaningful_pr_delta is a boolean fact, never a blockers[] heuristic.
# ---------------------------------------------------------------------------


def test_meaningful_pr_delta_derived_from_runtime_ac_results_comparison_not_blockers_text():
    """AC4: blockers[] free text (even text that reads like a no-op
    admission) must never, by itself, select already_satisfied or suppress
    it -- only the explicit already_satisfied_evidence.meaningful_pr_delta
    boolean does."""
    blockers_say_trivial_but_delta_flag_true = route_loop_verdict_v2(
        _reviewer_verdict(blockers=["this is a trivial no-op change, nothing to fix"]),
        _live_mergeability(
            already_satisfied_evidence=_evidence(meaningful_pr_delta=True),
            main_drift=_main_drift(),
        ),
    )
    assert blockers_say_trivial_but_delta_flag_true.route == "continue_loop", (
        "blockers[] text must not override the explicit meaningful_pr_delta=true fact"
    )

    blockers_say_real_bug_but_delta_flag_false = route_loop_verdict_v2(
        _reviewer_verdict(blockers=["actual regression found in handler"]),
        _live_mergeability(
            already_satisfied_evidence=_evidence(meaningful_pr_delta=False),
            main_drift=_main_drift(),
        ),
    )
    assert blockers_say_real_bug_but_delta_flag_false.route == "already_satisfied", (
        "blockers[] text must not suppress already_satisfied when the "
        "explicit meaningful_pr_delta=false fact holds"
    )


# ---------------------------------------------------------------------------
# AC11 case (1): PR exists + base_ac_satisfied=true + meaningful_pr_delta=false.
# ---------------------------------------------------------------------------


def test_ac11_case1_pr_exists_and_base_satisfied_and_no_delta_routes_already_satisfied():
    result = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(base_ac_satisfied=True, meaningful_pr_delta=False),
            main_drift=_main_drift(),
        ),
    )
    assert result.route == "already_satisfied"
    assert result.selected_action["recommendation"]["pr"]["action"] == "close"
    assert result.selected_action["recommendation"]["pr"]["reason"] == "no_meaningful_delta"
    assert result.selected_action["recommendation"]["issue"]["action"] == "close"
    assert result.selected_action["recommendation"]["issue"]["state_reason"] == "completed"
    assert result.selected_action["result"]["termination_reason"] == "already_satisfied"
    assert result.selected_action["result"]["merge_ready"] is False


# ---------------------------------------------------------------------------
# AC11 case (2): PR未作成 + base_ac_satisfied=true -> no-op early-exit,
# independent of next_action.route / product_spec_preflight.routing_action.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "next_action_route,product_spec_routing_action",
    [
        ("proceed_to_step_1", "continue"),
        ("proceed_to_step_1", "refresh_contract_snapshot"),
        ("request_readiness_check", "continue"),
        ("run_contract_blocker_triage", "stop_human"),
        ("human_review_required", "refresh_contract_snapshot"),
    ],
)
def test_ac11_case2_no_pr_and_base_satisfied_early_exits_regardless_of_upstream_route(
    next_action_route: str, product_spec_routing_action: str,
):
    decision = resolve_already_satisfied_early_exit_decision(
        next_action_route=next_action_route,
        product_spec_routing_action=product_spec_routing_action,
        pr_exists=False,
        base_ac_satisfied=True,
    )
    assert decision["early_exit"] is True
    assert decision["dispatch_step1"] is False
    assert decision["recommendation"]["pr"]["action"] == "none"
    assert decision["recommendation"]["pr"]["reason"] == "no_pr_created"
    assert decision["result"]["termination_reason"] == "already_satisfied"


def test_ac11_case2_dispatch_callback_never_invoked_when_early_exit_fires():
    """A stub implementation-worker dispatch callback must never be invoked
    once the choke point fires."""
    calls: list[str] = []

    def _dispatch_implementation_worker() -> None:
        calls.append("dispatched")

    decision = resolve_already_satisfied_early_exit_decision(
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=True,
    )
    if decision["dispatch_step1"]:
        _dispatch_implementation_worker()

    assert calls == [], "implementation-worker dispatch must not fire when already_satisfied early-exits"


# ---------------------------------------------------------------------------
# AC11 case (3): base_ac_satisfied=false -> ordinary implementation route.
# ---------------------------------------------------------------------------


def test_ac11_case3_base_not_satisfied_routes_ordinary_implementation():
    step5 = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(base_ac_satisfied=False),
            main_drift=_main_drift(),
        ),
    )
    assert step5.route == "continue_loop"

    early_exit = resolve_already_satisfied_early_exit_decision(
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=False,
    )
    assert early_exit["dispatch_step1"] is True
    assert early_exit["early_exit"] is False


# ---------------------------------------------------------------------------
# AC11 case (4): meaningful_pr_delta=true -> no PR close recommendation.
# ---------------------------------------------------------------------------


def test_ac11_case4_meaningful_delta_never_yields_close_recommendation():
    result = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(meaningful_pr_delta=True),
            main_drift=_main_drift(),
        ),
    )
    assert result.route == "continue_loop"
    assert result.selected_action is None


# ---------------------------------------------------------------------------
# AC11 case (5): evidence_base_sha stale/mismatch -> not already_satisfied.
# ---------------------------------------------------------------------------


def test_ac11_case5_stale_evidence_base_sha_never_yields_already_satisfied():
    mismatched = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(evidence_base_sha=SHA_STALE),
            main_drift=_main_drift(current_base_sha=SHA_OTHER),
        ),
    )
    assert mismatched.route == "continue_loop"

    # main_drift entirely absent: freshness is undecidable, co-occurrence
    # requirement fails closed to "not eligible" (not a schema error).
    no_main_drift = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(already_satisfied_evidence=_evidence()),
    )
    assert no_main_drift.route == "continue_loop"
    assert no_main_drift.fail_closed is False


# ---------------------------------------------------------------------------
# AC11 case (6): ordinary REQUEST_CHANGES unrelated to already_satisfied.
# ---------------------------------------------------------------------------


def test_ac11_case6_ordinary_request_changes_without_evidence_key_keeps_continue_loop():
    result = route_loop_verdict_v2(
        _reviewer_verdict(blockers=["fix the typo in the docstring"]),
        _live_mergeability(),
    )
    assert result.route == "continue_loop"
    assert result.fail_closed is False


# ---------------------------------------------------------------------------
# AC11 case (7): existing conflict/fail-closed/main-drift regressions hold.
# ---------------------------------------------------------------------------


def test_ac11_case7_conflict_precedes_already_satisfied_evidence():
    """A real conflict must still hard-stop even when already_satisfied's
    four conditions would otherwise all be satisfied."""
    result = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            mergeable="CONFLICTING",
            already_satisfied_evidence=_evidence(),
            main_drift=_main_drift(),
        ),
    )
    assert result.route == "conflict_hard_stop"


def test_ac11_case7_main_drift_hard_stop_precedes_already_satisfied_evidence():
    """A malformed/hard-stopping main_drift payload (stale allowed-paths
    snapshot, #2102) still hard-stops even when already_satisfied_evidence's
    own fields are individually well-formed and its evidence_base_sha
    matches main_drift.current_base_sha."""
    drifted_main_drift = {
        "current_base_sha": SHA_OTHER,
        "evidence_base_sha": SHA_STALE,  # != current_base_sha -> not "fresh"
        "allowed_paths_snapshot_base_sha": SHA_STALE,  # != current_base_sha -> hard stop
        "allowed_paths": ["some/path.py"],
        "latest_main_net_diff": [],
        "expected_old_sha": SHA_MAIN,
        "observed_old_sha": SHA_MAIN,
    }
    result = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(evidence_base_sha=SHA_OTHER),
            main_drift=drifted_main_drift,
        ),
    )
    assert result.route == "fail_closed"
    assert result.reason_code == "stale_allowed_paths_snapshot"


def test_ac11_case7_unknown_mergeability_gate_does_not_apply_to_request_changes():
    """The `mergeability_unknown` fail-closed gate is APPROVE-branch-only
    (pre-existing regression, #1860 Owner Decision). Adding
    already_satisfied support must not have accidentally pulled
    REQUEST_CHANGES through that unrelated APPROVE-only gate."""
    result = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            mergeable="UNKNOWN",
            already_satisfied_evidence=_evidence(),
            main_drift=_main_drift(),
        ),
    )
    assert result.route == "already_satisfied"


# ---------------------------------------------------------------------------
# AC11 case (8): already_satisfied never performs an auto-close mutation.
# ---------------------------------------------------------------------------


def test_ac11_case8_module_still_forbids_mutation_imports():
    """already_satisfied support must not have introduced any gh / git /
    network / subprocess import into the pure router module."""
    src = SCRIPTS_DIR / "route_loop_verdict_v2.py"
    tree = ast.parse(src.read_text())
    forbidden = {"subprocess", "socket", "urllib", "requests", "httpx", "os"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                assert name.split(".")[0] not in forbidden, f"Forbidden import: {name!r}"


def test_ac11_case8_already_satisfied_selected_action_is_pure_data():
    """The already_satisfied selected_action is inert structured data (no
    callables, no side-effecting handles) -- the route itself cannot
    perform a close mutation even if a careless caller invoked something on
    it."""
    result = route_loop_verdict_v2(
        _reviewer_verdict(),
        _live_mergeability(
            already_satisfied_evidence=_evidence(),
            main_drift=_main_drift(),
        ),
    )
    assert result.route == "already_satisfied"

    def _walk(value: Any) -> None:
        assert not callable(value) or isinstance(value, (str, bytes))
        if isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                _walk(v)

    _walk(dict(result.selected_action))

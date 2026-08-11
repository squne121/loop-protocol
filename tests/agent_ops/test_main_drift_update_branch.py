"""Integration tests binding route_loop_verdict_v2() main-drift routing
output to update_branch.py's request contract (Issue #2102 fix_delta).

Prior revision of this file only re-tested unrelated generic invariants
(_validate_request, PRODUCTION_POLL_MAX/INTERVAL) that never referenced
main_drift / scope_clean_reconciliation / evidence_epoch at all, despite the
file name implying main-drift coverage. It is rewritten here to actually
connect the two modules' contracts:

  - route_loop_verdict_v2()'s BEHIND + behind_fast_path main-drift route
    synthesizes a `selected_action` shape that update_branch.py's
    UpdateBranchRequest / _validate_request() must accept as-is (same
    expected_head_sha, same known caller label, same bounded poll --
    AC2's "fast path / reconciliation both use bounded update_branch poll,
    no unbounded poll or generic retry").
  - route_scope_clean_reconciliation (the non-BEHIND-fast-path branch) must
    NOT synthesize an update_branch action at all -- proving the routing
    separation between the two main-drift routes.

update_branch.py itself is unchanged in this PR (main's existing #1429 CAS
/ ancestry implementation already satisfies #2102's requirements per the
Issue's explicit DISPROVED/exclusion list); only this test file's coverage
is corrected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

UPDATE_BRANCH_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skills"
    / "implement-issue"
    / "scripts"
    / "update_branch.py"
)
_UB_SPEC = importlib.util.spec_from_file_location("update_branch_main_drift", UPDATE_BRANCH_SCRIPT)
assert _UB_SPEC and _UB_SPEC.loader
UPDATE_BRANCH_MODULE = importlib.util.module_from_spec(_UB_SPEC)
sys.modules[_UB_SPEC.name] = UPDATE_BRANCH_MODULE
_UB_SPEC.loader.exec_module(UPDATE_BRANCH_MODULE)

ROUTE_SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
)
sys.path.insert(0, str(ROUTE_SCRIPTS))
from route_loop_verdict_v2 import (  # noqa: E402
    ROUTE_SCOPE_CLEAN_RECONCILIATION,
    ROUTE_TO_UPDATE_BRANCH,
    route_loop_verdict_v2,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _verdict() -> dict[str, object]:
    return {"verdict": "APPROVE", "reviewed_head_sha": SHA_A, "blockers": [], "warnings": []}


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


def _live(merge_state_status: str, main_drift: dict[str, object]) -> dict[str, object]:
    return {
        "head_sha": SHA_A,
        "mergeable": "MERGEABLE",
        "merge_state_status": merge_state_status,
        "main_drift": main_drift,
    }


def test_given_behind_fast_path_main_drift_route_when_bridged_to_update_branch_then_request_validates():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(behind_fast_path_eligible=True)),
    )
    assert decision.route == ROUTE_TO_UPDATE_BRANCH
    action = decision.selected_action
    assert action["main_drift_strategy"] == "behind_fast_path"

    # The route's synthesized action must be directly consumable by
    # update_branch.py's request contract -- same expected_head_sha, and a
    # caller label update_branch.py actually knows about.
    request = UPDATE_BRANCH_MODULE.UpdateBranchRequest(
        pr_number=2118,
        repo=UPDATE_BRANCH_MODULE.CANONICAL_REPO,
        expected_head_sha=action["expected_head_sha"],
        caller="impl-review-loop.step-5",
    )
    assert UPDATE_BRANCH_MODULE._validate_request(request) is None

    # AC2: fast path and reconciliation both stay on the same bounded poll;
    # main-drift routing does not introduce a second, unbounded poll path.
    assert UPDATE_BRANCH_MODULE.PRODUCTION_POLL_MAX > 0
    assert UPDATE_BRANCH_MODULE.PRODUCTION_POLL_INTERVAL > 0


def test_given_scope_clean_reconciliation_route_when_checked_then_no_update_branch_action_is_synthesized():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(behind_fast_path_eligible=False)),
    )
    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.selected_action["kind"] == "scope_clean_reconciliation"
    # Must not accidentally carry an update_branch-shaped action -- the two
    # main-drift routes have disjoint mutation authority.
    assert "expected_head_sha" not in decision.selected_action
    assert "main_drift_strategy" not in decision.selected_action


def test_given_invalid_expected_head_when_update_branch_validates_then_no_poll_or_api_is_authorized():
    request = UPDATE_BRANCH_MODULE.UpdateBranchRequest(
        pr_number=1,
        repo=UPDATE_BRANCH_MODULE.CANONICAL_REPO,
        expected_head_sha="old",
        caller="impl-review-loop.step-5",
    )
    assert (
        UPDATE_BRANCH_MODULE._validate_request(request)
        == "expected_head_sha must be a full-length hexadecimal commit SHA"
    )

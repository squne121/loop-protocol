"""
Destination-state tests for Issue #1856 (evidence authority cutover, Phase 1),
adapted to the Issue #1873 route_loop_verdict_v2(reviewer_verdict,
live_mergeability, test_verdict=None) signature.

These tests assert that route_loop_verdict_v2():

- accepts an optional `test_verdict` argument (Issue #1873 signature: reviewer
  self-report was replaced by reviewer_verdict + live_mergeability, and
  test_verdict was kept as an optional diagnostics-only input rather than
  removed outright).
- AC1 (adapted): whatever `test_verdict` (or its absence) contains, it never
  changes the computed route. BEHIND routing is derived solely from
  `live_mergeability["merge_state_status"]`.
- AC2: derives BEHIND-routing consistency solely from
  `live_mergeability["merge_state_status"]`, without any branch_behind_main /
  test_verdict cross-check gating the outcome.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# parents[3] = .claude/skills/impl-review-loop (from fixtures/step5_routing_consumer/test_*.py)
IMPL_REVIEW_LOOP_DIR = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = IMPL_REVIEW_LOOP_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402

FIXTURE_DIR = Path(__file__).parent


def _load_fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_test_verdict_argument_is_diagnostics_only_and_optional():
    """AC1 (adapted for #1873): `test_verdict` is optional and never gates
    the route, regardless of whether it is omitted, null, or supplies a
    branch_behind_main value that disagrees with live merge_state_status."""
    reviewer_verdict = {
        "verdict": "APPROVE",
        "reviewed_head_sha": "abc123def456",
        "blockers": [],
    }
    live_mergeability = {
        "head_sha": "abc123def456",
        "mergeable": "MERGEABLE",
        "merge_state_status": "BEHIND",
    }

    result_omitted = route_loop_verdict_v2(reviewer_verdict, live_mergeability)
    result_none = route_loop_verdict_v2(reviewer_verdict, live_mergeability, test_verdict=None)
    result_disagreeing = route_loop_verdict_v2(
        reviewer_verdict, live_mergeability, test_verdict={"branch_behind_main": False},
    )

    for label, result in (
        ("omitted", result_omitted),
        ("none", result_none),
        ("disagreeing", result_disagreeing),
    ):
        assert result.route == "route_to_update_branch", (
            f"[{label}] test_verdict must not gate BEHIND routing; "
            f"got route={result.route!r} errors={result.errors}"
        )
        assert result.fail_closed is False


def test_behind_action_consistency_via_merge_state_status_only():
    """AC2: BEHIND routing consistency, derived solely from
    live_mergeability.merge_state_status.

    - BEHIND with no test_verdict.branch_behind_main confirmation still
      routes to update_branch (positive_behind_no_test_verdict.yml).
    - a test_verdict.branch_behind_main disagreeing with live BEHIND/CLEAN
      never overrides the live signal
      (positive_behind_test_verdict_false_ignored.yml /
      positive_clean_test_verdict_true_ignored.yml).
    """
    fx_no_test_verdict = _load_fixture("positive_behind_no_test_verdict.yml")
    result_no_test_verdict = route_loop_verdict_v2(
        fx_no_test_verdict["reviewer_verdict"],
        fx_no_test_verdict["live_mergeability"],
        test_verdict=fx_no_test_verdict.get("test_verdict"),
    )
    assert result_no_test_verdict.route == "route_to_update_branch", (
        f"Expected route_to_update_branch, got "
        f"route={result_no_test_verdict.route!r} errors={result_no_test_verdict.errors}"
    )
    assert result_no_test_verdict.fail_closed is False

    fx_behind_false = _load_fixture("positive_behind_test_verdict_false_ignored.yml")
    result_behind_false = route_loop_verdict_v2(
        fx_behind_false["reviewer_verdict"],
        fx_behind_false["live_mergeability"],
        test_verdict=fx_behind_false.get("test_verdict"),
    )
    assert result_behind_false.route == "route_to_update_branch", (
        f"Expected route_to_update_branch, got "
        f"route={result_behind_false.route!r} errors={result_behind_false.errors}"
    )

    fx_clean_true = _load_fixture("positive_clean_test_verdict_true_ignored.yml")
    result_clean_true = route_loop_verdict_v2(
        fx_clean_true["reviewer_verdict"],
        fx_clean_true["live_mergeability"],
        test_verdict=fx_clean_true.get("test_verdict"),
    )
    assert result_clean_true.route == "approved", (
        f"Expected approved, got "
        f"route={result_clean_true.route!r} errors={result_clean_true.errors}"
    )

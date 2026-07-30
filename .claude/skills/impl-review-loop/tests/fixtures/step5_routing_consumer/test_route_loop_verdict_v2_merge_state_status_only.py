"""
Destination-state tests for Issue #1856 (evidence authority cutover, Phase 1).

These tests assert that route_loop_verdict_v2():

- AC1: no longer accepts a `test_verdict` argument (the signature has been
  reduced to `route_loop_verdict_v2(loop_verdict)` only).
- AC2: derives BEHIND-routing consistency solely from
  `loop_verdict["mergeability"]["merge_state_status"]`, without any
  branch_behind_main / test_verdict cross-check. Both directions of the
  inconsistency (BEHIND without an update_branch action, and an
  update_branch action while not BEHIND) remain fail-closed.
"""

from __future__ import annotations

import inspect
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


def test_test_verdict_argument_removed():
    """AC1: route_loop_verdict_v2 no longer accepts a `test_verdict` parameter."""
    sig = inspect.signature(route_loop_verdict_v2)
    assert "test_verdict" not in sig.parameters, (
        f"route_loop_verdict_v2 must not accept 'test_verdict'; "
        f"got parameters={list(sig.parameters)}"
    )
    assert list(sig.parameters) == ["loop_verdict"], (
        f"route_loop_verdict_v2 must accept exactly one parameter "
        f"'loop_verdict'; got {list(sig.parameters)}"
    )

    # Calling with a legacy test_verdict kwarg must now raise TypeError
    # (proves the argument was actually removed, not merely undocumented).
    minimal_loop_verdict = {
        "verdict": "REQUEST_CHANGES",
        "merge_ready": False,
        "reviewed_head_sha": "abc123def456",
        "mergeability": {"merge_state_status": "CLEAN"},
        "required_auto_actions": [],
    }
    try:
        route_loop_verdict_v2(minimal_loop_verdict, test_verdict={"branch_behind_main": True})  # type: ignore[call-arg]
    except TypeError:
        pass
    else:
        raise AssertionError(
            "route_loop_verdict_v2(loop_verdict, test_verdict=...) must raise "
            "TypeError; the test_verdict argument must be fully removed (AC1)."
        )


def test_behind_action_consistency_without_branch_behind_main():
    """AC2: BEHIND/update_branch action consistency, without branch_behind_main.

    Exercises both fail-closed directions using only
    loop_verdict.mergeability.merge_state_status:

    - BEHIND with an empty required_auto_actions list (missing update_branch
      action) → fail_closed / behind_without_update_branch_action.
    - non-BEHIND (CLEAN) with an update_branch action present
      → fail_closed / update_branch_without_behind_status.
    """
    fx_missing_action = _load_fixture("negative_behind_missing_branch_behind_main.yml")
    result_missing_action = route_loop_verdict_v2(fx_missing_action["loop_verdict"])
    assert result_missing_action.route == "fail_closed", (
        f"Expected fail_closed for BEHIND without update_branch action, "
        f"got route={result_missing_action.route!r} errors={result_missing_action.errors}"
    )
    assert result_missing_action.fail_closed is True
    assert result_missing_action.reason_code == fx_missing_action["expected"]["reason_code"], (
        f"Expected reason_code={fx_missing_action['expected']['reason_code']!r}, "
        f"got {result_missing_action.reason_code!r}"
    )

    fx_action_not_behind = _load_fixture("negative_behind_branch_behind_main_key_absent.yml")
    result_action_not_behind = route_loop_verdict_v2(fx_action_not_behind["loop_verdict"])
    assert result_action_not_behind.route == "fail_closed", (
        f"Expected fail_closed for update_branch action while not BEHIND, "
        f"got route={result_action_not_behind.route!r} errors={result_action_not_behind.errors}"
    )
    assert result_action_not_behind.fail_closed is True
    assert result_action_not_behind.reason_code == fx_action_not_behind["expected"]["reason_code"], (
        f"Expected reason_code={fx_action_not_behind['expected']['reason_code']!r}, "
        f"got {result_action_not_behind.reason_code!r}"
    )

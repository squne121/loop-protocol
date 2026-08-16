#!/usr/bin/env python3
"""
Unit tests for Issue #2207 AC5-AC7: cleanup-aware review timeout budget
formula (`contract_readiness_check.derive_review_budget()`).
"""

import sys
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import contract_readiness_check as formula  # noqa: E402


def test_low_count_compatibility_preserves_existing_budget():
    """AC5: `N <= 2` (`effective_n = max(2, N)`) preserves the current
    production budget values (350s / 400s / 480s / 520s) exactly."""
    for n in (0, 1, 2):
        budget = formula.derive_review_budget(n)
        assert budget.effective_n == 2
        assert budget.baseline_aggregate_seconds == 350
        assert budget.readiness_wrapper_seconds == 400
        assert budget.per_attempt_seconds == 480
        assert budget.total_seconds == 520


def test_cleanup_aware_growth_scales_with_occurrence_count():
    """AC6: `N >= 3` scales `baseline_aggregate` using
    `N * (150 + 15) + 20` (command cap + SIGTERM/SIGKILL/pipe-drain cleanup
    tail), and the downstream wrapper/per-attempt/total values grow
    monotonically with it (not just the command cap alone)."""
    budget_n3 = formula.derive_review_budget(3)
    assert budget_n3.effective_n == 3
    assert budget_n3.baseline_aggregate_seconds == 3 * (150 + 15) + 20 == 515
    assert budget_n3.readiness_wrapper_seconds == 30 + 515 + 20 == 565
    assert budget_n3.per_attempt_seconds == 30 + 565 + 30 + 20 == 645
    assert budget_n3.total_seconds == 645 + 40 == 685

    budget_n10 = formula.derive_review_budget(10)
    assert budget_n10.baseline_aggregate_seconds == 10 * (150 + 15) + 20 == 1670

    # Monotonic growth as N increases.
    assert budget_n10.baseline_aggregate_seconds > budget_n3.baseline_aggregate_seconds
    assert budget_n10.readiness_wrapper_seconds > budget_n3.readiness_wrapper_seconds
    assert budget_n10.per_attempt_seconds > budget_n3.per_attempt_seconds
    assert budget_n10.total_seconds > budget_n3.total_seconds


def test_policy_ceiling_rejects_before_subprocess_launch():
    """AC7: `N` exceeding the fixed policy ceiling raises the typed,
    non-retryable `VerificationBudgetExceedsPolicyError`
    (`verification_budget_exceeds_policy`) instead of returning a budget --
    the caller MUST reject before launching any subprocess."""
    cap = formula.MAX_VC_EXECUTION_SLOTS

    # At the ceiling: still accepted.
    budget = formula.derive_review_budget(cap, policy_cap=cap)
    assert budget.n == cap

    # One over the ceiling: typed rejection, no budget computed/returned.
    with pytest.raises(formula.VerificationBudgetExceedsPolicyError) as excinfo:
        formula.derive_review_budget(cap + 1, policy_cap=cap)

    assert excinfo.value.error_code == "verification_budget_exceeds_policy"
    assert excinfo.value.n == cap + 1
    assert excinfo.value.policy_cap == cap

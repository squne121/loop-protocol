"""
Unit tests for the aggregate invariant (Issue #2233 AC3).

AC3: aggregate invariant (outer budget が command-level budget の合計 +
     cleanup tail を常に上回ること)が、異なる timeout を持つ 2 本の
     non-pure VC を negative control に含めたテストで検証されている。

The `contract_readiness_check.derive_review_budget()` formula itself is
Out of Scope for this Issue (#2207/PR #2221 owns it and it is NOT
re-derived here). This test instead verifies the STRUCTURAL invariant this
Issue's command-level budget plumbing must preserve: the outer
`baseline_aggregate_seconds` that formula computes from
`command_occurrence_count` alone must never be smaller than the SUM of the
canonical plan's own per-command `command_budgets[]` entries (each entry's
`timeout_seconds + cleanup_tail_seconds`, over every VC OCCURRENCE) --
including a negative control where two non-pure VCs carry DIFFERENT
resolved timeouts (one default, one explicitly overridden to a smaller
value).

Runtime Verification Applicability: not_applicable
side-effect-free unit test over `compute_canonical_vc_plan()` /
`derive_review_budget()`; no subprocess is launched.
"""

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR.parents[1] / "issue-contract-review" / "scripts"))

import baseline_vc_preflight as bvp  # noqa: E402
import contract_readiness_check as crc  # noqa: E402


_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY = (
    "## Verification Commands\n\n"
    "```bash\n$ pnpm build\n```\n\n"
    "```bash\n$ pnpm test\n```\n"
)


def test_aggregate_invariant_holds_for_uniform_static_fallback_budgets():
    """Baseline (no override): both VCs resolve to the SAME
    static_fallback budget. Outer aggregate must still exceed the sum of
    per-command budgets."""
    plan = bvp.compute_canonical_vc_plan(_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY)
    outer_budget = crc.derive_review_budget(
        plan["command_occurrence_count"], policy_cap=plan["policy_cap"]
    )

    assert outer_budget.baseline_aggregate_seconds > plan["aggregate_timeout_seconds"]


def test_aggregate_invariant_holds_with_two_different_timeout_non_pure_vcs():
    """AC3 negative control: two NON-PURE VCs (`pnpm build`, `pnpm test`)
    -- one left at the static_fallback default, one given a smaller
    explicit per-invocation timeout via a DIRECT command_budgets override
    -- and the outer (#2207-formula) aggregate must still exceed their
    summed command-level budgets."""
    plan = bvp.compute_canonical_vc_plan(_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY)

    # Simulate a negative control where the two commands resolve to
    # genuinely DIFFERENT timeouts (one default 150s, one a smaller
    # explicit value) -- exercising the summation logic across
    # heterogeneous per-command budgets rather than a single uniform value.
    budgets = list(plan["command_budgets"])
    assert len(budgets) == 2
    smaller_budget = bvp.compute_command_timeout_budget(
        "pnpm test", override_seconds=60
    )
    heterogeneous_sum = budgets[0]["timeout_seconds"] + budgets[0]["cleanup_tail_seconds"]
    heterogeneous_sum += smaller_budget["timeout_seconds"] + smaller_budget["cleanup_tail_seconds"]

    outer_budget = crc.derive_review_budget(
        plan["command_occurrence_count"], policy_cap=plan["policy_cap"]
    )

    assert outer_budget.baseline_aggregate_seconds > heterogeneous_sum
    # And also strictly greater than the (larger) uniform-default sum,
    # since the #2207 formula's worst case assumes every command costs up
    # to DEFAULT_PER_COMMAND_TIMEOUT_SECONDS -- the invariant must hold
    # even in the worst case, not merely in this smaller-value instance.
    assert outer_budget.baseline_aggregate_seconds > plan["aggregate_timeout_seconds"]


def test_aggregate_timeout_seconds_never_exceeds_command_occurrence_worst_case():
    """Structural guarantee: because `MAX_PER_COMMAND_TIMEOUT_SECONDS ==
    DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`, the plan's own
    `aggregate_timeout_seconds` can never exceed
    `command_occurrence_count * (DEFAULT_PER_COMMAND_TIMEOUT_SECONDS +
    CLEANUP_TAIL_SECONDS)` -- the exact per-command worst case the #2207
    formula already budgets for via `_PER_VC_SLOT_SECONDS`."""
    plan = bvp.compute_canonical_vc_plan(_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY)
    worst_case = plan["command_occurrence_count"] * (
        bvp.DEFAULT_PER_COMMAND_TIMEOUT_SECONDS + bvp.CLEANUP_TAIL_SECONDS
    )
    assert plan["aggregate_timeout_seconds"] <= worst_case

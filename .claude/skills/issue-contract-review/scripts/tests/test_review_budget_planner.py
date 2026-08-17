#!/usr/bin/env python3
"""
Unit tests for Issue #2207 AC1-AC4: canonical VC occurrence planner
(`baseline_vc_preflight.compute_canonical_vc_plan()`).
"""

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import hashlib  # noqa: E402

import baseline_vc_preflight as planner  # noqa: E402


def _body(vc_block: str) -> str:
    return f"""## Verification Commands

```bash
{vc_block}
```
"""


def test_canonical_plan_binds_body_sha256():
    """AC1: canonical plan is a side-effect-free function returning a dict
    binding `body_sha256` (computed over the EXACT pinned body bytes) plus
    `parser_contract_version` / `command_occurrence_count` /
    `launch_upper_bound` / `per_command_timeout_seconds` / `max_workers` /
    `policy_cap`."""
    body = _body("$ rg -q pattern_ac1_2207 .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py")

    plan = planner.compute_canonical_vc_plan(body)

    assert plan["body_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert plan["parser_contract_version"] == planner.VC_PLAN_PARSER_CONTRACT_VERSION
    assert plan["command_occurrence_count"] == 1
    assert plan["launch_upper_bound"] == 1
    assert plan["per_command_timeout_seconds"] > 0
    assert plan["max_workers"] >= 1
    assert plan["policy_cap"] == planner.MAX_VC_EXECUTION_SLOTS

    # Side-effect-free: calling twice with the same body yields an identical plan.
    assert planner.compute_canonical_vc_plan(body) == plan


def test_conservative_count_counts_duplicate_non_pure_occurrences():
    """AC2: `command_occurrence_count` counts EVERY occurrence of a duplicate
    non-pure command (e.g. `uv run --locked pytest ...`), not a
    unique/dedup-collapsed count -- non-pure commands are never
    dedup-replayed by the executor."""
    body = _body(
        "\n".join(
            [
                "$ uv run --locked pytest .claude/skills/issue-contract-review/scripts/tests -q",
                "$ uv run --locked pytest .claude/skills/issue-contract-review/scripts/tests -q",
                "$ uv run --locked pytest .claude/skills/issue-contract-review/scripts/tests -q",
            ]
        )
    )

    plan = planner.compute_canonical_vc_plan(body)

    assert plan["command_occurrence_count"] == 3
    # Non-pure: never dedup-replayed -> every occurrence is a separate launch.
    assert plan["launch_upper_bound"] == 3


def test_pure_coverage_included_in_budget():
    """AC3: distinct pure commands (`rg` / exact `test -f|-d|-s`) are
    included in `command_occurrence_count`, not silently excluded from the
    budget-relevant total (the residual-risk gap the OWNER review flagged:
    pure VCs were previously uncounted)."""
    body = _body(
        "\n".join(
            [
                "$ rg -q pattern_a_2207 .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py",
                "$ test -f .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py",
            ]
        )
    )

    plan = planner.compute_canonical_vc_plan(body)

    assert plan["command_occurrence_count"] == 2
    assert plan["launch_upper_bound"] == 2


def test_state_barrier_separates_repeated_pure_command_launches():
    """AC4: the SAME pure command occurring before AND after a non-pure
    barrier command counts as two SEPARATE launches (matching the executor's
    `_state_epoch` semantics: dedup only ever replays within a single
    epoch), while occurring TWICE with no barrier between collapses to a
    single launch (an actual dedup-replay, not a second subprocess)."""
    pure_cmd = "rg -q pattern_barrier_2207 .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"
    barrier_cmd = "uv run --locked pytest .claude/skills/issue-contract-review/scripts/tests -q"

    # No barrier between the two identical pure observations: same epoch,
    # dedup-replayed -> 1 launch.
    same_epoch_body = _body("\n".join([f"$ {pure_cmd}", f"$ {pure_cmd}"]))
    same_epoch_plan = planner.compute_canonical_vc_plan(same_epoch_body)
    assert same_epoch_plan["command_occurrence_count"] == 2
    assert same_epoch_plan["launch_upper_bound"] == 1

    # A non-pure barrier between the two identical pure observations: new
    # epoch after the barrier -> both pure launches count separately.
    barrier_body = _body("\n".join([f"$ {pure_cmd}", f"$ {barrier_cmd}", f"$ {pure_cmd}"]))
    barrier_plan = planner.compute_canonical_vc_plan(barrier_body)
    assert barrier_plan["command_occurrence_count"] == 3
    # 1 (pure, epoch 0) + 1 (barrier, epoch 0->1) + 1 (pure, epoch 1) = 3
    assert barrier_plan["launch_upper_bound"] == 3

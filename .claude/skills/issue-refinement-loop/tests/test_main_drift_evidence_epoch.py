from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plan_refinement_loop.py"
SPEC = importlib.util.spec_from_file_location("plan_refinement_loop_main_drift", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SHA_A = "a" * 40
SHA_B = "b" * 40
ISSUE_BODY = """
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
```

## Parent Issue
#1

## Parent Goal Ref
- Goal: test

## Current Validated Scope
- docs/dev/

## Outcome
Current-base evidence is required.

## In Scope
- docs/dev/workflow.md

## Out of Scope
- none

## Remaining Parent Gaps
- none

## Acceptance Criteria
- [ ] AC1: preserve current-base evidence epoch

## Verification Commands
```bash
echo hi
```

## Allowed Paths
- docs/dev/workflow.md

## Stop Conditions
- none

## Required Skills
- none
"""


def _context(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "current_base_sha": SHA_B,
        "evidence_base_sha": SHA_A,
        "allowed_paths_snapshot_base_sha": SHA_B,
        "allowed_paths": ["docs/dev/"],
        "latest_main_net_diff": ["docs/dev/workflow.md"],
        "expected_head_sha": SHA_A,
        "observed_head_sha": SHA_A,
        "expected_old_sha": SHA_B,
        "observed_old_sha": SHA_B,
    }
    value.update(extra)
    return value


def _planner_input(context: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": 2102,
            "title": "main drift evidence epoch",
            "body": ISSUE_BODY,
            "labels": [],
        },
        "comments": [],
        "known_context": {"main_drift": context},
        "now": "2026-08-12T00:00:00+00:00",
    }


def test_given_drift_when_refinement_classifies_then_old_evidence_is_not_reusable():
    result = MODULE.classify_refinement_evidence_epoch(_context())

    assert result["route"] == "scope_clean_reconciliation"
    assert result["reusable_evidence"] == {
        "snapshot": None,
        "ci": None,
        "review": None,
    }
    assert result["mutation_owner"] == "refinement"


def test_given_drift_when_planner_runs_then_current_base_rebind_is_in_actual_output():
    plan, exit_code = MODULE.plan_refinement_loop(_planner_input(_context()))
    decision = plan["decisions"]["main_drift_evidence_epoch"]

    assert exit_code == 0
    assert decision["route"] == "scope_clean_reconciliation"
    assert decision["evidence_epoch"]["base_sha"] == SHA_B
    assert decision["evidence_epoch"]["implementation_iteration_delta"] == 0
    assert decision["reverify"] == {"snapshot": True, "ci": True, "review": True}


def test_given_stale_scope_snapshot_when_refinement_plans_then_it_fails_closed():
    plan, exit_code = MODULE.plan_refinement_loop(
        _planner_input(_context(allowed_paths_snapshot_base_sha=SHA_A))
    )

    assert exit_code == 0
    assert plan["fail_closed"]["required"] is True
    assert plan["fail_closed"]["reason_codes"] == ["stale_allowed_paths_snapshot"]

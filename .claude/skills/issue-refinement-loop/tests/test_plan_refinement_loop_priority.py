"""
Tests for ISSUE_EXECUTION_DECISION_V1 in plan_refinement_loop.py output (#1677).

Covers:
- AC1: relations[] field present (machine-readable, array)
- AC2: execution.state / execution.predecessors / execution.defer_reason present
- AC3: downstream_policy block present with fixed values
- AC6: execution.state == selected fixture
- AC7: execution.state == blocked with non-empty predecessors fixture
- AC8: relations containing relation_type duplicate/absorb fixture
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import plan_refinement_loop as prl  # noqa: E402


REQUIRED_SECTIONS_BODY = """
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
- foo

## Outcome
Some outcome.

## In Scope
- foo.py

## Out of Scope
- bar.py

## Remaining Parent Gaps
- none

## Acceptance Criteria
- [ ] AC1: foo

## Verification Commands
```bash
echo hi
```

## Allowed Paths
- foo.py

## Stop Conditions
- none

## Required Skills
- none
"""


def _planner_input(issue_number: int, known_context: dict | None = None) -> dict:
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": "test",
            "body": REQUIRED_SECTIONS_BODY,
            "labels": [],
        },
        "comments": [],
        "known_context": known_context,
        "now": "2026-07-25T00:00:00Z",
    }


def _run(issue_number: int, known_context: dict | None = None) -> dict:
    plan, exit_code = prl.plan_refinement_loop(_planner_input(issue_number, known_context))
    assert exit_code == 0, plan
    assert plan["fail_closed"]["required"] is False, plan["fail_closed"]
    return plan


def test_relations_field_present():
    """AC1: ISSUE_EXECUTION_DECISION_V1.relations is a machine-readable array."""
    plan = _run(1001)
    decision = plan["issue_execution_decision"]
    assert isinstance(decision["relations"], list)
    assert decision["schema_version"] == "ISSUE_EXECUTION_DECISION_V1"


def test_execution_state_fields_present():
    """AC2: execution.state / predecessors / defer_reason are present."""
    plan = _run(1002)
    execution = plan["issue_execution_decision"]["execution"]
    assert execution["state"] in ("selected", "deferred", "blocked", "duplicate")
    assert isinstance(execution["predecessors"], list)
    assert "defer_reason" in execution


def test_downstream_policy_present():
    """AC3: downstream_policy block with fixed values is present."""
    plan = _run(1003)
    policy = plan["issue_execution_decision"]["downstream_policy"]
    assert policy == {
        "semantic_reclassification": "forbidden",
        "freshness_validation": "required",
        "stale_action": "rerun_issue_refinement",
    }


def test_execution_state_selected_fixture():
    """AC6: no scope-rollup collisions -> execution.state == selected."""
    plan = _run(1004)
    decision = plan["issue_execution_decision"]
    assert decision["execution"]["state"] == "selected"
    assert decision["execution"]["predecessors"] == []
    assert decision["execution"]["defer_reason"] is None
    assert prl.validate_issue_execution_decision(decision) == []


def test_execution_state_blocked_fixture():
    """AC7: open predecessor via scope-rollup ordering_constraint -> blocked."""
    scope_rollup = {
        "schema_version": 2,
        "input": {"completeness": "full", "warnings": []},
        "candidates": [
            {
                "kind": "issue",
                "number": 9001,
                "state": "OPEN",
                "signals": ["same_parent_issue"],
                "suggested_action": "keep_separate_with_reason",
                "ordering_constraint": "sequential_required",
            }
        ],
    }
    plan = _run(1005, known_context={"scope_rollup_result": scope_rollup})
    decision = plan["issue_execution_decision"]
    assert decision["execution"]["state"] == "blocked"
    assert decision["execution"]["predecessors"] == [9001]
    assert decision["execution"]["defer_reason"]
    assert prl.validate_issue_execution_decision(decision) == []


def test_relations_duplicate_fixture():
    """AC8: merge_into_current_pr candidate -> relation_type duplicate."""
    scope_rollup = {
        "schema_version": 2,
        "input": {"completeness": "full", "warnings": []},
        "candidates": [
            {
                "kind": "issue",
                "number": 9002,
                "state": "OPEN",
                "signals": ["shared_dedupe_key"],
                "suggested_action": "merge_into_current_pr",
                "ordering_constraint": "parallel_ok",
            }
        ],
    }
    plan = _run(1006, known_context={"scope_rollup_result": scope_rollup})
    decision = plan["issue_execution_decision"]
    relation_types = {r["relation_type"] for r in decision["relations"]}
    assert "duplicate" in relation_types
    assert decision["execution"]["state"] == "duplicate"
    assert prl.validate_issue_execution_decision(decision) == []

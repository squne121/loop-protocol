"""
Tests for ISSUE_EXECUTION_DECISION_V1 reaching LOOP_HANDOFF_RESULT_V1 and the
termination report with the same digest as LOOP_STATE_V1 (#1677 AC5).

Chain under test (all production functions, no mocking of the digest itself):
    plan_refinement_loop.plan_refinement_loop()
      -> issue_execution_decision.identity.collection_digest
    build_loop_state.build_loop_state()
      -> loop_state["issue_execution_decision"].identity.collection_digest (same)
    build_loop_state.project_issue_execution_decision_ref()
      -> issue_execution_decision_ref.collection_digest (same)
    render_termination_report.render() with loop_handoff.issue_execution_decision_ref
      -> marker YAML block schema-valid + same digest byte-for-byte
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import plan_refinement_loop as prl  # noqa: E402
import build_loop_state as bls  # noqa: E402
import render_termination_report as rtr  # noqa: E402


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


def _plan(issue_number: int) -> dict:
    input_data = {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": "test",
            "body": REQUIRED_SECTIONS_BODY,
            "labels": [],
        },
        "comments": [],
        "known_context": None,
        "now": "2026-07-25T00:00:00Z",
    }
    plan, exit_code = prl.plan_refinement_loop(input_data)
    assert exit_code == 0
    assert plan["fail_closed"]["required"] is False
    return plan


def _extract_marker_yaml_block(body: str) -> dict:
    pattern = (
        r"^<!-- LOOP_HANDOFF_RESULT_V1 -->" + "\n"
        r"(`{3,}|~{3,})yaml" + "\n"
        r"(.*?)" + "\n"
        r"\1\s*$"
    )
    m = re.search(pattern, body, re.DOTALL | re.MULTILINE)
    assert m is not None, "LOOP_HANDOFF_RESULT_V1 marker block not found in body:\n" + body
    return yaml.safe_load(m.group(2))


def _base_loop_handoff() -> dict:
    return {
        "status": "impl_ready",
        "routing_action": "run_impl_review_loop",
        "contract_review": {
            "status": "go",
            "gate_result": "fresh_go",
            "latest_comment_url": "https://github.com/o/r/issues/1677#issuecomment-1",
            "generated_at": "2026-07-25T00:00:00Z",
            "body_sha256": "sha256:" + "a" * 64,
        },
        "metadata": {"title_prefix_ready": True, "phase_label_ready": True},
        "auto_fixes": {"result": "auto_fixed", "required": [], "skipped": []},
        "blockers": [],
        "permissions": {"unavailable": []},
        "generated_at": "2026-07-25T00:00:00Z",
    }


def test_loop_state_issue_execution_decision_matches_planner_digest():
    """AC5 (partial): LOOP_STATE_V1 carries the same digest the planner emitted."""
    plan = _plan(2001)
    review = {"VERDICT": "approve", "issue_number": 2001}
    loop_state, blocked, _ = bls.build_loop_state(plan, review, issue_number=2001, iteration=0)
    assert blocked == []
    assert loop_state is not None
    assert (
        loop_state["issue_execution_decision"]["identity"]["collection_digest"]
        == plan["issue_execution_decision"]["identity"]["collection_digest"]
    )


def test_project_issue_execution_decision_ref_matches_digest():
    """project_issue_execution_decision_ref() preserves the collection_digest."""
    plan = _plan(2002)
    ref = bls.project_issue_execution_decision_ref(plan["issue_execution_decision"])
    assert ref is not None
    assert ref["schema_version"] == "ISSUE_EXECUTION_DECISION_V1"
    assert ref["target_issue_number"] == 2002
    assert ref["collection_digest"] == plan["issue_execution_decision"]["identity"]["collection_digest"]


def test_project_issue_execution_decision_ref_none_when_absent():
    assert bls.project_issue_execution_decision_ref(None) is None
    assert bls.project_issue_execution_decision_ref({"not": "a decision"}) is None


def test_loop_handoff_ref_schema_valid():
    """The ref produced by build_loop_state.py validates against
    schemas/loop_handoff_result_v1.json's issue_execution_decision_ref."""
    plan = _plan(2003)
    ref = bls.project_issue_execution_decision_ref(plan["issue_execution_decision"])
    loop_handoff = _base_loop_handoff()
    loop_handoff["issue_execution_decision_ref"] = ref

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "loop_handoff_result_v1.json"
    import json as _json

    schema = _json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(instance={"LOOP_HANDOFF_RESULT_V1": loop_handoff}, schema=schema)


def test_termination_report_marker_carries_same_digest_end_to_end():
    """
    AC5 (production test, PR #1767 owner review P0-4): the full chain
    (planner -> LOOP_STATE -> termination report marker) reaches the
    termination report with the exact same collection_digest.

    issue_execution_decision_ref is NOT assembled by this test -- it is
    passed as data["issue_execution_decision"] (the full LOOP_STATE
    decision) and render_termination_report.normalize_input() auto-generates
    and attaches the ref via the shared validate_issue_execution_decision /
    project_issue_execution_decision_ref API (production E2E, not a
    test-assembled substitute for missing orchestration).
    """
    plan = _plan(2004)
    review = {"VERDICT": "approve", "issue_number": 2004}
    loop_state, blocked, _ = bls.build_loop_state(plan, review, issue_number=2004, iteration=0)
    assert blocked == []

    loop_handoff = _base_loop_handoff()

    data = {
        "termination_reason": "approved",
        "issue_number": 2004,
        "iteration": 0,
        "loop_handoff": loop_handoff,
        "issue_execution_decision": loop_state["issue_execution_decision"],
    }
    result = rtr.render(data)
    assert result["publishable"] is True

    parsed = _extract_marker_yaml_block(result["body"])
    parsed_ref = parsed["LOOP_HANDOFF_RESULT_V1"]["issue_execution_decision_ref"]

    expected_digest = plan["issue_execution_decision"]["identity"]["collection_digest"]
    assert parsed_ref["collection_digest"] == expected_digest
    assert parsed_ref["target_issue_number"] == 2004

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "loop_handoff_result_v1.json"
    import json as _json

    schema = _json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = list(validator.iter_errors({"LOOP_HANDOFF_RESULT_V1": parsed["LOOP_HANDOFF_RESULT_V1"]}))
    assert errors == [], f"schema validation errors: {errors}"


def test_termination_report_rejects_invalid_issue_execution_decision():
    """
    PR #1767 owner review (P0-4 negative test): render_termination_report.py
    must fail closed (not silently drop the ref) when given a malformed
    issue_execution_decision, rather than trusting caller-provided shape.
    """
    loop_handoff = _base_loop_handoff()
    data = {
        "termination_reason": "approved",
        "issue_number": 2005,
        "iteration": 0,
        "loop_handoff": loop_handoff,
        "issue_execution_decision": {"schema_version": "not-a-real-schema"},
    }
    with pytest.raises(rtr.InputValidationError):
        rtr.render(data)

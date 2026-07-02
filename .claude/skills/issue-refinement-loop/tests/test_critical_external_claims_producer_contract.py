#!/usr/bin/env python3
"""
test_critical_external_claims_producer_contract.py

AC1: plan_refinement_loop.py's _extract_critical_external_claims() output
(as surfaced via decisions.web_research_policy.critical_external_claims) must
remain an ExternalClaim[] object array as defined by
schemas/refinement_loop_plan_v1.json. The producer's return type must NOT be
changed to string[] by this issue (#1277); only build_loop_state.py projects
the object array down to string[] at the LOOP_STATE_V1 boundary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).parent.parent
SCRIPT = SKILL_ROOT / "scripts" / "plan_refinement_loop.py"
SCHEMA_PATH = SKILL_ROOT / "schemas" / "refinement_loop_plan_v1.json"
FIXTURE_MD = SKILL_ROOT / "tests" / "fixtures" / "positive" / "critical_external_claim_in_vc.md"

EXTERNAL_CLAIM_REQUIRED_KEYS = {"claim", "affects", "source_hint"}


def _run_planner(input_data: dict[str, Any]) -> dict[str, Any]:
    input_json = json.dumps(input_data, ensure_ascii=False)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=input_json,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"planner exited with {result.returncode}:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_planner_critical_external_claims_remain_external_claim_objects():
    """AC1: producer output for critical_external_claims stays ExternalClaim[] object[]."""
    assert FIXTURE_MD.exists(), f"Missing fixture: {FIXTURE_MD}"
    body = FIXTURE_MD.read_text(encoding="utf-8")

    input_data = {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": 2,
            "title": "Test Issue: critical_external_claim_in_vc",
            "body": body,
            "labels": [],
        },
        "comments": None,
        "known_context": None,
        "now": "2025-05-25T12:00:00+00:00",
    }

    output = _run_planner(input_data)
    claims = output["decisions"]["web_research_policy"]["critical_external_claims"]

    # Non-empty (this fixture is designed to trigger the keyword-based extractor).
    assert claims, "Expected non-empty critical_external_claims for this fixture"

    # AC1: each element must be an ExternalClaim object (not a plain string).
    for claim in claims:
        assert isinstance(claim, dict), (
            f"critical_external_claims element must remain an object, got {type(claim).__name__}: {claim!r}"
        )
        assert set(claim.keys()) == EXTERNAL_CLAIM_REQUIRED_KEYS, (
            f"ExternalClaim object must have exactly {EXTERNAL_CLAIM_REQUIRED_KEYS}, got {set(claim.keys())}"
        )
        assert isinstance(claim["claim"], str) and claim["claim"], (
            "ExternalClaim.claim must be a non-empty string"
        )
        assert claim["affects"] in {"Outcome", "InScope", "AC", "VC", "StopCondition"}
        assert claim["source_hint"] is None or isinstance(claim["source_hint"], str)

    # Cross-check against the ExternalClaim schema definition directly.
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    external_claim_schema = schema["definitions"]["ExternalClaim"]
    assert external_claim_schema["required"] == ["claim", "affects", "source_hint"]
    assert external_claim_schema["type"] == "object"

    # Full REFINEMENT_LOOP_PLAN_V1 output validates against the schema (jsonschema).
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(output))
    assert errors == [], f"Schema validation errors: {errors}"

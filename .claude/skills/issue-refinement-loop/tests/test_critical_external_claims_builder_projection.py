#!/usr/bin/env python3
"""
test_critical_external_claims_builder_projection.py

AC2: build_loop_state.py projects ExternalClaim[] (object[]) into string[]
for LOOP_STATE_V1.web_research_policy.critical_external_claims, keeping only
the 'claim' text.

AC3: A non-empty critical_external_claims (ExternalClaim[]) produced by the
real plan_refinement_loop.py, when fed into build_loop_state.py, results in a
LOOP_STATE_V1 that validates against loop_state.schema.json without error.

AC4: Elements that cannot be projected (non-object item, missing 'claim',
empty 'claim', non-string 'claim') are rejected fail-closed
(status=invalid + errors[]), never silently stringified or dropped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).parent.parent
BUILD_SCRIPT = SKILL_ROOT / "scripts" / "build_loop_state.py"
PLANNER_SCRIPT = SKILL_ROOT / "scripts" / "plan_refinement_loop.py"
SCHEMA_PATH = SKILL_ROOT / "schemas" / "loop_state.schema.json"
FIXTURE_DIR = SKILL_ROOT / "tests" / "fixtures" / "loop_state_builder"
PLANNER_FIXTURE_MD = (
    SKILL_ROOT / "tests" / "fixtures" / "positive" / "critical_external_claim_in_vc.md"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_builder(
    planner_file: Path,
    review_file: Path,
    issue_number: int,
    iteration: int,
    out: Path,
) -> subprocess.CompletedProcess:
    argv = [
        sys.executable,
        str(BUILD_SCRIPT),
        "--planner-result-file", str(planner_file),
        "--review-result-file", str(review_file),
        "--issue-number", str(issue_number),
        "--iteration", str(iteration),
        "--out", str(out),
    ]
    return subprocess.run(argv, capture_output=True, text=True)


def load_build_result(result: subprocess.CompletedProcess) -> dict[str, Any]:
    return json.loads(result.stdout)


def _base_planner_data() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "planner_approve.json").read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_real_planner_with_non_empty_claims(tmp_path: Path) -> Path:
    """Run the real plan_refinement_loop.py against a fixture that produces a
    non-empty critical_external_claims (ExternalClaim[] object array), and
    write its stdout to a file. Returns the path to the planner result file.
    """
    body = PLANNER_FIXTURE_MD.read_text(encoding="utf-8")
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
    result = subprocess.run(
        [sys.executable, str(PLANNER_SCRIPT)],
        input=json.dumps(input_data, ensure_ascii=False),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"planner exited with {result.returncode}:\n{result.stdout}\n{result.stderr}"
    )
    planner_output = json.loads(result.stdout)
    claims = planner_output["decisions"]["web_research_policy"]["critical_external_claims"]
    assert claims, "Fixture must produce a non-empty critical_external_claims for this test"
    assert all(isinstance(c, dict) for c in claims), "Producer must still emit ExternalClaim objects"

    planner_file = tmp_path / "planner_result_non_empty_claims.json"
    _write_json(planner_file, planner_output)
    return planner_file


def _review_approve_file() -> Path:
    return FIXTURE_DIR / "review_approve.json"


# ---------------------------------------------------------------------------
# AC2: projection to string[]
# ---------------------------------------------------------------------------


def test_build_loop_state_projects_external_claim_objects_to_string_claims(tmp_path):
    """AC2: ExternalClaim[] object array is projected to string[] of claim text only."""
    planner_data = _base_planner_data()
    planner_data["decisions"]["web_research_policy"]["required"] = True
    planner_data["decisions"]["web_research_policy"]["reason_code"] = "critical_external_spec_claim"
    planner_data["decisions"]["web_research_policy"]["critical_external_claims"] = [
        {"claim": "Verify against official API documentation", "affects": "VC", "source_hint": None},
        {"claim": "Validate auth migration behavior", "affects": "InScope", "source_hint": "comment_1"},
    ]

    planner_file = tmp_path / "planner_result.json"
    _write_json(planner_file, planner_data)

    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=planner_file,
        review_file=_review_approve_file(),
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0, f"Build failed:\n{result.stdout}\n{result.stderr}"
    build_result = load_build_result(result)
    assert build_result["status"] == "ok"

    loop_state = json.loads(out.read_text(encoding="utf-8"))
    projected = loop_state["web_research_policy"]["critical_external_claims"]

    assert projected == [
        "Verify against official API documentation",
        "Validate auth migration behavior",
    ]
    for item in projected:
        assert isinstance(item, str), f"Projected element must be a string, got {type(item).__name__}"


# ---------------------------------------------------------------------------
# AC3: end-to-end planner -> builder -> schema validation
# ---------------------------------------------------------------------------


def test_end_to_end_planner_to_builder_with_non_empty_critical_external_claims_validates_loop_state(
    tmp_path,
):
    """AC3: real planner output with non-empty critical_external_claims feeds
    into build_loop_state.py and produces a LOOP_STATE_V1 that passes
    loop_state.schema.json validation without exception.
    """
    planner_file = _run_real_planner_with_non_empty_claims(tmp_path)

    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=planner_file,
        review_file=_review_approve_file(),
        issue_number=2,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0, f"Build failed:\n{result.stdout}\n{result.stderr}"
    build_result = load_build_result(result)
    assert build_result["status"] == "ok"
    assert build_result["errors"] == [], f"Unexpected schema validation errors: {build_result['errors']}"

    loop_state = json.loads(out.read_text(encoding="utf-8"))

    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = list(validator.iter_errors(loop_state))
    assert errors == [], f"loop_state.schema.json validation errors: {errors}"

    claims = loop_state["web_research_policy"]["critical_external_claims"]
    assert claims, "Expected non-empty projected critical_external_claims"
    assert all(isinstance(c, str) for c in claims), "All projected claims must be strings"


# ---------------------------------------------------------------------------
# AC4: fail-closed rejection of unprojectable elements
# ---------------------------------------------------------------------------


def test_build_loop_state_rejects_external_claim_object_without_claim(tmp_path):
    """AC4: an ExternalClaim-like object missing 'claim' must fail-closed, not silently drop."""
    planner_data = _base_planner_data()
    planner_data["decisions"]["web_research_policy"]["required"] = True
    planner_data["decisions"]["web_research_policy"]["reason_code"] = "critical_external_spec_claim"
    planner_data["decisions"]["web_research_policy"]["critical_external_claims"] = [
        {"affects": "VC", "source_hint": None},  # missing 'claim'
    ]

    planner_file = tmp_path / "planner_result.json"
    _write_json(planner_file, planner_data)

    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=planner_file,
        review_file=_review_approve_file(),
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode != 0, "Builder must fail-closed on unprojectable claim"
    build_result = load_build_result(result)
    assert build_result["status"] == "invalid"
    assert build_result["errors"], "errors[] must be populated"
    assert not out.exists(), "Output file must not be written when projection fails"


def test_build_loop_state_rejects_external_claim_non_object_item(tmp_path):
    """AC4: a non-object item in critical_external_claims must fail-closed."""
    planner_data = _base_planner_data()
    planner_data["decisions"]["web_research_policy"]["required"] = True
    planner_data["decisions"]["web_research_policy"]["reason_code"] = "critical_external_spec_claim"
    planner_data["decisions"]["web_research_policy"]["critical_external_claims"] = [
        "this is a plain string, not an ExternalClaim object",
    ]

    planner_file = tmp_path / "planner_result.json"
    _write_json(planner_file, planner_data)

    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=planner_file,
        review_file=_review_approve_file(),
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode != 0, "Builder must fail-closed on non-object claim item"
    build_result = load_build_result(result)
    assert build_result["status"] == "invalid"
    assert build_result["errors"], "errors[] must be populated"
    assert not out.exists(), "Output file must not be written when projection fails"


def test_build_loop_state_rejects_external_claim_with_empty_claim_text(tmp_path):
    """AC4: an ExternalClaim object with an empty-string 'claim' must fail-closed."""
    planner_data = _base_planner_data()
    planner_data["decisions"]["web_research_policy"]["required"] = True
    planner_data["decisions"]["web_research_policy"]["reason_code"] = "critical_external_spec_claim"
    planner_data["decisions"]["web_research_policy"]["critical_external_claims"] = [
        {"claim": "   ", "affects": "VC", "source_hint": None},
    ]

    planner_file = tmp_path / "planner_result.json"
    _write_json(planner_file, planner_data)

    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=planner_file,
        review_file=_review_approve_file(),
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode != 0, "Builder must fail-closed on empty claim text"
    build_result = load_build_result(result)
    assert build_result["status"] == "invalid"
    assert not out.exists(), "Output file must not be written when projection fails"


def test_build_loop_state_rejects_external_claim_with_non_string_claim(tmp_path):
    """AC4: an ExternalClaim object with a non-string 'claim' must fail-closed (no silent stringify)."""
    planner_data = _base_planner_data()
    planner_data["decisions"]["web_research_policy"]["required"] = True
    planner_data["decisions"]["web_research_policy"]["reason_code"] = "critical_external_spec_claim"
    planner_data["decisions"]["web_research_policy"]["critical_external_claims"] = [
        {"claim": 12345, "affects": "VC", "source_hint": None},
    ]

    planner_file = tmp_path / "planner_result.json"
    _write_json(planner_file, planner_data)

    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=planner_file,
        review_file=_review_approve_file(),
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode != 0, "Builder must fail-closed on non-string claim (no silent stringify)"
    build_result = load_build_result(result)
    assert build_result["status"] == "invalid"
    assert not out.exists(), "Output file must not be written when projection fails"


def test_empty_critical_external_claims_still_projects_to_empty_list(tmp_path):
    """Regression: empty critical_external_claims (existing fixtures) still builds fine."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_approve.json",
        review_file=_review_approve_file(),
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0
    loop_state = json.loads(out.read_text(encoding="utf-8"))
    assert loop_state["web_research_policy"]["critical_external_claims"] == []

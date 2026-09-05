"""Issue #2498 (AC1/AC2): evaluators for the ``skill-invocation-runtime-smoke``
verification profile declared in ``docs/dev/extension-surface-runtime-policy.yaml``
(``procedure_steps_executed_in_declared_order`` / ``output_contract_schema_
fields_present``).

These tests are entirely hermetic: they call the pure evidence-evaluation
functions in ``run_worktree_agent_runtime_smoke.py`` directly with synthetic
fixture stream-json text -- no live Claude Code process is ever spawned
(Runtime Verification Applicability for AC1/AC2 themselves: not_applicable;
the one AC in this Issue that requires a real ``claude`` process is AC7,
covered by ``test_expect_marker_source_provenance.py``). Fixture/module-load
conventions mirror ``test_run_worktree_agent_runtime_smoke_causal_evidence.py``
(Issue #2183).

Per Issue #2498's Out of Scope: this runner does not interpret an arbitrary
SKILL.md Markdown Procedure -- AC1's ordered marker list and AC2's schema
path are both caller-supplied inputs, never derived by the runner itself
from SKILL.md text.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_MODULE_PATH = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2498_profile"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = smoke
_spec.loader.exec_module(smoke)


REVIEW_ISSUE_RESULT_V1_SCHEMA_PATH = (
    REPO_ROOT / ".claude" / "skills" / "review-issue" / "schemas" / "review_issue_result_v1.json"
)


def _line(payload: dict) -> str:
    return json.dumps(payload)


def _result_event(text: str) -> str:
    return _line({"type": "result", "subtype": "success", "result": text})


# ---------------------------------------------------------------------------
# AC1: procedure_steps_executed_in_declared_order -> evaluate_ordered_evidence_match()
# ---------------------------------------------------------------------------


def test_procedure_steps_executed_in_declared_order_ordered_evidence_match() -> None:
    """GIVEN a caller-supplied ordered marker list that names three declared
    procedure steps, WHEN the native evidence text contains all three
    markers in that exact declared order, THEN the ordered-evidence-match
    assertion verifies (Issue #2498 AC1)."""
    native_evidence = "\n".join(
        [
            _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "STEP1_WORKTREE_CREATED"}]}}),
            _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "STEP2_IMPLEMENTED"}]}}),
            _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "STEP3_VERIFIED"}]}}),
            _result_event("done"),
        ]
    )
    verdict = smoke.evaluate_ordered_evidence_match(
        native_evidence,
        ["STEP1_WORKTREE_CREATED", "STEP2_IMPLEMENTED", "STEP3_VERIFIED"],
    )
    assert verdict["verified"] is True
    assert verdict["missing_markers"] == []
    assert (
        verdict["observed_positions"]["STEP1_WORKTREE_CREATED"]
        < verdict["observed_positions"]["STEP2_IMPLEMENTED"]
        < verdict["observed_positions"]["STEP3_VERIFIED"]
    )


def test_ordered_evidence_match_fails_when_declared_order_is_violated() -> None:
    """GIVEN the same three markers but observed OUT of the caller's
    declared order, THEN the assertion must fail (never silently accept an
    unordered set-membership match, unlike the pre-existing --expect-marker
    check this new flag is deliberately independent from)."""
    native_evidence = "\n".join(
        [
            _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "STEP2_IMPLEMENTED"}]}}),
            _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "STEP1_WORKTREE_CREATED"}]}}),
            _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "STEP3_VERIFIED"}]}}),
        ]
    )
    verdict = smoke.evaluate_ordered_evidence_match(
        native_evidence,
        ["STEP1_WORKTREE_CREATED", "STEP2_IMPLEMENTED", "STEP3_VERIFIED"],
    )
    assert verdict["verified"] is False
    # STEP1_WORKTREE_CREATED IS found (the first search starts at cursor 0,
    # and it is present somewhere in the text). But STEP2_IMPLEMENTED only
    # occurs BEFORE that match position in the raw text -- searching from
    # the cursor advanced past STEP1's match, the sequential subsequence
    # search can no longer find it, so it (not STEP1) is reported missing.
    assert verdict["missing_markers"] == ["STEP2_IMPLEMENTED"]
    assert "STEP1_WORKTREE_CREATED" in verdict["observed_positions"]


def test_ordered_evidence_match_fails_when_a_marker_is_absent() -> None:
    native_evidence = _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "STEP1_ONLY"}]}})
    verdict = smoke.evaluate_ordered_evidence_match(native_evidence, ["STEP1_ONLY", "STEP2_NEVER_OBSERVED"])
    assert verdict["verified"] is False
    assert verdict["missing_markers"] == ["STEP2_NEVER_OBSERVED"]


def test_ordered_evidence_match_empty_expected_list_trivially_verifies() -> None:
    verdict = smoke.evaluate_ordered_evidence_match("anything", [])
    assert verdict["verified"] is True
    assert verdict["missing_markers"] == []


def test_ordered_evidence_match_tolerates_a_repeated_marker() -> None:
    """A marker that legitimately repeats (e.g. a step re-entered in a
    retry loop) must not be mistaken for out-of-order evidence: each
    subsequent occurrence is searched for strictly after the previous
    match, not merely the marker's very first occurrence in the text."""
    native_evidence = "STEP_A STEP_B STEP_A STEP_C"
    verdict = smoke.evaluate_ordered_evidence_match(native_evidence, ["STEP_A", "STEP_B", "STEP_A", "STEP_C"])
    assert verdict["verified"] is True
    assert verdict["missing_markers"] == []


# ---------------------------------------------------------------------------
# AC2: output_contract_schema_fields_present -> evaluate_output_contract_schema_fields_present()
# ---------------------------------------------------------------------------


def _minimal_valid_review_issue_result_v1() -> dict:
    return {
        "schema": "REVIEW_ISSUE_RESULT_V1",
        "schema_version": "v1",
        "verdict": "approve",
        "status": "ok",
        "body_sha256": "sha256:" + ("a" * 64),
        "issue_kind": "implementation",
        "generated_at": "2026-09-05T00:00:00Z",
        "deterministic_checks": {},
        "blocking_issues": [],
        "structured_blockers": [],
        "non_blocking_improvements": [],
        "findings": [],
        "diff_proposal": {},
        "parsed_vc_commands": [],
    }


def test_output_contract_schema_fields_present_full_schema_validation() -> None:
    """GIVEN a final native result text embedding a REVIEW_ISSUE_RESULT_V1
    payload that satisfies the EXISTING canonical JSON Schema file, WHEN
    evaluated against that schema path via jsonschema.validate(), THEN the
    assertion verifies (Issue #2498 AC2, full schema validation -- not a
    required-key-existence-only check)."""
    assert REVIEW_ISSUE_RESULT_V1_SCHEMA_PATH.is_file()
    payload = _minimal_valid_review_issue_result_v1()
    stdout = _result_event(json.dumps(payload))
    verdict = smoke.evaluate_output_contract_schema_fields_present(stdout, str(REVIEW_ISSUE_RESULT_V1_SCHEMA_PATH))
    assert verdict["verified"] is True, verdict
    assert verdict["output_payload_found"] is True
    assert verdict["error"] is None


def test_output_contract_schema_fields_present_fails_full_validation_not_just_required_keys() -> None:
    """A payload with every REQUIRED key present but a WRONG type/enum value
    on one of them (``verdict`` outside its enum) must fail full schema
    validation -- proving this is genuinely jsonschema.validate() full
    validation, not a required-key-existence-only checker (Issue #2498
    AC2's explicit Out of Scope constraint)."""
    payload = _minimal_valid_review_issue_result_v1()
    payload["verdict"] = "not-a-real-enum-value"
    stdout = _result_event(json.dumps(payload))
    verdict = smoke.evaluate_output_contract_schema_fields_present(stdout, str(REVIEW_ISSUE_RESULT_V1_SCHEMA_PATH))
    assert verdict["verified"] is False
    assert verdict["output_payload_found"] is True
    assert verdict["error"]


def test_output_contract_schema_fields_present_fails_when_required_key_missing() -> None:
    payload = _minimal_valid_review_issue_result_v1()
    del payload["blocking_issues"]
    stdout = _result_event(json.dumps(payload))
    verdict = smoke.evaluate_output_contract_schema_fields_present(stdout, str(REVIEW_ISSUE_RESULT_V1_SCHEMA_PATH))
    assert verdict["verified"] is False


def test_output_contract_schema_fields_present_no_result_event_is_not_found() -> None:
    verdict = smoke.evaluate_output_contract_schema_fields_present("", str(REVIEW_ISSUE_RESULT_V1_SCHEMA_PATH))
    assert verdict["verified"] is False
    assert verdict["output_payload_found"] is False
    assert "no final result text observed" in verdict["error"]


def test_output_contract_schema_fields_present_non_json_result_text_is_not_found() -> None:
    stdout = _result_event("just plain prose, not a JSON payload at all")
    verdict = smoke.evaluate_output_contract_schema_fields_present(stdout, str(REVIEW_ISSUE_RESULT_V1_SCHEMA_PATH))
    assert verdict["verified"] is False
    assert verdict["output_payload_found"] is False


def test_output_contract_schema_fields_present_missing_schema_file_reports_error() -> None:
    payload = _minimal_valid_review_issue_result_v1()
    stdout = _result_event(json.dumps(payload))
    verdict = smoke.evaluate_output_contract_schema_fields_present(stdout, "/nonexistent/schema/path.json")
    assert verdict["verified"] is False
    assert "schema load failed" in verdict["error"]


def test_extract_claude_final_result_text_recovers_the_final_result_field() -> None:
    stdout = "\n".join(
        [
            _line({"type": "system", "subtype": "init"}),
            _result_event("PROBE_SKILL_RAN"),
        ]
    )
    assert smoke.extract_claude_final_result_text(stdout) == "PROBE_SKILL_RAN"


def test_extract_claude_final_result_text_none_when_absent() -> None:
    assert smoke.extract_claude_final_result_text("") is None
    assert smoke.extract_claude_final_result_text(_line({"type": "assistant"})) is None

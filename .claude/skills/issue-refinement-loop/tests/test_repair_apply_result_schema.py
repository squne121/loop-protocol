"""
test_repair_apply_result_schema.py

Issue #2039 AC2: repair_apply_result/v1 の JSON Schema validation テスト。

repair_action.apply consumer が生成する repair_apply_result_v1.schema.json の
required / additionalProperties:false / closed enum / cross-field invariant
（multiple_mutation_intents, second_body_drift, applied, complete, fresh
validation success の各不変条件）を固定する。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = SKILL_ROOT / "schemas"
SCHEMA_FILE = SCHEMAS_DIR / "repair_apply_result_v1.schema.json"


def load_schema() -> dict:
    assert SCHEMA_FILE.exists(), f"Schema file not found: {SCHEMA_FILE}"
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def validate(data: dict) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        pytest.skip("jsonschema not available")

    schema = load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    return [e.message for e in errors]


def test_schema_is_valid_draft202012():
    import jsonschema

    schema = load_schema()
    jsonschema.Draft202012Validator.check_schema(schema)


VALID_NOT_ATTEMPTED = {
    "schema_version": "repair_apply_result/v1",
    "phase": "not_started",
    "mutation_outcome": "not_attempted",
    "failure_code": "multiple_mutation_intents",
    "repo": "squne121/loop-protocol",
    "issue_number": 2039,
    "provenance": {
        "repo": "squne121/loop-protocol",
        "issue_number": 2039,
        "original_body_sha256": "a" * 64,
        "original_updated_at": "2026-08-16T00:00:00Z",
        "preflight_run_identity": "b" * 64,
        "producer_schema_version": "repair_action/v1",
        "producer_policy_version": "deterministic-issue-repair/v1",
        "repair_action_core_sha256": "c" * 64,
        "candidate_digest": "d" * 64,
        "source_lane": "anchor",
        "source_refs_digest": "e" * 64,
    },
    "rebase": {
        "attempted": False,
        "producer_reruns": 0,
        "drift_detected": False,
        "second_drift": False,
    },
    "retry": {
        "post_dispatch_retry_budget": 0,
        "retries_used": 0,
    },
    "receipt": {
        "patch_attempted": False,
        "executor_status": None,
        "mutation_outcome": "not_attempted",
        "failure_code": None,
        "final_readback": {
            "status": "not_applicable",
            "digest": None,
            "digest_class": "not_applicable",
        },
    },
    "fresh_validation": {
        "status": "not_run",
        "source_lane_preserved": False,
        "actionable_repair_remaining": None,
        "final_body_digest_match": None,
    },
    "historical_artifacts": {
        "physically_deleted": False,
        "latest_action_reference_invalidated": False,
    },
}


def _applied_success_payload() -> dict:
    data = copy.deepcopy(VALID_NOT_ATTEMPTED)
    data["phase"] = "complete"
    data["mutation_outcome"] = "applied"
    data["failure_code"] = None
    data["receipt"]["patch_attempted"] = True
    data["receipt"]["executor_status"] = "ok"
    data["receipt"]["mutation_outcome"] = "applied"
    data["receipt"]["final_readback"] = {
        "status": "verified",
        "digest": "d" * 64,
        "digest_class": "candidate",
    }
    data["fresh_validation"] = {
        "status": "success",
        "source_lane_preserved": True,
        "actionable_repair_remaining": False,
        "final_body_digest_match": True,
    }
    data["historical_artifacts"]["latest_action_reference_invalidated"] = True
    return data


class TestRequiredFields:
    def test_valid_not_attempted_payload_passes(self):
        errors = validate(VALID_NOT_ATTEMPTED)
        assert errors == [], f"Expected valid payload to pass, got: {errors}"

    def test_valid_applied_success_payload_passes(self):
        errors = validate(_applied_success_payload())
        assert errors == [], f"Expected valid applied payload to pass, got: {errors}"

    @pytest.mark.parametrize(
        "field",
        [
            "schema_version",
            "phase",
            "mutation_outcome",
            "repo",
            "issue_number",
            "provenance",
            "rebase",
            "retry",
            "receipt",
            "fresh_validation",
            "historical_artifacts",
        ],
    )
    def test_missing_required_top_level_field(self, field):
        data = {k: v for k, v in VALID_NOT_ATTEMPTED.items() if k != field}
        errors = validate(data)
        assert errors, f"Expected error for missing {field}"

    @pytest.mark.parametrize(
        "field",
        [
            "repo",
            "issue_number",
            "original_body_sha256",
            "original_updated_at",
            "preflight_run_identity",
            "producer_schema_version",
            "producer_policy_version",
            "repair_action_core_sha256",
            "candidate_digest",
            "source_lane",
            "source_refs_digest",
        ],
    )
    def test_missing_required_provenance_field(self, field):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        del data["provenance"][field]
        errors = validate(data)
        assert errors, f"Expected error for missing provenance.{field}"


class TestAdditionalProperties:
    def test_extra_top_level_property_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["extra_field"] = "unexpected"
        errors = validate(data)
        assert errors, "Expected error for extra top-level property"

    def test_extra_provenance_property_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["provenance"]["extra_field"] = "unexpected"
        errors = validate(data)
        assert errors, "Expected error for extra provenance property"

    def test_extra_receipt_property_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["receipt"]["extra_field"] = "unexpected"
        errors = validate(data)
        assert errors, "Expected error for extra receipt property"


class TestClosedEnums:
    def test_wrong_schema_version_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["schema_version"] = "repair_apply_result/v2"
        errors = validate(data)
        assert errors, "Expected error for wrong schema_version"

    def test_wrong_phase_enum_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["phase"] = "unknown_phase"
        errors = validate(data)
        assert errors, "Expected error for unknown phase"

    def test_wrong_mutation_outcome_enum_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["mutation_outcome"] = "failed"
        errors = validate(data)
        assert errors, "Expected error for invalid mutation_outcome (failed not in enum)"

    def test_contract_patch_plan_missing_rejected_as_failure_code(self):
        """Repair lane must never emit contract_patch_plan_missing (that code
        belongs to the contract_update lane only)."""
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["failure_code"] = "contract_patch_plan_missing"
        errors = validate(data)
        assert errors, "Expected error for contract_patch_plan_missing outside closed enum"

    def test_wrong_source_lane_enum_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["provenance"]["source_lane"] = "with_anchor"
        errors = validate(data)
        assert errors, "Expected error for invalid source_lane value"

    def test_wrong_digest_class_enum_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["receipt"]["final_readback"]["digest_class"] = "new_digest"
        errors = validate(data)
        assert errors, "Expected error for invalid digest_class value"


class TestRetryBudgetIsZero:
    def test_nonzero_post_dispatch_retry_budget_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["retry"]["post_dispatch_retry_budget"] = 1
        errors = validate(data)
        assert errors, "Expected error: post_dispatch_retry_budget must be const 0"

    def test_nonzero_retries_used_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["retry"]["retries_used"] = 1
        errors = validate(data)
        assert errors, "Expected error: retries_used must be const 0"


class TestCrossFieldInvariants:
    def test_multiple_mutation_intents_requires_not_attempted(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["mutation_outcome"] = "applied"
        errors = validate(data)
        assert errors, (
            "Expected error: failure_code=multiple_mutation_intents must force "
            "mutation_outcome=not_attempted"
        )

    def test_second_drift_true_requires_not_attempted_and_failure_code(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["failure_code"] = None
        data["rebase"]["second_drift"] = True
        data["mutation_outcome"] = "applied"
        errors = validate(data)
        assert errors, "Expected error: second_drift=true must force not_attempted/second_body_drift"

    def test_second_drift_true_valid_when_fields_consistent(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["failure_code"] = "second_body_drift"
        data["rebase"]["second_drift"] = True
        errors = validate(data)
        assert errors == [], f"Expected consistent second_drift payload to pass, got: {errors}"

    def test_applied_requires_patch_attempted_true(self):
        data = _applied_success_payload()
        data["receipt"]["patch_attempted"] = False
        errors = validate(data)
        assert errors, "Expected error: mutation_outcome=applied requires receipt.patch_attempted=true"

    def test_applied_requires_failure_code_null(self):
        data = _applied_success_payload()
        data["failure_code"] = "transaction_execute_error"
        errors = validate(data)
        assert errors, "Expected error: mutation_outcome=applied requires failure_code null"

    def test_complete_phase_rejects_unknown_outcome(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["phase"] = "complete"
        data["mutation_outcome"] = "unknown"
        errors = validate(data)
        assert errors, "Expected error: phase=complete must not carry mutation_outcome=unknown"

    def test_fresh_validation_success_requires_no_actionable_repair(self):
        data = _applied_success_payload()
        data["fresh_validation"]["actionable_repair_remaining"] = True
        errors = validate(data)
        assert errors, "Expected error: fresh_validation success requires actionable_repair_remaining=false"

    def test_fresh_validation_success_requires_digest_match(self):
        data = _applied_success_payload()
        data["fresh_validation"]["final_body_digest_match"] = False
        errors = validate(data)
        assert errors, "Expected error: fresh_validation success requires final_body_digest_match=true"

    def test_fresh_validation_success_requires_source_lane_preserved(self):
        data = _applied_success_payload()
        data["fresh_validation"]["source_lane_preserved"] = False
        errors = validate(data)
        assert errors, "Expected error: fresh_validation success requires source_lane_preserved=true"


class TestHistoricalArtifactsNeverPhysicallyDeleted:
    def test_physically_deleted_true_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["historical_artifacts"]["physically_deleted"] = True
        errors = validate(data)
        assert errors, "Expected error: physically_deleted must be const false"

"""
test_contract_update_handoff_schema.py

Issue #2039 AC2: refinement_preflight_result_v1.schema.json migration の
compatibility テスト。

contract_update handoff ブロックの既存契約は変更しない（後方互換）ことを固定し、
repair_action ブロックへ追加した provenance-lane フィールド
（source_lane / preflight_run_identity / original_updated_at /
source_refs_digest）が additive-optional であり、旧アーティファクト
（これらのフィールドを持たない、または null の repair_action）も
引き続き valid であることを検証する。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = SKILL_ROOT / "schemas"
SCHEMA_FILE = SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json"


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


MINIMAL_PASS_PAYLOAD = {
    "schema_version": "refinement_preflight_result/v1",
    "status": "pass",
    "issue_number": 2039,
    "repo": "squne121/loop-protocol",
    "planner_exit_code": 0,
    "planner_fail_closed": False,
    "next_action": "proceed",
    "must_read": [],
    "do_not_read": [],
    "commands": [],
    "blockers": [],
    "artifacts": {},
    "hashes": {},
}

# Historical (pre-#2039-migration) needs_fix payload: repair_action carries
# only the original required fields, with no provenance-lane additions.
LEGACY_NEEDS_FIX_PAYLOAD = {
    **MINIMAL_PASS_PAYLOAD,
    "status": "needs_fix",
    "artifacts": {
        "repair_diagnostics": "/abs/repair_diagnostics.json",
        "repair_candidate_body": "/abs/repaired_issue_body.md",
    },
    "repair_action": {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": "a" * 64,
        "repaired_body_sha256": "b" * 64,
        "diagnostics_artifact": "/abs/repair_diagnostics.json",
        "candidate_body_artifact": "/abs/repaired_issue_body.md",
        "repair_kinds": ["trailing_whitespace"],
        "reason_codes": ["known_safe"],
    },
}


class TestBackwardCompatibilityOfLegacyPayloads:
    def test_minimal_pass_payload_still_valid(self):
        errors = validate(MINIMAL_PASS_PAYLOAD)
        assert errors == [], f"Expected minimal pass payload to remain valid, got: {errors}"

    def test_legacy_needs_fix_payload_without_provenance_lane_fields_still_valid(self):
        errors = validate(LEGACY_NEEDS_FIX_PAYLOAD)
        assert errors == [], f"Expected legacy needs_fix payload to remain valid, got: {errors}"

    def test_legacy_contract_update_block_shape_unchanged(self):
        data = copy.deepcopy(MINIMAL_PASS_PAYLOAD)
        data["contract_update"] = {
            "status": "no_change",
            "writes": 0,
            "iterations": 0,
            "final_readback": "not_applicable",
            "fresh_preflight": "not_run",
            "fresh_review": "not_run",
            "fresh_readiness": "not_run",
        }
        errors = validate(data)
        assert errors == [], f"Expected unchanged contract_update shape to remain valid, got: {errors}"

    def test_contract_update_extra_property_still_rejected(self):
        data = copy.deepcopy(MINIMAL_PASS_PAYLOAD)
        data["contract_update"] = {
            "status": "no_change",
            "writes": 0,
            "iterations": 0,
            "final_readback": "not_applicable",
            "fresh_preflight": "not_run",
            "fresh_review": "not_run",
            "fresh_readiness": "not_run",
            "extra_field": "unexpected",
        }
        errors = validate(data)
        assert errors, "Expected contract_update additionalProperties:false to still reject extras"


class TestProvenanceLaneMigrationIsAdditiveOptional:
    def test_repair_action_with_new_provenance_lane_fields_valid(self):
        data = copy.deepcopy(LEGACY_NEEDS_FIX_PAYLOAD)
        data["repair_action"]["source_lane"] = "anchor"
        data["repair_action"]["preflight_run_identity"] = "c" * 64
        data["repair_action"]["original_updated_at"] = "2026-08-16T00:00:00Z"
        data["repair_action"]["source_refs_digest"] = "d" * 64
        errors = validate(data)
        assert errors == [], f"Expected additive provenance-lane fields to validate, got: {errors}"

    def test_repair_action_with_null_provenance_lane_fields_valid(self):
        data = copy.deepcopy(LEGACY_NEEDS_FIX_PAYLOAD)
        data["repair_action"]["source_lane"] = None
        data["repair_action"]["preflight_run_identity"] = None
        data["repair_action"]["original_updated_at"] = None
        data["repair_action"]["source_refs_digest"] = None
        errors = validate(data)
        assert errors == [], f"Expected null provenance-lane fields to validate, got: {errors}"

    def test_repair_action_with_invalid_source_lane_enum_rejected(self):
        data = copy.deepcopy(LEGACY_NEEDS_FIX_PAYLOAD)
        data["repair_action"]["source_lane"] = "with_anchor"
        errors = validate(data)
        assert errors, "Expected invalid source_lane value to be rejected by closed enum"

    def test_repair_action_still_rejects_unrelated_extra_property(self):
        data = copy.deepcopy(LEGACY_NEEDS_FIX_PAYLOAD)
        data["repair_action"]["unrelated_new_field"] = "unexpected"
        errors = validate(data)
        assert errors, "Expected repair_action additionalProperties:false to still reject unrelated extras"

    def test_needs_fix_still_requires_auto_apply_safe_disposition(self):
        data = copy.deepcopy(LEGACY_NEEDS_FIX_PAYLOAD)
        data["repair_action"]["disposition"] = "human_review_required"
        errors = validate(data)
        assert errors, "Expected needs_fix cross-field invariant (disposition==auto_apply_safe) to hold"

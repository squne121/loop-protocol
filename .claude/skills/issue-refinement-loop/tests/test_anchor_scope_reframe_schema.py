"""
test_anchor_scope_reframe_schema.py

AC2: ANCHOR_SCOPE_REFRAME_V1 の JSON Schema validation テスト。

required / additionalProperties:false / enum / null handling が固定されることを確認する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = SKILL_ROOT / "schemas"
SCHEMA_FILE = SCHEMAS_DIR / "anchor_scope_reframe_v1.schema.json"


def load_schema() -> dict:
    """Load ANCHOR_SCOPE_REFRAME_V1 schema."""
    assert SCHEMA_FILE.exists(), f"Schema file not found: {SCHEMA_FILE}"
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def validate(data: dict) -> list[str]:
    """Validate data against schema. Returns list of error messages (empty = valid)."""
    try:
        import jsonschema
    except ImportError:
        pytest.skip("jsonschema not available")

    schema = load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    return [e.message for e in errors]


# ---------------------------------------------------------------------------
# Minimal valid fixture
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {
    "schema_version": "ANCHOR_SCOPE_REFRAME_V1",
    "target": {
        "repo": "squne121/loop-protocol",
        "issue_number": 920,
    },
    "decision": "approve_scope_delta",
    "allowed_path_deltas": [
        ".claude/skills/issue-refinement-loop/schemas/anchor_scope_reframe_v1.schema.json"
    ],
    "rationale": "Adding anchor schema to fix scope signal regression.",
    "required_rerun": ["contract_review", "refinement_preflight", "allowed_paths_gate"],
}


# ---------------------------------------------------------------------------
# AC2: required fields
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_valid_payload_passes(self):
        errors = validate(VALID_PAYLOAD)
        assert errors == [], f"Expected valid payload to pass, got: {errors}"

    def test_missing_schema_version(self):
        data = {k: v for k, v in VALID_PAYLOAD.items() if k != "schema_version"}
        errors = validate(data)
        assert errors, "Expected error for missing schema_version"

    def test_missing_target(self):
        data = {k: v for k, v in VALID_PAYLOAD.items() if k != "target"}
        errors = validate(data)
        assert errors, "Expected error for missing target"

    def test_missing_decision(self):
        data = {k: v for k, v in VALID_PAYLOAD.items() if k != "decision"}
        errors = validate(data)
        assert errors, "Expected error for missing decision"

    def test_missing_allowed_path_deltas(self):
        data = {k: v for k, v in VALID_PAYLOAD.items() if k != "allowed_path_deltas"}
        errors = validate(data)
        assert errors, "Expected error for missing allowed_path_deltas"

    def test_missing_rationale(self):
        data = {k: v for k, v in VALID_PAYLOAD.items() if k != "rationale"}
        errors = validate(data)
        assert errors, "Expected error for missing rationale"

    def test_missing_required_rerun(self):
        data = {k: v for k, v in VALID_PAYLOAD.items() if k != "required_rerun"}
        errors = validate(data)
        assert errors, "Expected error for missing required_rerun"


# ---------------------------------------------------------------------------
# AC2: additionalProperties: false
# ---------------------------------------------------------------------------


class TestAdditionalProperties:
    def test_extra_top_level_property_fails(self):
        data = dict(VALID_PAYLOAD)
        data["extra_field"] = "unexpected"
        errors = validate(data)
        assert errors, "Expected error for extra top-level property"

    def test_extra_target_property_fails(self):
        data = dict(VALID_PAYLOAD)
        data["target"] = dict(VALID_PAYLOAD["target"])
        data["target"]["extra_field"] = "unexpected"
        errors = validate(data)
        assert errors, "Expected error for extra target property"

    def test_missing_target_issue_number(self):
        data = dict(VALID_PAYLOAD)
        data["target"] = {"repo": "squne121/loop-protocol"}
        errors = validate(data)
        assert errors, "Expected error for missing target.issue_number"

    def test_missing_target_repo(self):
        data = dict(VALID_PAYLOAD)
        data["target"] = {"issue_number": 920}
        errors = validate(data)
        assert errors, "Expected error for missing target.repo"


# ---------------------------------------------------------------------------
# AC2: enum constraints
# ---------------------------------------------------------------------------


class TestEnumConstraints:
    def test_wrong_schema_version_fails(self):
        data = dict(VALID_PAYLOAD)
        data["schema_version"] = "ANCHOR_SCOPE_REFRAME_V2"
        errors = validate(data)
        assert errors, "Expected error for wrong schema_version"

    def test_wrong_repo_const_fails(self):
        data = dict(VALID_PAYLOAD)
        data["target"] = dict(VALID_PAYLOAD["target"])
        data["target"]["repo"] = "other-owner/other-repo"
        errors = validate(data)
        assert errors, "Expected error for wrong repo const"

    def test_wrong_decision_enum_fails(self):
        data = dict(VALID_PAYLOAD)
        data["decision"] = "reject_scope_delta"
        errors = validate(data)
        assert errors, "Expected error for wrong decision enum value"

    def test_wrong_required_rerun_enum_fails(self):
        data = dict(VALID_PAYLOAD)
        data["required_rerun"] = ["contract_review", "unknown_step"]
        errors = validate(data)
        assert errors, "Expected error for invalid required_rerun enum value"

    def test_issue_number_zero_fails(self):
        data = dict(VALID_PAYLOAD)
        data["target"] = dict(VALID_PAYLOAD["target"])
        data["target"]["issue_number"] = 0
        errors = validate(data)
        assert errors, "Expected error for issue_number=0 (minimum: 1)"

    def test_issue_number_negative_fails(self):
        data = dict(VALID_PAYLOAD)
        data["target"] = dict(VALID_PAYLOAD["target"])
        data["target"]["issue_number"] = -1
        errors = validate(data)
        assert errors, "Expected error for negative issue_number"


# ---------------------------------------------------------------------------
# AC2: null handling (none of the required fields accept null)
# ---------------------------------------------------------------------------


class TestNullHandling:
    def test_null_schema_version_fails(self):
        data = dict(VALID_PAYLOAD)
        data["schema_version"] = None
        errors = validate(data)
        assert errors, "Expected error for null schema_version"

    def test_null_decision_fails(self):
        data = dict(VALID_PAYLOAD)
        data["decision"] = None
        errors = validate(data)
        assert errors, "Expected error for null decision"

    def test_null_rationale_fails(self):
        data = dict(VALID_PAYLOAD)
        data["rationale"] = None
        errors = validate(data)
        assert errors, "Expected error for null rationale"

    def test_null_allowed_path_deltas_fails(self):
        data = dict(VALID_PAYLOAD)
        data["allowed_path_deltas"] = None
        errors = validate(data)
        assert errors, "Expected error for null allowed_path_deltas"

    def test_null_required_rerun_fails(self):
        data = dict(VALID_PAYLOAD)
        data["required_rerun"] = None
        errors = validate(data)
        assert errors, "Expected error for null required_rerun"

    def test_null_issue_number_fails(self):
        data = dict(VALID_PAYLOAD)
        data["target"] = dict(VALID_PAYLOAD["target"])
        data["target"]["issue_number"] = None
        errors = validate(data)
        assert errors, "Expected error for null issue_number"


# ---------------------------------------------------------------------------
# AC2: minItems constraints
# ---------------------------------------------------------------------------


class TestMinItems:
    def test_empty_allowed_path_deltas_fails(self):
        data = dict(VALID_PAYLOAD)
        data["allowed_path_deltas"] = []
        errors = validate(data)
        assert errors, "Expected error for empty allowed_path_deltas (minItems: 1)"

    def test_empty_required_rerun_fails(self):
        data = dict(VALID_PAYLOAD)
        data["required_rerun"] = []
        errors = validate(data)
        assert errors, "Expected error for empty required_rerun (minItems: 1)"

    def test_single_required_rerun_passes(self):
        data = dict(VALID_PAYLOAD)
        data["required_rerun"] = ["contract_review"]
        errors = validate(data)
        assert errors == [], f"Expected single required_rerun to pass, got: {errors}"

    def test_empty_rationale_fails(self):
        data = dict(VALID_PAYLOAD)
        data["rationale"] = ""
        errors = validate(data)
        assert errors, "Expected error for empty rationale (minLength: 1)"

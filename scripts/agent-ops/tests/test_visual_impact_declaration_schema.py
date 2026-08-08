"""test_visual_impact_declaration_schema.py (Issue #2019 AC10)

GIVEN/WHEN/THEN tests proving VISUAL_IMPACT_DECLARATION_V1's schema
(docs/dev/visual-impact.schema.json $defs.VISUAL_IMPACT_DECLARATION_V1) is
strict: additionalProperties=false at every object level and closed enums
for `disposition`.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-impact.schema.json"


@pytest.fixture(scope="module")
def declaration_schema() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {"$defs": schema["$defs"], **schema["$defs"]["VISUAL_IMPACT_DECLARATION_V1"]}


def _validate(doc: dict, schema: dict) -> None:
    jsonschema.validate(doc, schema)


def test_valid_verified_unchanged_entry(declaration_schema):
    _validate(
        {
            "schema": "VISUAL_IMPACT_DECLARATION_V1",
            "surfaces": [{"surface_id": "combat-hud-running", "disposition": "verified_unchanged"}],
        },
        declaration_schema,
    )


def test_valid_waived_entry_requires_waiver_object(declaration_schema):
    _validate(
        {
            "schema": "VISUAL_IMPACT_DECLARATION_V1",
            "surfaces": [
                {
                    "surface_id": "combat-hud-running",
                    "disposition": "waived",
                    "waiver": {
                        "reason": "layout refactor in progress",
                        "tracking_issue": "#9999",
                        "expiry": "2099-01-01",
                    },
                }
            ],
        },
        declaration_schema,
    )


def test_waived_without_waiver_object_is_rejected(declaration_schema):
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "schema": "VISUAL_IMPACT_DECLARATION_V1",
                "surfaces": [{"surface_id": "combat-hud-running", "disposition": "waived"}],
            },
            declaration_schema,
        )


def test_unknown_top_level_field_is_rejected(declaration_schema):
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "schema": "VISUAL_IMPACT_DECLARATION_V1",
                "surfaces": [],
                "unexpected_field": True,
            },
            declaration_schema,
        )


def test_unknown_surface_entry_field_is_rejected(declaration_schema):
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "schema": "VISUAL_IMPACT_DECLARATION_V1",
                "surfaces": [
                    {
                        "surface_id": "combat-hud-running",
                        "disposition": "verified_unchanged",
                        "extra": "nope",
                    }
                ],
            },
            declaration_schema,
        )


def test_unknown_waiver_field_is_rejected(declaration_schema):
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "schema": "VISUAL_IMPACT_DECLARATION_V1",
                "surfaces": [
                    {
                        "surface_id": "combat-hud-running",
                        "disposition": "waived",
                        "waiver": {
                            "reason": "x",
                            "tracking_issue": "#1",
                            "expiry": "2099-01-01",
                            "owner_self_report": "@someone",
                        },
                    }
                ],
            },
            declaration_schema,
        )


def test_disposition_enum_is_closed(declaration_schema):
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "schema": "VISUAL_IMPACT_DECLARATION_V1",
                "surfaces": [{"surface_id": "combat-hud-running", "disposition": "trust_me_bro"}],
            },
            declaration_schema,
        )


def test_tracking_issue_pattern_is_enforced(declaration_schema):
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "schema": "VISUAL_IMPACT_DECLARATION_V1",
                "surfaces": [
                    {
                        "surface_id": "combat-hud-running",
                        "disposition": "waived",
                        "waiver": {"reason": "x", "tracking_issue": "no-hash-prefix", "expiry": "2099-01-01"},
                    }
                ],
            },
            declaration_schema,
        )


def test_wrong_schema_const_is_rejected(declaration_schema):
    with pytest.raises(jsonschema.ValidationError):
        _validate({"schema": "SOMETHING_ELSE", "surfaces": []}, declaration_schema)

#!/usr/bin/env python3
"""
test_loop_state_schema.py

AC2: schemas/loop_state.schema.json exists and pytest validates the
current loop state fixture against it.
"""

import json
import pytest
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
SCHEMA_PATH = SKILL_ROOT / "schemas" / "loop_state.schema.json"
FIXTURE_PATH = SKILL_ROOT / "fixtures" / "loop_state_v1_fixture.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_schema() -> dict:
    assert SCHEMA_PATH.exists(), f"schema not found: {SCHEMA_PATH}"
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_fixture() -> dict:
    assert FIXTURE_PATH.exists(), f"fixture not found: {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_schema_file_exists():
    """AC2: schemas/loop_state.schema.json exists."""
    assert SCHEMA_PATH.exists(), f"Missing: {SCHEMA_PATH}"


def test_schema_is_valid_json():
    """schema file is valid JSON."""
    schema = load_schema()
    assert isinstance(schema, dict)


def test_schema_has_required_fields_declaration():
    """schema declares required fields including iteration and max_iterations."""
    schema = load_schema()
    required = schema.get("required", [])
    for field in ["issue_number", "iteration", "max_iterations", "last_verdict"]:
        assert field in required, f"'{field}' must be in schema required list"


def test_schema_has_routing_critical_properties():
    """schema properties include all routing-critical fields."""
    schema = load_schema()
    props = schema.get("properties", {})
    routing_critical = [
        "scope_rollup_decision",
        "scope_signal_guard",
        "delivery_rollup",
        "follow_up_materialization",
        "superseded_decision",
        "termination_reason",
    ]
    for field in routing_critical:
        assert field in props, f"Routing-critical field '{field}' missing from schema"


def test_fixture_file_exists():
    """Fixture file loop_state_v1_fixture.json exists."""
    assert FIXTURE_PATH.exists(), f"Missing fixture: {FIXTURE_PATH}"


def test_fixture_passes_schema_validation():
    """AC2: current loop state fixture validates against loop_state.schema.json."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = load_schema()
    fixture = load_fixture()
    # Should not raise
    jsonschema.validate(instance=fixture, schema=schema)


def test_fixture_schema_version():
    """fixture has correct schema_version."""
    fixture = load_fixture()
    assert fixture.get("schema_version") == "loop_state/v1"


def test_fixture_has_valid_verdict():
    """fixture last_verdict is one of: approve | needs-fix | null."""
    fixture = load_fixture()
    assert fixture.get("last_verdict") in ("approve", "needs-fix", None)


def test_invalid_last_verdict_fails_schema():
    """A fixture with invalid last_verdict fails schema validation."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = load_schema()
    fixture = load_fixture()
    bad = dict(fixture, last_verdict="unknown_verdict")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=schema)


def test_missing_required_field_fails_schema():
    """A fixture missing 'iteration' fails schema validation."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = load_schema()
    fixture = load_fixture()
    bad = {k: v for k, v in fixture.items() if k != "iteration"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=schema)


def test_negative_iteration_fails_schema():
    """A fixture with iteration=-1 fails schema validation (minimum: 0)."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = load_schema()
    fixture = load_fixture()
    bad = dict(fixture, iteration=-1)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=schema)


def test_schema_additional_properties_false():
    """schema has additionalProperties: false at the top level."""
    schema = load_schema()
    assert schema.get("additionalProperties") is False, \
        "Top-level additionalProperties must be false"


def test_schema_version_const():
    """schema_version property is a const string."""
    schema = load_schema()
    sv_prop = schema.get("properties", {}).get("schema_version", {})
    assert sv_prop.get("const") == "loop_state/v1", \
        "schema_version must be const 'loop_state/v1'"

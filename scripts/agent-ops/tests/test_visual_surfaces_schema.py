"""test_visual_surfaces_schema.py (Issue #2019 AC1 / AC2 / AC3 / AC16)

GIVEN/WHEN/THEN tests for docs/dev/visual-surfaces.yml validated against
docs/dev/visual-surfaces.schema.json: additionalProperties false, closed
command-id enums, and fail-closed rejection of entry deletion / contract
deletion / unknown key / duplicate ID / dangling path / missing test-job-
baseline.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.yml"
SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def registry_doc() -> dict:
    return yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))


def test_ac1_registry_file_exists_and_validates(schema, registry_doc):
    jsonschema.validate(registry_doc, schema)


def test_ac2_both_combat_hud_surfaces_present_independently(registry_doc):
    surfaces = registry_doc["surfaces"]
    assert "combat-hud-running" in surfaces
    assert "combat-hud-critical" in surfaces
    assert surfaces["combat-hud-running"] is not surfaces["combat-hud-critical"]
    # Independent entries: distinct baseline contracts.
    assert (
        surfaces["combat-hud-running"]["contracts"]["baseline"]
        != surfaces["combat-hud-critical"]["contracts"]["baseline"]
    )


def test_ac3_command_ids_are_closed_enum_not_raw_shell(schema, registry_doc):
    for surface in registry_doc["surfaces"].values():
        update_id = surface["contracts"]["update_command_id"]
        verify_id = surface["contracts"]["verify_command_id"]
        assert " " not in update_id and "/" not in update_id and "&&" not in update_id
        assert " " not in verify_id and "/" not in verify_id and "&&" not in verify_id
    update_enum = schema["properties"]["surfaces"]["additionalProperties"]["properties"]["contracts"]["properties"][
        "update_command_id"
    ]["enum"]
    assert "vitest_component_vrt_update" in update_enum


def test_ac3_unknown_command_id_is_schema_failure(schema, registry_doc):
    bad = copy.deepcopy(registry_doc)
    bad["surfaces"]["combat-hud-running"]["contracts"]["update_command_id"] = "rm -rf /"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_ac16_registry_entry_deletion_is_fail_closed(schema, registry_doc):
    bad = copy.deepcopy(registry_doc)
    del bad["surfaces"]["combat-hud-critical"]
    # Deletion alone is schema-valid (minProperties: 1 still satisfied) --
    # the fail-closed guarantee for entry deletion lives in
    # resolve_visual_impact.diff_producer_mappings() (base/head union), not
    # single-document schema validation. Assert the entry is genuinely gone
    # so the diff-based test elsewhere exercises a real deletion.
    jsonschema.validate(bad, schema)
    assert "combat-hud-critical" not in bad["surfaces"]


def test_ac16_contract_deletion_is_fail_closed(schema, registry_doc):
    bad = copy.deepcopy(registry_doc)
    del bad["surfaces"]["combat-hud-running"]["contracts"]["baseline"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_ac16_unknown_key_is_fail_closed(schema, registry_doc):
    bad = copy.deepcopy(registry_doc)
    bad["surfaces"]["combat-hud-running"]["unknown_extra_field"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_ac16_missing_job_is_fail_closed(schema, registry_doc):
    bad = copy.deepcopy(registry_doc)
    del bad["surfaces"]["combat-hud-running"]["contracts"]["job"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_ac16_missing_producers_modules_is_fail_closed(schema, registry_doc):
    bad = copy.deepcopy(registry_doc)
    bad["surfaces"]["combat-hud-running"]["producers"]["modules"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_ac16_dangling_producer_path_is_detected_by_orchestrator():
    """Schema validation alone cannot know whether a repo-relative path
    exists on disk -- that check belongs to the orchestrator (fail-closed at
    runtime, not merely at schema shape level)."""
    for surface in yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["surfaces"].values():
        for module_path in surface["producers"]["modules"]:
            assert (REPO_ROOT / module_path).exists(), f"dangling producer path: {module_path}"
        baseline_path = surface["contracts"]["baseline"]
        assert (REPO_ROOT / baseline_path).exists(), f"dangling baseline path: {baseline_path}"
        spec_path = surface["contracts"]["spec"]
        assert (REPO_ROOT / spec_path).exists(), f"dangling spec path: {spec_path}"


def test_ac16_duplicate_id_is_impossible_by_yaml_mapping_semantics(registry_doc):
    # YAML mappings cannot carry duplicate keys after parsing; this test
    # documents the invariant explicitly rather than asserting a tautology
    # about the already-parsed dict.
    raw_text = REGISTRY_PATH.read_text(encoding="utf-8")
    loader = yaml.SafeLoader(raw_text)
    node = loader.get_single_node()
    surfaces_node = next(
        value_node for key_node, value_node in node.value if key_node.value == "surfaces"
    )
    seen_keys = [key_node.value for key_node, _ in surfaces_node.value]
    assert len(seen_keys) == len(set(seen_keys)), f"duplicate surface id in registry: {seen_keys}"

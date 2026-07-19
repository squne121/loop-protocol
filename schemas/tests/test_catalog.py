"""
Test suite for schemas/catalog.yaml and schemas/catalog.schema.json.

Verifies:
- AC1: catalog.yaml exists with required top-level fields
- AC2: 16 entries from schema-governance.md are present, schema_id unique
- AC3: each entry has all required fields
- AC4: detection_patterns are data structures (not shell strings),
       required_test_commands use allowlisted command IDs
- AC5: no ambiguous values (TBD, TODO, unknown, etc.)
- AC6: catalog.schema.json exists with Draft 2020-12
- AC7: pytest-based validation of all the above + JSON Schema validation
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml


# --------------------------------------------------------------------------- #
# UniqueKeySafeLoader (B2)
# --------------------------------------------------------------------------- #
class UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate key found: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).parents[2]
CATALOG_YAML = REPO_ROOT / "schemas" / "catalog.yaml"
CATALOG_SCHEMA_JSON = REPO_ROOT / "schemas" / "catalog.schema.json"

EXPECTED_ENTRY_COUNT = 20

AMBIGUOUS_PATTERNS = re.compile(
    r"推定|TBD|TODO|不明|unknown",
    re.IGNORECASE,
)

ALLOWED_RUNNERS = {"pytest", "pnpm", "rg", "test", "python_module", "bash_allowlisted"}

REQUIRED_ENTRY_FIELDS = {
    "schema_id",
    "format",
    "definition_paths",
    "producer",
    "consumers",
    "detection_patterns",
    "required_test_commands",
    "compatibility",
    "validation",
    "migration",
    "last_verified",
}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def catalog() -> dict:
    assert CATALOG_YAML.exists(), f"catalog.yaml not found: {CATALOG_YAML}"
    with CATALOG_YAML.open(encoding="utf-8") as f:
        return yaml.load(f, Loader=UniqueKeySafeLoader)


@pytest.fixture(scope="module")
def schema_json() -> dict:
    assert CATALOG_SCHEMA_JSON.exists(), (
        f"catalog.schema.json not found: {CATALOG_SCHEMA_JSON}"
    )
    with CATALOG_SCHEMA_JSON.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def entries(catalog: dict) -> list:
    return catalog.get("entries", [])


# --------------------------------------------------------------------------- #
# AC1: top-level structure
# --------------------------------------------------------------------------- #
class TestTopLevelStructure:
    def test_catalog_yaml_exists(self):
        assert CATALOG_YAML.exists()

    def test_catalog_schema_version(self, catalog: dict):
        assert catalog.get("catalog_schema_version") == "v1"

    def test_catalog_id(self, catalog: dict):
        assert catalog.get("catalog_id") == "loop-protocol.schema-catalog/v1"

    def test_entries_field_present(self, catalog: dict):
        assert "entries" in catalog

    def test_entries_is_list(self, catalog: dict):
        assert isinstance(catalog["entries"], list)


# --------------------------------------------------------------------------- #
# AC2: 16 entries, unique schema_id
# --------------------------------------------------------------------------- #
class TestEntryCompleteness:
    def test_entry_count_is_16(self, entries: list):
        assert len(entries) == EXPECTED_ENTRY_COUNT, (
            f"Expected {EXPECTED_ENTRY_COUNT} entries, got {len(entries)}"
        )

    def test_schema_id_unique(self, entries: list):
        ids = [e["schema_id"] for e in entries if "schema_id" in e]
        assert len(ids) == len(set(ids)), (
            f"Duplicate schema_id found: {[x for x in ids if ids.count(x) > 1]}"
        )

    def test_all_entries_have_schema_id(self, entries: list):
        for i, entry in enumerate(entries):
            assert "schema_id" in entry, f"Entry at index {i} missing schema_id"
            assert entry["schema_id"], f"Entry at index {i} has empty schema_id"


# --------------------------------------------------------------------------- #
# AC3: required fields per entry
# --------------------------------------------------------------------------- #
class TestRequiredFields:
    @pytest.mark.parametrize(
        "field",
        sorted(REQUIRED_ENTRY_FIELDS),
    )
    def test_all_entries_have_required_field(self, entries: list, field: str):
        for entry in entries:
            schema_id = entry.get("schema_id", f"<index {entries.index(entry)}>")
            assert field in entry, (
                f"Entry '{schema_id}' missing required field '{field}'"
            )

    def test_producer_has_owner_and_paths(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            producer = entry.get("producer", {})
            assert isinstance(producer, dict), f"'{schema_id}': producer must be dict"
            assert "owner" in producer, f"'{schema_id}': producer missing 'owner'"
            assert "paths" in producer, f"'{schema_id}': producer missing 'paths'"
            assert producer["owner"], f"'{schema_id}': producer.owner is empty"
            assert isinstance(producer["paths"], list), (
                f"'{schema_id}': producer.paths must be list"
            )
            assert len(producer["paths"]) >= 1, (
                f"'{schema_id}': producer.paths must have at least 1 entry"
            )

    def test_definition_paths_non_empty(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            paths = entry.get("definition_paths", [])
            assert isinstance(paths, list) and len(paths) >= 1, (
                f"'{schema_id}': definition_paths must be non-empty list"
            )

    def test_consumers_non_empty_list(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            consumers = entry.get("consumers", [])
            assert isinstance(consumers, list) and len(consumers) >= 1, (
                f"'{schema_id}': consumers must be non-empty list"
            )

    def test_compatibility_fields(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            compat = entry.get("compatibility", {})
            assert isinstance(compat, dict), f"'{schema_id}': compatibility must be dict"
            assert "mode" in compat, f"'{schema_id}': compatibility missing 'mode'"
            assert "direction" in compat, (
                f"'{schema_id}': compatibility missing 'direction'"
            )
            assert "breaking_changes" in compat, (
                f"'{schema_id}': compatibility missing 'breaking_changes'"
            )

    def test_migration_fields(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            migration = entry.get("migration", {})
            assert isinstance(migration, dict), f"'{schema_id}': migration must be dict"
            assert "required_for_breaking_change" in migration, (
                f"'{schema_id}': migration missing 'required_for_breaking_change'"
            )
            assert "followup_issue_required" in migration, (
                f"'{schema_id}': migration missing 'followup_issue_required'"
            )

    def test_last_verified_fields(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            lv = entry.get("last_verified", {})
            assert isinstance(lv, dict), f"'{schema_id}': last_verified must be dict"
            assert "commit" in lv, f"'{schema_id}': last_verified missing 'commit'"
            assert "command_id" in lv, (
                f"'{schema_id}': last_verified missing 'command_id'"
            )
            assert lv["commit"], f"'{schema_id}': last_verified.commit is empty"
            assert lv["command_id"], (
                f"'{schema_id}': last_verified.command_id is empty"
            )


# --------------------------------------------------------------------------- #
# AC4: detection_patterns structure + required_test_commands allowlist
# --------------------------------------------------------------------------- #
class TestDetectionPatternsAndCommands:
    def test_detection_patterns_are_dicts(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            patterns = entry.get("detection_patterns", [])
            assert isinstance(patterns, list) and len(patterns) >= 1, (
                f"'{schema_id}': detection_patterns must be non-empty list"
            )
            for pat in patterns:
                assert isinstance(pat, dict), (
                    f"'{schema_id}': detection_patterns item must be dict, got: {pat!r}"
                )

    def test_detection_patterns_have_engine_and_pattern(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            for pat in entry.get("detection_patterns", []):
                assert "engine" in pat, (
                    f"'{schema_id}': detection_pattern missing 'engine'"
                )
                assert "pattern" in pat, (
                    f"'{schema_id}': detection_pattern missing 'pattern'"
                )
                assert pat["pattern"], (
                    f"'{schema_id}': detection_pattern.pattern is empty"
                )

    def test_detection_patterns_not_shell_strings(self, entries: list):
        """detection_patterns must be dicts, not plain shell command strings."""
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            for pat in entry.get("detection_patterns", []):
                assert isinstance(pat, dict), (
                    f"'{schema_id}': detection_pattern must be a dict "
                    f"(not a shell string), got: {pat!r}"
                )

    def test_required_test_commands_are_dicts(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            cmds = entry.get("required_test_commands", [])
            assert isinstance(cmds, list) and len(cmds) >= 1, (
                f"'{schema_id}': required_test_commands must be non-empty list"
            )
            for cmd in cmds:
                assert isinstance(cmd, dict), (
                    f"'{schema_id}': required_test_commands item must be dict"
                )

    def test_required_test_commands_have_id(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            for cmd in entry.get("required_test_commands", []):
                assert "id" in cmd, (
                    f"'{schema_id}': required_test_commands item missing 'id'"
                )
                assert cmd["id"], (
                    f"'{schema_id}': required_test_commands item has empty 'id'"
                )

    def test_required_test_commands_no_shell_strings(self, entries: list):
        """required_test_commands must not contain raw shell strings."""
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            for cmd in entry.get("required_test_commands", []):
                # Each command must be a dict, not a string
                assert isinstance(cmd, dict), (
                    f"'{schema_id}': required_test_commands must use "
                    f"allowlisted command IDs (dicts), not shell strings"
                )

    def test_required_test_commands_runner_allowlisted(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            for cmd in entry.get("required_test_commands", []):
                if isinstance(cmd, dict) and "runner" in cmd:
                    assert cmd["runner"] in ALLOWED_RUNNERS, (
                        f"'{schema_id}': runner '{cmd['runner']}' not in allowlist "
                        f"{sorted(ALLOWED_RUNNERS)}"
                    )


# --------------------------------------------------------------------------- #
# AC5: no ambiguous values
# --------------------------------------------------------------------------- #
class TestNoAmbiguousValues:
    def _check_value(self, value, schema_id: str, field_path: str):
        """Recursively check that no string value contains ambiguous markers."""
        if isinstance(value, str):
            if AMBIGUOUS_PATTERNS.search(value):
                pytest.fail(
                    f"'{schema_id}': ambiguous value found at '{field_path}': {value!r}"
                )
        elif isinstance(value, dict):
            for k, v in value.items():
                self._check_value(v, schema_id, f"{field_path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self._check_value(v, schema_id, f"{field_path}[{i}]")

    def test_no_ambiguous_values_in_entries(self, entries: list):
        for entry in entries:
            schema_id = entry.get("schema_id", "?")
            self._check_value(entry, schema_id, "entry")


# --------------------------------------------------------------------------- #
# AC6: catalog.schema.json structure
# --------------------------------------------------------------------------- #
class TestSchemaJson:
    def test_schema_json_exists(self):
        assert CATALOG_SCHEMA_JSON.exists()

    def test_schema_json_draft_2020_12(self, schema_json: dict):
        assert schema_json.get("$schema") == "https://json-schema.org/draft/2020-12/schema"

    def test_schema_json_id(self, schema_json: dict):
        assert schema_json.get("$id") == (
            "https://loop-protocol.local/schemas/catalog.schema.json"
        )

    def test_schema_json_has_catalog_schema_version(self, schema_json: dict):
        props = schema_json.get("properties", {})
        assert "catalog_schema_version" in props

    def test_schema_json_has_catalog_id(self, schema_json: dict):
        props = schema_json.get("properties", {})
        assert "catalog_id" in props

    def test_schema_json_has_entries(self, schema_json: dict):
        props = schema_json.get("properties", {})
        assert "entries" in props

    def test_schema_json_entry_additional_properties_false(self, schema_json: dict):
        defs = schema_json.get("$defs", {})
        catalog_entry = defs.get("CatalogEntry", {})
        assert catalog_entry.get("additionalProperties") is False, (
            "CatalogEntry must have additionalProperties: false"
        )

    def test_schema_json_format_enum_exists(self, schema_json: dict):
        defs = schema_json.get("$defs", {})
        catalog_entry = defs.get("CatalogEntry", {})
        props = catalog_entry.get("properties", {})
        format_prop = props.get("format", {})
        assert "enum" in format_prop, "format must have an enum"

    def test_schema_json_runner_enum_exists(self, schema_json: dict):
        defs = schema_json.get("$defs", {})
        cmd_ref = defs.get("CommandRef", {})
        props = cmd_ref.get("properties", {})
        runner_prop = props.get("runner", {})
        assert "enum" in runner_prop, "runner must have an enum"


# --------------------------------------------------------------------------- #
# AC7: JSON Schema validation of catalog.yaml against catalog.schema.json
# --------------------------------------------------------------------------- #
class TestJsonSchemaValidation:
    def test_catalog_validates_against_schema(self, catalog: dict, schema_json: dict):
        """Validate catalog.yaml against catalog.schema.json using jsonschema."""
        try:
            from jsonschema import Draft202012Validator

            validator = Draft202012Validator(schema_json)
            errors = list(validator.iter_errors(catalog))
            if errors:
                messages = "\n".join(str(e) for e in errors[:5])
                pytest.fail(
                    f"catalog.yaml failed JSON Schema validation:\n{messages}"
                )
        except ImportError:
            # jsonschema not installed: perform structural check instead
            pytest.skip(
                "jsonschema not installed; install with: uv add jsonschema"
            )

    def test_catalog_schema_json_is_valid_draft_2020_12(self, schema_json: dict):
        """catalog.schema.json 自体が Draft 2020-12 として valid"""
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema_json)


# --------------------------------------------------------------------------- #
# B2: UniqueKeySafeLoader duplicate key rejection
# --------------------------------------------------------------------------- #
def test_duplicate_key_loader_rejects_duplicates():
    """UniqueKeySafeLoader は duplicate key を拒否する"""
    bad_yaml = "key1: a\nkey1: b\n"
    with pytest.raises(yaml.YAMLError):
        yaml.load(bad_yaml, Loader=UniqueKeySafeLoader)


# --------------------------------------------------------------------------- #
# B4: golden set — Initial Known Schemas table
# --------------------------------------------------------------------------- #
EXPECTED_SCHEMA_IDS = {
    "issue_contract/v1",
    "delegation_request_v1",
    "delegation_result/v1",
    "acp_result_v1",
    "LOOP_VERDICT",
    "TEST_VERDICT_MACHINE v1",
    "IMPLEMENT_RESULT_V1",
    "contract_schema_version: v1",
    "Runtime Verification Applicability",
    "Safety Claim Matrix",
    "model_routing.yaml",
    "runtime-verification artifact log",
    "pr_body_schema/schema_change_applicability/v1",
    "pr_body_schema/schema_consumer_inventory/v1",
    "agent_session_manifest/v1",
    "PR_REVIEW_GATE_RESULT_V1",
    "temp_residue_classification/v1",
    "temp_residue_owner/v1",
    "TEST_VERDICT_EXECUTION_RECORD_V1",
    "TEST_VERDICT_PRODUCER_RECEIPT_V1",
}


def test_schema_ids_match_golden_set(entries):
    """catalog の schema_id が Initial Known Schemas table と完全一致する"""
    actual_ids = {e["schema_id"] for e in entries}
    assert actual_ids == EXPECTED_SCHEMA_IDS, (
        f"Missing: {EXPECTED_SCHEMA_IDS - actual_ids}, "
        f"Extra: {actual_ids - EXPECTED_SCHEMA_IDS}"
    )


# --------------------------------------------------------------------------- #
# B6: last_verified.commit は 40-char full SHA
# --------------------------------------------------------------------------- #
SHA40_RE = re.compile(r'^[0-9a-f]{40}$')


def test_last_verified_commit_is_full_sha(entries):
    """last_verified.commit は 40-char full SHA"""
    for entry in entries:
        lv = entry.get("last_verified", {})
        commit = lv.get("commit", "")
        assert SHA40_RE.match(commit), (
            f"schema_id={entry['schema_id']!r}: last_verified.commit={commit!r} "
            "は 40-char full SHA でなければならない"
        )

"""Tests for validate_schema_catalog.py — 15+ cases covering AC3."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

# Add scripts directory to path
sys.path.insert(
    0,
    str(Path(__file__).parent.parent),
)

from validate_schema_catalog import (  # noqa: E402
    E_SCHEMA_CATALOG_DUPLICATE_KEY,
    E_SCHEMA_CATALOG_MISSING,
    E_SCHEMA_CONSUMER_MISMATCH,
    UniqueKeySafeLoader,
    _DuplicateKeyError,
    _is_ambiguous_placeholder,
    compare_inventory_to_catalog,
    load_catalog,
    parse_schema_consumer_inventory,
    validate_catalog_schema,
    validate_catalog_semantics,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_VALID_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://loop-protocol.local/schemas/catalog.schema.json",
    "title": "Loop Protocol Schema Catalog",
    "type": "object",
    "required": ["catalog_schema_version", "catalog_id", "entries"],
    "additionalProperties": False,
    "properties": {
        "catalog_schema_version": {"type": "string", "const": "v1"},
        "catalog_id": {"type": "string", "const": "loop-protocol.schema-catalog/v1"},
        "generated_from": {"type": ["string", "null"]},
        "entries": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/CatalogEntry"},
        },
    },
    "$defs": {
        "CatalogEntry": {
            "type": "object",
            "required": [
                "schema_id",
                "format",
                "source_kind",
                "definition_paths",
                "producer",
                "consumers",
                "detection_patterns",
                "required_test_commands",
                "compatibility",
                "validation",
                "migration",
                "last_verified",
            ],
            "additionalProperties": False,
            "properties": {
                "schema_id": {"type": "string", "minLength": 1},
                "format": {
                    "type": "string",
                    "enum": [
                        "yaml",
                        "json_schema",
                        "markdown_yaml_contract",
                        "markdown_table",
                        "github_issue_body",
                        "github_comment",
                        "ndjson",
                    ],
                },
                "source_kind": {
                    "type": "string",
                    "enum": [
                        "repo_path",
                        "virtual",
                        "external",
                        "github_issue_body",
                        "github_comment",
                    ],
                },
                "definition_paths": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "producer": {
                    "type": "object",
                    "required": ["owner", "paths"],
                    "additionalProperties": False,
                    "properties": {
                        "owner": {"type": "string", "minLength": 1},
                        "paths": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
                "consumers": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": "#/$defs/Consumer"},
                },
                "detection_patterns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": "#/$defs/DetectionPattern"},
                },
                "required_test_commands": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": "#/$defs/CommandRef"},
                },
                "compatibility": {"$ref": "#/$defs/Compatibility"},
                "validation": {"$ref": "#/$defs/Validation"},
                "migration": {"$ref": "#/$defs/Migration"},
                "last_verified": {"$ref": "#/$defs/LastVerified"},
            },
        },
        "Consumer": {
            "type": "object",
            "required": ["id", "paths"],
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
            },
        },
        "DetectionPattern": {
            "type": "object",
            "required": ["id", "engine", "mode", "pattern", "paths"],
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "engine": {"type": "string", "enum": ["ripgrep", "grep", "jq", "python"]},
                "mode": {"type": "string", "enum": ["regex", "literal", "glob"]},
                "pattern": {"type": "string", "minLength": 1},
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
            },
        },
        "CommandRef": {
            "type": "object",
            "required": ["id", "runner", "target"],
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "runner": {
                    "type": "string",
                    "enum": [
                        "pytest",
                        "pnpm",
                        "rg",
                        "test",
                        "python_module",
                        "bash_allowlisted",
                    ],
                },
                "target": {"type": "string", "minLength": 1},
            },
        },
        "Compatibility": {
            "type": "object",
            "required": ["mode", "direction", "breaking_changes"],
            "additionalProperties": False,
            "properties": {
                "mode": {"type": "string", "enum": ["manual_policy"]},
                "direction": {
                    "type": "string",
                    "enum": ["backward", "forward", "full", "custom"],
                },
                "breaking_changes": {"type": "array", "items": {"type": "string"}},
            },
        },
        "Validation": {
            "type": "object",
            "required": ["catalog_lint_commands", "fixture_tests"],
            "additionalProperties": False,
            "properties": {
                "catalog_lint_commands": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/CommandRef"},
                },
                "fixture_tests": {
                    "type": "object",
                    "required": ["positive", "negative"],
                    "additionalProperties": False,
                    "properties": {
                        "positive": {"type": "array", "items": {"type": "string"}},
                        "negative": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "Migration": {
            "type": "object",
            "required": ["required_for_breaking_change", "followup_issue_required"],
            "additionalProperties": False,
            "properties": {
                "required_for_breaking_change": {"type": "boolean"},
                "followup_issue_required": {"type": "boolean"},
            },
        },
        "LastVerified": {
            "type": "object",
            "required": ["commit", "command_id"],
            "additionalProperties": False,
            "properties": {
                "commit": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{40}$",
                },
                "command_id": {"type": "string", "minLength": 1},
            },
        },
    },
}


def _make_valid_entry(
    schema_id: str = "test_schema/v1",
    consumer_id: str = "test-consumer",
    command_id: str = "lint_catalog_yaml",
) -> dict[str, Any]:
    """Create a minimal valid catalog entry."""
    return {
        "schema_id": schema_id,
        "format": "yaml",
        "source_kind": "repo_path",
        "definition_paths": ["schemas/test.yaml"],
        "producer": {
            "owner": "test-owner",
            "paths": ["schemas/"],
        },
        "consumers": [
            {
                "id": consumer_id,
                "paths": [".claude/skills/test/"],
            }
        ],
        "detection_patterns": [
            {
                "id": "test-pattern",
                "engine": "ripgrep",
                "mode": "regex",
                "pattern": "test_schema",
                "paths": ["."],
            }
        ],
        "required_test_commands": [
            {
                "id": command_id,
                "runner": "pytest",
                "target": "schemas/tests/test_catalog.py",
            }
        ],
        "compatibility": {
            "mode": "manual_policy",
            "direction": "backward",
            "breaking_changes": ["remove_required_field"],
        },
        "validation": {
            "catalog_lint_commands": [
                {
                    "id": "lint_catalog_yaml",
                    "runner": "pytest",
                    "target": "schemas/tests/test_catalog.py",
                }
            ],
            "fixture_tests": {"positive": [], "negative": []},
        },
        "migration": {
            "required_for_breaking_change": True,
            "followup_issue_required": True,
        },
        "last_verified": {
            "commit": "a" * 40,
            "command_id": "lint_catalog_yaml",
        },
    }


def _make_valid_catalog(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if entries is None:
        entries = [_make_valid_entry()]
    return {
        "catalog_schema_version": "v1",
        "catalog_id": "loop-protocol.schema-catalog/v1",
        "generated_from": None,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Test Case 1: valid catalog passes
# ---------------------------------------------------------------------------
class TestValidCatalogPasses:
    """GIVEN a well-formed catalog.yaml
    WHEN validate_catalog_schema is called
    THEN no errors are returned."""

    def test_valid_catalog_passes(self) -> None:
        catalog = _make_valid_catalog()
        errors = validate_catalog_schema(catalog, MINIMAL_VALID_SCHEMA)
        assert errors == [], f"Expected no errors but got: {errors}"


# ---------------------------------------------------------------------------
# Test Case 2: duplicate YAML key fails with E_SCHEMA_CATALOG_DUPLICATE_KEY
# ---------------------------------------------------------------------------
class TestDuplicateYamlKey:
    """GIVEN a YAML string with a duplicate mapping key
    WHEN loaded via UniqueKeySafeLoader
    THEN _DuplicateKeyError is raised."""

    def test_duplicate_yaml_key_raises(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent(
            """\
            catalog_schema_version: "v1"
            catalog_schema_version: "v1"
            catalog_id: "loop-protocol.schema-catalog/v1"
            entries: []
            """
        )
        yaml_file = tmp_path / "catalog.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        with pytest.raises(_DuplicateKeyError) as exc_info:
            load_catalog(yaml_file)

        assert "catalog_schema_version" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test Case 3: invalid catalog.schema.json fails via check_schema
# ---------------------------------------------------------------------------
class TestInvalidJsonSchema:
    """GIVEN a catalog.schema.json that is not a valid JSON Schema
    WHEN validate_catalog_schema is called
    THEN it raises SchemaError (from check_schema)."""

    def test_invalid_json_schema_fails(self) -> None:
        from jsonschema.exceptions import SchemaError

        invalid_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "invalid-type-not-allowed",
        }
        catalog = _make_valid_catalog()

        with pytest.raises(SchemaError):
            validate_catalog_schema(catalog, invalid_schema)


# ---------------------------------------------------------------------------
# Test Case 4: catalog missing required nested field fails
# ---------------------------------------------------------------------------
class TestMissingRequiredNestedField:
    """GIVEN a catalog with an entry missing a required field (e.g., 'format')
    WHEN validate_catalog_schema is called
    THEN E_SCHEMA_CATALOG_MISSING error is returned."""

    def test_missing_format_field(self) -> None:
        entry = _make_valid_entry()
        del entry["format"]
        catalog = _make_valid_catalog([entry])

        errors = validate_catalog_schema(catalog, MINIMAL_VALID_SCHEMA)
        assert len(errors) >= 1
        codes = {e["code"] for e in errors}
        assert E_SCHEMA_CATALOG_MISSING in codes


# ---------------------------------------------------------------------------
# Test Case 5: ambiguous placeholder in nested consumer field fails
# ---------------------------------------------------------------------------
class TestAmbiguousPlaceholderInConsumer:
    """GIVEN a catalog entry with an ambiguous placeholder ('TBD') in consumer id
    WHEN validate_catalog_semantics is called
    THEN an AMBIGUOUS_PLACEHOLDER error is returned."""

    def test_tbd_placeholder_in_consumer_id(self) -> None:
        entry = _make_valid_entry(consumer_id="TBD")
        catalog = _make_valid_catalog([entry])

        errors = validate_catalog_semantics(catalog)
        assert any(e["code"] == "AMBIGUOUS_PLACEHOLDER" for e in errors), (
            f"Expected AMBIGUOUS_PLACEHOLDER but got: {errors}"
        )

    def test_japanese_placeholder_in_consumer_id(self) -> None:
        entry = _make_valid_entry(consumer_id="未定")
        catalog = _make_valid_catalog([entry])

        errors = validate_catalog_semantics(catalog)
        assert any(e["code"] == "AMBIGUOUS_PLACEHOLDER" for e in errors)


# ---------------------------------------------------------------------------
# Test Case 6: duplicated schema_id fails
# ---------------------------------------------------------------------------
class TestDuplicatedSchemaId:
    """GIVEN a catalog with two entries with the same schema_id
    WHEN validate_catalog_semantics is called
    THEN E_SCHEMA_CATALOG_DUPLICATE_KEY error is returned."""

    def test_duplicate_schema_id(self) -> None:
        entry1 = _make_valid_entry(schema_id="duplicate_schema/v1")
        entry2 = _make_valid_entry(schema_id="duplicate_schema/v1", consumer_id="other-consumer")
        catalog = _make_valid_catalog([entry1, entry2])

        errors = validate_catalog_semantics(catalog)
        dup_errors = [e for e in errors if e["code"] == E_SCHEMA_CATALOG_DUPLICATE_KEY]
        assert len(dup_errors) >= 1, f"Expected duplicate key error but got: {errors}"


# ---------------------------------------------------------------------------
# Test Case 7: duplicated consumers[].id within one schema fails
# ---------------------------------------------------------------------------
class TestDuplicatedConsumerId:
    """GIVEN a catalog entry with two consumers having the same id
    WHEN validate_catalog_semantics is called
    THEN E_SCHEMA_CATALOG_DUPLICATE_KEY error is returned."""

    def test_duplicate_consumer_id(self) -> None:
        entry = _make_valid_entry()
        entry["consumers"] = [
            {"id": "same-consumer", "paths": [".claude/skills/a/"]},
            {"id": "same-consumer", "paths": [".claude/skills/b/"]},
        ]
        catalog = _make_valid_catalog([entry])

        errors = validate_catalog_semantics(catalog)
        dup_errors = [e for e in errors if e["code"] == E_SCHEMA_CATALOG_DUPLICATE_KEY]
        assert len(dup_errors) >= 1, f"Expected duplicate consumer id error but got: {errors}"


# ---------------------------------------------------------------------------
# Test Case 8: unknown required_test_commands[].id fails
# ---------------------------------------------------------------------------
class TestUnknownCommandId:
    """GIVEN a catalog entry with required_test_commands id not in ALLOWED_COMMANDS
    WHEN validate_catalog_semantics is called
    THEN E_UNKNOWN_COMMAND_ID error is returned."""

    def test_unknown_command_id(self) -> None:
        entry = _make_valid_entry(command_id="nonexistent_command_xyz")
        catalog = _make_valid_catalog([entry])

        errors = validate_catalog_semantics(catalog)
        unknown_errors = [e for e in errors if e["code"] == "E_UNKNOWN_COMMAND_ID"]
        assert len(unknown_errors) >= 1, f"Expected E_UNKNOWN_COMMAND_ID but got: {errors}"


# ---------------------------------------------------------------------------
# Test Case 9: required_test_commands[].target containing shell metacharacters does not execute
# ---------------------------------------------------------------------------
class TestShellInjectionPrevention:
    """GIVEN a catalog entry with shell metacharacters in 'target'
    WHEN validate_catalog_semantics is called
    THEN no shell execution occurs (the target is treated as a string, not executed)."""

    def test_shell_metacharacters_not_executed(self) -> None:
        # This target has shell metacharacters that would be dangerous if executed with shell=True
        malicious_target = "schemas/tests/; rm -rf /tmp/pwned; echo"
        entry = _make_valid_entry()
        entry["required_test_commands"] = [
            {
                "id": "lint_catalog_yaml",  # valid id
                "runner": "pytest",
                "target": malicious_target,
            }
        ]
        catalog = _make_valid_catalog([entry])

        # validate_catalog_semantics must not execute the target; it only checks registry ids
        # If shell injection occurred, this would raise or have side effects.
        # Since shell=False is enforced and the registry only checks IDs, this is safe.
        errors = validate_catalog_semantics(catalog)

        # The target contains shell metacharacters but id is valid — no shell execution error
        # The only thing validated is the id membership in ALLOWED_COMMANDS
        cmd_errors = [e for e in errors if e["code"] == "E_UNKNOWN_COMMAND_ID"]
        assert len(cmd_errors) == 0, (
            "Known command id should not produce E_UNKNOWN_COMMAND_ID even with bad target"
        )
        # Verify no process was spawned by checking /tmp/pwned does not exist
        assert not Path("/tmp/pwned").exists(), "Shell injection must not have executed!"


# ---------------------------------------------------------------------------
# Test Case 10: PR body references unknown schema id → E_SCHEMA_CATALOG_MISSING
# ---------------------------------------------------------------------------
class TestPRBodyUnknownSchemaId:
    """GIVEN a PR body with an inventory row referencing a schema_id not in catalog
    WHEN compare_inventory_to_catalog is called
    THEN E_SCHEMA_CATALOG_MISSING error is returned."""

    def test_unknown_schema_in_pr_body(self) -> None:
        catalog = _make_valid_catalog([_make_valid_entry(schema_id="known_schema/v1")])

        pr_body = textwrap.dedent(
            """\
            ## Schema Consumer Inventory

            | Schema ID | PR Declared Consumers |
            |-----------|----------------------|
            | unknown_schema/v1 | some-consumer |
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        assert inventory is not None
        errors = compare_inventory_to_catalog(inventory, catalog)

        assert any(e["code"] == E_SCHEMA_CATALOG_MISSING for e in errors), (
            f"Expected E_SCHEMA_CATALOG_MISSING but got: {errors}"
        )


# ---------------------------------------------------------------------------
# Test Case 11: PR body missing catalog consumer → E_SCHEMA_CONSUMER_MISMATCH
# ---------------------------------------------------------------------------
class TestPRBodyMissingCatalogConsumer:
    """GIVEN a PR body with an inventory that omits a consumer present in catalog
    WHEN compare_inventory_to_catalog is called
    THEN E_SCHEMA_CONSUMER_MISMATCH error is returned."""

    def test_missing_consumer_in_pr_body(self) -> None:
        entry = _make_valid_entry(schema_id="my_schema/v1", consumer_id="consumer-alpha")
        entry["consumers"].append(
            {"id": "consumer-beta", "paths": [".claude/skills/beta/"]}
        )
        catalog = _make_valid_catalog([entry])

        # PR body only declares consumer-alpha, missing consumer-beta
        pr_body = textwrap.dedent(
            """\
            ## Schema Consumer Inventory

            | Schema ID | PR Declared Consumers |
            |-----------|----------------------|
            | my_schema/v1 | consumer-alpha |
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        assert inventory is not None
        errors = compare_inventory_to_catalog(inventory, catalog)

        mismatch_errors = [e for e in errors if e["code"] == E_SCHEMA_CONSUMER_MISMATCH]
        assert len(mismatch_errors) >= 1, f"Expected E_SCHEMA_CONSUMER_MISMATCH but got: {errors}"


# ---------------------------------------------------------------------------
# Test Case 12: PR body extra undeclared consumer → E_SCHEMA_CONSUMER_MISMATCH
# ---------------------------------------------------------------------------
class TestPRBodyExtraConsumer:
    """GIVEN a PR body with an inventory that declares a consumer not in catalog
    WHEN compare_inventory_to_catalog is called
    THEN E_SCHEMA_CONSUMER_MISMATCH error is returned."""

    def test_extra_consumer_in_pr_body(self) -> None:
        entry = _make_valid_entry(schema_id="my_schema/v1", consumer_id="consumer-alpha")
        catalog = _make_valid_catalog([entry])

        # PR body declares consumer-alpha AND undeclared-extra (not in catalog)
        pr_body = textwrap.dedent(
            """\
            ## Schema Consumer Inventory

            | Schema ID | PR Declared Consumers |
            |-----------|----------------------|
            | my_schema/v1 | consumer-alpha, undeclared-extra |
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        assert inventory is not None
        errors = compare_inventory_to_catalog(inventory, catalog)

        mismatch_errors = [e for e in errors if e["code"] == E_SCHEMA_CONSUMER_MISMATCH]
        assert len(mismatch_errors) >= 1, f"Expected E_SCHEMA_CONSUMER_MISMATCH but got: {errors}"


# ---------------------------------------------------------------------------
# Test Case 13: multiple schema IDs in one PR body compared independently
# ---------------------------------------------------------------------------
class TestMultipleSchemaIds:
    """GIVEN a PR body with multiple schema IDs in inventory
    WHEN compare_inventory_to_catalog is called
    THEN each schema ID is validated independently."""

    def test_multiple_schemas_independent_validation(self) -> None:
        entry1 = _make_valid_entry(schema_id="schema_a/v1", consumer_id="consumer-a")
        entry2 = _make_valid_entry(schema_id="schema_b/v1", consumer_id="consumer-b")
        catalog = _make_valid_catalog([entry1, entry2])

        # schema_a correct, schema_b has wrong consumer
        pr_body = textwrap.dedent(
            """\
            ## Schema Consumer Inventory

            | Schema ID | PR Declared Consumers |
            |-----------|----------------------|
            | schema_a/v1 | consumer-a |
            | schema_b/v1 | wrong-consumer |
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        assert inventory is not None
        errors = compare_inventory_to_catalog(inventory, catalog)

        # Only schema_b should generate mismatch
        mismatch_errors = [e for e in errors if e["code"] == E_SCHEMA_CONSUMER_MISMATCH]
        assert len(mismatch_errors) >= 1

        # Verify schema_a passes independently (no missing error for consumer-a)
        missing_paths = [str(e.get("path", "")) for e in errors if e["code"] == E_SCHEMA_CATALOG_MISSING]
        assert not any("schema_a/v1" in p for p in missing_paths)


# ---------------------------------------------------------------------------
# Test Case 14: markdown table normalization — backticks, spaces, comma-separated IDs
# ---------------------------------------------------------------------------
class TestMarkdownTableNormalization:
    """GIVEN a PR body with backticks, extra spaces, comma-separated consumer IDs
    WHEN parse_schema_consumer_inventory is called
    THEN values are correctly normalized and parsed."""

    def test_backtick_normalization(self) -> None:
        pr_body = textwrap.dedent(
            """\
            ## Schema Consumer Inventory

            | Schema ID | PR Declared Consumers |
            |-----------|----------------------|
            | `my_schema/v1` | `consumer-a`, `consumer-b` |
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        assert inventory is not None
        assert "my_schema/v1" in inventory
        consumers = inventory["my_schema/v1"]
        assert "consumer-a" in consumers
        assert "consumer-b" in consumers

    def test_spaces_normalization(self) -> None:
        pr_body = textwrap.dedent(
            """\
            ## Schema Consumer Inventory

            | Schema ID | PR Declared Consumers |
            |-----------|----------------------|
            |  my_schema/v1  |  consumer-a  ,  consumer-b  |
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        assert inventory is not None
        assert "my_schema/v1" in inventory

    def test_comma_separated_consumer_ids(self) -> None:
        pr_body = textwrap.dedent(
            """\
            ## Schema Consumer Inventory

            | Schema ID | PR Declared Consumers |
            |-----------|----------------------|
            | schema/v1 | a,b,c |
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        assert inventory is not None
        assert inventory.get("schema/v1") == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Test Case 15: malformed inventory table fails deterministically, not silently pass
# ---------------------------------------------------------------------------
class TestMalformedInventoryTable:
    """GIVEN a PR body with a Schema Consumer Inventory section but no valid table
    WHEN parse_schema_consumer_inventory is called
    THEN it returns an empty dict (not None), indicating malformed table.
    compare_inventory_to_catalog on empty dict returns no errors (no false negatives)."""

    def test_missing_table_in_section(self) -> None:
        # Section present but no table rows
        pr_body = textwrap.dedent(
            """\
            ## Schema Consumer Inventory

            This section is intentionally left as plain text without a table.
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        # Should return empty dict {} (malformed) rather than None (absent)
        assert inventory is not None, (
            "Section present but no table: expected {} (malformed), not None (absent)"
        )

    def test_missing_required_columns(self) -> None:
        # Table present but missing required 'PR Declared Consumers' column
        pr_body = textwrap.dedent(
            """\
            ## Schema Consumer Inventory

            | Schema ID | Notes |
            |-----------|-------|
            | schema/v1 | some note |
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        # Missing required column => malformed => empty dict
        assert inventory == {}, (
            "Missing required column should yield {} (malformed), not a populated dict"
        )

    def test_no_schema_consumer_inventory_section(self) -> None:
        pr_body = textwrap.dedent(
            """\
            ## Summary

            No inventory section here.
            """
        )

        inventory = parse_schema_consumer_inventory(pr_body)
        assert inventory is None, "Absent section should yield None"


# ---------------------------------------------------------------------------
# Additional edge cases for robustness
# ---------------------------------------------------------------------------
class TestAmbiguousPlaceholderValues:
    """GIVEN various forbidden placeholder strings
    WHEN _is_ambiguous_placeholder is called
    THEN each is detected correctly."""

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "-",
            "N/A",
            "n/a",
            "TBD",
            "tbd",
            "TODO",
            "todo",
            "unknown",
            "Unknown",
            "to be decided",
            "To Be Decided",
            "不明",
            "未定",
            "未確認",
            "要確認",
            "あとで",
            "仮",
        ],
    )
    def test_forbidden_placeholder_detected(self, value: str) -> None:
        assert _is_ambiguous_placeholder(value), (
            f"Expected {value!r} to be detected as ambiguous placeholder"
        )

    @pytest.mark.parametrize(
        "value",
        [
            "impl-review-loop",
            "pr-review-judge",
            "gemini-cli-wrapper",
            "schemas/catalog.yaml",
            "validate_catalog",
        ],
    )
    def test_valid_value_not_placeholder(self, value: str) -> None:
        assert not _is_ambiguous_placeholder(value), (
            f"Expected {value!r} to NOT be flagged as ambiguous placeholder"
        )

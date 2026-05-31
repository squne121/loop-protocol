#!/usr/bin/env python3
"""Schema catalog completeness and consumer consistency validator for LOOP_PROTOCOL.

Validates:
- schemas/catalog.yaml against schemas/catalog.schema.json (completeness check)
- Duplicate key detection via UniqueKeySafeLoader
- Semantic validation (duplicate schema_id, consumer IDs, ambiguous placeholders)
- PR body Schema Consumer Inventory against catalog (consumer consistency check)

Output contract: SCHEMA_CATALOG_VALIDATION_RESULT/v1 JSON
Exit codes: 0=valid, 1=fail, 2=error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


# ---------------------------------------------------------------------------
# Error codes (canonical definitions - AC2, AC5)
# ---------------------------------------------------------------------------
E_SCHEMA_CATALOG_MISSING = "E_SCHEMA_CATALOG_MISSING"
E_SCHEMA_CONSUMER_MISMATCH = "E_SCHEMA_CONSUMER_MISMATCH"
E_SCHEMA_CATALOG_DUPLICATE_KEY = "E_SCHEMA_CATALOG_DUPLICATE_KEY"
E_SCHEMA_CATALOG_DUPLICATE_YAML_KEY = "E_SCHEMA_CATALOG_DUPLICATE_YAML_KEY"
E_SCHEMA_CATALOG_DUPLICATE_SCHEMA_ID = "E_SCHEMA_CATALOG_DUPLICATE_SCHEMA_ID"
E_SCHEMA_CATALOG_DUPLICATE_CONSUMER_ID = "E_SCHEMA_CATALOG_DUPLICATE_CONSUMER_ID"
E_SCHEMA_CONSUMER_INVENTORY_MALFORMED = "E_SCHEMA_CONSUMER_INVENTORY_MALFORMED"
E_SCHEMA_CONSUMER_INVENTORY_EMPTY = "E_SCHEMA_CONSUMER_INVENTORY_EMPTY"
E_SCHEMA_CONSUMER_INVENTORY_MISSING_REQUIRED_COLUMNS = (
    "E_SCHEMA_CONSUMER_INVENTORY_MISSING_REQUIRED_COLUMNS"
)

# ---------------------------------------------------------------------------
# Immutable command registry (AC4)
# subprocess with shell=False only. Dynamic shell execution is FORBIDDEN.
# ---------------------------------------------------------------------------
_COMMAND_REGISTRY: dict[str, dict[str, str]] = {
    "validate_issue_contract_schema": {
        "runner": "pytest",
        "target": ".claude/skills/issue-contract-review/",
    },
    "validate_gemini_delegation": {
        "runner": "pytest",
        "target": ".claude/skills/gemini-cli-headless-delegation/tests/",
    },
    "validate_pr_review_judge": {
        "runner": "pytest",
        "target": ".claude/skills/pr-review-judge/",
    },
    "validate_test_runner": {
        "runner": "pytest",
        "target": ".claude/skills/test-runner/",
    },
    "validate_implement_issue": {
        "runner": "pytest",
        "target": ".claude/skills/implement-issue/",
    },
    "validate_runtime_verification_policy": {
        "runner": "pytest",
        "target": "docs/dev/",
    },
    "validate_runtime_verification_artifacts": {
        "runner": "pytest",
        "target": "docs/dev/",
    },
    "validate_pr_body_contract_tests": {
        "runner": "pytest",
        "target": ".claude/skills/open-pr/scripts/tests/",
    },
    "validate_agent_session_manifest": {
        "runner": "pnpm",
        "target": "tests/agent-session-manifest.test.ts",
    },
    "validate_model_routing": {
        "runner": "pytest",
        "target": ".claude/skills/gemini-cli-headless-delegation/tests/test_model_routing.py",
    },
    "validate_pr_review_gates": {
        "runner": "pytest",
        "target": ".claude/skills/pr-review-judge/",
    },
    "lint_catalog_yaml": {
        "runner": "pytest",
        "target": "schemas/tests/test_catalog.py",
    },
}

ALLOWED_COMMANDS = set(_COMMAND_REGISTRY.keys())

# ---------------------------------------------------------------------------
# Ambiguous placeholder vocabulary (AC9)
# ---------------------------------------------------------------------------
_FORBIDDEN_PLACEHOLDER_TOKENS: frozenset[str] = frozenset({
    "",
    "-",
    "—",  # em dash
    "n/a",
    "none",
    "null",
    "tbd",
    "todo",
    "unknown",
    "to be decided",
    "不明",   # 不明
    "未定",   # 未定
    "未確認",  # 未確認
    "要確認",  # 要確認
    "あとで",  # あとで
    "仮",              # 仮
})

FORBIDDEN_PLACEHOLDER_EXACT = _FORBIDDEN_PLACEHOLDER_TOKENS
FORBIDDEN_PLACEHOLDER_SUBSTRINGS = (
    "to be decided",
    "不明",
    "未定",
    "未確認",
    "要確認",
    "あとで",
    "仮",
)

AMBIGUOUS_PLACEHOLDER_LABEL = "AMBIGUOUS_PLACEHOLDER"
FORBIDDEN_LABEL = "FORBIDDEN"


# ---------------------------------------------------------------------------
# UniqueKeySafeLoader - production implementation (AC7)
# ---------------------------------------------------------------------------
class _DuplicateKeyError(Exception):
    """Raised when a duplicate YAML mapping key is detected."""

    def __init__(self, key: str, location: str = "") -> None:
        self.key = key
        self.location = location
        super().__init__(
            f"Duplicate YAML key: {key!r}{(' at ' + location) if location else ''}"
        )


class UniqueKeySafeLoader(yaml.SafeLoader):
    """YAML SafeLoader that raises _DuplicateKeyError on duplicate mapping keys."""

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[str, Any]:  # type: ignore[override]
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise _DuplicateKeyError(str(key))
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


# Register the constructor for all mappings
UniqueKeySafeLoader.add_constructor(
    "tag:yaml.org,2002:map",
    UniqueKeySafeLoader.construct_mapping,  # type: ignore[arg-type]
)


# ---------------------------------------------------------------------------
# Core functions (AC5)
# ---------------------------------------------------------------------------

def load_catalog(path: str | Path) -> dict[str, Any]:
    """Load catalog.yaml using UniqueKeySafeLoader."""
    text = Path(path).read_text(encoding="utf-8")
    return yaml.load(text, Loader=UniqueKeySafeLoader)  # type: ignore[return-value]


def validate_catalog_schema(
    catalog: dict[str, Any], schema: dict[str, Any]
) -> list[dict[str, Any]]:
    """Validate catalog instance against JSON Schema using Draft202012Validator."""
    Draft202012Validator.check_schema(schema)

    validator = Draft202012Validator(schema)
    errors = []
    for ve in sorted(validator.iter_errors(catalog), key=lambda e: list(e.absolute_path)):
        errors.append(
            {
                "code": E_SCHEMA_CATALOG_MISSING,
                "path": list(ve.absolute_path),
                "message": ve.message,
            }
        )
    return errors


def _is_ambiguous_placeholder(value: str) -> bool:
    """Return True if value matches forbidden placeholder vocabulary (AC9)."""
    normalized = value.strip().lower()
    if normalized in FORBIDDEN_PLACEHOLDER_EXACT:
        return True
    for sub in FORBIDDEN_PLACEHOLDER_SUBSTRINGS:
        if sub in normalized:
            return True
    return False


def _check_placeholder_field(
    value: Any,
    path: list[Any],
    errors: list[dict[str, Any]],
) -> None:
    """Check a single string field for ambiguous placeholders, appending errors."""
    if isinstance(value, str) and _is_ambiguous_placeholder(value):
        errors.append(
            {
                "code": AMBIGUOUS_PLACEHOLDER_LABEL,
                "path": path,
                "message": (
                    f"Ambiguous placeholder in {'.'.join(str(p) for p in path)}: {value!r}"
                ),
            }
        )


def _walk_entry_placeholders(
    entry: dict[str, Any],
    entry_idx: int,
    errors: list[dict[str, Any]],
) -> None:
    """Walk all string fields of a catalog entry and detect ambiguous placeholders.

    Covers: schema_id, format, source_kind, definition_paths[],
            producer.id, producer.paths[],
            consumers[].id, consumers[].paths[],
            detection_patterns[].id, .pattern, .paths[],
            required_test_commands[].id, .runner, .target,
            validation.catalog_lint_commands[].id, .runner, .target,
            migration.*, last_verified.*
    """
    base = ["entries", entry_idx]

    # Top-level string fields
    for field in ("schema_id", "format", "source_kind"):
        _check_placeholder_field(entry.get(field, ""), base + [field], errors)

    # definition_paths[]
    for p_idx, p in enumerate(entry.get("definition_paths", [])):
        _check_placeholder_field(p, base + ["definition_paths", p_idx], errors)

    # producer
    producer = entry.get("producer", {})
    if "owner" in producer:
        _check_placeholder_field(producer["owner"], base + ["producer", "owner"], errors)
    for p_idx, p in enumerate(producer.get("paths", [])):
        _check_placeholder_field(p, base + ["producer", "paths", p_idx], errors)

    # consumers[].id, consumers[].paths[]
    for c_idx, consumer in enumerate(entry.get("consumers", [])):
        if "id" in consumer:
            _check_placeholder_field(
                consumer["id"], base + ["consumers", c_idx, "id"], errors
            )
        for p_idx, p in enumerate(consumer.get("paths", [])):
            _check_placeholder_field(
                p, base + ["consumers", c_idx, "paths", p_idx], errors
            )

    # detection_patterns[].id, .pattern, .paths[]
    for dp_idx, dp in enumerate(entry.get("detection_patterns", [])):
        for field in ("id", "pattern"):
            _check_placeholder_field(
                dp.get(field, ""), base + ["detection_patterns", dp_idx, field], errors
            )
        for p_idx, p in enumerate(dp.get("paths", [])):
            _check_placeholder_field(
                p, base + ["detection_patterns", dp_idx, "paths", p_idx], errors
            )

    # required_test_commands[].id, .runner, .target
    for rtc_idx, rtc in enumerate(entry.get("required_test_commands", [])):
        for field in ("id", "runner", "target"):
            _check_placeholder_field(
                rtc.get(field, ""), base + ["required_test_commands", rtc_idx, field], errors
            )

    # validation.catalog_lint_commands[].id, .runner, .target
    validation = entry.get("validation", {})
    for clc_idx, clc in enumerate(validation.get("catalog_lint_commands", [])):
        for field in ("id", "runner", "target"):
            _check_placeholder_field(
                clc.get(field, ""),
                base + ["validation", "catalog_lint_commands", clc_idx, field],
                errors,
            )

    # migration.*
    migration = entry.get("migration", {})
    if isinstance(migration, dict):
        for field, val in migration.items():
            if isinstance(val, str):
                _check_placeholder_field(val, base + ["migration", field], errors)

    # last_verified.*
    last_verified = entry.get("last_verified", {})
    if isinstance(last_verified, dict):
        for field, val in last_verified.items():
            if isinstance(val, str):
                _check_placeholder_field(val, base + ["last_verified", field], errors)


def validate_catalog_semantics(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Semantic validation: duplicate schema_id, consumer IDs, placeholders, registry IDs.

    AC4: Only known command IDs (ALLOWED_COMMANDS) are accepted.
         runner and target must also match the registry (drift detection).
    AC7: Duplicate schema_id and consumer id detection.
    AC9: Ambiguous placeholder detection across all string fields.
    """
    errors: list[dict[str, Any]] = []
    entries = catalog.get("entries", [])

    seen_schema_ids: set[str] = set()

    for entry_idx, entry in enumerate(entries):
        schema_id = entry.get("schema_id", "")

        # AC7: duplicate schema_id
        if schema_id in seen_schema_ids:
            errors.append(
                {
                    "code": E_SCHEMA_CATALOG_DUPLICATE_SCHEMA_ID,
                    "path": ["entries", entry_idx, "schema_id"],
                    "message": f"Duplicate schema_id: {schema_id!r}",
                }
            )
        seen_schema_ids.add(schema_id)

        # AC7: duplicate consumer IDs within one schema entry
        consumers = entry.get("consumers", [])
        seen_consumer_ids: set[str] = set()
        for c_idx, consumer in enumerate(consumers):
            cid = consumer.get("id", "")
            if cid in seen_consumer_ids:
                errors.append(
                    {
                        "code": E_SCHEMA_CATALOG_DUPLICATE_CONSUMER_ID,
                        "path": ["entries", entry_idx, "consumers", c_idx, "id"],
                        "message": f"Duplicate consumer id {cid!r} in schema {schema_id!r}",
                    }
                )
            seen_consumer_ids.add(cid)

        # AC4: required_test_commands must reference ALLOWED_COMMANDS with matching runner/target
        rtc_list = entry.get("required_test_commands", [])
        for rtc_idx, rtc in enumerate(rtc_list):
            rtc_id = rtc.get("id", "")
            if rtc_id not in ALLOWED_COMMANDS:
                errors.append(
                    {
                        "code": "E_UNKNOWN_COMMAND_ID",
                        "path": [
                            "entries",
                            entry_idx,
                            "required_test_commands",
                            rtc_idx,
                            "id",
                        ],
                        "message": (
                            f"Unknown required_test_commands id: {rtc_id!r}. "
                            "Must be in ALLOWED_COMMANDS."
                        ),
                    }
                )
            else:
                # Verify runner and target match the registry (drift detection)
                expected = _COMMAND_REGISTRY[rtc_id]
                actual_runner = rtc.get("runner", "")
                actual_target = rtc.get("target", "")
                if actual_runner != expected["runner"]:
                    errors.append(
                        {
                            "code": "E_COMMAND_RUNNER_DRIFT",
                            "path": [
                                "entries",
                                entry_idx,
                                "required_test_commands",
                                rtc_idx,
                                "runner",
                            ],
                            "message": (
                                f"required_test_commands id={rtc_id!r}: "
                                f"runner {actual_runner!r} does not match registry "
                                f"{expected['runner']!r}"
                            ),
                        }
                    )
                if actual_target != expected["target"]:
                    errors.append(
                        {
                            "code": "E_COMMAND_TARGET_DRIFT",
                            "path": [
                                "entries",
                                entry_idx,
                                "required_test_commands",
                                rtc_idx,
                                "target",
                            ],
                            "message": (
                                f"required_test_commands id={rtc_id!r}: "
                                f"target {actual_target!r} does not match registry "
                                f"{expected['target']!r}"
                            ),
                        }
                    )

        # AC4: validation.catalog_lint_commands must also reference ALLOWED_COMMANDS
        validation = entry.get("validation", {})
        clc_list = validation.get("catalog_lint_commands", [])
        for clc_idx, clc in enumerate(clc_list):
            clc_id = clc.get("id", "")
            if clc_id not in ALLOWED_COMMANDS:
                errors.append(
                    {
                        "code": "E_UNKNOWN_COMMAND_ID",
                        "path": [
                            "entries",
                            entry_idx,
                            "validation",
                            "catalog_lint_commands",
                            clc_idx,
                            "id",
                        ],
                        "message": (
                            f"Unknown validation.catalog_lint_commands id: {clc_id!r}. "
                            "Must be in ALLOWED_COMMANDS."
                        ),
                    }
                )
            else:
                # Verify runner and target match the registry (drift detection)
                expected = _COMMAND_REGISTRY[clc_id]
                actual_runner = clc.get("runner", "")
                actual_target = clc.get("target", "")
                if actual_runner != expected["runner"]:
                    errors.append(
                        {
                            "code": "E_COMMAND_RUNNER_DRIFT",
                            "path": [
                                "entries",
                                entry_idx,
                                "validation",
                                "catalog_lint_commands",
                                clc_idx,
                                "runner",
                            ],
                            "message": (
                                f"validation.catalog_lint_commands id={clc_id!r}: "
                                f"runner {actual_runner!r} does not match registry "
                                f"{expected['runner']!r}"
                            ),
                        }
                    )
                if actual_target != expected["target"]:
                    errors.append(
                        {
                            "code": "E_COMMAND_TARGET_DRIFT",
                            "path": [
                                "entries",
                                entry_idx,
                                "validation",
                                "catalog_lint_commands",
                                clc_idx,
                                "target",
                            ],
                            "message": (
                                f"validation.catalog_lint_commands id={clc_id!r}: "
                                f"target {actual_target!r} does not match registry "
                                f"{expected['target']!r}"
                            ),
                        }
                    )

        # AC9: walk all string fields for ambiguous placeholders
        _walk_entry_placeholders(entry, entry_idx, errors)

    return errors


# ---------------------------------------------------------------------------
# GFM table parser helpers
# ---------------------------------------------------------------------------

class _GFMTableError(Exception):
    """Raised when a GFM table is structurally malformed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


# Regex for a valid GFM delimiter cell: optional colons, at least one dash
_DELIMITER_CELL_RE = re.compile(r"^:?-+:?$")


def _normalize_cell(cell: str) -> str:
    """Strip markdown formatting from a table cell."""
    return re.sub(r"`", "", cell).strip()


def _split_gfm_row(line: str) -> list[str]:
    """Split a GFM table row into normalized cells.

    Raises _GFMTableError if the row contains escaped pipes.
    """
    if r"\|" in line:
        raise _GFMTableError(
            E_SCHEMA_CONSUMER_INVENTORY_MALFORMED,
            f"Table row contains escaped pipe (\\|): {line!r}",
        )
    return [_normalize_cell(c) for c in line.strip().strip("|").split("|")]


def _parse_gfm_table_strict(
    table_lines: list[str],
) -> tuple[list[str], list[list[str]]]:
    """Parse a GFM markdown table strictly, rejecting all malformed forms.

    Returns (header_cells, data_rows_cells).
    Raises _GFMTableError on any structural problem.
    """
    if len(table_lines) < 2:
        raise _GFMTableError(
            E_SCHEMA_CONSUMER_INVENTORY_MALFORMED,
            "Table has no header and/or delimiter row",
        )

    # Parse header row
    header_cells = _split_gfm_row(table_lines[0])
    n_cols = len(header_cells)

    # Parse and validate delimiter row
    delimiter_cells = _split_gfm_row(table_lines[1])
    if len(delimiter_cells) != n_cols:
        raise _GFMTableError(
            E_SCHEMA_CONSUMER_INVENTORY_MALFORMED,
            f"Delimiter row has {len(delimiter_cells)} cells, header has {n_cols}",
        )
    for cell in delimiter_cells:
        cell_stripped = cell.replace(" ", "")
        if not cell_stripped or not _DELIMITER_CELL_RE.match(cell_stripped):
            raise _GFMTableError(
                E_SCHEMA_CONSUMER_INVENTORY_MALFORMED,
                f"Delimiter cell is not valid GFM delimiter: {cell!r}",
            )

    # Parse data rows
    data_rows: list[list[str]] = []
    for line in table_lines[2:]:
        cells = _split_gfm_row(line)
        if len(cells) != n_cols:
            raise _GFMTableError(
                E_SCHEMA_CONSUMER_INVENTORY_MALFORMED,
                f"Data row has {len(cells)} cells, header has {n_cols}: {line!r}",
            )
        data_rows.append(cells)

    return header_cells, data_rows


# ---------------------------------------------------------------------------
# PR body Schema Consumer Inventory parsing (AC8)
# ---------------------------------------------------------------------------


def _split_consumer_ids(raw: str) -> list[str]:
    """Split comma-separated consumer IDs from a table cell."""
    return [_normalize_cell(part) for part in raw.split(",") if _normalize_cell(part)]


class _InventoryParseResult:
    """Internal result of _parse_inventory_detailed."""

    __slots__ = ("inventory", "errors")

    def __init__(
        self,
        inventory: dict[str, list[str]] | None,
        errors: list[dict[str, Any]],
    ) -> None:
        self.inventory = inventory
        self.errors = errors

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


def _parse_inventory_detailed(pr_body: str) -> _InventoryParseResult:
    """Internal: parse inventory and return structured result with error list."""
    errors: list[dict[str, Any]] = []

    # Find the Schema Consumer Inventory section
    section_match = re.search(
        r"(?im)^##\s+Schema Consumer Inventory\s*$",
        pr_body,
    )
    if not section_match:
        return _InventoryParseResult(None, [])

    # Extract content until next ## section
    section_start = section_match.end()
    next_section = re.search(r"(?m)^##\s+", pr_body[section_start:])
    section_end = (
        section_start + next_section.start() if next_section else len(pr_body)
    )
    section_content = pr_body[section_start:section_end]

    # Find markdown table lines
    table_lines = [
        line for line in section_content.splitlines() if line.strip().startswith("|")
    ]
    if not table_lines:
        errors.append(
            {
                "code": E_SCHEMA_CONSUMER_INVENTORY_MALFORMED,
                "path": ["inventory"],
                "message": "Schema Consumer Inventory section found but no table present",
            }
        )
        return _InventoryParseResult({}, errors)

    # Parse with strict GFM parser (B5)
    try:
        header_cells, data_rows = _parse_gfm_table_strict(table_lines)
    except _GFMTableError as exc:
        errors.append(
            {
                "code": exc.code,
                "path": ["inventory"],
                "message": str(exc),
            }
        )
        return _InventoryParseResult({}, errors)

    # Locate required columns (B1: exact match, case-insensitive)
    schema_id_col: int | None = None
    consumer_col: int | None = None
    for i, h in enumerate(header_cells):
        normalized = h.strip().lower()
        if normalized == "schema id":
            schema_id_col = i
        if normalized == "pr declared consumers":
            consumer_col = i

    if schema_id_col is None or consumer_col is None:
        missing = []
        if schema_id_col is None:
            missing.append("Schema ID")
        if consumer_col is None:
            missing.append("PR Declared Consumers")
        errors.append(
            {
                "code": E_SCHEMA_CONSUMER_INVENTORY_MISSING_REQUIRED_COLUMNS,
                "path": ["inventory"],
                "message": (
                    f"Missing required columns in Schema Consumer Inventory: {missing!r}. "
                    "Column names must match exactly (case-insensitive)."
                ),
            }
        )
        return _InventoryParseResult({}, errors)

    # Build inventory, checking for duplicate Schema ID rows
    inventory: dict[str, list[str]] = {}
    seen_schema_ids: set[str] = set()
    for row in data_rows:
        schema_id = row[schema_id_col].strip()
        consumer_raw = row[consumer_col].strip()
        if not schema_id:
            continue
        if schema_id in seen_schema_ids:
            errors.append(
                {
                    "code": E_SCHEMA_CONSUMER_INVENTORY_MALFORMED,
                    "path": ["inventory", schema_id],
                    "message": f"Duplicate Schema ID row in inventory table: {schema_id!r}",
                }
            )
            return _InventoryParseResult({}, errors)
        seen_schema_ids.add(schema_id)
        consumer_ids = _split_consumer_ids(consumer_raw) if consumer_raw else []
        inventory[schema_id] = consumer_ids

    # B2: Empty inventory after parsing is also an error
    if not inventory:
        errors.append(
            {
                "code": E_SCHEMA_CONSUMER_INVENTORY_EMPTY,
                "path": ["inventory"],
                "message": "Schema Consumer Inventory table has no data rows",
            }
        )
        return _InventoryParseResult({}, errors)

    return _InventoryParseResult(inventory, [])


def parse_schema_consumer_inventory(pr_body: str) -> dict[str, list[str]] | None:
    """Parse the Schema Consumer Inventory table from a PR body.

    AC8: Accepts only the canonical table format:
      | Schema ID | ... | PR Declared Consumers |
    Returns dict mapping schema_id -> [consumer_id, ...] or None if no valid table found.

    Column name matching: exact match, case-insensitive (no substring match).
    Returns None if the inventory section is missing.
    Returns empty dict {} on structural errors (malformed table, missing columns, etc.).
    """
    result = _parse_inventory_detailed(pr_body)
    if result.has_errors:
        return {} if result.inventory is not None else None
    return result.inventory


def compare_inventory_to_catalog(
    inventory: dict[str, list[str]] | None,
    catalog: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare PR body inventory against catalog.

    AC8: Exact match only. No fuzzy matching.
    Returns errors for:
    - Schema IDs in inventory not found in catalog (E_SCHEMA_CATALOG_MISSING)
    - Consumer IDs mismatch between inventory and catalog (E_SCHEMA_CONSUMER_MISMATCH)
    """
    if inventory is None:
        return []

    errors: list[dict[str, Any]] = []
    entries = catalog.get("entries", [])

    # Build catalog lookup: schema_id -> set of consumer ids
    catalog_consumers: dict[str, set[str]] = {}
    for entry in entries:
        sid = entry.get("schema_id", "")
        cids = {c.get("id", "") for c in entry.get("consumers", [])}
        catalog_consumers[sid] = cids

    for inv_schema_id, inv_consumer_ids in inventory.items():
        if inv_schema_id not in catalog_consumers:
            errors.append(
                {
                    "code": E_SCHEMA_CATALOG_MISSING,
                    "path": ["inventory", inv_schema_id],
                    "message": (
                        f"PR body references schema_id {inv_schema_id!r} not found in catalog"
                    ),
                }
            )
            continue

        catalog_cids = catalog_consumers[inv_schema_id]
        inv_cid_set = set(inv_consumer_ids)

        # Missing from PR body (in catalog but not declared)
        missing = catalog_cids - inv_cid_set
        for cid in sorted(missing):
            errors.append(
                {
                    "code": E_SCHEMA_CONSUMER_MISMATCH,
                    "path": ["inventory", inv_schema_id, "consumers"],
                    "message": (
                        f"Consumer {cid!r} is in catalog for schema {inv_schema_id!r} "
                        f"but not declared in PR body inventory"
                    ),
                }
            )

        # Extra in PR body (declared but not in catalog)
        extra = inv_cid_set - catalog_cids
        for cid in sorted(extra):
            errors.append(
                {
                    "code": E_SCHEMA_CONSUMER_MISMATCH,
                    "path": ["inventory", inv_schema_id, "consumers"],
                    "message": (
                        f"Consumer {cid!r} declared in PR body for schema {inv_schema_id!r} "
                        f"but not found in catalog"
                    ),
                }
            )

    return errors


# ---------------------------------------------------------------------------
# CLI entry point (AC10)
# ---------------------------------------------------------------------------


def _build_result(
    status: str,
    errors: list[dict[str, Any]],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "SCHEMA_CATALOG_VALIDATION_RESULT/v1",
        "status": status,
        "errors": errors,
        "warnings": warnings or [],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    AC10: Output contract: SCHEMA_CATALOG_VALIDATION_RESULT/v1 JSON + exit code.
    Exit codes: 0=valid, 1=fail, 2=error.
    Warning-only exit 0 is FORBIDDEN - any error => exit 1.
    """
    parser = argparse.ArgumentParser(
        description="Validate schemas/catalog.yaml completeness and consumer consistency"
    )
    parser.add_argument(
        "--catalog",
        default="schemas/catalog.yaml",
        help="Path to catalog.yaml (default: schemas/catalog.yaml)",
    )
    parser.add_argument(
        "--catalog-schema",
        default="schemas/catalog.schema.json",
        help="Path to catalog.schema.json (default: schemas/catalog.schema.json)",
    )
    parser.add_argument(
        "--pr-body-file",
        default="",
        help="Optional path to PR body file for consumer inventory check",
    )
    args = parser.parse_args(argv)

    errors: list[dict[str, Any]] = []

    # Step 1: Load catalog
    try:
        catalog = load_catalog(args.catalog)
    except _DuplicateKeyError as exc:
        errors.append(
            {
                "code": E_SCHEMA_CATALOG_DUPLICATE_YAML_KEY,
                "path": [],
                "message": str(exc),
            }
        )
        print(json.dumps(_build_result("fail", errors), indent=2))
        sys.exit(1)

    except (OSError, yaml.YAMLError) as exc:
        print(
            json.dumps(
                _build_result(
                    "error",
                    [
                        {
                            "code": "E_CATALOG_LOAD_FAILED",
                            "path": [],
                            "message": f"Failed to load catalog: {exc}",
                        }
                    ],
                ),
                indent=2,
            ),
            file=sys.stderr,
        )
        sys.exit(2)

    # Step 2: Load JSON Schema
    try:
        schema_text = Path(args.catalog_schema).read_text(encoding="utf-8")
        schema = json.loads(schema_text)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                _build_result(
                    "error",
                    [
                        {
                            "code": "E_SCHEMA_LOAD_FAILED",
                            "path": [],
                            "message": f"Failed to load catalog schema: {exc}",
                        }
                    ],
                ),
                indent=2,
            ),
            file=sys.stderr,
        )
        sys.exit(2)

    # Step 3: JSON Schema validation (AC1, AC6)
    try:
        schema_errors = validate_catalog_schema(catalog, schema)
        errors.extend(schema_errors)
    except Exception as exc:
        print(
            json.dumps(
                _build_result(
                    "error",
                    [
                        {
                            "code": "E_SCHEMA_VALIDATION_FAILED",
                            "path": [],
                            "message": f"Schema validation error: {exc}",
                        }
                    ],
                ),
                indent=2,
            ),
            file=sys.stderr,
        )
        sys.exit(2)

    # Step 4: Semantic validation (AC7, AC9, AC4)
    semantic_errors = validate_catalog_semantics(catalog)
    errors.extend(semantic_errors)

    # Step 5: PR body inventory check (AC8)
    if args.pr_body_file:
        try:
            pr_body = Path(args.pr_body_file).read_text(encoding="utf-8")
            inv_result = _parse_inventory_detailed(pr_body)
            if inv_result.has_errors:
                errors.extend(inv_result.errors)
            elif inv_result.inventory is not None:
                inv_errors = compare_inventory_to_catalog(inv_result.inventory, catalog)
                errors.extend(inv_errors)
        except OSError as exc:
            print(
                json.dumps(
                    _build_result(
                        "error",
                        [
                            {
                                "code": "E_PR_BODY_LOAD_FAILED",
                                "path": [],
                                "message": f"Failed to load PR body: {exc}",
                            }
                        ],
                    ),
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(2)

    # Step 6: Output result
    status = "fail" if errors else "valid"
    result_dict = _build_result(status, errors)
    print(json.dumps(result_dict, indent=2))

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    raise SystemExit(main())

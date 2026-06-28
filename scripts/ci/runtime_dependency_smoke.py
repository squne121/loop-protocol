#!/usr/bin/env python3
"""Runtime dependency smoke test.

Canonical invocation (AC4):
    uv run --isolated --locked --no-default-groups python scripts/ci/runtime_dependency_smoke.py

Verifies that runtime dependencies (pyyaml, jsonschema) are available in a fresh
isolated environment and that key runtime consumers work correctly (AC5).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def check_yaml() -> None:
    """AC5: yaml.safe_load behavioral check."""
    import yaml

    result = yaml.safe_load("key: value")
    expected = {"key": "value"}
    assert result == expected, f"yaml.safe_load failed: {result!r} != {expected!r}"
    print("OK: yaml.safe_load")


def check_jsonschema() -> None:
    """AC5: jsonschema.Draft202012Validator behavioral check."""
    import jsonschema

    jsonschema.Draft202012Validator.check_schema({"type": "object"})
    print("OK: jsonschema.Draft202012Validator.check_schema")


def check_mrc_contract_parser() -> None:
    """AC5: parse_machine_readable_contract behavioral check."""
    # Locate mrc_contract_parser.py relative to the repo root
    repo_root = Path(__file__).parent.parent.parent
    parser_path = (
        repo_root / ".claude" / "skills" / "create-issue" / "scripts" / "mrc_contract_parser.py"
    )

    if not parser_path.exists():
        raise FileNotFoundError(f"mrc_contract_parser.py not found at: {parser_path}")

    spec = importlib.util.spec_from_file_location("mrc_contract_parser", parser_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec from {parser_path}")

    import sys
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so dataclass __module__ lookup resolves
    sys.modules["mrc_contract_parser"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    valid_fixture = (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        "issue_kind: implementation\n"
        "```\n"
    )
    result = module.parse_machine_readable_contract(valid_fixture)
    assert result.ok, f"parse_machine_readable_contract failed: {result}"
    print("OK: parse_machine_readable_contract")


def main() -> int:
    checks = [check_yaml, check_jsonschema, check_mrc_contract_parser]
    failed = []

    for check in checks:
        try:
            check()
        except Exception as exc:
            print(f"FAIL: {check.__name__}: {exc}", file=sys.stderr)
            failed.append(check.__name__)

    if failed:
        print(f"FAILED: {failed}", file=sys.stderr)
        return 1

    print("All runtime dependency smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

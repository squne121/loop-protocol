#!/usr/bin/env python3
"""
verify_scope_rollup_result.py

Verify the integrity of an ISSUE_SCOPE_ROLLUP_PLAN_V2 JSON result file produced by
plan_issue_scope_rollup.py. Checks the self_validation block: schema name/version
and payload_sha256.

Usage:
    python3 verify_scope_rollup_result.py --result-json <path>

Exit codes:
    0  - STATUS: verified (sha match, schema match)
    10 - STATUS: sha_mismatch (payload_sha256 does not match recomputed hash)
    20 - STATUS: schema_mismatch (schema_name or schema_version mismatch)
    30 - STATUS: invalid_input (missing file, parse error, duplicate keys, not an object, etc.)

Error priority: invalid_input > schema_mismatch > sha_mismatch > verified

Stdout contract (fixed compact lines, raw plan JSON is NOT output):
    STATUS: verified|sha_mismatch|schema_mismatch|invalid_input
    SUMMARY: <1 line>
    RESULT_PATH: <path>
    PAYLOAD_SHA256: <actual>
    EXPECTED_PAYLOAD_SHA256: <expected>
    SCRIPT_FILE_SHA256: <actual>
    EXPECTED_SCRIPT_FILE_SHA256: <expected>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# Expected schema constants (must match plan_issue_scope_rollup.py)
EXPECTED_SCHEMA_NAME = "ISSUE_SCOPE_ROLLUP_PLAN_V2"
EXPECTED_SCHEMA_VERSION = 2

EXIT_VERIFIED = 0
EXIT_SHA_MISMATCH = 10
EXIT_SCHEMA_MISMATCH = 20
EXIT_INVALID_INPUT = 30


def _compute_payload_sha256(plan_without_self_validation: dict[str, Any]) -> str:
    """Recompute payload_sha256 using the same canonicalization as plan_issue_scope_rollup.py.

    Canonicalization: json.dumps with ensure_ascii=False, sort_keys=True,
    separators=(",", ":"), encoded as UTF-8.
    """
    canonical_bytes = json.dumps(
        plan_without_self_validation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def _load_json_strict(path: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load a JSON object from path, detecting duplicate keys.

    Returns (parsed_dict, error_message).
    error_message is None on success.
    Rejects: missing file, parse error, non-object JSON, duplicate keys.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"File not found: {path!r}"
    except OSError as exc:
        return None, f"Cannot read file {path!r}: {exc}"

    # Detect duplicate keys by counting via object_pairs_hook
    seen_keys: list[str] = []
    duplicate_keys: list[str] = []

    def check_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for key, value in pairs:
            if key in d:
                duplicate_keys.append(key)
            d[key] = value
            seen_keys.append(key)
        return d

    try:
        data = json.loads(raw, object_pairs_hook=check_duplicate)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"

    if duplicate_keys:
        return None, f"Duplicate JSON keys detected: {sorted(set(duplicate_keys))}"

    if not isinstance(data, dict):
        return None, f"Expected a JSON object, got {type(data).__name__}"

    return data, None


def _format_output(
    status: str,
    summary: str,
    result_path: str,
    actual_payload_sha256: str,
    expected_payload_sha256: str,
    actual_script_file_sha256: str,
    expected_script_file_sha256: str,
) -> str:
    """Format the fixed compact stdout output."""
    lines = [
        f"STATUS: {status}",
        f"SUMMARY: {summary}",
        f"RESULT_PATH: {result_path}",
        f"PAYLOAD_SHA256: {actual_payload_sha256}",
        f"EXPECTED_PAYLOAD_SHA256: {expected_payload_sha256}",
        f"SCRIPT_FILE_SHA256: {actual_script_file_sha256}",
        f"EXPECTED_SCRIPT_FILE_SHA256: {expected_script_file_sha256}",
    ]
    return "\n".join(lines)


def verify(result_json_path: str) -> tuple[str, int]:
    """Run verification on the result JSON file.

    Returns (output_text, exit_code).
    """
    plan, error = _load_json_strict(result_json_path)

    if error is not None:
        output = _format_output(
            status="invalid_input",
            summary=error,
            result_path=result_json_path,
            actual_payload_sha256="N/A",
            expected_payload_sha256="N/A",
            actual_script_file_sha256="N/A",
            expected_script_file_sha256="N/A",
        )
        return output, EXIT_INVALID_INPUT

    return _verify_plan_dict(plan, result_json_path)


def verify_payload(plan: dict[str, Any]) -> tuple[str, int]:
    """In-memory equivalent of :func:`verify` for a plan dict that has never
    been (and never needs to be) written to disk.

    Issue #1547 fix_delta (P0-2): the ``scope_rollup.run`` executor now
    invokes the planner and captures its stdout in-memory instead of writing
    a ``plan_result.json`` file; this function lets it (and any other
    caller, e.g. ``parse_scope_rollup_run_result.py``) run the exact same
    schema/payload_sha256/script_file_sha256 checks against that in-memory
    dict, with no filesystem involvement at all.

    Returns (output_text, exit_code) with the same semantics as
    :func:`verify` (``result_path`` in the output is rendered as
    ``"<in-memory>"``).
    """
    if not isinstance(plan, dict):
        output = _format_output(
            status="invalid_input",
            summary=f"Expected a dict, got {type(plan).__name__}",
            result_path="<in-memory>",
            actual_payload_sha256="N/A",
            expected_payload_sha256="N/A",
            actual_script_file_sha256="N/A",
            expected_script_file_sha256="N/A",
        )
        return output, EXIT_INVALID_INPUT
    return _verify_plan_dict(plan, "<in-memory>")


def _verify_plan_dict(plan: dict[str, Any], result_path_label: str) -> tuple[str, int]:
    """Shared core: verify self_validation/schema/payload_sha256 for an
    already-parsed plan dict, regardless of whether it came from a file
    (:func:`verify`) or from memory (:func:`verify_payload`)."""
    # Placeholder values for output (used on early exit)
    actual_payload_sha256 = "N/A"
    expected_payload_sha256 = "N/A"
    actual_script_file_sha256 = "N/A"
    expected_script_file_sha256 = "N/A"
    result_json_path = result_path_label

    # Extract self_validation block
    self_validation = plan.get("self_validation")
    if not isinstance(self_validation, dict):
        output = _format_output(
            status="invalid_input",
            summary="self_validation block is missing or not an object",
            result_path=result_json_path,
            actual_payload_sha256=actual_payload_sha256,
            expected_payload_sha256=expected_payload_sha256,
            actual_script_file_sha256=actual_script_file_sha256,
            expected_script_file_sha256=expected_script_file_sha256,
        )
        return output, EXIT_INVALID_INPUT

    expected_payload_sha256 = str(self_validation.get("payload_sha256", ""))
    expected_script_file_sha256 = str(self_validation.get("script_file_sha256", ""))
    schema_name = self_validation.get("schema_name", "")
    schema_version = self_validation.get("schema_version", None)

    # Check schema name/version (priority: schema_mismatch before sha_mismatch)
    schema_name_ok = schema_name == EXPECTED_SCHEMA_NAME
    schema_version_ok = schema_version == EXPECTED_SCHEMA_VERSION

    if not schema_name_ok or not schema_version_ok:
        mismatches: list[str] = []
        if not schema_name_ok:
            mismatches.append(f"schema_name {schema_name!r} != {EXPECTED_SCHEMA_NAME!r}")
        if not schema_version_ok:
            mismatches.append(f"schema_version {schema_version!r} != {EXPECTED_SCHEMA_VERSION!r}")
        output = _format_output(
            status="schema_mismatch",
            summary="; ".join(mismatches),
            result_path=result_json_path,
            actual_payload_sha256=actual_payload_sha256,
            expected_payload_sha256=expected_payload_sha256,
            actual_script_file_sha256=actual_script_file_sha256,
            expected_script_file_sha256=expected_script_file_sha256,
        )
        return output, EXIT_SCHEMA_MISMATCH

    # Recompute payload_sha256 (exclude self_validation block)
    plan_without_self_validation = {k: v for k, v in plan.items() if k != "self_validation"}
    actual_payload_sha256 = _compute_payload_sha256(plan_without_self_validation)

    # Recompute script_file_sha256 from the actual plan_issue_scope_rollup.py bytes
    plan_script = Path(__file__).with_name("plan_issue_scope_rollup.py")
    actual_script_file_sha256 = hashlib.sha256(plan_script.read_bytes()).hexdigest()

    payload_ok = actual_payload_sha256 == expected_payload_sha256
    script_ok = actual_script_file_sha256 == expected_script_file_sha256

    if not payload_ok or not script_ok:
        mismatches: list[str] = []
        if not payload_ok:
            mismatches.append(
                f"payload_sha256 mismatch: actual={actual_payload_sha256[:16]}... "
                f"expected={expected_payload_sha256[:16]}..."
            )
        if not script_ok:
            mismatches.append(
                f"script_file_sha256 mismatch: actual={actual_script_file_sha256[:16]}... "
                f"expected={expected_script_file_sha256[:16]}..."
            )
        output = _format_output(
            status="sha_mismatch",
            summary="; ".join(mismatches),
            result_path=result_json_path,
            actual_payload_sha256=actual_payload_sha256,
            expected_payload_sha256=expected_payload_sha256,
            actual_script_file_sha256=actual_script_file_sha256,
            expected_script_file_sha256=expected_script_file_sha256,
        )
        return output, EXIT_SHA_MISMATCH

    # All checks passed
    output = _format_output(
        status="verified",
        summary="payload_sha256 and schema verified",
        result_path=result_json_path,
        actual_payload_sha256=actual_payload_sha256,
        expected_payload_sha256=expected_payload_sha256,
        actual_script_file_sha256=actual_script_file_sha256,
        expected_script_file_sha256=expected_script_file_sha256,
    )
    return output, EXIT_VERIFIED


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Verify integrity of an ISSUE_SCOPE_ROLLUP_PLAN_V2 JSON result file."
    )
    parser.add_argument(
        "--result-json",
        required=True,
        help="Path to the ISSUE_SCOPE_ROLLUP_PLAN_V2 JSON file to verify.",
    )

    args = parser.parse_args(argv)

    output, exit_code = verify(args.result_json)
    print(output)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

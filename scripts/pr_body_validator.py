#!/usr/bin/env python3
"""Thin wrapper around the open-pr PR body validator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "open-pr" / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from validate_pr_body import _load_changed_paths, _parse_schema_decision, validate_pr_body

ERROR_CODE_MAP = {
    "LP051": "E_SAFETY_CLAIM_MATRIX_MISSING",
    "E_SAFETY_CLAIMS_PARSE_ERROR": "E_SAFETY_CLAIMS_PARSE_ERROR",
    "E_SAFETY_CLAIMS_SCHEMA_INVALID": "E_SAFETY_CLAIMS_SCHEMA_INVALID",
    "E_FOLLOW_UP_MISSING_CONTRACT": "E_FOLLOW_UP_MISSING_CONTRACT",
}


def _classify_missing_safety_claim_matrix(error: dict[str, object]) -> bool:
    return (
        str(error.get("rule_id")) == "LP052"
        and str(error.get("message", "")).strip() == "Missing required section: Safety Claim Matrix"
    )


def _resolve_error_code(result_errors: list[dict[str, object]]) -> str:
    if not result_errors:
        return "E_UNKNOWN"
    if any(_classify_missing_safety_claim_matrix(error) for error in result_errors):
        return "E_SAFETY_CLAIM_MATRIX_MISSING"
    for error in result_errors:
        rule_id = str(error.get("rule_id", "E_UNKNOWN"))
        mapped = ERROR_CODE_MAP.get(rule_id)
        if mapped:
            return mapped
    return str(result_errors[0].get("rule_id", "E_UNKNOWN"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate PR body via the canonical open-pr validator")
    parser.add_argument("--body-file", required=True, type=str)
    parser.add_argument("--changed-paths-file", type=str, default="")
    parser.add_argument("--linked-issue", type=int, default=None)
    parser.add_argument(
        "--schema-change",
        choices=sorted({"schema_change", "not_schema_change", "uncertain"}),
        default=None,
    )
    args = parser.parse_args(argv)

    body = Path(args.body_file).read_text(encoding="utf-8")
    changed_paths = _load_changed_paths(args.changed_paths_file or None)
    body_decision = _parse_schema_decision(body)
    if (
        args.schema_change
        and body_decision in {"schema_change", "not_schema_change", "uncertain"}
        and args.schema_change != body_decision
    ):
        payload = {
            "schema": "loop_body_lint/v1",
            "target": "pr",
            "body_sha256": None,
            "status": "fail",
            "errors": [
                {
                    "rule_id": "E_SCHEMA_CHANGE_FLAG_MISMATCH",
                    "severity": "error",
                    "section": "Schema Change Applicability",
                    "line_start": 1,
                    "line_end": 1,
                    "message": f"--schema-change={args.schema_change} does not match PR body decision {body_decision}.",
                    "minimal_context": [f"body_decision={body_decision}", f"flag_decision={args.schema_change}"],
                    "context_truncated": False,
                    "fix_hint": "Make the helper flag match the PR body decision or remove the flag.",
                    "autofixable": False,
                }
            ],
        }
        print(json.dumps(payload, indent=2))
        print("ERROR=E_SCHEMA_CHANGE_FLAG_MISMATCH")
        return 1

    result = validate_pr_body(body, changed_paths, args.linked_issue, schema_decision_override=args.schema_change)
    payload = {
        "schema": result.schema,
        "target": result.target,
        "body_sha256": result.body_sha256,
        "status": result.status,
        "errors": [error.__dict__ for error in result.errors],
    }
    print(json.dumps(payload, indent=2))
    if result.status == "fail":
        print(f"ERROR={_resolve_error_code(payload['errors'])}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

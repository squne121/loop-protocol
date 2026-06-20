#!/usr/bin/env python3
"""
reviewer_claim_replay.py - REVIEWER_CLAIM_REPLAY_V1

Verifies whether an issue-reviewer blocker claim is backed by deterministic
tool results (contract_readiness_check, baseline_vc_preflight, vc_contract_syntax).

Unbacked blockers are classified as `reviewer_claim_unbacked_by_deterministic_checker`
and should NOT consume a refinement loop iteration.

Exit codes:
  0: analysis complete (check `deterministic_backed` field for verdict)
  1: input/runtime error

stdout: compact JSON only (≤ 2048 bytes)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = "REVIEWER_CLAIM_REPLAY_V1"

# Blocker code classification sets (normalized to lowercase)
_VC_SYNTAX_CODES = frozenset(
    [
        "c4",
        "vc_command_format",
        "vc command format",
        "$ prefix",
        "missing $ prefix",
        "missing_$_prefix",
        "lp010",
    ]
)

_SECTION_CODES = frozenset(
    [
        "missing_required_section",
        "missing_section",
        "c5",
    ]
)


def _classify_blocker(blocker_code: str) -> str:
    """Return 'vc_syntax', 'section', or 'unknown'."""
    normalized = blocker_code.lower().strip()
    if normalized in _VC_SYNTAX_CODES:
        return "vc_syntax"
    if normalized in _SECTION_CODES:
        return "section"
    return "unknown"


# ---------------------------------------------------------------------------
# Source result checkers
# ---------------------------------------------------------------------------


def _check_readiness_for_vc_syntax(readiness: dict) -> list[str]:
    """Return matched source check descriptions for VC syntax blocker."""
    matched: list[str] = []
    for err in readiness.get("errors", []):
        rule_id = err.get("rule_id", "")
        category = err.get("category", "")
        source_check = err.get("source_check", "")
        # LP010 from validate_issue_body, or compound_command_disallowed / VCS001
        if rule_id.startswith("LP0") and source_check == "validate_issue_body":
            matched.append(f"validate_issue_body: {rule_id}")
        elif rule_id == "VCS001" or category == "compound_command_disallowed":
            matched.append(f"contract_readiness_check: {rule_id} ({category})")
        elif category in ("no_commands_extracted", "compound_command_disallowed"):
            matched.append(f"{source_check}: {category}")
    return matched


def _check_readiness_for_section(readiness: dict) -> list[str]:
    """Return matched source check descriptions for missing section blocker."""
    matched: list[str] = []
    for err in readiness.get("errors", []):
        rule_id = err.get("rule_id", "")
        source_check = err.get("source_check", "")
        category = err.get("category", "")
        # LP* rules from validate_issue_body = section/structure lint
        if rule_id.startswith("LP") and source_check == "validate_issue_body":
            matched.append(f"validate_issue_body: {rule_id}")
        elif category in ("missing_required_section", "rva_immediate_field_missing"):
            matched.append(f"{source_check}: {category}")
    return matched


def _check_vc_syntax_result(vc_syntax: dict) -> list[str]:
    """Return matched source check descriptions from vc_contract_syntax result."""
    matched: list[str] = []
    # vc_contract_syntax result may contain LP010 errors or similar
    for err in vc_syntax.get("errors", []):
        rule_id = err.get("rule_id", "")
        if rule_id:
            matched.append(f"vc_contract_syntax: {rule_id}")
    # Also check top-level errors list (some schemas use flat list)
    if isinstance(vc_syntax.get("lp010_errors"), list) and vc_syntax["lp010_errors"]:
        for item in vc_syntax["lp010_errors"]:
            matched.append(f"vc_contract_syntax: LP010 {item}")
    return matched


def _check_vc_preflight_for_syntax(vc_preflight: dict) -> list[str]:
    """Return matched source check descriptions from vc_preflight result for syntax issues."""
    matched: list[str] = []
    for result in vc_preflight.get("results", []):
        category = result.get("category", "")
        if category in ("compound_command_disallowed", "no_commands_extracted"):
            ac = result.get("ac", "")
            matched.append(f"baseline_vc_preflight: {category} {ac}".strip())
    for err in vc_preflight.get("errors", []):
        if isinstance(err, dict):
            kind = err.get("kind", "")
            if kind in ("extraction_error", "unsupported_vc_format"):
                matched.append(f"baseline_vc_preflight: {kind}")
        elif isinstance(err, str) and "command" in err.lower():
            matched.append(f"baseline_vc_preflight: {err[:80]}")
    return matched


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def analyze(
    blocker_code: str,
    body_file: Optional[str],
    readiness_result: dict,
    vc_syntax_result: Optional[dict],
    vc_preflight_result: Optional[dict],
    consecutive_count: int = 1,
) -> dict:
    """Perform replay analysis and return REVIEWER_CLAIM_REPLAY_V1 dict."""
    kind = _classify_blocker(blocker_code)

    matched_source_checks: list[str] = []

    if kind == "vc_syntax":
        matched_source_checks.extend(_check_readiness_for_vc_syntax(readiness_result))
        if vc_syntax_result:
            matched_source_checks.extend(_check_vc_syntax_result(vc_syntax_result))
        if vc_preflight_result:
            matched_source_checks.extend(_check_vc_preflight_for_syntax(vc_preflight_result))
    elif kind == "section":
        matched_source_checks.extend(_check_readiness_for_section(readiness_result))
    else:
        # Unknown blocker type — prose-only or unclassifiable
        return {
            "schema": SCHEMA,
            "blocker_code": blocker_code,
            "blocker_kind": "unknown_blocker_type",
            "deterministic_backed": False,
            "verdict": "reviewer_claim_unbacked_by_deterministic_checker",
            "matched_source_checks": [],
            "routing": "downgrade_to_non_blocking",
            "should_consume_iteration": False,
        }

    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for item in matched_source_checks:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    matched_source_checks = deduped

    deterministic_backed = len(matched_source_checks) > 0

    if deterministic_backed:
        verdict = "deterministic_fail_confirmed"
        routing = "proceed_to_rewrite"
        should_consume = True
    else:
        # Check for repeated false positive suspicion
        if consecutive_count >= 2:
            verdict = "reviewer_false_positive_suspected"
        else:
            verdict = "reviewer_claim_unbacked_by_deterministic_checker"
        routing = "downgrade_to_non_blocking"
        should_consume = False

    return {
        "schema": SCHEMA,
        "blocker_code": blocker_code,
        "blocker_kind": kind,
        "deterministic_backed": deterministic_backed,
        "verdict": verdict,
        "matched_source_checks": matched_source_checks,
        "routing": routing,
        "should_consume_iteration": should_consume,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="REVIEWER_CLAIM_REPLAY_V1 — verify reviewer blocker against deterministic tools"
    )
    parser.add_argument("--blocker-code", required=True, help="Blocker code from reviewer")
    parser.add_argument("--body-file", help="Path to issue body file (optional, reserved)")
    parser.add_argument(
        "--readiness-result-file",
        required=True,
        help="Path to ISSUE_CONTRACT_READINESS_RESULT_V1 JSON file",
    )
    parser.add_argument(
        "--vc-syntax-result-file",
        help="Path to vc_contract_syntax result JSON file (optional)",
    )
    parser.add_argument(
        "--vc-preflight-result-file",
        help="Path to baseline_vc_preflight result JSON file (optional)",
    )
    parser.add_argument(
        "--consecutive-count",
        type=int,
        default=1,
        help="Number of consecutive times this blocker has been replayed (default: 1)",
    )

    args = parser.parse_args()

    # Load readiness result
    try:
        readiness_result = json.loads(Path(args.readiness_result_file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            json.dumps({"error": f"readiness-result-file not found: {args.readiness_result_file}"}),
            flush=True,
        )
        return 1
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"json decode error in readiness-result-file: {exc}"}), flush=True)
        return 1

    # Load optional vc-syntax result
    vc_syntax_result: Optional[dict] = None
    if args.vc_syntax_result_file:
        try:
            vc_syntax_result = json.loads(
                Path(args.vc_syntax_result_file).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # Optional — absence is not an error

    # Load optional vc-preflight result
    vc_preflight_result: Optional[dict] = None
    if args.vc_preflight_result_file:
        try:
            vc_preflight_result = json.loads(
                Path(args.vc_preflight_result_file).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # Optional — absence is not an error

    result = analyze(
        blocker_code=args.blocker_code,
        body_file=args.body_file,
        readiness_result=readiness_result,
        vc_syntax_result=vc_syntax_result,
        vc_preflight_result=vc_preflight_result,
        consecutive_count=args.consecutive_count,
    )

    output = json.dumps(result, separators=(",", ":"))
    # Guard: stdout must be compact JSON only, ≤ 2048 bytes
    if len(output.encode("utf-8")) > 2048:
        # Truncate matched_source_checks to fit
        result["matched_source_checks"] = result["matched_source_checks"][:3]
        output = json.dumps(result, separators=(",", ":"))

    print(output, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

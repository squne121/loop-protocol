#!/usr/bin/env python3
"""
Product Spec Gate Evaluator

Consumes CONTRACT_REVIEW_RESULT_V1.checks.product_spec_check from contract snapshot
and emits PRODUCT_SPEC_GATE_DECISION_V1 with routing_action: continue | stop_human | refresh_contract_snapshot.

Does NOT reimplement PS001-PS006 semantics. Reads product_spec_check output only.
"""

import argparse
import json
import sys
from typing import Any, Dict


def load_contract_snapshot(snapshot_source: str) -> Dict[str, Any]:
    """Load contract snapshot from JSON string or stdin."""
    if snapshot_source == "-":
        content = sys.stdin.read()
    else:
        content = snapshot_source
    return json.loads(content)


def evaluate_product_spec_check(
    contract_snapshot: Dict[str, Any], contract_snapshot_url: str = None
) -> Dict[str, Any]:
    """
    Evaluate product_spec_check from contract snapshot.

    Args:
        contract_snapshot: CONTRACT_REVIEW_RESULT_V1 snapshot JSON
        contract_snapshot_url: Snapshot comment URL for provenance (optional)

    Returns PRODUCT_SPEC_GATE_DECISION_V1 with routing_action.
    """
    # Blocker 2: Check if CONTRACT_REVIEW_RESULT_V1 exists
    if "CONTRACT_REVIEW_RESULT_V1" not in contract_snapshot:
        return {
            "status": "ok",
            "applicability": "missing",
            "decision": "missing",
            "blocked_rule_ids": [],
            "contract_snapshot_url": None,
            "body_sha256": None,
            "routing_action": "refresh_contract_snapshot",
            "reason": "CONTRACT_REVIEW_RESULT_V1 not found in snapshot",
        }

    contract_result = contract_snapshot["CONTRACT_REVIEW_RESULT_V1"]

    # Blocker 3: Extract body_sha256 at the top so it survives all return paths
    body_sha256 = contract_snapshot.get("body_sha256", None)

    # Check if checks.product_spec_check exists
    # Do NOT rely on standalone product_spec_check_triggers
    if "checks" not in contract_result or "product_spec_check" not in contract_result.get("checks", {}):
        # Blocker 2: Use CLI-provided or snapshot field for contract_snapshot_url
        resolved_snapshot_url = (
            contract_snapshot_url
            or contract_snapshot.get("contract_snapshot_url")
            or None
        )
        return {
            "status": "ok",
            "applicability": "missing",
            "decision": "missing",
            "blocked_rule_ids": [],
            "contract_snapshot_url": resolved_snapshot_url,
            "body_sha256": body_sha256,
            "routing_action": "refresh_contract_snapshot",
            "reason": "product_spec_check missing from contract snapshot",
        }

    product_spec_check = contract_result["checks"]["product_spec_check"]

    # Extract key fields
    applicability = product_spec_check.get("applicability", "unknown")
    decision = product_spec_check.get("decision", "unknown")

    # Blocker 1: Normalize blocked_rule_ids from blocked_reasons[].rule_id
    # Fallback to direct blocked_rule_ids for backward compatibility with old fixtures
    blocked_rule_ids = product_spec_check.get("blocked_rule_ids")
    if blocked_rule_ids is None:
        blocked_reasons = product_spec_check.get("blocked_reasons", [])
        blocked_rule_ids = [
            r.get("rule_id")
            for r in blocked_reasons
            if isinstance(r, dict) and r.get("rule_id")
        ]

    # Blocker 4: Enum validation
    valid_applicability = {"applicable", "not_applicable"}
    valid_decision = {"pass", "fail", "human_judgment"}

    if applicability not in valid_applicability or decision not in valid_decision:
        # Blocker 2: Use resolved contract_snapshot_url
        resolved_snapshot_url = (
            contract_snapshot_url
            or contract_snapshot.get("contract_snapshot_url")
            or contract_result.get("issue_url")
        )
        return {
            "status": "ok",
            "applicability": applicability,
            "decision": decision,
            "blocked_rule_ids": blocked_rule_ids if blocked_rule_ids else [],
            "contract_snapshot_url": resolved_snapshot_url,
            "body_sha256": body_sha256,
            "routing_action": "refresh_contract_snapshot",
            "reason": "Invalid product_spec_check enum value",
        }

    # Blocker 1: Pair invariant check — not_applicable requires decision=pass
    if applicability == "not_applicable" and decision != "pass":
        # Blocker 2: Use resolved contract_snapshot_url
        resolved_snapshot_url = (
            contract_snapshot_url
            or contract_snapshot.get("contract_snapshot_url")
            or contract_result.get("issue_url")
        )
        return {
            "status": "ok",
            "applicability": applicability,
            "decision": decision,
            "blocked_rule_ids": blocked_rule_ids if blocked_rule_ids else [],
            "contract_snapshot_url": resolved_snapshot_url,
            "body_sha256": body_sha256,
            "routing_action": "refresh_contract_snapshot",
            "reason": "Inconsistent product_spec_check: not_applicable requires decision=pass",
        }

    # Determine routing action
    routing_action = "continue"

    if applicability == "not_applicable":
        # No product spec relevance, continue normally
        routing_action = "continue"
    elif decision == "pass":
        routing_action = "continue"
    elif decision == "fail":
        routing_action = "stop_human"
    elif decision == "human_judgment":
        routing_action = "stop_human"

    # Blocker 2: Use resolved contract_snapshot_url
    resolved_snapshot_url = (
        contract_snapshot_url
        or contract_snapshot.get("contract_snapshot_url")
        or contract_result.get("issue_url")
    )

    return {
        "status": "ok",
        "applicability": applicability,
        "decision": decision,
        "blocked_rule_ids": blocked_rule_ids if blocked_rule_ids else [],
        "contract_snapshot_url": resolved_snapshot_url,
        "issue_url": contract_result.get("issue_url"),
        "body_sha256": body_sha256,
        "routing_action": routing_action,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate product_spec_check from contract snapshot"
    )
    parser.add_argument(
        "--snapshot-json",
        type=str,
        default=None,
        help="Contract snapshot JSON string (or '-' for stdin)",
    )
    parser.add_argument(
        "--contract-snapshot-url",
        type=str,
        default=None,
        help="Contract snapshot comment URL for provenance",
    )

    args = parser.parse_args()

    # Determine snapshot source
    if args.snapshot_json:
        snapshot_source = args.snapshot_json
    else:
        # Default: read from stdin
        snapshot_source = "-"

    try:
        snapshot = load_contract_snapshot(snapshot_source)
        decision = evaluate_product_spec_check(snapshot, args.contract_snapshot_url)
        print(json.dumps(decision, indent=2))
        return 0
    except json.JSONDecodeError as e:
        print(
            json.dumps({
                "status": "error",
                "error": f"Invalid JSON: {str(e)}",
            }),
            file=sys.stderr
        )
        return 1
    except Exception as e:
        print(
            json.dumps({
                "status": "error",
                "error": str(e),
            }),
            file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

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
from typing import Any, Dict, Optional


def load_contract_snapshot(snapshot_source: str) -> Dict[str, Any]:
    """Load contract snapshot from JSON string or stdin."""
    if snapshot_source == "-":
        content = sys.stdin.read()
    else:
        content = snapshot_source
    return json.loads(content)


def evaluate_product_spec_check(contract_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate product_spec_check from contract snapshot.

    Returns PRODUCT_SPEC_GATE_DECISION_V1 with routing_action.
    """
    # Check if CONTRACT_REVIEW_RESULT_V1 exists
    if "CONTRACT_REVIEW_RESULT_V1" not in contract_snapshot:
        return {
            "status": "ok",
            "applicability": "missing",
            "decision": "missing",
            "blocked_rule_ids": [],
            "contract_snapshot_url": None,
            "body_sha256": None,
            "routing_action": "continue",
            "reason": "CONTRACT_REVIEW_RESULT_V1 not found in snapshot",
        }

    contract_result = contract_snapshot["CONTRACT_REVIEW_RESULT_V1"]

    # Check if checks.product_spec_check exists
    if "checks" not in contract_result or "product_spec_check" not in contract_result.get("checks", {}):
        # Check if product spec context is triggered
        triggers = contract_result.get("checks", {}).get("product_spec_check_triggers", {})
        has_product_spec_trigger = any([
            triggers.get("docs_product_allowed_paths", False),
            triggers.get("tasks_md_mentioned", False),
            triggers.get("specify_artifact_mentioned", False),
            triggers.get("generated_task_mentioned", False),
            triggers.get("product_spec_context_present", False),
        ])

        if has_product_spec_trigger:
            # Product/spec trigger present but product_spec_check missing
            return {
                "status": "ok",
                "applicability": "applicable",
                "decision": "missing",
                "blocked_rule_ids": [],
                "contract_snapshot_url": None,
                "body_sha256": None,
                "routing_action": "refresh_contract_snapshot",
                "reason": "Product/spec trigger present but product_spec_check missing from contract snapshot",
            }
        else:
            # No product spec context, so continue normally
            return {
                "status": "ok",
                "applicability": "not_applicable",
                "decision": "pass",
                "blocked_rule_ids": [],
                "contract_snapshot_url": None,
                "body_sha256": None,
                "routing_action": "continue",
                "reason": "No product/spec trigger detected",
            }

    product_spec_check = contract_result["checks"]["product_spec_check"]

    # Extract key fields
    applicability = product_spec_check.get("applicability", "unknown")
    decision = product_spec_check.get("decision", "unknown")
    blocked_rule_ids = product_spec_check.get("blocked_rule_ids", [])
    contract_snapshot_url = contract_result.get("issue_url", None)
    body_sha256 = contract_snapshot.get("body_sha256", None)

    # Determine routing action
    routing_action = "continue"

    if applicability == "not_applicable":
        # No product spec relevance, continue normally
        routing_action = "continue"
        decision = "pass"
    elif decision == "pass":
        routing_action = "continue"
    elif decision == "fail":
        routing_action = "stop_human"
    elif decision == "human_judgment":
        routing_action = "stop_human"
    elif applicability == "applicable" and decision == "missing":
        # Stale snapshot case
        routing_action = "refresh_contract_snapshot"

    return {
        "status": "ok",
        "applicability": applicability,
        "decision": decision,
        "blocked_rule_ids": blocked_rule_ids,
        "contract_snapshot_url": contract_snapshot_url,
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

    args = parser.parse_args()

    # Determine snapshot source
    if args.snapshot_json:
        snapshot_source = args.snapshot_json
    else:
        # Default: read from stdin
        snapshot_source = "-"

    try:
        snapshot = load_contract_snapshot(snapshot_source)
        decision = evaluate_product_spec_check(snapshot)
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

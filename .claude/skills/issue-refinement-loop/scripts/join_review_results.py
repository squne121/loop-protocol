#!/usr/bin/env python3
"""Pure joiner for deterministic + semantic Step 2 review results (Issue #2296).

``join_review_results()`` is a pure function: it never invokes any child
process, never calls GitHub, and never launches ``issue-design-reviewer``.
It only combines three already-computed inputs:

- ``deterministic_verdict``: ``approve`` | ``needs-fix`` (from the existing
  Step 2 root_review_pipeline / ISSUE_REVIEW_RESULT_COMPACT_V2 pipeline --
  unchanged by this Issue)
- ``semantic_assessment``: ``clear`` | ``findings`` | ``not_required`` (the
  latter when ``semantic_review_applicable=False``)
- ``transport_status``: ``ok`` | ``missing`` | ``stale`` | ``error`` |
  ``not_required``

into a single ``effective_verdict`` (``approve`` | ``needs-fix`` |
``human_judgment_required``) plus a ``warnings`` list.

``decide_next_loop_action.py`` is NOT called from here and is not modified
by this Issue -- ``effective_verdict`` is handed to the EXISTING Step 2
routing table unchanged (approve -> Step 4.5 / needs-fix -> Step 4).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

SCHEMA = "JOIN_REVIEW_RESULT_V1"

_OPEN_DISPOSITION_STATUSES = {None, "", "open"}
_ACCEPTED_LIKE_STATUSES = {"accepted", "deferred"}

DEFAULT_TRANSPORT_POLICY = "best_effort"
DEFAULT_FINDING_POLICY = "route_high_open_to_rewrite"

_VALID_TRANSPORT_POLICIES = {"best_effort", "required"}
_VALID_FINDING_POLICIES = {"route_high_open_to_rewrite"}


def _finding_is_open_blocking(finding: "dict[str, Any]") -> bool:
    severity = finding.get("severity")
    if severity not in ("blocker", "high"):
        return False
    disposition = finding.get("owner_disposition") or {}
    status = disposition.get("status")
    if status in _ACCEPTED_LIKE_STATUSES:
        return False
    return True


def join_review_results(
    *,
    deterministic_verdict: str,
    semantic_assessment: str = "not_required",
    transport_status: str = "not_required",
    findings: "list[dict[str, Any]] | None" = None,
    transport_policy: str = DEFAULT_TRANSPORT_POLICY,
    finding_policy: str = DEFAULT_FINDING_POLICY,
    retry_already_attempted: bool = False,
) -> "dict[str, Any]":
    findings = findings or []
    warnings: "list[str]" = []

    if transport_policy not in _VALID_TRANSPORT_POLICIES:
        raise ValueError(f"unknown transport_policy: {transport_policy!r}")
    if finding_policy not in _VALID_FINDING_POLICIES:
        raise ValueError(f"unknown finding_policy: {finding_policy!r}")

    # deterministic needs-fix always wins; semantic gate only applies to the
    # approve path (Design Decision Note).
    if deterministic_verdict != "approve":
        return {
            "schema": SCHEMA,
            "effective_verdict": "needs-fix",
            "warnings": warnings,
            "transport_policy": transport_policy,
            "finding_policy": finding_policy,
        }

    if semantic_assessment == "not_required":
        return {
            "schema": SCHEMA,
            "effective_verdict": "approve",
            "warnings": warnings,
            "transport_policy": transport_policy,
            "finding_policy": finding_policy,
        }

    if transport_status in ("missing", "stale", "error"):
        if transport_policy == "required":
            return {
                "schema": SCHEMA,
                "effective_verdict": "human_judgment_required",
                "warnings": [f"semantic review transport_status={transport_status}"],
                "transport_policy": transport_policy,
                "finding_policy": finding_policy,
            }
        # best_effort: caller is expected to have retried once already; if
        # still not ok, continue with approve + warning rather than block.
        warnings.append(
            f"semantic review transport_status={transport_status} "
            f"(best_effort, retry_already_attempted={retry_already_attempted})"
        )
        return {
            "schema": SCHEMA,
            "effective_verdict": "approve",
            "warnings": warnings,
            "transport_policy": transport_policy,
            "finding_policy": finding_policy,
        }

    # transport_status == ok
    if semantic_assessment == "clear":
        return {
            "schema": SCHEMA,
            "effective_verdict": "approve",
            "warnings": warnings,
            "transport_policy": transport_policy,
            "finding_policy": finding_policy,
        }

    # semantic_assessment == findings
    open_blocking = [f for f in findings if _finding_is_open_blocking(f)]
    if open_blocking:
        warnings.append(
            f"{len(open_blocking)} open blocker/high semantic finding(s) route to rewrite"
        )
        return {
            "schema": SCHEMA,
            "effective_verdict": "needs-fix",
            "warnings": warnings,
            "transport_policy": transport_policy,
            "finding_policy": finding_policy,
        }

    if findings:
        warnings.append(
            f"{len(findings)} semantic finding(s) recorded as warning "
            "(medium/low severity or owner-dispositioned)"
        )
    return {
        "schema": SCHEMA,
        "effective_verdict": "approve",
        "warnings": warnings,
        "transport_policy": transport_policy,
        "finding_policy": finding_policy,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file")
    parser.add_argument("--input-json")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.input_json:
        raw = json.loads(args.input_json)
    elif args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        raw = json.load(sys.stdin)
    result = join_review_results(
        deterministic_verdict=raw["deterministic_verdict"],
        semantic_assessment=raw.get("semantic_assessment", "not_required"),
        transport_status=raw.get("transport_status", "not_required"),
        findings=raw.get("findings"),
        transport_policy=raw.get("transport_policy", DEFAULT_TRANSPORT_POLICY),
        finding_policy=raw.get("finding_policy", DEFAULT_FINDING_POLICY),
        retry_already_attempted=bool(raw.get("retry_already_attempted", False)),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

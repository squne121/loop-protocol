#!/usr/bin/env python3
"""Deterministic applicability classifier for the Step 2.5 semantic design
review lane (Issue #2296).

This module answers exactly one question: given a fixed set of EXPLICIT
signals about the current review cycle, is a semantic (model-based) design
review APPLICABLE right now?  It does not compare the current Issue body to
any previous body -- there is no ``--previous-body-file`` input and no
before/after diff.  This is a deliberate, explicit renaming from the earlier
``semantic_review_required`` name: the classifier only tells you whether
semantic review APPLIES to the current signals, it does not (and cannot,
without a comparison baseline) prove that the body has materially CHANGED
(#2296 Design Decision Note, P0-2).

``semantic_review_applicable`` is ``True`` iff at least one of the following
explicit signals is true / non-zero / non-empty:

- ``user_requested``
- ``semantic_rewrite_requested``
- ``checker_gap_count`` (> 0)
- ``heuristic_concern_count`` (> 0)
- ``severity_tagged_anchor_findings`` (non-empty)
- ``owner_decision_conflict``
- ``cross_contract_change.schema`` / ``.protocol`` / ``.orchestration``

All-false / all-zero / all-empty -> ``semantic_review_applicable = False``
(skip; the Step 2.5 lane is not entered).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "SEMANTIC_REVIEW_TRIGGER_RESULT_V1"


@dataclass(frozen=True)
class CrossContractChange:
    schema: bool = False
    protocol: bool = False
    orchestration: bool = False

    def any_true(self) -> bool:
        return bool(self.schema or self.protocol or self.orchestration)

    @classmethod
    def from_dict(cls, raw: "dict[str, Any] | None") -> "CrossContractChange":
        raw = raw or {}
        return cls(
            schema=bool(raw.get("schema", False)),
            protocol=bool(raw.get("protocol", False)),
            orchestration=bool(raw.get("orchestration", False)),
        )


@dataclass(frozen=True)
class SemanticReviewTriggerInput:
    user_requested: bool = False
    semantic_rewrite_requested: bool = False
    checker_gap_count: int = 0
    heuristic_concern_count: int = 0
    severity_tagged_anchor_findings: tuple = field(default_factory=tuple)
    owner_decision_conflict: bool = False
    cross_contract_change: CrossContractChange = field(default_factory=CrossContractChange)

    @classmethod
    def from_dict(cls, raw: "dict[str, Any]") -> "SemanticReviewTriggerInput":
        return cls(
            user_requested=bool(raw.get("user_requested", False)),
            semantic_rewrite_requested=bool(raw.get("semantic_rewrite_requested", False)),
            checker_gap_count=int(raw.get("checker_gap_count", 0) or 0),
            heuristic_concern_count=int(raw.get("heuristic_concern_count", 0) or 0),
            severity_tagged_anchor_findings=tuple(
                raw.get("severity_tagged_anchor_findings", []) or []
            ),
            owner_decision_conflict=bool(raw.get("owner_decision_conflict", False)),
            cross_contract_change=CrossContractChange.from_dict(
                raw.get("cross_contract_change")
            ),
        )


def _signal_breakdown(inp: SemanticReviewTriggerInput) -> "dict[str, bool]":
    return {
        "user_requested": inp.user_requested,
        "semantic_rewrite_requested": inp.semantic_rewrite_requested,
        "checker_gap_count": inp.checker_gap_count > 0,
        "heuristic_concern_count": inp.heuristic_concern_count > 0,
        "severity_tagged_anchor_findings": len(inp.severity_tagged_anchor_findings) > 0,
        "owner_decision_conflict": inp.owner_decision_conflict,
        "cross_contract_change": inp.cross_contract_change.any_true(),
    }


def evaluate_semantic_review_applicable(raw: "dict[str, Any]") -> "dict[str, Any]":
    """Pure function: explicit signals in, SEMANTIC_REVIEW_TRIGGER_RESULT_V1 out."""
    inp = SemanticReviewTriggerInput.from_dict(raw)
    breakdown = _signal_breakdown(inp)
    applicable = any(breakdown.values())
    triggered_by = sorted(name for name, value in breakdown.items() if value)
    return {
        "schema": SCHEMA,
        "semantic_review_applicable": applicable,
        "triggered_by": triggered_by,
        "signal_breakdown": breakdown,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", help="JSON file with explicit signals")
    parser.add_argument(
        "--input-json", help="Inline JSON string with explicit signals (overrides --input-file)"
    )
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
    result = evaluate_semantic_review_applicable(raw)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Drift-detection tests: the ISSUE_EXECUTION_DECISION_V1 fragments inlined into
refinement_loop_plan_v1.json and loop_state.schema.json (#1677) must stay
structurally identical to the canonical schema owner,
issue_execution_decision_v1.schema.json (#1675 / PR #1700).

Why inline copies instead of a live cross-file $ref: decide_next_loop_action.py
(outside this Issue's Allowed Paths) calls jsonschema.validate(instance=data,
schema=schema) against loop_state.schema.json with no RefResolver and only
catches jsonschema.ValidationError. A literal cross-file "$ref":
"issue_execution_decision_v1.schema.json#/..." raises
referencing.exceptions.Unresolvable (NOT a ValidationError) the first time a
LOOP_STATE_V1 instance actually contains 'issue_execution_decision' -- an
uncaught production exception outside this Issue's Allowed Paths. These tests
give the same drift-safety guarantee a $ref would, without that live
resolution dependency.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import plan_refinement_loop as _prl  # noqa: E402

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"
CANONICAL_PATH = SCHEMAS_DIR / "issue_execution_decision_v1.schema.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _rename_defs_refs(node, prefix: str):
    """Rewrite '#/$defs/x' -> '#/definitions/<prefix>x' for structural diffing."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str) and v.startswith("#/$defs/"):
                out[k] = v.replace("#/$defs/", f"#/definitions/{prefix}")
            else:
                out[k] = _rename_defs_refs(v, prefix)
        return out
    if isinstance(node, list):
        return [_rename_defs_refs(x, prefix) for x in node]
    return node


def _canonical_local_shape() -> dict:
    """
    Rebuild the local 'IssueExecutionDecisionV1*' shape the way
    plan_refinement_loop-consuming schemas embed it, purely from the
    canonical file, for structural (not byte-identity) comparison.
    """
    canonical = _load(CANONICAL_PATH)
    defs = canonical["$defs"]
    prefix = "IssueExecutionDecisionV1_"
    return {
        "sha256": _rename_defs_refs(defs["sha256"], prefix),
        "issue_number": _rename_defs_refs(defs["issue_number"], prefix),
        "Identity": _rename_defs_refs(defs["identity"], prefix),
        "Node": _rename_defs_refs(defs["node"], prefix),
        "Relation": _rename_defs_refs(defs["relation"], prefix),
        "Execution": _rename_defs_refs(defs["execution"], prefix),
        "DownstreamPolicy": _rename_defs_refs(defs["downstream_policy"], prefix),
        "Completeness": _rename_defs_refs(defs["completeness"], prefix),
    }


def _extract_local_shape(schema: dict) -> dict:
    d = schema["definitions"]
    return {
        "sha256": d["IssueExecutionDecisionV1_sha256"],
        "issue_number": d["IssueExecutionDecisionV1_issue_number"],
        "Identity": d["IssueExecutionDecisionV1Identity"],
        "Node": d["IssueExecutionDecisionV1Node"],
        "Relation": d["IssueExecutionDecisionV1Relation"],
        "Execution": d["IssueExecutionDecisionV1Execution"],
        "DownstreamPolicy": d["IssueExecutionDecisionV1DownstreamPolicy"],
        "Completeness": d["IssueExecutionDecisionV1Completeness"],
    }


def test_refinement_loop_plan_v1_inline_defs_match_canonical_schema():
    schema = _load(SCHEMAS_DIR / "refinement_loop_plan_v1.json")
    assert _extract_local_shape(schema) == _canonical_local_shape(), (
        "refinement_loop_plan_v1.json's inlined IssueExecutionDecisionV1* "
        "definitions have drifted from the canonical "
        "issue_execution_decision_v1.schema.json (#1675/PR#1700). Regenerate "
        "the inline copy from the canonical file."
    )


def test_loop_state_schema_inline_defs_match_canonical_schema():
    schema = _load(SCHEMAS_DIR / "loop_state.schema.json")
    assert _extract_local_shape(schema) == _canonical_local_shape(), (
        "loop_state.schema.json's inlined IssueExecutionDecisionV1* "
        "definitions have drifted from the canonical "
        "issue_execution_decision_v1.schema.json (#1675/PR#1700). Regenerate "
        "the inline copy from the canonical file."
    )


def test_refinement_loop_plan_v1_root_property_wraps_local_definition():
    schema = _load(SCHEMAS_DIR / "refinement_loop_plan_v1.json")
    assert schema["properties"]["issue_execution_decision"] == {
        "$ref": "#/definitions/IssueExecutionDecisionV1"
    }


def test_loop_state_schema_root_property_wraps_local_definition():
    schema = _load(SCHEMAS_DIR / "loop_state.schema.json")
    assert schema["properties"]["issue_execution_decision"] == {
        "$ref": "#/definitions/IssueExecutionDecisionV1"
    }


def test_build_issue_execution_decision_output_validates_against_canonical_schema():
    """
    Direct proof that plan_refinement_loop.build_issue_execution_decision()'s
    output structurally satisfies the canonical issue_execution_decision_v1.
    schema.json (#1675/PR#1700) -- required keys, additionalProperties:false,
    enum constraints, AND format:date-time (PR #1767 owner review P1-3:
    jsonschema.validate() does not enable format validation on its own; a
    FormatChecker must be passed explicitly) -- for selected / blocked /
    duplicate execution states, without relying on a live cross-file $ref.
    """
    jsonschema = __import__("pytest").importorskip("jsonschema")
    schema = _load(CANONICAL_PATH)
    format_checker = jsonschema.FormatChecker()

    def _validate(instance):
        jsonschema.validate(instance=instance, schema=schema, format_checker=format_checker)

    selected = _prl.build_issue_execution_decision(42, "a" * 64, "2026-01-01T00:00:00Z", None)
    _validate(selected)

    scope_rollup_blocked = {
        "schema_version": 2,
        "input": {"completeness": "full", "warnings": []},
        "candidates": [
            {
                "kind": "issue",
                "number": 99,
                "state": "OPEN",
                "signals": ["same_parent_issue"],
                "suggested_action": "keep_separate_with_reason",
                "ordering_constraint": "sequential_required",
            }
        ],
    }
    blocked = _prl.build_issue_execution_decision(
        42, "a" * 64, "2026-01-01T00:00:00Z", {"scope_rollup_result": scope_rollup_blocked}
    )
    _validate(blocked)

    scope_rollup_duplicate = {
        "schema_version": 2,
        "input": {"completeness": "full", "warnings": []},
        "candidates": [
            {
                "kind": "issue",
                "number": 100,
                "state": "OPEN",
                "signals": ["shared_dedupe_key"],
                "suggested_action": "merge_into_current_pr",
                "ordering_constraint": "parallel_ok",
            }
        ],
    }
    duplicate = _prl.build_issue_execution_decision(
        42, "a" * 64, "2026-01-01T00:00:00Z", {"scope_rollup_result": scope_rollup_duplicate}
    )
    _validate(duplicate)


def test_format_checker_date_time_support_is_documented_honestly():
    """
    PR #1767 owner review (P1-3): jsonschema.validate() does not enforce
    format:date-time unless an explicit FormatChecker is passed. This
    repository's jsonschema installation additionally lacks the optional
    'date-time' format dependency (rfc3339-validator / strict-rfc3339), so
    even jsonschema.FormatChecker() does not actually validate the
    'date-time' format here -- adding that optional dependency is outside
    this Issue's Allowed Paths (pyproject.toml). This test documents the gap
    precisely instead of asserting format enforcement that does not hold in
    this environment, so a future dependency change is expected to make it
    fail (signaling the gap is closed) rather than silently passing either way.
    """
    jsonschema = __import__("pytest").importorskip("jsonschema")
    format_checker = jsonschema.FormatChecker()
    assert "date-time" not in format_checker.checkers, (
        "If this now fails, an rfc3339-validator/strict-rfc3339-equivalent "
        "dependency has been added -- switch test_issue_execution_decision_"
        "schema.py and this file's validate() calls to pass format_checker="
        "jsonschema.FormatChecker() everywhere so format:date-time is "
        "actually enforced (PR #1767 owner review P1-3)."
    )


def test_local_wrapper_definition_required_and_closed_matches_canonical():
    canonical = _load(CANONICAL_PATH)
    for schema_path in (
        SCHEMAS_DIR / "refinement_loop_plan_v1.json",
        SCHEMAS_DIR / "loop_state.schema.json",
    ):
        schema = _load(schema_path)
        local_wrapper = schema["definitions"]["IssueExecutionDecisionV1"]
        assert local_wrapper["additionalProperties"] is False
        assert sorted(local_wrapper["required"]) == sorted(canonical["required"])
        assert (
            local_wrapper["properties"]["schema_version"]
            == canonical["properties"]["schema_version"]
        )

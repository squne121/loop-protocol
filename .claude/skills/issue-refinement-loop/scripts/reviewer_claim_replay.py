#!/usr/bin/env python3
"""
reviewer_claim_replay.py - REVIEWER_CLAIM_REPLAY_V1

Arbitrates reviewer blockers against deterministic artifacts before a
`needs-fix` verdict consumes another refinement-loop iteration.

Reviewer codes / deterministic check names / readiness rule ids / readiness
categories / domain keys are normalized through a single table-driven
taxonomy (`REVIEWER_CHECKER_TAXONOMY_V1`) so that adding coverage for a new
reviewer blocker requires only a table entry, not new branching logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "REVIEWER_CLAIM_REPLAY_V1"
STATE_SCHEMA = "REVIEWER_CLAIM_REPLAY_STATE_V1"
TAXONOMY_SCHEMA = "REVIEWER_CHECKER_TAXONOMY_V1"

VALID_FINDING_KINDS = frozenset(
    {"deterministic_domain_blocker", "checker_gap", "heuristic_concern"}
)
VALID_ARTIFACT_SCHEMAS = frozenset({"REVIEW_ISSUE_RESULT_V1", "CHECK_ISSUE_CONTRACT_V1"})

# REVIEWER_CHECKER_TAXONOMY_V1 -- table-driven parity between:
#   - reviewer_codes: raw `reviewer_blocker_code` strings a reviewer may emit
#   - deterministic_checks: `deterministic_checks` dict key(s) in the review artifact
#   - readiness_rule_ids / readiness_categories: `errors[]` entries in the
#     ISSUE_CONTRACT_READINESS_RESULT_V1 artifact that back this entry
#   - domain_keys: `deterministic_domain_key` values used by structured findings
#
# Adding coverage for a new reviewer blocker / checker pair is a table
# addition, not a code change (Issue #1286 In Scope).
REVIEWER_CHECKER_TAXONOMY_V1: list[dict[str, Any]] = [
    {
        "entry_id": "vc_command_format",
        "reviewer_codes": [
            "c4",
            "vc_command_format",
            "vc command format",
            "missing $ prefix",
            "missing_$_prefix",
        ],
        "deterministic_checks": ["C4_vc_commands_present"],
        "readiness_rule_ids": ["VCS001", "LP011", "LP016"],
        "readiness_rule_id_source_check": None,
        "readiness_categories": [
            "non_dollar_command",
            "compound_shell",
            "compound_command_disallowed",
            "no_commands_extracted",
        ],
        "readiness_category_source_check": "contract_readiness_check",
        "domain_keys": ["vc_command_format"],
    },
    {
        "entry_id": "ac_vc_number_mismatch",
        "reviewer_codes": ["lp010", "ac_vc_number_mismatch", "c5"],
        "deterministic_checks": ["C5_ac_vc_number_alignment"],
        "readiness_rule_ids": ["LP010"],
        "readiness_rule_id_source_check": None,
        "readiness_categories": [],
        "readiness_category_source_check": None,
        "domain_keys": ["vc_number_alignment"],
    },
    {
        "entry_id": "missing_section",
        "reviewer_codes": ["missing_required_section", "missing_section"],
        "deterministic_checks": ["C1_required_sections"],
        "readiness_rule_ids": ["LP001"],
        "readiness_rule_id_source_check": "validate_issue_body",
        "readiness_categories": [],
        "readiness_category_source_check": None,
        "domain_keys": ["required_sections"],
    },
    {
        "entry_id": "rva_immediate_field_missing",
        "reviewer_codes": ["c9", "rva_immediate_field_missing"],
        "deterministic_checks": ["C9_runtime_applicability_present"],
        "readiness_rule_ids": [],
        "readiness_rule_id_source_check": None,
        "readiness_categories": ["rva_immediate_field_missing"],
        "readiness_category_source_check": "contract_readiness_check",
        "domain_keys": ["runtime_applicability"],
    },
]

TAXONOMY_BY_ENTRY_ID: dict[str, dict[str, Any]] = {
    entry["entry_id"]: entry for entry in REVIEWER_CHECKER_TAXONOMY_V1
}


def _normalize_blocker_code(code: str) -> str:
    return " ".join(code.strip().lower().split())


def _build_taxonomy_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for entry in REVIEWER_CHECKER_TAXONOMY_V1:
        entry_id = entry["entry_id"]
        keys: list[str] = (
            list(entry["reviewer_codes"])
            + list(entry["deterministic_checks"])
            + list(entry["domain_keys"])
            + [entry_id]
        )
        for raw_key in keys:
            normalized = _normalize_blocker_code(str(raw_key))
            existing = lookup.get(normalized)
            if existing is not None and existing != entry_id:
                raise ValueError(
                    f"taxonomy key collision: {normalized!r} maps to both "
                    f"{existing!r} and {entry_id!r}"
                )
            lookup[normalized] = entry_id
    return lookup


TAXONOMY_LOOKUP: dict[str, str] = _build_taxonomy_lookup()


def normalize_taxonomy_key(raw: str) -> str | None:
    """Normalize a reviewer code / deterministic check name / domain key /
    entry id to its canonical taxonomy entry_id, or None if unregistered."""
    return TAXONOMY_LOOKUP.get(_normalize_blocker_code(str(raw)))


def _domain_key_for(kind: str) -> str | None:
    entry = TAXONOMY_BY_ENTRY_ID.get(kind)
    if entry is None:
        return None
    domain_keys = entry["domain_keys"]
    return domain_keys[0] if domain_keys else None


def _deterministic_check_for(kind: str) -> str | None:
    entry = TAXONOMY_BY_ENTRY_ID.get(kind)
    if entry is None:
        return None
    checks = entry["deterministic_checks"]
    return checks[0] if checks else None


def _reject_nonfinite_json(token: str) -> None:
    raise ValueError(f"Non-finite JSON constant rejected: {token}")


def _strict_json_loads(text: str) -> dict[str, Any]:
    return json.loads(text, parse_constant=_reject_nonfinite_json)


def _strict_json_dumps(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _classify_blocker(blocker: dict[str, Any]) -> str:
    code = str(blocker.get("reviewer_blocker_code") or "")
    return normalize_taxonomy_key(code) or "unknown_blocker_type"


def _load_json_file(path: str, label: str) -> dict[str, Any]:
    try:
        return _strict_json_loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} json decode error: {exc}") from exc


def _extract_review_blockers(review_result: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for source in ("structured_blockers", "blocking_issues"):
        items = review_result.get(source)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                code = str(item.get("code") or item.get("reviewer_blocker_code") or "").strip()
                if code:
                    blockers.append(
                        {
                            "reviewer_blocker_code": code,
                            "message": item.get("message"),
                            "line_start": item.get("line_start"),
                            "line_end": item.get("line_end"),
                        }
                    )
            elif isinstance(item, str) and item.strip():
                blockers.append({"reviewer_blocker_code": item.strip()})
        if blockers:
            return blockers
    return blockers


def _extract_findings(review_result: dict[str, Any]) -> list[dict[str, Any]]:
    findings = review_result.get("findings", [])
    if not isinstance(findings, list):
        raise ValueError("review-result-file.findings must be a list when present")
    return [item for item in findings if isinstance(item, dict)]


def _matching_readiness_errors(kind: str, readiness_result: dict[str, Any]) -> list[dict[str, Any]]:
    entry = TAXONOMY_BY_ENTRY_ID.get(kind)
    if entry is None:
        return []
    matches: list[dict[str, Any]] = []
    rule_ids = frozenset(entry["readiness_rule_ids"])
    rule_id_source_check = entry.get("readiness_rule_id_source_check")
    categories = frozenset(entry["readiness_categories"])
    category_source_check = entry.get("readiness_category_source_check")
    for err in readiness_result.get("errors", []):
        if not isinstance(err, dict):
            continue
        rule_id = str(err.get("rule_id") or "")
        category = str(err.get("category") or "")
        source_check = str(err.get("source_check") or "")

        if rule_id in rule_ids and (rule_id_source_check is None or source_check == rule_id_source_check):
            matches.append(err)
            continue
        if category in categories and (
            category_source_check is None or source_check == category_source_check
        ):
            matches.append(err)
    return matches


def _validate_minimal_schema(payload: dict[str, Any], label: str, required_keys: tuple[str, ...]) -> None:
    missing = [key for key in required_keys if key not in payload]
    if missing:
        raise ValueError(f"{label} missing required keys: {', '.join(missing)}")


def _matching_vc_preflight(kind: str, vc_preflight_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if vc_preflight_result is None:
        return []
    results = vc_preflight_result.get("results", [])
    if not isinstance(results, list):
        raise ValueError("vc-preflight-result-file.results must be a list")
    if kind != "vc_command_format":
        return []
    entry = TAXONOMY_BY_ENTRY_ID.get(kind)
    categories = frozenset(entry["readiness_categories"]) if entry else frozenset()
    return [item for item in results if isinstance(item, dict) and item.get("category") in categories]


def _matching_vc_syntax(kind: str, vc_syntax_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if vc_syntax_result is None:
        return []
    errors = vc_syntax_result.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("vc-syntax-result-file.errors must be a list")
    if kind != "vc_command_format":
        return []
    entry = TAXONOMY_BY_ENTRY_ID.get(kind)
    rule_ids = frozenset(entry["readiness_rule_ids"]) if entry else frozenset()
    categories = frozenset(entry["readiness_categories"]) if entry else frozenset()
    return [
        item
        for item in errors
        if isinstance(item, dict)
        and (item.get("rule_id") in rule_ids or item.get("category") in categories)
    ]


def _evidence(source_check: str, payload: dict[str, Any], body_sha256: str) -> dict[str, Any]:
    return {
        "source_check": source_check,
        "rule_id": payload.get("rule_id"),
        "category": payload.get("category"),
        "artifact_path": payload.get("artifact_path", source_check or "unknown_artifact"),
        "artifact_schema": payload.get("artifact_schema", "CHECK_ISSUE_CONTRACT_V1"),
        "line_start": payload.get("line_start"),
        "line_end": payload.get("line_end"),
        "body_sha256": body_sha256,
        "iteration_id": payload.get("iteration_id", "legacy_replay_evidence"),
    }


def _is_valid_checker_evidence(entry: dict[str, Any], body_sha256: str) -> bool:
    required_fields = (
        "source_check",
        "rule_id",
        "category",
        "artifact_path",
        "artifact_schema",
        "body_sha256",
        "iteration_id",
    )
    for field_name in required_fields:
        value = entry.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return False
    if entry.get("artifact_schema") not in VALID_ARTIFACT_SCHEMAS:
        return False
    if entry.get("body_sha256") != body_sha256:
        return False
    return True


def _matching_findings(kind: str, findings: list[dict[str, Any]], body_sha256: str) -> list[dict[str, Any]]:
    deterministic_domain_key = _domain_key_for(kind)
    if deterministic_domain_key is None:
        return []

    matches: list[dict[str, Any]] = []
    for finding in findings:
        finding_kind = finding.get("finding_kind")
        if finding_kind not in VALID_FINDING_KINDS:
            continue
        if finding.get("deterministic_domain_key") != deterministic_domain_key:
            continue
        checker_evidence = finding.get("checker_evidence", [])
        if not isinstance(checker_evidence, list):
            continue
        valid_evidence = [
            entry
            for entry in checker_evidence
            if isinstance(entry, dict) and _is_valid_checker_evidence(entry, body_sha256)
        ]
        matches.append(
            {
                "finding_kind": finding_kind,
                "blocking": bool(finding.get("blocking")),
                "message": finding.get("message"),
                "checker_evidence": valid_evidence,
                "evidence_valid": bool(valid_evidence),
            }
        )
    return matches


def _load_state(state_file: str | None) -> dict[str, Any]:
    if not state_file:
        return {}
    path = Path(state_file)
    if not path.exists():
        return {}
    data = _strict_json_loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != STATE_SCHEMA:
        raise ValueError(f"unexpected state schema: {data.get('schema')!r}")
    return data


def _save_state(state_file: str | None, state: dict[str, Any]) -> None:
    if not state_file:
        return
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


# LEGACY_VERDICT_MAP_V1 -- the pre-#1286 verdict set documented in
# `issue-refinement-loop/SKILL.md` Step 2a (`deterministic_fail_confirmed`,
# `reviewer_claim_unbacked_by_deterministic_checker`,
# `reviewer_false_positive_suspected`, `input_or_runtime_error`) plus the
# pre-existing `checker_artifact_inconsistency` verdict.
#
# The top-level `verdict` field returned by `analyze()` is ALWAYS one of
# these legacy values (PR #1304 iteration-2 fix_delta): SKILL.md Step 2a's
# routing table only understands this set, and is Out of Scope to update
# for Issue #1286 (not in Allowed Paths). New verdicts introduced by Issue
# #1286 (`taxonomy_gap`, `checker_gap`, `checker_gap_repeated`) are mapped
# here to the semantically-closest legacy value with matching routing
# semantics (same consume/rerun/escalation behavior), so a consumer that
# has not been updated for the new values still receives a value it
# recognizes and routes safely.
#
# The precise Issue #1286 classification (including the new-only values)
# is NOT lost: it is carried in the secondary `verdict_detail_v1` field
# (see `analyze()`), which a `taxonomy_gap` / `checker_gap`-aware consumer
# can read without requiring the primary `verdict` field to carry a value
# outside the legacy set.
_LEGACY_VERDICT_MAP_V1: dict[str, str] = {
    "deterministic_fail_confirmed": "deterministic_fail_confirmed",
    "checker_artifact_inconsistency": "checker_artifact_inconsistency",
    "reviewer_claim_unbacked_by_deterministic_checker": "reviewer_claim_unbacked_by_deterministic_checker",
    "reviewer_false_positive_suspected": "reviewer_false_positive_suspected",
    "input_or_runtime_error": "input_or_runtime_error",
    # New Issue #1286 verdicts -> legacy fallback with matching `routing`
    # semantics (same consume/rerun/escalation behavior as the legacy value
    # it maps to):
    "taxonomy_gap": "checker_artifact_inconsistency",
    "checker_gap": "reviewer_claim_unbacked_by_deterministic_checker",
    "checker_gap_repeated": "reviewer_false_positive_suspected",
}


def _legacy_verdict(verdict: str) -> str:
    """Map a precise verdict to the pre-#1286 SKILL.md Step 2a compatible
    value. This is the value emitted as the top-level `verdict` field."""
    return _LEGACY_VERDICT_MAP_V1.get(verdict, "input_or_runtime_error")


def _has_deterministic_check_failure(kind: str, review_result: dict[str, Any]) -> bool:
    check_name = _deterministic_check_for(kind)
    if not check_name:
        return False
    deterministic_checks = review_result.get("deterministic_checks", {})
    if not isinstance(deterministic_checks, dict):
        return False
    return deterministic_checks.get(check_name) == "fail"


def analyze(
    *,
    review_result: dict[str, Any],
    readiness_result: dict[str, Any],
    vc_syntax_result: dict[str, Any] | None,
    vc_preflight_result: dict[str, Any] | None,
    previous_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    blockers = _extract_review_blockers(review_result)
    findings = _extract_findings(review_result)
    body_sha256 = str(readiness_result.get("body_sha256") or "")
    review_body_sha256 = str(review_result.get("body_sha256") or body_sha256)
    issue_url = str(review_result.get("issue_url") or "")
    previous_state = previous_state or {}

    if not blockers:
        return (
            {
                "schema": SCHEMA,
                "verdict": _legacy_verdict("input_or_runtime_error"),
                "verdict_detail_v1": "input_or_runtime_error",
                "routing": "human_judgment_required",
                "should_consume_iteration": False,
                "blockers": [],
                "error": "review-result has no blocker entries",
            },
            previous_state,
        )

    blocker_results: list[dict[str, Any]] = []
    rewrite_ready_blockers: list[dict[str, Any]] = []
    inconsistency_blockers: list[dict[str, Any]] = []
    taxonomy_gap_blockers: list[dict[str, Any]] = []
    checker_gap_blockers: list[dict[str, Any]] = []
    for blocker in blockers:
        kind = _classify_blocker(blocker)
        is_known = kind in TAXONOMY_BY_ENTRY_ID
        matched_findings = _matching_findings(kind, findings, body_sha256) if is_known else []
        evidence: list[dict[str, Any]] = []
        deterministic_backed = False
        if matched_findings:
            for finding in matched_findings:
                if (
                    finding["finding_kind"] == "deterministic_domain_blocker"
                    and finding["blocking"]
                    and finding["evidence_valid"]
                ):
                    deterministic_backed = True
                    evidence.extend(finding["checker_evidence"])
        fallback_evidence: list[dict[str, Any]] = []
        if is_known and not deterministic_backed and (
            not matched_findings
            or all(
                finding["finding_kind"] in {"checker_gap", "heuristic_concern"}
                or not finding["evidence_valid"]
                for finding in matched_findings
            )
        ):
            fallback_evidence = [
                _evidence("baseline_vc_preflight", item, body_sha256)
                for item in _matching_vc_preflight(kind, vc_preflight_result)
            ]
            fallback_evidence.extend(
                _evidence("vc_contract_syntax", item, body_sha256)
                for item in _matching_vc_syntax(kind, vc_syntax_result)
            )
            fallback_evidence.extend(
                _evidence(str(err.get("source_check") or ""), err, body_sha256)
                for err in _matching_readiness_errors(kind, readiness_result)
            )
        if not matched_findings and fallback_evidence:
            evidence = list(fallback_evidence)
            deterministic_backed = True
        has_inconsistency = (
            bool(matched_findings)
            and not deterministic_backed
            and (
                _has_deterministic_check_failure(kind, review_result)
                or bool(fallback_evidence)
            )
        )
        if has_inconsistency:
            evidence = list(fallback_evidence)

        # taxonomy_gap: the deterministic checker for a *known* taxonomy
        # entry has failed for the same body hash the reviewer analyzed,
        # but there is neither a structured finding nor fallback readiness
        # evidence to corroborate it (a mapping / artifact gap). This must
        # not be routed back through the reviewer-rerun lanes below --
        # it is a machine-actionable checker/taxonomy gap, not a reviewer
        # false-positive candidate (Issue #1286 AC3).
        taxonomy_gap = (
            is_known
            and not deterministic_backed
            and not has_inconsistency
            and not matched_findings
            and _has_deterministic_check_failure(kind, review_result)
            and bool(body_sha256)
            and review_body_sha256 == body_sha256
        )

        if deterministic_backed:
            bucket = "rewrite_ready"
        elif has_inconsistency:
            bucket = "checker_artifact_inconsistency"
        elif taxonomy_gap:
            bucket = "taxonomy_gap"
        elif not is_known:
            bucket = "checker_gap"
        else:
            bucket = "unbacked"

        blocker_result = {
            "reviewer_blocker_code": blocker["reviewer_blocker_code"],
            "normalized_kind": kind,
            "deterministic_backed": deterministic_backed,
            "checker_artifact_inconsistency": has_inconsistency,
            "taxonomy_gap": taxonomy_gap,
            "checker_gap": bucket == "checker_gap",
            "message": blocker.get("message"),
            "line_start": blocker.get("line_start"),
            "line_end": blocker.get("line_end"),
            "evidence": evidence,
            "matched_findings": matched_findings,
        }
        blocker_results.append(blocker_result)
        if bucket == "rewrite_ready":
            rewrite_ready_blockers.append(blocker_result)
        elif bucket == "checker_artifact_inconsistency":
            inconsistency_blockers.append(blocker_result)
        elif bucket == "taxonomy_gap":
            taxonomy_gap_blockers.append(blocker_result)
        elif bucket == "checker_gap":
            checker_gap_blockers.append(blocker_result)

    primary = blocker_results[0]
    same_lane = (
        primary["reviewer_blocker_code"] == previous_state.get("reviewer_blocker_code")
        and primary["normalized_kind"] == previous_state.get("normalized_kind")
        and body_sha256 == previous_state.get("body_sha256")
    )
    prior_count = int(previous_state.get("consecutive_unbacked_count", 0))

    if rewrite_ready_blockers:
        verdict_detail = "deterministic_fail_confirmed"
        routing = "proceed_to_rewrite"
        should_consume_iteration = True
        next_count = 0
    elif inconsistency_blockers:
        verdict_detail = "checker_artifact_inconsistency"
        routing = "fix_checker_artifact"
        should_consume_iteration = False
        next_count = 0
    elif taxonomy_gap_blockers:
        # Deterministic check fail confirmed for the same body hash but
        # unbacked by structured evidence -- block without returning to
        # the reviewer-rerun lane (Issue #1286 AC3). This is a new-in
        # Issue #1286 classification; the top-level `verdict` field still
        # downgrades to the legacy `checker_artifact_inconsistency` value
        # (same `fix_checker_artifact` routing semantics) so a consumer
        # that only understands the legacy verdict set routes correctly.
        verdict_detail = "taxonomy_gap"
        routing = "fix_checker_artifact"
        should_consume_iteration = False
        next_count = 0
    elif checker_gap_blockers:
        # Reviewer blocker code is unregistered in the taxonomy entirely.
        # Allow exactly one reviewer rerun for the same lane before
        # escalating to a human (Issue #1286 AC4).
        next_count = prior_count + 1 if same_lane else 1
        if next_count >= 2:
            verdict_detail = "checker_gap_repeated"
            routing = "human_escalation"
        else:
            verdict_detail = "checker_gap"
            routing = "downgrade_to_non_blocking"
        should_consume_iteration = False
    else:
        next_count = prior_count + 1 if same_lane else 1
        verdict_detail = (
            "reviewer_false_positive_suspected"
            if next_count >= 2
            else "reviewer_claim_unbacked_by_deterministic_checker"
        )
        routing = (
            "human_escalation"
            if verdict_detail == "reviewer_false_positive_suspected"
            else "downgrade_to_non_blocking"
        )
        should_consume_iteration = False

    verdict = _legacy_verdict(verdict_detail)

    next_state = {
        "schema": STATE_SCHEMA,
        "issue_url": issue_url,
        "body_sha256": body_sha256,
        "reviewer_blocker_code": primary["reviewer_blocker_code"],
        "normalized_kind": primary["normalized_kind"],
        "consecutive_unbacked_count": next_count,
        "last_review_artifact": str(review_result.get("_artifact_path") or ""),
    }
    return (
        {
            "schema": SCHEMA,
            "verdict": verdict,
            "verdict_detail_v1": verdict_detail,
            "routing": routing,
            "should_consume_iteration": should_consume_iteration,
            "body_sha256": body_sha256,
            "issue_url": issue_url,
            "blockers": blocker_results,
            "rewrite_ready_blockers": rewrite_ready_blockers,
        },
        next_state,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay reviewer blockers against deterministic artifacts")
    parser.add_argument("--review-result-file")
    parser.add_argument("--readiness-result-file")
    parser.add_argument("--vc-syntax-result-file")
    parser.add_argument("--vc-preflight-result-file")
    parser.add_argument("--state-file")
    parser.add_argument(
        "--dump-taxonomy",
        action="store_true",
        help="Print REVIEWER_CHECKER_TAXONOMY_V1 as JSON and exit (no other args required)",
    )
    args = parser.parse_args()

    if args.dump_taxonomy:
        print(
            _strict_json_dumps(
                {"schema": TAXONOMY_SCHEMA, "entries": REVIEWER_CHECKER_TAXONOMY_V1}
            ),
            flush=True,
        )
        return 0

    if not args.review_result_file or not args.readiness_result_file:
        print(
            _strict_json_dumps(
                {
                    "schema": SCHEMA,
                    "verdict": "input_or_runtime_error",
                    "routing": "human_judgment_required",
                    "error": "--review-result-file and --readiness-result-file required unless --dump-taxonomy",
                }
            ),
            flush=True,
        )
        return 1

    try:
        review_result = _load_json_file(args.review_result_file, "review-result-file")
        review_result["_artifact_path"] = args.review_result_file
        readiness_result = _load_json_file(args.readiness_result_file, "readiness-result-file")
        _validate_minimal_schema(review_result, "review-result-file", ("issue_url",))
        _validate_minimal_schema(readiness_result, "readiness-result-file", ("body_sha256", "errors"))
        vc_syntax_result = (
            _load_json_file(args.vc_syntax_result_file, "vc-syntax-result-file")
            if args.vc_syntax_result_file
            else None
        )
        vc_preflight_result = (
            _load_json_file(args.vc_preflight_result_file, "vc-preflight-result-file")
            if args.vc_preflight_result_file
            else None
        )
        previous_state = _load_state(args.state_file)
        result, next_state = analyze(
            review_result=review_result,
            readiness_result=readiness_result,
            vc_syntax_result=vc_syntax_result,
            vc_preflight_result=vc_preflight_result,
            previous_state=previous_state,
        )
        _save_state(args.state_file, next_state)
    except ValueError as exc:
        print(
            _strict_json_dumps(
                {
                    "schema": SCHEMA,
                    "verdict": _legacy_verdict("input_or_runtime_error"),
                    "verdict_detail_v1": "input_or_runtime_error",
                    "routing": "human_judgment_required",
                    "error": str(exc),
                },
            ),
            flush=True,
        )
        return 1

    output = _strict_json_dumps(result)
    if len(output.encode("utf-8")) > 2048:
        trimmed = dict(result)
        trimmed["blockers"] = [
            {
                "reviewer_blocker_code": blocker["reviewer_blocker_code"],
                "normalized_kind": blocker["normalized_kind"],
                "deterministic_backed": blocker["deterministic_backed"],
                "checker_artifact_inconsistency": blocker["checker_artifact_inconsistency"],
            }
            for blocker in result["blockers"][:2]
        ]
        output = _strict_json_dumps(trimmed)
    print(output, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

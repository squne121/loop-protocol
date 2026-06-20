#!/usr/bin/env python3
"""
reviewer_claim_replay.py - REVIEWER_CLAIM_REPLAY_V1

Arbitrates reviewer blockers against deterministic artifacts before a
`needs-fix` verdict consumes another refinement-loop iteration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "REVIEWER_CLAIM_REPLAY_V1"
STATE_SCHEMA = "REVIEWER_CLAIM_REPLAY_STATE_V1"

VC_COMMAND_RULE_IDS = frozenset({"VCS001", "LP011", "LP016"})
VC_COMMAND_CATEGORIES = frozenset(
    {"non_dollar_command", "compound_shell", "compound_command_disallowed", "no_commands_extracted"}
)
LP010_RULE_IDS = frozenset({"LP010"})
MISSING_SECTION_RULE_IDS = frozenset({"LP001"})
RVA_CATEGORIES = frozenset({"rva_immediate_field_missing"})

KIND_ALIASES = {
    "c4": "vc_command_format",
    "vc_command_format": "vc_command_format",
    "vc command format": "vc_command_format",
    "missing $ prefix": "vc_command_format",
    "missing_$_prefix": "vc_command_format",
    "lp010": "ac_vc_number_mismatch",
    "ac_vc_number_mismatch": "ac_vc_number_mismatch",
    "missing_required_section": "missing_section",
    "missing_section": "missing_section",
    "c5": "missing_section",
    "rva_immediate_field_missing": "rva_immediate_field_missing",
}
KIND_TO_DETERMINISTIC_DOMAIN_KEY = {
    "vc_command_format": "vc_command_format",
    "ac_vc_number_mismatch": "vc_number_alignment",
    "missing_section": "required_sections",
    "rva_immediate_field_missing": "runtime_applicability",
}
VALID_FINDING_KINDS = frozenset(
    {"deterministic_domain_blocker", "checker_gap", "heuristic_concern"}
)
VALID_ARTIFACT_SCHEMAS = frozenset({"REVIEW_ISSUE_RESULT_V1", "CHECK_ISSUE_CONTRACT_V1"})


def _normalize_blocker_code(code: str) -> str:
    return " ".join(code.strip().lower().split())


def _classify_blocker(blocker: dict[str, Any]) -> str:
    code = str(blocker.get("reviewer_blocker_code") or "")
    return KIND_ALIASES.get(_normalize_blocker_code(code), "unknown_blocker_type")


def _load_json_file(path: str, label: str) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
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
    matches: list[dict[str, Any]] = []
    for err in readiness_result.get("errors", []):
        if not isinstance(err, dict):
            continue
        rule_id = str(err.get("rule_id") or "")
        category = str(err.get("category") or "")
        source_check = str(err.get("source_check") or "")

        if kind == "vc_command_format":
            if rule_id in VC_COMMAND_RULE_IDS:
                matches.append(err)
            elif source_check == "contract_readiness_check" and category in VC_COMMAND_CATEGORIES:
                matches.append(err)
        elif kind == "ac_vc_number_mismatch" and rule_id in LP010_RULE_IDS:
            matches.append(err)
        elif kind == "missing_section" and source_check == "validate_issue_body" and rule_id in MISSING_SECTION_RULE_IDS:
            matches.append(err)
        elif kind == "rva_immediate_field_missing" and source_check == "contract_readiness_check" and category in RVA_CATEGORIES:
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
    return [item for item in results if isinstance(item, dict) and item.get("category") in VC_COMMAND_CATEGORIES]


def _matching_vc_syntax(kind: str, vc_syntax_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if vc_syntax_result is None:
        return []
    errors = vc_syntax_result.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("vc-syntax-result-file.errors must be a list")
    if kind != "vc_command_format":
        return []
    return [
        item
        for item in errors
        if isinstance(item, dict)
        and (item.get("rule_id") in VC_COMMAND_RULE_IDS or item.get("category") in VC_COMMAND_CATEGORIES)
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
    deterministic_domain_key = KIND_TO_DETERMINISTIC_DOMAIN_KEY.get(kind)
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
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != STATE_SCHEMA:
        raise ValueError(f"unexpected state schema: {data.get('schema')!r}")
    return data


def _save_state(state_file: str | None, state: dict[str, Any]) -> None:
    if not state_file:
        return
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
    issue_url = str(review_result.get("issue_url") or "")
    previous_state = previous_state or {}

    if not blockers:
        return (
            {
                "schema": SCHEMA,
                "verdict": "input_or_runtime_error",
                "routing": "human_judgment_required",
                "should_consume_iteration": False,
                "blockers": [],
                "error": "review-result has no blocker entries",
            },
            previous_state,
        )

    blocker_results: list[dict[str, Any]] = []
    rewrite_ready_blockers: list[dict[str, Any]] = []
    for blocker in blockers:
        kind = _classify_blocker(blocker)
        matched_findings = _matching_findings(kind, findings, body_sha256)
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
        else:
            evidence = [
                _evidence("baseline_vc_preflight", item, body_sha256)
                for item in _matching_vc_preflight(kind, vc_preflight_result)
            ]
            evidence.extend(
                _evidence("vc_contract_syntax", item, body_sha256)
                for item in _matching_vc_syntax(kind, vc_syntax_result)
            )
            evidence.extend(
                _evidence(str(err.get("source_check") or ""), err, body_sha256)
                for err in _matching_readiness_errors(kind, readiness_result)
            )
            deterministic_backed = bool(evidence)
        blocker_result = {
            "reviewer_blocker_code": blocker["reviewer_blocker_code"],
            "normalized_kind": kind,
            "deterministic_backed": deterministic_backed,
            "message": blocker.get("message"),
            "line_start": blocker.get("line_start"),
            "line_end": blocker.get("line_end"),
            "evidence": evidence,
            "matched_findings": matched_findings,
        }
        blocker_results.append(blocker_result)
        if deterministic_backed:
            rewrite_ready_blockers.append(blocker_result)

    primary = blocker_results[0]
    same_lane = (
        primary["reviewer_blocker_code"] == previous_state.get("reviewer_blocker_code")
        and primary["normalized_kind"] == previous_state.get("normalized_kind")
        and body_sha256 == previous_state.get("body_sha256")
    )
    prior_count = int(previous_state.get("consecutive_unbacked_count", 0))

    if rewrite_ready_blockers:
        verdict = "deterministic_fail_confirmed"
        routing = "proceed_to_rewrite"
        should_consume_iteration = True
        next_count = 0
    else:
        next_count = prior_count + 1 if same_lane else 1
        verdict = (
            "reviewer_false_positive_suspected"
            if next_count >= 2
            else "reviewer_claim_unbacked_by_deterministic_checker"
        )
        routing = "human_escalation" if verdict == "reviewer_false_positive_suspected" else "downgrade_to_non_blocking"
        should_consume_iteration = False

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
    parser.add_argument("--review-result-file", required=True)
    parser.add_argument("--readiness-result-file", required=True)
    parser.add_argument("--vc-syntax-result-file")
    parser.add_argument("--vc-preflight-result-file")
    parser.add_argument("--state-file")
    args = parser.parse_args()

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
            json.dumps(
                {
                    "schema": SCHEMA,
                    "verdict": "input_or_runtime_error",
                    "routing": "human_judgment_required",
                    "error": str(exc),
                },
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1

    output = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
    if len(output.encode("utf-8")) > 2048:
        trimmed = dict(result)
        trimmed["blockers"] = [
            {
                "reviewer_blocker_code": blocker["reviewer_blocker_code"],
                "normalized_kind": blocker["normalized_kind"],
                "deterministic_backed": blocker["deterministic_backed"],
            }
            for blocker in result["blockers"][:2]
        ]
        output = json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)
    print(output, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

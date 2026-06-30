#!/usr/bin/env python3
"""Classify VC failures into VC_ADJUDICATION_RESULT_V1."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SCHEMA_NAME = "VC_ADJUDICATION_RESULT_V1"
SCHEMA_VERSION = 1
PRIVATE_BUNDLE_SCHEMA = "VC_ADJUDICATION_PRIVATE_BUNDLE_V1"
PRIVATE_ARTIFACT_REF = "vc-adjudication-private-bundle"
STATUS_PRIORITY = {
    "pass": 0,
    "pre_existing_fail": 1,
    "out_of_scope_fail": 2,
    "regression_fail": 3,
    "environment_blocked": 4,
    "indeterminate": 5,
}
PATH_RELEVANCE_KINDS = {"pytest_nodeid", "repo_path"}
ENVIRONMENT_BLOCKED_CATEGORIES = {
    "runtime_dependency_error",
    "package_manager_no_tty_prompt",
    "timeout",
}

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ALLOWED_PATHS_GATE_PATH = (
    _REPO_ROOT
    / ".claude"
    / "skills"
    / "pr-review-judge"
    / "scripts"
    / "allowed_paths_review_gate.py"
)
_ALLOWED_PATHS_MATCHER = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_command(command: str) -> str:
    return _sha256(command)


def _is_hex_64(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _normalize_command_hash(value: Any, raw_command: Any) -> tuple[str | None, str | None]:
    if isinstance(value, str):
        if value.startswith("sha256:") and _is_hex_64(value[7:]):
            return value, None
        if _is_hex_64(value):
            return "sha256:" + value, "normalized_legacy_bare_command_hash"
    if isinstance(raw_command, str):
        return _sha256_command(raw_command), "derived_command_hash_from_raw_command"
    return None, "missing_or_invalid_command_hash"


def _load_json_file(path: str | None) -> tuple[Any, list[str]]:
    if not path:
        return None, ["missing_input_file"]
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [f"input_file_not_found:{path}"]
    except OSError as exc:
        return None, [f"input_read_error:{path}:{type(exc).__name__}"]

    try:
        return json.loads(text), []
    except json.JSONDecodeError as exc:
        return None, [f"input_json_error:{path}:{exc}"]


def _normalize_list_payload(payload: Any) -> tuple[list[Any], list[str], str | None]:
    if payload is None:
        return [], ["input_missing"], None
    if isinstance(payload, list):
        return payload, [], None
    if not isinstance(payload, dict):
        return [], ["input_not_object"], None

    schema = payload.get("schema")
    if schema == "baseline_vc_preflight/v1":
        results = payload.get("results")
        if isinstance(results, list):
            return results, [], schema
        return [], ["missing_baseline_results"], schema

    if schema == "CONTRACT_REVIEW_RESULT_V1":
        checks = payload.get("checks")
        if not isinstance(checks, dict):
            return [], ["missing_checks"], schema
        vc_preflight = checks.get("vc_preflight")
        if isinstance(vc_preflight, dict):
            classifications = vc_preflight.get("classifications")
            if isinstance(classifications, list):
                return classifications, [], schema
        checks_classifications = checks.get("vc_preflight_classifications")
        if isinstance(checks_classifications, list):
            return checks_classifications, [], schema
        return [], ["missing_vc_preflight_classifications"], schema

    if schema == "CONTRACT_REVIEW_ONCE_RESULT_V1":
        results = payload.get("vc_preflight_classifications")
        if isinstance(results, list):
            return results, [], schema
        return [], ["missing_vc_preflight_classifications"], schema

    return [], [f"unsupported_schema:{schema}"], schema


def _normalize_failure_keys(value: Any) -> tuple[list[dict[str, str]], bool]:
    if value is None:
        return [], False
    if isinstance(value, str):
        return [{"kind": "unknown", "key": value}], True
    if not isinstance(value, list):
        return [], False

    normalized: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            normalized.append({"kind": "unknown", "key": item})
            continue
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            continue
        kind = item.get("kind")
        normalized.append(
            {
                "kind": kind if isinstance(kind, str) and kind else "unknown",
                "key": key,
            }
        )
    return normalized, bool(normalized)


def _normalize_item(item: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(item, dict):
        return None, ["non_object_item"]

    ac = item.get("ac")
    if not isinstance(ac, str) or not ac:
        return None, ["missing_or_invalid_ac"]

    command_hash, command_hash_note = _normalize_command_hash(
        item.get("command_hash"),
        item.get("raw_command"),
    )
    if command_hash is None:
        return None, ["missing_or_invalid_command_hash"]

    exit_code = item.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        return None, ["invalid_exit_code"]

    category = item.get("category")
    if category is not None and not isinstance(category, str):
        return None, ["invalid_category"]

    failure_keys, failure_keys_present = _normalize_failure_keys(item.get("failure_keys"))
    normalized = {
        "ac": ac,
        "command_hash": command_hash,
        "exit_code": exit_code,
        "category": category,
        "failure_keys": failure_keys,
        "failure_keys_present": failure_keys_present,
    }
    if command_hash_note is not None:
        normalized["command_hash_note"] = command_hash_note
    return normalized, []


def _load_path_list(raw: Any) -> tuple[list[str], list[str]]:
    if raw is None:
        return [], ["missing_path_input"]
    if not isinstance(raw, list):
        return [], ["invalid_path_input"]
    return [item for item in raw if isinstance(item, str)], []


def _extract_changed_paths(diff_summary: Any) -> tuple[list[str], bool, list[str]]:
    if diff_summary is None:
        return [], False, ["missing_diff_summary"]
    if not isinstance(diff_summary, dict):
        return [], False, ["invalid_diff_summary"]

    raw_paths = None
    for key in ("changed_paths", "changed", "paths", "files"):
        if key in diff_summary:
            raw_paths = diff_summary.get(key)
            break

    if isinstance(raw_paths, list):
        values: list[str] = []
        for item in raw_paths:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                values.append(item.get("path"))
            elif isinstance(item, dict) and isinstance(item.get("file"), str):
                values.append(item.get("file"))
        return values, bool(values), []

    return [], False, ["missing_changed_paths"]


def _normalize_path(path: str) -> str:
    return path.rstrip("/")


def _get_allowed_paths_matcher():
    global _ALLOWED_PATHS_MATCHER
    if _ALLOWED_PATHS_MATCHER is not None:
        return _ALLOWED_PATHS_MATCHER, None

    spec = importlib.util.spec_from_file_location(
        "allowed_paths_review_gate_for_adjudicator",
        _ALLOWED_PATHS_GATE_PATH,
    )
    if spec is None or spec.loader is None:
        return None, "allowed_paths_matcher_unavailable"
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover
        return None, f"allowed_paths_matcher_import_failed:{type(exc).__name__}"
    _ALLOWED_PATHS_MATCHER = module.AllowedPathsMatcher
    return _ALLOWED_PATHS_MATCHER, None


def _failure_key_root(failure_key: dict[str, str]) -> str | None:
    kind = failure_key["kind"]
    key = failure_key["key"].strip()
    if kind not in PATH_RELEVANCE_KINDS:
        return None
    if "::" in key:
        return key.split("::", 1)[0]
    return key


def _normalize_allowed_paths(allowed_paths: list[str]) -> tuple[list[str], str | None]:
    matcher, error = _get_allowed_paths_matcher()
    if matcher is None:
        return [], error

    normalized: list[str] = []
    for path in allowed_paths:
        normalized_path = matcher.normalize_allowed_pattern(path)
        if normalized_path is None:
            return [], f"invalid_allowed_path_pattern:{path}"
        normalized.append(normalized_path)
    return normalized, None


def _normalize_scope_paths(paths: list[str]) -> list[str]:
    return [_normalize_path(path) for path in paths if isinstance(path, str) and path.strip()]


def _is_related_to_scope(
    failure_keys: list[dict[str, str]],
    changed_paths: list[str],
    allowed_paths: list[str],
) -> tuple[bool, bool]:
    matcher, error = _get_allowed_paths_matcher()
    if matcher is None or error is not None:
        return False, False

    scope = [*_normalize_scope_paths(changed_paths), *allowed_paths]
    for failure_key in failure_keys:
        root = _failure_key_root(failure_key)
        if root is None:
            return False, False
        normalized_root = matcher.normalize_path(root)
        if normalized_root is None:
            return False, False
        for path in scope:
            if matcher.matches_pattern(normalized_root, path):
                return True, True
    return False, True


def _extract_source_integrity(
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    allowed_paths: list[str] | None,
    normalized_allowed: list[str],
    baseline_schema: str | None,
    current_items_count: int,
    baseline_items_count: int,
    changed_paths_present: bool,
) -> dict[str, Any]:
    contract_body_sha256 = None
    if isinstance(contract_snapshot, dict):
        contract_body_sha256 = contract_snapshot.get("body_sha256")

    base_sha = None
    head_sha = None
    reviewed_head_sha = None
    current_vc_result_head_sha = None
    diff_summary_head_sha = None
    if isinstance(diff_summary, dict):
        base_sha = diff_summary.get("base_sha")
        head_sha = diff_summary.get("head_sha")
        diff_summary_head_sha = diff_summary.get("head_sha")
    if isinstance(current_vc_result, dict):
        current_vc_result_head_sha = current_vc_result.get("head_sha")
        reviewed_head_sha = current_vc_result.get("reviewed_head_sha")

    if isinstance(current_vc_result_head_sha, str) and current_vc_result_head_sha:
        head_sha = current_vc_result_head_sha
    if isinstance(reviewed_head_sha, str) and reviewed_head_sha:
        head_sha = head_sha or reviewed_head_sha

    evidence_fresh = True
    if (
        isinstance(diff_summary_head_sha, str)
        and isinstance(current_vc_result_head_sha, str)
        and diff_summary_head_sha
        and current_vc_result_head_sha
        and diff_summary_head_sha != current_vc_result_head_sha
    ):
        evidence_fresh = False

    return {
        "contract_snapshot_present": contract_snapshot is not None,
        "current_vc_result_present": current_vc_result is not None,
        "diff_summary_present": diff_summary is not None,
        "allowed_paths_present": allowed_paths is not None,
        "baseline_schema": baseline_schema,
        "current_items_count": current_items_count,
        "baseline_items_count": baseline_items_count,
        "changed_paths_present": changed_paths_present,
        "allowed_paths_normalized_sha256": _sha256(_canonical_json(normalized_allowed))
        if normalized_allowed
        else None,
        "contract_body_sha256": contract_body_sha256,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "current_vc_result_head_sha": current_vc_result_head_sha,
        "diff_summary_head_sha": diff_summary_head_sha,
        "evidence_complete": (
            contract_snapshot is not None
            and current_vc_result is not None
            and diff_summary is not None
            and allowed_paths is not None
        ),
        "evidence_fresh": evidence_fresh,
    }


def _build_evidence_refs(
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: dict[str, Any] | None,
    normalized_allowed: list[str] | None,
) -> list[dict[str, str]]:
    refs = [
        {
            "kind": "contract_snapshot",
            "ref": "inline:contract_snapshot",
            "digest": _sha256(_canonical_json(contract_snapshot)),
            "validation_verdict": "pass",
        },
        {
            "kind": "current_vc_result",
            "ref": "inline:current_vc_result",
            "digest": _sha256(_canonical_json(current_vc_result)),
            "validation_verdict": "pass",
        },
    ]
    if diff_summary is not None:
        refs.append(
            {
                "kind": "diff_summary",
                "ref": "inline:diff_summary",
                "digest": _sha256(_canonical_json(diff_summary)),
                "validation_verdict": "pass",
            }
        )
    if normalized_allowed is not None:
        refs.append(
            {
                "kind": "allowed_paths",
                "ref": "inline:allowed_paths",
                "digest": _sha256(_canonical_json(normalized_allowed)),
                "validation_verdict": "pass",
            }
        )
    return refs


def _result(
    *,
    overall_status: str,
    per_ac: list[dict[str, Any]],
    rerun_required: bool,
    source_integrity: dict[str, Any],
    evidence_refs: list[dict[str, str]],
    artifact_ref: str | None = None,
    artifact_digest: str | None = None,
    errors: list[str] | None = None,
    stdout_truncated: bool = False,
    omitted_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "overall_status": overall_status,
        "blocking": (
            any(item["blocking"] for item in per_ac)
            if per_ac
            else overall_status not in {"pass", "pre_existing_fail", "out_of_scope_fail"}
        ),
        "rerun_required": rerun_required,
        "per_ac": per_ac,
        "evidence_refs": evidence_refs,
        "source_integrity": source_integrity,
        "errors": errors or [],
        "artifact_ref": artifact_ref,
        "artifact_digest": artifact_digest,
        "stdout_truncated": stdout_truncated,
        "omitted_fields": omitted_fields or [],
    }


def _classify_item(
    item: dict[str, Any],
    baseline_signatures: set[tuple[str, tuple[tuple[str, str], ...]]],
    baseline_failure_index: set[tuple[str, str]],
    changed_paths: list[str],
    allowed_paths: list[str],
    source_integrity: dict[str, Any],
) -> tuple[str, bool, bool, str, str]:
    command_hash = item["command_hash"]
    failure_keys = item["failure_keys"]

    if item.get("exit_code") == 5 or item.get("category") == "vc_no_tests_collected":
        return "indeterminate", True, True, "pytest_exit_5", "Pytest exit code 5 is not treated as regression"

    if item.get("category") in ENVIRONMENT_BLOCKED_CATEGORIES:
        return "environment_blocked", True, True, "environment_signal", "Environment/tooling blocker detected"

    if not source_integrity["evidence_fresh"]:
        return "indeterminate", True, True, "stale_evidence", "Source evidence is stale for current head"

    baseline_key = (
        command_hash,
        tuple((entry["kind"], entry["key"]) for entry in failure_keys),
    )
    if baseline_key in baseline_signatures:
        if item["failure_keys_present"] and source_integrity["evidence_complete"] and not changed_paths:
            return (
                "pre_existing_fail",
                False,
                False,
                "same_baseline_no_diff",
                "Exact baseline signature with no diff and complete evidence",
            )
        return (
            "indeterminate",
            True,
            True,
            "baseline_match_inconclusive",
            "Baseline match exists but evidence is incomplete or diff is present",
        )

    if not item["failure_keys_present"]:
        return "indeterminate", True, True, "missing_failure_keys", "Failure key evidence is missing"

    if not changed_paths or not allowed_paths:
        return (
            "indeterminate",
            True,
            True,
            "insufficient_scope_evidence",
            "Diff scope evidence is incomplete for adjudication",
        )

    related, relevance_deterministic = _is_related_to_scope(
        failure_keys,
        changed_paths,
        allowed_paths,
    )
    if not relevance_deterministic:
        return (
            "indeterminate",
            True,
            True,
            "unsupported_failure_key_kind",
            "Failure key kind cannot prove scope irrelevance",
        )
    if related:
        return (
            "regression_fail",
            True,
            False,
            "related_to_changed_scope",
            "Failure is related to changed and/or allowed scope",
        )

    current_failure_index = {(entry["kind"], entry["key"]) for entry in failure_keys}
    if not current_failure_index.issubset(baseline_failure_index):
        return (
            "indeterminate",
            True,
            True,
            "new_failure_without_scope_proof",
            "New failure keys cannot be downgraded to out_of_scope without baseline match",
        )

    return (
        "out_of_scope_fail",
        False,
        False,
        "unrelated_to_scope_with_baseline_match",
        "Failure is unrelated to changed scope and was already present in baseline",
    )


def adjudicate_vc_result(
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: dict[str, Any] | None,
    allowed_paths: list[str] | None,
) -> dict[str, Any]:
    baseline_items, baseline_errors, baseline_schema = _normalize_list_payload(contract_snapshot)
    current_items, current_errors, _ = _normalize_list_payload(current_vc_result)
    changed_paths, changed_paths_present, diff_errors = _extract_changed_paths(diff_summary)
    allowed_path_values, allowed_paths_errors = _load_path_list(allowed_paths)
    normalized_allowed, normalize_allowed_error = _normalize_allowed_paths(allowed_path_values)
    if normalize_allowed_error is not None:
        allowed_paths_errors.append(normalize_allowed_error)

    source_integrity = _extract_source_integrity(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        normalized_allowed=normalized_allowed,
        baseline_schema=baseline_schema,
        current_items_count=len(current_items),
        baseline_items_count=len(baseline_items),
        changed_paths_present=changed_paths_present,
    )
    extraction_errors = list(baseline_errors + current_errors + diff_errors + allowed_paths_errors)
    evidence_refs = _build_evidence_refs(
        contract_snapshot,
        current_vc_result,
        diff_summary,
        normalized_allowed,
    )

    if extraction_errors:
        return _result(
            overall_status="indeterminate",
            rerun_required=True,
            per_ac=[],
            source_integrity=source_integrity,
            evidence_refs=evidence_refs,
            errors=extraction_errors,
        )

    baseline_signatures: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    baseline_failure_index: set[tuple[str, str]] = set()
    for idx, item in enumerate(baseline_items):
        norm, errs = _normalize_item(item)
        if norm is None:
            return _result(
                overall_status="indeterminate",
                per_ac=[],
                rerun_required=True,
                source_integrity=source_integrity,
                evidence_refs=evidence_refs,
                errors=[f"baseline[{idx}]:{err}" for err in errs],
            )
        baseline_signatures.add(
            (
                norm["command_hash"],
                tuple((entry["kind"], entry["key"]) for entry in norm["failure_keys"]),
            )
        )
        baseline_failure_index.update((entry["kind"], entry["key"]) for entry in norm["failure_keys"])

    per_ac: list[dict[str, Any]] = []
    for idx, item in enumerate(current_items):
        norm, errs = _normalize_item(item)
        if norm is None:
            return _result(
                overall_status="indeterminate",
                per_ac=[],
                rerun_required=True,
                source_integrity=source_integrity,
                evidence_refs=evidence_refs,
                errors=[f"current[{idx}]:{err}" for err in errs],
            )

        status, blocking, rerun_required, reason_code, summary = _classify_item(
            item=norm,
            baseline_signatures=baseline_signatures,
            baseline_failure_index=baseline_failure_index,
            changed_paths=changed_paths,
            allowed_paths=normalized_allowed,
            source_integrity=source_integrity,
        )
        entry = {
            "ac": norm["ac"],
            "status": status,
            "blocking": blocking,
            "command_hash": norm["command_hash"],
            "failure_keys": norm["failure_keys"],
            "reason_code": reason_code,
            "summary": summary,
        }
        if "command_hash_note" in norm:
            entry["command_hash_note"] = norm["command_hash_note"]
        per_ac.append(entry)
        if rerun_required:
            continue

    if not per_ac:
        return _result(
            overall_status="indeterminate",
            per_ac=[],
            rerun_required=True,
            source_integrity=source_integrity,
            evidence_refs=evidence_refs,
            errors=["empty_current_results_without_pass_signal"],
        )

    highest = max(
        (entry["status"] for entry in per_ac),
        key=lambda status: STATUS_PRIORITY.get(status, STATUS_PRIORITY["indeterminate"]),
    )
    rerun_required = any(entry["status"] in {"indeterminate", "environment_blocked"} for entry in per_ac)

    if not source_integrity["evidence_complete"] and highest in {"pre_existing_fail", "out_of_scope_fail"}:
        highest = "indeterminate"
        for entry in per_ac:
            if entry["status"] in {"pre_existing_fail", "out_of_scope_fail"}:
                entry["status"] = "indeterminate"
                entry["blocking"] = True
                entry["reason_code"] = "incomplete_evidence"
                entry["summary"] = "Status downgraded due to missing evidence"
        rerun_required = True

    return _result(
        overall_status=highest,
        per_ac=per_ac,
        rerun_required=rerun_required,
        source_integrity=source_integrity,
        evidence_refs=evidence_refs,
        errors=[],
    )


def _compact_payload(payload: dict[str, Any], *, truncated: bool, omitted_fields: list[str]) -> dict[str, Any]:
    compact = dict(payload)
    compact["stdout_truncated"] = truncated
    compact["omitted_fields"] = omitted_fields
    return compact


def _compact_output(payload: dict[str, Any], max_stdout_bytes: int) -> str:
    compact = _canonical_json(_compact_payload(payload, truncated=False, omitted_fields=[]))
    if len(compact.encode("utf-8")) <= max_stdout_bytes:
        return compact

    trimmed = _compact_payload(payload, truncated=True, omitted_fields=["per_ac.summary", "evidence_refs"])
    trimmed["per_ac"] = [
        {key: value for key, value in entry.items() if key != "summary"}
        for entry in payload["per_ac"]
    ]
    trimmed["evidence_refs"] = []
    compact = _canonical_json(trimmed)
    if len(compact.encode("utf-8")) <= max_stdout_bytes:
        return compact

    fail_closed = _compact_payload(
        _result(
            overall_status="indeterminate",
            per_ac=[],
            rerun_required=True,
            source_integrity=payload["source_integrity"],
            evidence_refs=[],
            artifact_ref=payload.get("artifact_ref"),
            artifact_digest=payload.get("artifact_digest"),
            errors=["stdout_budget_exceeded"],
            stdout_truncated=True,
            omitted_fields=["per_ac", "evidence_refs"],
        ),
        truncated=True,
        omitted_fields=["per_ac", "evidence_refs"],
    )
    return _canonical_json(fail_closed)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adjudicate VC result against baseline")
    parser.add_argument("--contract-snapshot-file", required=True)
    parser.add_argument("--current-vc-result-file", required=True)
    parser.add_argument("--diff-summary-file")
    parser.add_argument("--allowed-paths-file")
    parser.add_argument("--artifact-out")
    parser.add_argument("--max-stdout-bytes", type=int, default=4096)
    return parser.parse_args(argv)


def _load_allowed_paths(path: str | None) -> tuple[list[str] | None, list[str]]:
    if path is None:
        return None, ["missing_allowed_paths_file"]
    raw, errors = _load_json_file(path)
    if errors:
        return None, errors
    if not isinstance(raw, list):
        return None, ["allowed_paths_not_list"]
    return list(raw), []


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    contract_snapshot, contract_errors = _load_json_file(args.contract_snapshot_file)
    current_vc_result, current_errors = _load_json_file(args.current_vc_result_file)
    diff_summary, diff_errors = _load_json_file(args.diff_summary_file) if args.diff_summary_file else (None, [])
    diff_errors = diff_errors or []
    allowed_paths, allowed_errors = _load_allowed_paths(args.allowed_paths_file)

    result = adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
    )
    result["errors"].extend(contract_errors or [])
    result["errors"].extend(current_errors or [])
    result["errors"].extend(diff_errors or [])
    result["errors"].extend(allowed_errors or [])
    if result["errors"]:
        result["overall_status"] = "indeterminate"
        result["blocking"] = True
        result["rerun_required"] = True

    if args.artifact_out:
        bundle = {
            "schema": PRIVATE_BUNDLE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "contract_snapshot": contract_snapshot,
            "current_vc_result": current_vc_result,
            "diff_summary": diff_summary,
            "allowed_paths": allowed_paths,
            "artifact_inputs": {
                "contract_snapshot_file": args.contract_snapshot_file,
                "current_vc_result_file": args.current_vc_result_file,
                "diff_summary_file": args.diff_summary_file,
                "allowed_paths_file": args.allowed_paths_file,
            },
            "result": result,
        }
        bundle_text = _canonical_json(bundle)
        Path(args.artifact_out).write_text(bundle_text, encoding="utf-8")
        result["artifact_ref"] = PRIVATE_ARTIFACT_REF
        result["artifact_digest"] = _sha256(bundle_text)

    sys.stdout.write(_compact_output(result, args.max_stdout_bytes) + "\n")
    return 0 if not result["blocking"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

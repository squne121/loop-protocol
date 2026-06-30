#!/usr/bin/env python3
"""Classify VC failures into VC_ADJUDICATION_RESULT_V1.

The classifier compares:
- contract_snapshot: baseline VC evidence
- current_vc_result: latest VC evidence
- diff_summary: changed paths for failure-scope checks
- allowed_paths: issue Allowed Paths

Output is compact public-safe JSON with raw command output excluded.
Raw payloads are written only to optional private artifact references.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


SCHEMA_NAME = "VC_ADJUDICATION_RESULT_V1"
SCHEMA_VERSION = 1
KNOWN_STATUS = {
    "pass",
    "pre_existing_fail",
    "out_of_scope_fail",
    "regression_fail",
    "environment_blocked",
    "indeterminate",
}
STATUS_PRIORITY = {
    "pass": 0,
    "pre_existing_fail": 1,
    "out_of_scope_fail": 2,
    "regression_fail": 3,
    "environment_blocked": 4,
    "indeterminate": 5,
}


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_command(command: str) -> str:
    return _sha256(command)


def _is_valid_command_hash(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71


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


def _normalize_failure_keys(value: Any) -> tuple[list[str], bool]:
    if value is None:
        return [], False
    if isinstance(value, str):
        return [value], True
    if not isinstance(value, list):
        return [], False

    normalized: list[str] = []
    for item in value:
        if isinstance(item, str):
            normalized.append(item)
            continue
        if isinstance(item, dict):
            key = item.get("key")
            if isinstance(key, str):
                normalized.append(key)

    return normalized, bool(normalized)


def _normalize_item(item: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(item, dict):
        return None, ["non_object_item"]

    ac = item.get("ac")
    if not isinstance(ac, str) or not ac:
        return None, ["missing_or_invalid_ac"]

    command_hash = item.get("command_hash")
    raw_command = item.get("raw_command")
    if not _is_valid_command_hash(command_hash):
        if isinstance(raw_command, str):
            command_hash = _sha256_command(raw_command)
        else:
            return None, ["missing_or_invalid_command_hash"]

    exit_code = item.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        return None, ["invalid_exit_code"]

    category = item.get("category")
    if category is not None and not isinstance(category, str):
        return None, ["invalid_category"]

    failure_keys, failure_keys_present = _normalize_failure_keys(item.get("failure_keys"))

    return {
        "ac": ac,
        "command_hash": command_hash,
        "exit_code": exit_code,
        "category": category,
        "failure_keys": failure_keys,
        "failure_keys_present": failure_keys_present,
    }, []


def _load_path_list(raw: Any) -> tuple[list[str], list[str]]:
    if raw is None:
        return [], ["missing_path_input"]
    if not isinstance(raw, list):
        return [], ["invalid_path_input"]
    values: list[str] = []
    for item in raw:
        if isinstance(item, str):
            values.append(item)
    return values, []


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
        values = []
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


def _failure_key_root(key: str) -> str:
    root = key.strip()
    if "::" in root:
        root = root.split("::", 1)[0]
    if ":" in root and "::" not in root:
        root = root.split(":", 1)[0]
    return root


def _key_relates_to_path(key: str, path: str) -> bool:
    root = _failure_key_root(key)
    path = _normalize_path(path)
    return root == path or root.startswith(path + "/")


def _is_related_to_scope(failure_keys: list[str], changed_paths: list[str], allowed_paths: list[str]) -> bool:
    scope = [*_normalize_scope_paths(changed_paths), *_normalize_scope_paths(allowed_paths)]
    for key in failure_keys:
        for path in scope:
            if _key_relates_to_path(key, path):
                return True
    return False


def _normalize_scope_paths(paths: list[str]) -> list[str]:
    return [_normalize_path(path) for path in paths if isinstance(path, str) and path.strip()]


def _classify_item(
    item: dict[str, Any],
    baseline_signatures: set[tuple[str, tuple[str, ...]]],
    changed_paths: list[str],
    allowed_paths: list[str],
    evidence_complete: bool,
) -> tuple[str, bool, bool, str, str]:
    command_hash = item["command_hash"]
    failure_keys = item["failure_keys"]
    has_keys = item["failure_keys_present"]

    if item.get("exit_code") == 5 or item.get("category") == "vc_no_tests_collected":
        return "indeterminate", True, True, "pytest_exit_5", "Pytest exit code 5 is not treated as regression"

    if item.get("category") in {"runtime_dependency_error", "package_manager_no_tty_prompt", "timeout"}:
        return "environment_blocked", True, True, "environment_signal", "Environment/tooling blocker detected"

    baseline_key = (command_hash, tuple(failure_keys))
    has_diff = bool(changed_paths)
    has_allowed = bool(allowed_paths)

    if baseline_key in baseline_signatures:
        if has_keys and evidence_complete and not has_diff:
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

    if not has_keys:
        return "indeterminate", True, True, "missing_failure_keys", "Failure key evidence is missing"

    if not has_diff or not has_allowed or not changed_paths:
        return (
            "indeterminate",
            True,
            True,
            "insufficient_scope_evidence",
            "Diff scope evidence is incomplete for adjudication",
        )

    if _is_related_to_scope(failure_keys, changed_paths, allowed_paths):
        return (
            "regression_fail",
            True,
            False,
            "related_to_changed_scope",
            "Failure is related to changed and/or allowed scope",
        )

    return "out_of_scope_fail", False, False, "unrelated_to_scope", "Failure is unrelated to changed and allowed paths"


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
    }


def _build_evidence_refs(
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: dict[str, Any] | None,
    allowed_paths: list[str] | None,
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    refs.append(
        {
            "kind": "contract_snapshot",
            "ref": _sha256(json.dumps(contract_snapshot, sort_keys=True, ensure_ascii=False)),
        }
    )
    refs.append(
        {
            "kind": "current_vc_result",
            "ref": _sha256(json.dumps(current_vc_result, sort_keys=True, ensure_ascii=False)),
        }
    )
    if diff_summary is not None:
        refs.append(
            {
                "kind": "diff_summary",
                "ref": _sha256(json.dumps(diff_summary, sort_keys=True, ensure_ascii=False)),
            }
        )
    if allowed_paths is not None:
        refs.append(
            {
                "kind": "allowed_paths",
                "ref": _sha256(json.dumps(allowed_paths, sort_keys=True, ensure_ascii=False)),
            }
        )
    return refs


def adjudicate_vc_result(
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: dict[str, Any] | None,
    allowed_paths: list[str] | None,
    artifact_out: str | None = None,
) -> dict[str, Any]:
    baseline_items, baseline_errors, baseline_schema = _normalize_list_payload(contract_snapshot)
    current_items, current_errors, _ = _normalize_list_payload(current_vc_result)

    changed_paths, has_changed_paths, diff_errors = _extract_changed_paths(diff_summary)
    normalized_allowed, allowed_paths_errors = _load_path_list(allowed_paths)

    extraction_errors = list(baseline_errors + current_errors + diff_errors + allowed_paths_errors)
    source_integrity = {
        "contract_snapshot_present": contract_snapshot is not None,
        "current_vc_result_present": current_vc_result is not None,
        "diff_summary_present": diff_summary is not None,
        "allowed_paths_present": allowed_paths is not None,
        "baseline_schema": baseline_schema,
        "current_items_count": len(current_items),
        "baseline_items_count": len(baseline_items),
        "changed_paths_present": bool(changed_paths),
        "evidence_complete": (
            contract_snapshot is not None
            and current_vc_result is not None
            and diff_summary is not None
            and allowed_paths is not None
        ),
    }

    evidence_refs = _build_evidence_refs(contract_snapshot, current_vc_result, diff_summary, allowed_paths)

    if extraction_errors:
        return _result(
            overall_status="indeterminate",
            rerun_required=True,
            per_ac=[],
            source_integrity=source_integrity,
            evidence_refs=evidence_refs,
            errors=extraction_errors,
        )

    baseline_signatures: set[tuple[str, tuple[str, ...]]] = set()
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
        baseline_signatures.add((norm["command_hash"], tuple(norm["failure_keys"])) )

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
            changed_paths=changed_paths,
            allowed_paths=normalized_allowed,
            evidence_complete=source_integrity["evidence_complete"],
        )

        per_ac.append(
            {
                "ac": norm["ac"],
                "status": status,
                "blocking": blocking,
                "command_hash": norm["command_hash"],
                "failure_keys": norm["failure_keys"],
                "reason_code": reason_code,
                "summary": summary,
            }
        )
        if not rerun_required:
            continue
        # rerun_required is aggregated by status below

    if not per_ac:
        return _result(
            overall_status="pass",
            per_ac=[],
            rerun_required=False,
            source_integrity=source_integrity,
            evidence_refs=evidence_refs,
        )

    statuses = [entry["status"] for entry in per_ac]
    highest = max(statuses, key=lambda s: STATUS_PRIORITY.get(s, STATUS_PRIORITY["indeterminate"]))
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

    blocking = any(entry["blocking"] for entry in per_ac)

    return _result(
        overall_status=highest,
        per_ac=per_ac,
        rerun_required=rerun_required,
        source_integrity=source_integrity,
        evidence_refs=evidence_refs,
        errors=[],
    )


def _compact_output(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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
    return list(raw) if isinstance(raw, list) else None, []


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    contract_snapshot, contract_errors = _load_json_file(args.contract_snapshot_file)
    current_vc_result, current_errors = _load_json_file(args.current_vc_result_file)
    diff_summary, diff_errors = _load_json_file(args.diff_summary_file) if args.diff_summary_file else (None, [])
    if isinstance(diff_errors, list) and not diff_errors:
        pass
    else:
        diff_errors = diff_errors or []

    allowed_paths, allowed_errors = _load_allowed_paths(args.allowed_paths_file)

    result = adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        artifact_out=args.artifact_out,
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
            "schema": "VC_ADJUDICATION_PRIVATE_BUNDLE_V1",
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
        Path(args.artifact_out).write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        artifact_ref = str(Path(args.artifact_out))
        result["artifact_ref"] = artifact_ref
        result["artifact_digest"] = _sha256(json.dumps(bundle, ensure_ascii=False))

    compact = _compact_output(result)
    if len(compact) > args.max_stdout_bytes:
        compact = compact[: args.max_stdout_bytes - 3] + "..."
    sys.stdout.write(compact + "\n")

    return 0 if not result["blocking"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

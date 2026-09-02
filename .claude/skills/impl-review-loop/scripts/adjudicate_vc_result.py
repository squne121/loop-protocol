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
TEST_VERDICT_SCHEMA = "TEST_VERDICT_MACHINE/v2"
CURRENT_VC_RESULT_SCHEMA = "baseline_vc_preflight/v1"
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

# Issue #1648: receipt-aware verification of Child A
# (TEST_VERDICT_PRODUCER_RECEIPT_V1) provenance embedded in a
# TEST_VERDICT_MACHINE/v2 input. Only consulted when the caller opts in via
# require_producer_receipt=True (adjudicate_vc_result()) /
# --require-producer-receipt (CLI) -- the legacy self-attested TEST_VERDICT
# path (no producer_receipt field checked) is left unchanged so existing
# non-regression fixtures keep passing (Issue #1648 body).
_PRODUCER_RECEIPT_SCHEMA_PATH = _REPO_ROOT / "schemas" / "test-verdict-producer-receipt.schema.json"
_PRODUCER_RECEIPT_VALIDATOR = None


def _get_producer_receipt_validator():
    global _PRODUCER_RECEIPT_VALIDATOR
    if _PRODUCER_RECEIPT_VALIDATOR is not None:
        return _PRODUCER_RECEIPT_VALIDATOR, None
    try:
        from jsonschema import Draft202012Validator
    except Exception as exc:  # pragma: no cover
        return None, f"jsonschema_unavailable:{type(exc).__name__}"
    try:
        schema = json.loads(_PRODUCER_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"producer_receipt_schema_unreadable:{type(exc).__name__}"
    _PRODUCER_RECEIPT_VALIDATOR = Draft202012Validator(schema)
    return _PRODUCER_RECEIPT_VALIDATOR, None


# --- Issue #88 fix_delta Blocker 1: canonical adapter ----------------------
#
# adjudicate_vc_result() accepts a current_vc_result payload in the
# "baseline_vc_preflight/v1" schema, but the read-only report returned by
# the test-runner SubAgent uses a different shape (TEST_VERDICT_MACHINE/v2,
# see .claude/agents/test-runner.md "TEST_VERDICT 報告フォーマット"). Without
# this adapter the two schemas were never actually wired together in the
# live orchestration path, so evaluate_step4_vc_gate() was unreachable from
# a real test-runner report. This performs a structural, non-judgmental
# conversion only: it does not re-classify PASS/FAIL/SKIP and does not
# invent failure_keys. adjudicate_vc_result() remains the sole place VC
# failures are classified against the baseline contract snapshot.
#
# Known scope limitation: TEST_VERDICT.runtime_ac_results[] does not carry
# structured failure_keys (only free-text notes), so the adapted
# results[].failure_keys is always []. This does not affect PASS
# adjudication (Issue #88's primary concern).

_ADAPT_RESULT_STATUS_MAP = {"PASS": "pass", "FAIL": "fail", "PARTIAL": "partial"}


def adapt_test_verdict_to_current_vc_result(test_verdict: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Convert a TEST_VERDICT_MACHINE/v2 payload into a
    "baseline_vc_preflight/v1" payload suitable for adjudicate_vc_result()'s
    current_vc_result argument (or --current-vc-result-file). Returns
    (converted_payload, errors); converted_payload is None only when the
    input cannot be interpreted as TEST_VERDICT_MACHINE/v2 at all.
    """
    payload = test_verdict
    if not isinstance(payload, dict):
        return None, ["test_verdict_not_object"]
    if isinstance(payload.get("TEST_VERDICT"), dict):
        payload = payload["TEST_VERDICT"]
    if payload.get("schema") != TEST_VERDICT_SCHEMA:
        return None, [f"unsupported_source_schema:{payload.get('schema')!r}"]

    errors: list[str] = []
    head_sha = payload.get("head_sha")
    reviewed_head_sha = payload.get("reviewed_head_sha") or head_sha
    contract_body_sha256 = payload.get("contract_body_sha256")
    if not isinstance(head_sha, str) or not head_sha:
        errors.append("missing_head_sha")
    if not isinstance(contract_body_sha256, str) or not contract_body_sha256:
        errors.append("missing_contract_body_sha256")

    runtime_ac_results = payload.get("runtime_ac_results")
    if not isinstance(runtime_ac_results, list):
        errors.append("missing_runtime_ac_results")
        runtime_ac_results = []

    results: list[dict[str, Any]] = []
    any_fallback = False
    any_human_review = bool(payload.get("human_review_required"))
    any_stop_condition = False
    for idx, item in enumerate(runtime_ac_results):
        if not isinstance(item, dict):
            errors.append(f"runtime_ac_results[{idx}]:not_object")
            continue
        ac = item.get("ac")
        command_hash = item.get("command_hash")
        if not isinstance(ac, str) or not ac:
            errors.append(f"runtime_ac_results[{idx}]:missing_ac")
            continue
        if not isinstance(command_hash, str) or not command_hash:
            errors.append(f"runtime_ac_results[{idx}]:missing_command_hash")
            continue
        fallback_detected = bool(item.get("fallback_detected"))
        human_review_required = bool(item.get("human_review_required"))
        stop_condition_triggered = bool(item.get("stop_condition_triggered"))
        any_fallback = any_fallback or fallback_detected
        any_human_review = any_human_review or human_review_required
        any_stop_condition = any_stop_condition or stop_condition_triggered
        results.append(
            {
                "ac": ac,
                "command_hash": command_hash,
                "raw_command": item.get("command"),
                "exit_code": item.get("exit_code"),
                "failure_keys": [],
                "raw_stdout": "",
                "raw_stderr": item.get("notes") or "",
            }
        )

    converted = {
        "schema": CURRENT_VC_RESULT_SCHEMA,
        "generated_at": payload.get("generated_at"),
        "status": _ADAPT_RESULT_STATUS_MAP.get(payload.get("result"), "indeterminate"),
        "errors": [],
        "fallback_detected": any_fallback,
        "human_review_required": any_human_review,
        "stop_condition_triggered": any_stop_condition,
        "source": {"body_sha256": contract_body_sha256},
        "results": results,
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
    }
    return converted, errors
# --- end Issue #88 fix_delta Blocker 1 canonical adapter --------------------


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
    if isinstance(exit_code, bool) or (exit_code is not None and not isinstance(exit_code, int)):
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
        "classification": item.get("classification"),
        "decision": item.get("decision"),
        "scope_class": item.get("scope_class"),
        "runner": item.get("runner"),
        "verification_owner": item.get("verification_owner"),
        "deferred_reason": item.get("deferred_reason"),
        "runtime_verification_required": item.get("runtime_verification_required"),
    }
    if command_hash_note is not None:
        normalized["command_hash_note"] = command_hash_note
    return normalized, []


def _is_producer_authorized_pr_review_only_skip(item: dict[str, Any]) -> bool:
    """Recognize only a complete PR-review-only skip produced by the VC producer."""
    return (
        item.get("runner") == "skipped"
        and item.get("scope_class") == "pr_review_only"
        and item.get("classification") == "skipped"
        and item.get("decision") == "go"
        and item.get("category") == "preflight_scope_pr_review_only"
        and item.get("verification_owner") == "pr-review-judge"
        and isinstance(item.get("deferred_reason"), str)
        and bool(item["deferred_reason"])
        and item.get("runtime_verification_required") is False
    )


# Issue #2467: exact canonical runtime_only producer-skip envelope. This is
# a distinct fail-closed recognizer from _is_producer_authorized_pr_review_only_skip
# above -- runtime_only is NOT an extension of the pr_review_only authorized
# scope (Issue #2467 Out of Scope / #1540 precedent). Only the literal
# envelope emitted by baseline_vc_preflight.py for a `# preflight-scope:
# runtime_only` marker is recognized here; this module never parses the raw
# marker text itself (that remains the producer's responsibility).
def _is_producer_authorized_runtime_only_skip(item: dict[str, Any]) -> bool:
    """Recognize only a complete runtime_only skip produced by the VC producer."""
    return (
        item.get("runner") == "skipped"
        and item.get("scope_class") == "runtime_only"
        and item.get("classification") == "skipped"
        and item.get("decision") == "go"
        and item.get("category") == "preflight_scope_runtime_only"
        and item.get("verification_owner") == "impl-review-loop"
        and isinstance(item.get("deferred_reason"), str)
        and bool(item["deferred_reason"])
        and item.get("runtime_verification_required") is True
    )


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
        unrecognized_records: list[str] = []
        for item in raw_paths:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                matched = False
                for key in (
                    "path",
                    "file",
                    "filename",
                    "previous_path",
                    "previous_filename",
                    "old_path",
                ):
                    value = item.get(key)
                    if isinstance(value, str):
                        values.append(value)
                        matched = True
                if not matched and item:
                    unrecognized_records.append("unrecognized_changed_path_record")
        if unrecognized_records:
            return values, bool(values), unrecognized_records
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


def _all_changed_paths_allowed(changed_paths: list[str], allowed_paths: list[str]) -> bool:
    matcher, error = _get_allowed_paths_matcher()
    if matcher is None or error is not None:
        return False
    for path in changed_paths:
        normalized_path = matcher.normalize_path(path)
        if normalized_path is None or not any(
            matcher.matches_pattern(normalized_path, pattern) for pattern in allowed_paths
        ):
            return False
    return True


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _test_verdict_binding_error(
    test_verdict: Any,
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    expected_keys: set[tuple[str, str]],
    require_producer_receipt: bool = False,
) -> str | None:
    """Return a fail-closed reason unless runtime execution evidence is bound."""
    if not isinstance(test_verdict, dict):
        return "test_verdict_missing"
    if not isinstance(contract_snapshot, dict) or not isinstance(current_vc_result, dict):
        return "test_verdict_binding_context_invalid"
    if not isinstance(diff_summary, dict):
        return "test_verdict_diff_context_invalid"

    expected_issue = current_vc_result.get("issue")
    expected_pr = diff_summary.get("pr_number")
    expected_head = current_vc_result.get("head_sha")
    expected_reviewed_head = current_vc_result.get("reviewed_head_sha")
    expected_diff_head = diff_summary.get("head_sha")
    expected_contract_sha = contract_snapshot.get("body_sha256")
    required_bindings = {
        "issue_number": expected_issue,
        "pr_number": expected_pr,
        "head_sha": expected_head,
        "reviewed_head_sha": expected_reviewed_head,
        "diff_head_sha": expected_diff_head,
        "contract_body_sha256": expected_contract_sha,
    }
    if test_verdict.get("schema") != TEST_VERDICT_SCHEMA:
        return "test_verdict_schema_mismatch"
    for key, expected in required_bindings.items():
        if expected is None or test_verdict.get(key) != expected:
            return f"test_verdict_{key}_mismatch"
    if not _is_nonempty_string(test_verdict.get("run_id")):
        return "test_verdict_run_id_missing"
    run_url = test_verdict.get("run_url")
    if not isinstance(run_url, str) or not run_url.startswith("https://"):
        return "test_verdict_run_url_invalid"
    if test_verdict.get("result") != "PASS":
        return "test_verdict_result_not_pass"
    if test_verdict.get("verification_commands_fail") != 0:
        return "test_verdict_fail_count_nonzero"
    if test_verdict.get("verification_skipped_count") != 0:
        return "test_verdict_skip_count_nonzero"

    # A v2 verdict is only usable when it identifies the producer and the
    # GitHub Actions artifact that was read back.  The artifact payload is
    # bound to the digest recorded by that readback, so a copied or partial
    # summary cannot stand in for current-head execution evidence.
    if test_verdict.get("producer_kind") != "test-runner":
        return "test_verdict_producer_kind_mismatch"
    if not _is_nonempty_string(test_verdict.get("repository")):
        return "test_verdict_repository_missing"
    for key in ("workflow_run_id", "workflow_run_attempt", "check_run_id"):
        value = test_verdict.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return f"test_verdict_{key}_invalid"
    artifact = test_verdict.get("artifact")
    if not isinstance(artifact, dict):
        return "test_verdict_artifact_missing"
    if not _is_nonempty_string(artifact.get("name")):
        return "test_verdict_artifact_name_missing"
    artifact_digest = artifact.get("artifact_digest")
    if (
        not isinstance(artifact_digest, str)
        or not artifact_digest.startswith("sha256:")
        or not _is_hex_64(artifact_digest[7:])
    ):
        return "test_verdict_artifact_digest_invalid"
    artifact_url = artifact.get("url")
    if not isinstance(artifact_url, str) or not artifact_url.startswith("https://github.com/"):
        return "test_verdict_artifact_url_invalid"
    artifact_payload = test_verdict.get("artifact_payload")
    if not isinstance(artifact_payload, dict):
        return "test_verdict_artifact_payload_missing"
    artifact_payload_sha256 = test_verdict.get("artifact_payload_sha256")
    if (
        not isinstance(artifact_payload_sha256, str)
        or not artifact_payload_sha256.startswith("sha256:")
        or not _is_hex_64(artifact_payload_sha256[7:])
    ):
        return "test_verdict_artifact_payload_sha256_invalid"
    if _sha256(_canonical_json(artifact_payload)) != artifact_payload_sha256:
        return "test_verdict_artifact_digest_mismatch"
    for key, expected in required_bindings.items():
        if artifact_payload.get(key) != expected:
            return f"test_verdict_artifact_{key}_mismatch"
    if artifact_payload.get("command_hashes") != sorted(command_hash for _, command_hash in expected_keys):
        return "test_verdict_artifact_command_hashes_mismatch"

    raw_results = test_verdict.get("runtime_ac_results")
    if not isinstance(raw_results, list):
        return "test_verdict_runtime_ac_results_missing"
    observed_keys: set[tuple[str, str]] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            return "test_verdict_runtime_ac_result_invalid"
        ac = item.get("ac")
        command_hash = item.get("command_hash")
        if not isinstance(ac, str) or not isinstance(command_hash, str):
            return "test_verdict_runtime_ac_identity_missing"
        key = (ac, command_hash)
        if key in observed_keys:
            return f"test_verdict_runtime_ac_duplicate:{ac}"
        observed_keys.add(key)
        if (
            item.get("status") != "pass"
            or item.get("exit_code") != 0
            or item.get("fallback_detected") is not False
            or item.get("human_review_required") is not False
            or item.get("stop_condition_triggered") is not False
        ):
            return f"test_verdict_runtime_ac_not_executed_pass:{ac}"
    if observed_keys != expected_keys:
        return "test_verdict_runtime_ac_coverage_mismatch"

    if require_producer_receipt:
        receipt = test_verdict.get("producer_receipt")
        if not isinstance(receipt, dict):
            return "test_verdict_producer_receipt_missing"
        validator, validator_error = _get_producer_receipt_validator()
        if validator is None:
            return f"test_verdict_producer_receipt_validator_unavailable:{validator_error}"
        if list(validator.iter_errors(receipt)):
            return "test_verdict_producer_receipt_schema_invalid"
        receipt_sha256 = test_verdict.get("receipt_sha256")
        if receipt_sha256 != _sha256(_canonical_json(receipt)):
            return "test_verdict_receipt_sha256_mismatch"
        if receipt.get("pass_eligible") is not True:
            return "test_verdict_receipt_not_pass_eligible"
        receipt_subject = receipt.get("subject")
        if not isinstance(receipt_subject, dict) or receipt_subject.get("pr_head_sha") != test_verdict.get("head_sha"):
            return "test_verdict_receipt_subject_head_sha_mismatch"
        if receipt_subject.get("target_pr_number") != test_verdict.get("pr_number"):
            return "test_verdict_receipt_subject_pr_number_mismatch"
        receipt_contract = receipt.get("contract")
        if not isinstance(receipt_contract, dict) or receipt_contract.get("linked_issue_number") != test_verdict.get(
            "issue_number"
        ):
            return "test_verdict_receipt_contract_issue_number_mismatch"
        if receipt_contract.get("issue_body_sha256") != test_verdict.get("contract_body_sha256"):
            return "test_verdict_receipt_contract_body_sha256_mismatch"
        receipt_artifact = receipt.get("execution_artifact")
        if not isinstance(receipt_artifact, dict) or receipt_artifact.get("artifact_archive_digest") != artifact_digest:
            return "test_verdict_receipt_artifact_digest_mismatch"

    return None


# Issue #2467 AC2/AC3: current-head independent binding for a
# runtime_only producer skip. Unlike _test_verdict_binding_error() above,
# this does NOT require a GitHub Actions TEST_VERDICT/artifact readback --
# per Issue #2467 In Scope, artifact / receipt / materialized TEST_VERDICT is
# optional diagnostic provenance for runtime_only, not a mandatory input.
# The binding instead uses the orchestrator's own current-head evidence
# (contract_snapshot / current_vc_result / diff_summary), which is exactly
# what the orchestrator independently binds to the current head before
# calling adjudicate_vc_result() (Issue #2467 In Scope: "current routing
# authority は ... orchestrator が独立に current-head へ binding して生成する
# VC_ADJUDICATION_RESULT_V1"). A baseline skip declaration alone is
# insufficient -- every one of the checks below must hold, fail-closed.
def _runtime_only_current_head_binding_error(
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    changed_paths: list[str],
    changed_paths_present: bool,
    allowed_paths: list[str],
) -> str | None:
    """Return a fail-closed reason unless runtime_only current-head
    independent binding (Issue / PR / current head / reviewed head / diff
    head / Issue body digest / source integrity) is fully satisfied."""
    if not isinstance(contract_snapshot, dict) or not isinstance(current_vc_result, dict):
        return "runtime_only_binding_context_invalid"
    if not isinstance(diff_summary, dict):
        return "runtime_only_diff_context_invalid"

    if contract_snapshot.get("status") != "go":
        return "runtime_only_contract_not_go"
    contract_sha = contract_snapshot.get("body_sha256")
    if not _is_nonempty_string(contract_sha):
        return "runtime_only_contract_body_sha256_missing"

    if not _is_nonempty_string(current_vc_result.get("generated_at")):
        return "runtime_only_generated_at_missing"
    if current_vc_result.get("status") != "pass":
        return "runtime_only_current_vc_result_not_pass"
    if current_vc_result.get("errors") != []:
        return "runtime_only_current_vc_result_errors_present"
    if current_vc_result.get("fallback_detected") is not False:
        return "runtime_only_fallback_detected"
    if current_vc_result.get("human_review_required") is not False:
        return "runtime_only_human_review_required"
    if current_vc_result.get("stop_condition_triggered") is not False:
        return "runtime_only_stop_condition_triggered"

    current_head = current_vc_result.get("head_sha")
    reviewed_head = current_vc_result.get("reviewed_head_sha")
    diff_head = diff_summary.get("head_sha")
    if (
        not _is_nonempty_string(current_head)
        or current_head != reviewed_head
        or current_head != diff_head
    ):
        return "runtime_only_head_binding_mismatch"

    source = current_vc_result.get("source")
    if not isinstance(source, dict) or source.get("body_sha256") != contract_sha:
        return "runtime_only_source_body_sha256_mismatch"

    issue_number = current_vc_result.get("issue")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
        return "runtime_only_issue_number_missing"

    pr_number = diff_summary.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return "runtime_only_pr_number_missing"

    if changed_paths_present is not True:
        return "runtime_only_changed_paths_missing"
    if not _all_changed_paths_allowed(changed_paths, allowed_paths):
        return "runtime_only_changed_paths_not_certified"

    return None


def _current_pass_envelope_is_certified(
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    changed_paths: list[str],
    changed_paths_present: bool,
    allowed_paths: list[str],
) -> bool:
    if not isinstance(contract_snapshot, dict) or not isinstance(current_vc_result, dict):
        return False
    if not isinstance(diff_summary, dict):
        return False
    contract_sha = contract_snapshot.get("body_sha256")
    source = current_vc_result.get("source")
    if not isinstance(source, dict):
        return False
    current_head = current_vc_result.get("head_sha")
    reviewed_head = current_vc_result.get("reviewed_head_sha")
    diff_head = diff_summary.get("head_sha")
    return (
        contract_snapshot.get("status") == "go"
        and _is_nonempty_string(contract_sha)
        and _is_nonempty_string(current_vc_result.get("generated_at"))
        and current_vc_result.get("status") == "pass"
        and current_vc_result.get("errors") == []
        and current_vc_result.get("fallback_detected") is False
        and current_vc_result.get("human_review_required") is False
        and current_vc_result.get("stop_condition_triggered") is False
        and _is_nonempty_string(current_head)
        and current_head == reviewed_head == diff_head
        and source.get("body_sha256") == contract_sha
        and changed_paths_present is True
        and _all_changed_paths_allowed(changed_paths, allowed_paths)
    )


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
    test_verdict: Any | None,
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
    if test_verdict is not None:
        refs.append(
            {
                "kind": "test_verdict",
                "ref": "inline:test_verdict",
                "digest": _sha256(_canonical_json(test_verdict)),
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
    result_errors = errors or []
    if overall_status == "pass" and not per_ac:
        overall_status = "indeterminate"
        rerun_required = True
        result_errors = [*result_errors, "pass_requires_per_ac_coverage"]
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
        "errors": result_errors,
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
    test_verdict: Any | None = None,
    require_producer_receipt: bool = False,
) -> dict[str, Any]:
    # Issue #1648 fix_delta AC9 (P1-3): a caller that forgets to pass
    # --require-producer-receipt must not silently accept a materialized
    # TEST_VERDICT_MACHINE/v2 bundle (which always embeds a producer_receipt)
    # as if it were self-attested. If the test_verdict itself carries a
    # producer_receipt field, receipt verification is forced on regardless
    # of the caller-supplied flag. Legacy self-attested TEST_VERDICT input
    # (no producer_receipt field at all) is unaffected -- non-regression.
    effective_require_producer_receipt = require_producer_receipt or (
        isinstance(test_verdict, dict) and "producer_receipt" in test_verdict
    )
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
        test_verdict,
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
    baseline_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    excluded_pr_review_only_keys: set[tuple[str, str]] = set()
    excluded_runtime_only_keys: set[tuple[str, str]] = set()
    seen_baseline_keys: set[tuple[str, str]] = set()
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
        mapping_key = (norm["ac"], norm["command_hash"])
        if mapping_key in seen_baseline_keys:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[f"duplicate_baseline_ac_command_hash:{norm['ac']}"],
            )
        seen_baseline_keys.add(mapping_key)
        if _is_producer_authorized_pr_review_only_skip(norm):
            excluded_pr_review_only_keys.add(mapping_key)
            continue
        if _is_producer_authorized_runtime_only_skip(norm):
            excluded_runtime_only_keys.add(mapping_key)
            continue
        if norm["classification"] not in {"expected_fail", "expected_pass"}:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[f"unsupported_baseline_classification:{norm['ac']}"],
            )
        baseline_by_key[mapping_key] = norm
        baseline_signatures.add(
            (
                norm["command_hash"],
                tuple((entry["kind"], entry["key"]) for entry in norm["failure_keys"]),
            )
        )
        baseline_failure_index.update((entry["kind"], entry["key"]) for entry in norm["failure_keys"])

    normalized_current: list[dict[str, Any]] = []
    current_keys: set[tuple[str, str]] = set()
    seen_current_keys: set[tuple[str, str]] = set()
    excluded_current_count = 0
    excluded_current_keys: set[tuple[str, str]] = set()
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

        mapping_key = (norm["ac"], norm["command_hash"])
        if mapping_key in seen_current_keys:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[f"duplicate_current_ac_command_hash:{norm['ac']}"],
            )
        seen_current_keys.add(mapping_key)
        if mapping_key in excluded_pr_review_only_keys:
            if not _is_producer_authorized_pr_review_only_skip(norm):
                return _result(
                    overall_status="indeterminate", per_ac=[], rerun_required=True,
                    source_integrity=source_integrity, evidence_refs=evidence_refs,
                    errors=[f"pr_review_only_current_authorization_mismatch:{norm['ac']}"],
                )
            excluded_current_count += 1
            excluded_current_keys.add(mapping_key)
            continue
        if mapping_key in excluded_runtime_only_keys:
            if not _is_producer_authorized_runtime_only_skip(norm):
                return _result(
                    overall_status="indeterminate", per_ac=[], rerun_required=True,
                    source_integrity=source_integrity, evidence_refs=evidence_refs,
                    errors=[f"runtime_only_current_authorization_mismatch:{norm['ac']}"],
                )
            excluded_current_count += 1
            excluded_current_keys.add(mapping_key)
            continue
        current_keys.add(mapping_key)
        normalized_current.append(norm)

    excluded_skip_keys = excluded_pr_review_only_keys | excluded_runtime_only_keys
    if excluded_current_keys != excluded_skip_keys:
        missing_pr_review_only = excluded_pr_review_only_keys - excluded_current_keys
        missing_runtime_only = excluded_runtime_only_keys - excluded_current_keys
        if missing_runtime_only and not missing_pr_review_only:
            coverage_error = "runtime_only_coverage_mismatch"
        elif missing_pr_review_only and not missing_runtime_only:
            coverage_error = "pr_review_only_coverage_mismatch"
        else:
            coverage_error = "skipped_scope_coverage_mismatch"
        return _result(
            overall_status="indeterminate", per_ac=[], rerun_required=True,
            source_integrity=source_integrity, evidence_refs=evidence_refs,
            errors=[coverage_error],
        )

    if excluded_pr_review_only_keys:
        binding_error = _test_verdict_binding_error(
            test_verdict,
            contract_snapshot=contract_snapshot,
            current_vc_result=current_vc_result,
            diff_summary=diff_summary,
            expected_keys=set(baseline_by_key) | excluded_pr_review_only_keys,
            require_producer_receipt=effective_require_producer_receipt,
        )
        if binding_error is not None:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[binding_error],
            )

    if excluded_runtime_only_keys:
        runtime_only_binding_error = _runtime_only_current_head_binding_error(
            contract_snapshot=contract_snapshot,
            current_vc_result=current_vc_result,
            diff_summary=diff_summary,
            changed_paths=changed_paths,
            changed_paths_present=changed_paths_present,
            allowed_paths=normalized_allowed,
        )
        if runtime_only_binding_error is not None:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[runtime_only_binding_error],
            )

    current_pass_certified = _current_pass_envelope_is_certified(
        contract_snapshot,
        current_vc_result,
        diff_summary,
        changed_paths,
        changed_paths_present,
        normalized_allowed,
    )
    if not normalized_current:
        if excluded_current_count and current_keys != set(baseline_by_key):
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=["baseline_current_mapping_mismatch"],
            )
        if excluded_current_count and current_pass_certified:
            per_ac = [
                {
                    "ac": ac,
                    "status": "pass",
                    "blocking": False,
                    "command_hash": command_hash,
                    "failure_keys": [],
                    "reason_code": "pr_review_only_runtime_evidence_pass",
                    "summary": "Producer-authorized skip is covered by v2 runtime evidence",
                }
                for ac, command_hash in sorted(excluded_pr_review_only_keys)
            ] + [
                {
                    "ac": ac,
                    "status": "pass",
                    "blocking": False,
                    "command_hash": command_hash,
                    "failure_keys": [],
                    "reason_code": "runtime_only_current_head_binding_pass",
                    "summary": "Producer-authorized runtime_only skip is covered by current-head independent binding",
                }
                for ac, command_hash in sorted(excluded_runtime_only_keys)
            ]
            return _result(
                overall_status="pass", per_ac=per_ac, rerun_required=False,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[],
            )
        return _result(
            overall_status="indeterminate", per_ac=[], rerun_required=True,
            source_integrity=source_integrity, evidence_refs=evidence_refs,
            errors=["empty_current_results_without_pass_signal"],
        )

    if current_keys != set(baseline_by_key):
        return _result(
            overall_status="indeterminate", per_ac=[], rerun_required=True,
            source_integrity=source_integrity, evidence_refs=evidence_refs,
            errors=["baseline_current_mapping_mismatch"],
        )

    per_ac: list[dict[str, Any]] = []
    for norm in normalized_current:
        baseline_item = baseline_by_key[(norm["ac"], norm["command_hash"])]
        if norm["exit_code"] == 0:
            if norm["failure_keys_present"]:
                status, blocking, rerun_required, reason_code, summary = (
                    "indeterminate", True, True, "pass_with_failure_keys",
                    "Current PASS must not contain failure keys",
                )
            elif not current_pass_certified:
                status, blocking, rerun_required, reason_code, summary = (
                    "indeterminate", True, True, "uncertified_current_pass",
                    "Current PASS lacks complete producer-certified source integrity",
                )
            elif baseline_item["classification"] == "expected_fail":
                status, blocking, rerun_required, reason_code, summary = (
                    "pass", False, False, "expected_fail_resolved_on_current_head",
                    "Expected baseline failure resolved by certified current-head PASS",
                )
            else:
                status, blocking, rerun_required, reason_code, summary = (
                    "pass", False, False, "expected_pass_still_passes",
                    "Expected baseline PASS remains a certified current-head PASS",
                )
        else:
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


def _step4_command_hashes(adjudication_result: dict[str, Any]) -> list[str] | None:
    """Return the command hashes covered by a VC_ADJUDICATION_RESULT_V1, in the
    original per_ac order (Issue #88 fix_delta Blocker 3: ordered, not sorted --
    sorting here would make a binding-tuple whose Verification Commands were
    merely reordered look identical to the original ordering)."""
    per_ac = adjudication_result.get("per_ac")
    if not isinstance(per_ac, list) or not per_ac:
        return None
    hashes: list[str] = []
    for entry in per_ac:
        if not isinstance(entry, dict):
            return None
        command_hash = entry.get("command_hash")
        if not isinstance(command_hash, str) or not command_hash:
            return None
        hashes.append(command_hash)
    return hashes


def evaluate_step4_vc_gate(
    adjudication_result: Any,
    *,
    expected_head_sha: str,
    expected_contract_body_sha256: str,
    expected_command_hashes: list[str],
) -> dict[str, Any]:
    """Decide whether pr-reviewer may be invoked (Issue #88 current-head gate).

    Re-derives a fail-closed boolean decision from an existing
    VC_ADJUDICATION_RESULT_V1 payload produced by adjudicate_vc_result() and
    an explicit "expected" binding triple (current-head SHA, current live
    Issue body SHA256, and the ordered literal Verification Command hashes
    Step 4 is about to evaluate). This reuses the existing adjudication
    result rather than re-classifying VC failures (Issue #88 Required
    Design #2) and holds no state of its own -- no new persistent ledger,
    authorization packet, publisher, or hook is introduced by this function
    (Issue #88 Required Design #9 / Out of Scope). We do not add a new
    persistent ledger anywhere in this module; any future persistent ledger
    for this gate would require a separate Issue.

    Returns a mapping with keys:
      - invoke_pr_reviewer: bool
      - reason_code: str | None -- one of "adjudication_missing_or_malformed",
        "adjudication_blocking_true", "adjudication_ac_not_resolved",
        "head_mismatch", "body_mismatch", "command_mismatch", or None when
        invoke_pr_reviewer is True.
    """
    if not isinstance(adjudication_result, dict) or adjudication_result.get("schema") != SCHEMA_NAME:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}

    per_ac = adjudication_result.get("per_ac")
    source_integrity = adjudication_result.get("source_integrity")
    if not isinstance(per_ac, list) or not per_ac or not isinstance(source_integrity, dict):
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}

    # Issue #88 fix_delta Warning 5: minimal additional malformed checks so a
    # stale/incomplete/rerun-pending adjudication cannot slip through just
    # because "blocking" happens to be False.
    if adjudication_result.get("schema_version") != SCHEMA_VERSION:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}
    if adjudication_result.get("errors"):
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}
    if adjudication_result.get("rerun_required") is not False:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}
    if source_integrity.get("evidence_complete") is not True:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}
    if source_integrity.get("evidence_fresh") is not True:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}

    if adjudication_result.get("blocking") is not False:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_blocking_true"}

    for entry in per_ac:
        if not isinstance(entry, dict) or entry.get("status") not in {
            "pass",
            "pre_existing_fail",
            "out_of_scope_fail",
        }:
            return {"invoke_pr_reviewer": False, "reason_code": "adjudication_ac_not_resolved"}

    if source_integrity.get("head_sha") != expected_head_sha:
        return {"invoke_pr_reviewer": False, "reason_code": "head_mismatch"}

    if source_integrity.get("contract_body_sha256") != expected_contract_body_sha256:
        return {"invoke_pr_reviewer": False, "reason_code": "body_mismatch"}

    # Issue #88 fix_delta Blocker 3: compare in literal order, not sorted --
    # a reordering of the same command set must be treated as a binding
    # mismatch (the Verification Commands block changed even if its set of
    # SHA256 hashes did not).
    observed_hashes = _step4_command_hashes(adjudication_result)
    if observed_hashes is None or observed_hashes != list(expected_command_hashes):
        return {"invoke_pr_reviewer": False, "reason_code": "command_mismatch"}

    return {"invoke_pr_reviewer": True, "reason_code": None}


def step4_binding_key(
    *, head_sha: str, contract_body_sha256: str, command_hashes: list[str]
) -> str:
    """Identity of a Step 2/Step 4 binding tuple (Issue #88 AC5).

    Command hashes are kept in their literal order (Issue #88 fix_delta
    Blocker 3 -- a reordering of the same command set is a different
    binding). The key is a plain string so it round-trips through
    LOOP_STATE YAML (Issue #88 fix_delta Blocker 2 -- superseding
    Step4AdjudicationCache, an in-memory-only object that could not survive
    across separate CLI invocations of this script).
    """
    payload = {
        "head_sha": head_sha,
        "contract_body_sha256": contract_body_sha256,
        "command_hashes": list(command_hashes),
    }
    return _sha256(_canonical_json(payload))


def step4_gate_from_loop_state(
    loop_state: dict[str, Any],
    *,
    expected_head_sha: str,
    expected_contract_body_sha256: str,
    expected_command_hashes: list[str],
) -> dict[str, Any]:
    """LOOP_STATE-based replacement for Step4AdjudicationCache.get_or_run()
    (Issue #88 fix_delta Blocker 2).

    Looks up a previously persisted VC_ADJUDICATION_RESULT_V1 for the current
    binding tuple in ``loop_state["vc_adjudication"]`` -- a plain
    YAML-serializable mapping written by Step 2 via
    ``step4_persist_vc_adjudication()``, not a Python-process-scoped cache
    object -- and re-validates it through ``evaluate_step4_vc_gate()`` so a
    stale/invalid stored entry is never reused as-is.

    Returns the same mapping shape as ``evaluate_step4_vc_gate()`` plus a
    ``"reused"`` boolean: True when a stored adjudication was found for this
    binding tuple (regardless of whether it still opens the gate), False
    when Step 2 must run test-runner again because no entry exists yet.
    """
    entries = loop_state.get("vc_adjudication")
    if not isinstance(entries, dict):
        return {
            "invoke_pr_reviewer": False,
            "reason_code": "adjudication_missing_or_malformed",
            "reused": False,
        }
    binding_key = step4_binding_key(
        head_sha=expected_head_sha,
        contract_body_sha256=expected_contract_body_sha256,
        command_hashes=expected_command_hashes,
    )
    stored = entries.get(binding_key)
    if stored is None:
        return {
            "invoke_pr_reviewer": False,
            "reason_code": "adjudication_missing_or_malformed",
            "reused": False,
        }
    decision = evaluate_step4_vc_gate(
        stored,
        expected_head_sha=expected_head_sha,
        expected_contract_body_sha256=expected_contract_body_sha256,
        expected_command_hashes=expected_command_hashes,
    )
    decision["reused"] = True
    return decision


def step4_persist_vc_adjudication(
    loop_state: dict[str, Any],
    *,
    head_sha: str,
    contract_body_sha256: str,
    command_hashes: list[str],
    adjudication_result: dict[str, Any],
) -> dict[str, Any]:
    """Persist a VC_ADJUDICATION_RESULT_V1 into
    ``loop_state["vc_adjudication"]`` keyed by the Step 4 binding tuple
    (Issue #88 fix_delta Blocker 2). Only a result that
    ``evaluate_step4_vc_gate()`` would accept (``invoke_pr_reviewer is
    True`` against its own asserted binding) is stored -- malformed,
    blocking, or unresolved adjudications are never written, so a later
    ``step4_gate_from_loop_state()`` lookup can only ever reuse a genuinely
    valid adjudication (Issue #88 AC5: "valid adjudication のみ再利用").
    Returns the (possibly unmodified) ``loop_state`` for convenience.
    """
    self_check = evaluate_step4_vc_gate(
        adjudication_result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=contract_body_sha256,
        expected_command_hashes=command_hashes,
    )
    if not self_check["invoke_pr_reviewer"]:
        return loop_state
    entries = loop_state.setdefault("vc_adjudication", {})
    binding_key = step4_binding_key(
        head_sha=head_sha,
        contract_body_sha256=contract_body_sha256,
        command_hashes=command_hashes,
    )
    entries[binding_key] = adjudication_result
    return loop_state


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
    parser.add_argument(
        "command",
        nargs="?",
        default="adjudicate",
        choices=["adjudicate", "step4-gate", "adapt"],
        help=(
            "adjudicate (default): classify a current VC result against the "
            "baseline contract snapshot. step4-gate (Issue #88 fix_delta "
            "Blocker 1): re-derive the Step 4 invoke/rerun decision from a "
            "LOOP_STATE.vc_adjudication entry for an explicit current-head "
            "binding tuple, without re-running test-runner. adapt (Issue #88 "
            "fix_delta Blocker 1): convert a TEST_VERDICT_MACHINE/v2 report "
            "(--test-verdict-file) into the baseline_vc_preflight/v1 shape "
            "accepted by --current-vc-result-file."
        ),
    )
    parser.add_argument("--contract-snapshot-file")
    parser.add_argument("--current-vc-result-file")
    parser.add_argument("--diff-summary-file")
    parser.add_argument("--allowed-paths-file")
    parser.add_argument("--test-verdict-file")
    parser.add_argument(
        "--require-producer-receipt",
        action="store_true",
        help=(
            "Issue #1648: reject a pr_review_only test_verdict unless it embeds a "
            "schema-valid Child A producer_receipt bound to its own head_sha/pr_number/"
            "issue_number/contract_body_sha256/artifact digest. Legacy self-attested "
            "TEST_VERDICT input (no producer_receipt) stays accepted when this flag is "
            "absent."
        ),
    )
    parser.add_argument("--artifact-out")
    parser.add_argument("--max-stdout-bytes", type=int, default=4096)
    parser.add_argument(
        "--loop-state-file",
        help="step4-gate: path to a JSON LOOP_STATE snapshot containing 'vc_adjudication'.",
    )
    parser.add_argument("--expected-head-sha", help="step4-gate: current live PR head SHA.")
    parser.add_argument(
        "--expected-contract-body-sha256",
        help="step4-gate: current live linked Issue body SHA256.",
    )
    parser.add_argument(
        "--expected-command-hashes-file",
        help="step4-gate: path to a JSON array of literal Verification Command SHA256 hashes, in order.",
    )
    parser.add_argument(
        "--adapt-out",
        help="adapt: path to write the converted baseline_vc_preflight/v1 JSON (default: stdout).",
    )
    args = parser.parse_args(argv)
    if args.command == "adjudicate":
        if not args.contract_snapshot_file or not args.current_vc_result_file:
            parser.error(
                "adjudicate requires --contract-snapshot-file and --current-vc-result-file"
            )
    elif args.command == "adapt":
        if not args.test_verdict_file:
            parser.error("adapt requires --test-verdict-file")
    else:
        missing = [
            flag
            for flag, value in (
                ("--loop-state-file", args.loop_state_file),
                ("--expected-head-sha", args.expected_head_sha),
                ("--expected-contract-body-sha256", args.expected_contract_body_sha256),
                ("--expected-command-hashes-file", args.expected_command_hashes_file),
            )
            if not value
        ]
        if missing:
            parser.error(f"step4-gate requires: {', '.join(missing)}")
    return args


def _load_allowed_paths(path: str | None) -> tuple[list[str] | None, list[str]]:
    if path is None:
        return None, ["missing_allowed_paths_file"]
    raw, errors = _load_json_file(path)
    if errors:
        return None, errors
    if not isinstance(raw, list):
        return None, ["allowed_paths_not_list"]
    return list(raw), []


def _run_adapt(args: argparse.Namespace) -> int:
    """CLI entrypoint for the `adapt` subcommand (Issue #88 fix_delta
    Blocker 1). Exit codes: 0 = converted cleanly, 1 = converted with
    warnings (see stderr), 2 = --test-verdict-file unreadable/not JSON or
    not interpretable as TEST_VERDICT_MACHINE/v2 at all."""
    raw, load_errors = _load_json_file(args.test_verdict_file)
    if load_errors:
        sys.stdout.write(_canonical_json({"errors": load_errors}) + "\n")
        return 2

    converted, errors = adapt_test_verdict_to_current_vc_result(raw)
    if converted is None:
        sys.stdout.write(_canonical_json({"errors": errors}) + "\n")
        return 2

    text_out = _canonical_json(converted)
    if args.adapt_out:
        Path(args.adapt_out).write_text(text_out, encoding="utf-8")
    else:
        sys.stdout.write(text_out + "\n")

    if errors:
        sys.stderr.write(_canonical_json({"warnings": errors}) + "\n")
        return 1
    return 0


def _run_step4_gate(args: argparse.Namespace) -> int:
    """CLI entrypoint for the `step4-gate` subcommand (Issue #88 fix_delta
    Blocker 1). Exit codes: 0 = invoke pr-reviewer, 1 = rerun (Step 2 must
    run test-runner again), 2 = malformed CLI input (unparseable
    --loop-state-file / --expected-command-hashes-file, or a
    non-object LOOP_STATE payload)."""
    loop_state, loop_state_errors = _load_json_file(args.loop_state_file)
    if loop_state_errors or not isinstance(loop_state, dict):
        sys.stdout.write(
            _canonical_json(
                {
                    "invoke_pr_reviewer": False,
                    "reason_code": "loop_state_malformed",
                    "reused": False,
                    "errors": loop_state_errors or ["loop_state_not_object"],
                }
            )
            + "\n"
        )
        return 2

    expected_command_hashes, hashes_errors = _load_json_file(args.expected_command_hashes_file)
    if hashes_errors or not isinstance(expected_command_hashes, list):
        sys.stdout.write(
            _canonical_json(
                {
                    "invoke_pr_reviewer": False,
                    "reason_code": "expected_command_hashes_malformed",
                    "reused": False,
                    "errors": hashes_errors or ["expected_command_hashes_not_list"],
                }
            )
            + "\n"
        )
        return 2

    decision = step4_gate_from_loop_state(
        loop_state,
        expected_head_sha=args.expected_head_sha,
        expected_contract_body_sha256=args.expected_contract_body_sha256,
        expected_command_hashes=list(expected_command_hashes),
    )
    sys.stdout.write(_canonical_json(decision) + "\n")
    return 0 if decision["invoke_pr_reviewer"] else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.command == "step4-gate":
        return _run_step4_gate(args)
    if args.command == "adapt":
        return _run_adapt(args)

    contract_snapshot, contract_errors = _load_json_file(args.contract_snapshot_file)
    current_vc_result, current_errors = _load_json_file(args.current_vc_result_file)
    diff_summary, diff_errors = _load_json_file(args.diff_summary_file) if args.diff_summary_file else (None, [])
    diff_errors = diff_errors or []
    allowed_paths, allowed_errors = _load_allowed_paths(args.allowed_paths_file)
    test_verdict, test_verdict_errors = (
        _load_json_file(args.test_verdict_file) if args.test_verdict_file else (None, [])
    )

    result = adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=test_verdict,
        require_producer_receipt=args.require_producer_receipt,
    )
    result["errors"].extend(contract_errors or [])
    result["errors"].extend(current_errors or [])
    result["errors"].extend(diff_errors or [])
    result["errors"].extend(allowed_errors or [])
    result["errors"].extend(test_verdict_errors or [])
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
            "test_verdict": test_verdict,
            "artifact_inputs": {
                "contract_snapshot_file": args.contract_snapshot_file,
                "current_vc_result_file": args.current_vc_result_file,
                "diff_summary_file": args.diff_summary_file,
                "allowed_paths_file": args.allowed_paths_file,
                "test_verdict_file": args.test_verdict_file,
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

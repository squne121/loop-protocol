#!/usr/bin/env python3
"""parse_scope_rollup_run_result.py

Parse scope-rollup runner output and validate ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1
marker in fenced YAML only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

MARKER_NAME = "ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1"
OUTPUT_MARKER = "SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"
CAPTURE_MARKER = "SCOPE_ROLLUP_CAPTURE_RESULT_V1"

ALLOWED_MARKER_STATUS = {"ok", "failed", "runner_unavailable"}
REQUIRED_FIELDS_BASE = {
    "status",
    "repo",
    "current_issue",
    "invocation_id",
    "requested_at",
    "generated_at",
    "script_blob_sha256",
}
REQUIRE_RESULT_FIELDS = {"raw_plan_location", "result_sha256", "payload"}
COMPLETENESS_FIELDS = {"page_count", "item_count", "total_count", "pagination_complete", "sha256"}
BUDGET_FIELDS = {
    "page_count",
    "response_bytes",
    "inventory_items",
    "max_transaction_pages",
    "max_response_bytes",
    "max_inventory_items",
    "deadline_seconds",
}

FENCED_YAML_RE = re.compile(r"```ya?ml[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _safe_invocation_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _parse_iso8601(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("timestamp must include timezone offset")
    return dt.astimezone(timezone.utc)


def _load_issue_refinement_verifier():
    """Load issue-refinement-loop/scripts/verify_scope_rollup_result.py dynamically."""
    script_path = (
        Path(__file__).resolve().parents[2]
        / "issue-refinement-loop"
        / "scripts"
        / "verify_scope_rollup_result.py"
    )
    if not script_path.exists():
        return None

    spec = importlib.util.spec_from_file_location(
        "issue_refinement_verify_scope_rollup_result",
        script_path,
    )
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_yaml_no_duplicate_keys(text: str) -> Any:
    """Load YAML and fail when duplicate keys exist."""

    class _StrictLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(
        loader: Any,
        node: yaml.nodes.MappingNode,
    ) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            if key in mapping:
                raise ValueError(f"duplicate key: {key}")
            mapping[key] = loader.construct_object(value_node)
        return mapping

    _StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        _construct_mapping,
    )
    return yaml.load(text, Loader=_StrictLoader)


def _extract_marker_blocks(output: str) -> list[dict[str, Any]]:
    """Return ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1 payloads from fenced YAML blocks."""
    blocks: list[dict[str, Any]] = []
    for match in FENCED_YAML_RE.finditer(output):
        block_text = match.group(1).strip()
        if MARKER_NAME not in block_text:
            continue

        try:
            parsed = _load_yaml_no_duplicate_keys(block_text)
        except Exception:
            blocks.append({"__parse_error__": True})
            continue

        if not isinstance(parsed, dict):
            continue
        candidate = parsed.get(MARKER_NAME)
        if isinstance(candidate, dict):
            blocks.append(candidate)
        elif candidate is not None:
            blocks.append({"__type_error__": True})

    return blocks


def _load_capture_sidecar(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    parsed = _load_yaml_no_duplicate_keys(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return None
    capture = parsed.get(CAPTURE_MARKER)
    return capture if isinstance(capture, dict) else None


def _compute_payload_sha256(payload: dict[str, Any]) -> str:
    """Same canonicalization as plan_issue_scope_rollup.py /
    verify_scope_rollup_result.py: json.dumps(ensure_ascii=False,
    sort_keys=True, separators=(",", ":")).encode("utf-8")."""
    canonical_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def _has_valid_completeness_contract(inputs: Any) -> bool:
    if not isinstance(inputs, dict) or inputs.get("query_schema_version") != 3:
        return False
    for key in ("issues_completeness", "pull_requests_completeness"):
        block = inputs.get(key)
        if not isinstance(block, dict) or not COMPLETENESS_FIELDS.issubset(block):
            return False
        if (
            not isinstance(block["page_count"], int)
            or block["page_count"] < 1
            or not isinstance(block["item_count"], int)
            or block["item_count"] < 0
            or not isinstance(block["total_count"], int)
            or block["total_count"] != block["item_count"]
            or block["pagination_complete"] is not True
            or not isinstance(block["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", block["sha256"])
        ):
            return False
    budget = inputs.get("transaction_budget")
    if not isinstance(budget, dict) or not BUDGET_FIELDS.issubset(budget):
        return False
    return all(isinstance(budget[key], (int, float)) and not isinstance(budget[key], bool) for key in BUDGET_FIELDS)


def _validate_marker_payload(
    marker_payload: dict[str, Any],
    assistant_output_file: Path,
    capture_sidecar_file: Path,
    expected_repo: str,
    expected_issue_number: int,
    expected_invocation_id: str,
    expected_script_sha: str,
    requested_at: str,
) -> tuple[str, str | None, str | None, bool]:
    """Validate marker payload.

    Returns (parse_status, termination_cause, reject_reason, raw_plan_location_allowed).
    """
    for field in REQUIRED_FIELDS_BASE:
        if field not in marker_payload:
            return "marker_malformed", "scope_rollup_marker_malformed", "marker_malformed", False

    status = str(marker_payload.get("status", "")).strip()
    if status not in ALLOWED_MARKER_STATUS:
        return "marker_malformed", "scope_rollup_marker_malformed", "marker_malformed", False

    if marker_payload.get("repo") != expected_repo:
        return "rejected", "scope_rollup_marker_malformed", "repo_mismatch", False

    try:
        current_issue = int(marker_payload.get("current_issue"))
    except Exception:
        return "marker_malformed", "scope_rollup_marker_malformed", "issue_mismatch", False
    if current_issue != expected_issue_number:
        return "rejected", "scope_rollup_marker_malformed", "issue_mismatch", False

    if str(marker_payload.get("invocation_id", "")) != str(expected_invocation_id):
        return "rejected", "scope_rollup_marker_malformed", "invocation_id_mismatch", False

    if not isinstance(marker_payload.get("script_blob_sha256"), str):
        return "marker_malformed", "scope_rollup_marker_malformed", "marker_malformed", False
    if marker_payload.get("script_blob_sha256") != expected_script_sha:
        return "rejected", "scope_rollup_marker_malformed", "script_sha_mismatch", False

    try:
        requested_at_dt = _parse_iso8601(requested_at)
        marker_requested_at_dt = _parse_iso8601(str(marker_payload.get("requested_at")))
        if marker_requested_at_dt != requested_at_dt:
            return "rejected", "scope_rollup_marker_malformed", "requested_at_mismatch", False
        generated_at_dt = _parse_iso8601(str(marker_payload.get("generated_at")))
    except Exception:
        return "marker_malformed", "scope_rollup_marker_malformed", "marker_malformed", False

    if generated_at_dt <= requested_at_dt:
        return "rejected", "scope_rollup_marker_malformed", "stale", False

    if status in {"failed", "runner_unavailable"}:
        return status, None, None, False

    inputs = marker_payload.get("inputs")
    if inputs is not None and not isinstance(inputs, dict):
        return "rejected", "scope_rollup_marker_malformed", "inventory_completeness_contract_invalid", False

    # PR #1643 review (P0-3): the completeness gate below used to trigger
    # only when "query_schema_version" happened to still be present inside
    # ``inputs`` -- a marker that stripped just that one key (while leaving
    # the other v3 completeness/budget fields, or nothing at all) was
    # silently accepted as a "legacy v2" marker even though it came from the
    # *current* v3-speaking runner. That is exactly the silent-downgrade
    # this contract must never permit.
    #
    # The current runner (``.claude/agents/scope-rollup-runner.md``) always
    # stamps an explicit ``marker_schema_version: 3`` on every marker it
    # emits. That field -- not "does inputs still happen to contain
    # query_schema_version" -- is now the authoritative version
    # discriminator:
    #   marker_schema_version == 3  -> full v3 completeness contract is
    #                                  mandatory, regardless of what
    #                                  ``inputs`` does or does not contain.
    #   marker_schema_version == 2  -> explicit legacy marker; no
    #                                  completeness contract required.
    #   marker_schema_version absent -> a genuine pre-#1593 legacy marker
    #                                  (minted before this field existed).
    #                                  Preserve the original permissive
    #                                  behavior for these so historical/AC6
    #                                  legacy-v2 acceptance is not broken.
    #   any other value            -> unsupported/malformed.
    marker_schema_version = marker_payload.get("marker_schema_version")
    if marker_schema_version is not None:
        if marker_schema_version == 3:
            if not (isinstance(inputs, dict) and _has_valid_completeness_contract(inputs)):
                return "rejected", "scope_rollup_marker_malformed", "inventory_completeness_contract_invalid", False
        elif marker_schema_version == 2:
            pass
        else:
            return "rejected", "scope_rollup_marker_malformed", "marker_schema_version_unsupported", False
    elif isinstance(inputs, dict) and "query_schema_version" in inputs and not _has_valid_completeness_contract(inputs):
        return "rejected", "scope_rollup_marker_malformed", "inventory_completeness_contract_invalid", False

    result_block = marker_payload.get("result")
    if not isinstance(result_block, dict):
        return "marker_malformed", "scope_rollup_marker_malformed", "marker_malformed", False
    for field in REQUIRE_RESULT_FIELDS:
        if field not in result_block:
            return "marker_malformed", "scope_rollup_marker_malformed", "marker_malformed", False

    if result_block.get("verify_status") != "verified":
        return "rejected", "scope_rollup_marker_malformed", "verify_status_not_verified", False

    # Issue #1547 fix_delta (P0-1): raw_plan_location is fixed to null --
    # the executor's private invocation directory is cleaned up on every
    # exit path, so no persisted file location can ever exist. A non-null
    # value now indicates a runner still speaking the old (broken) contract.
    if result_block.get("raw_plan_location") is not None:
        return "rejected", "scope_rollup_marker_malformed", "raw_plan_location_invalid", False

    payload = result_block.get("payload")
    if not isinstance(payload, dict):
        return "marker_malformed", "scope_rollup_marker_malformed", "marker_malformed", False

    # result_sha256 means exactly one thing everywhere in this contract: the
    # sha256 of the canonical JSON encoding of the plan payload (candidates +
    # metadata, self_validation excluded) -- i.e. self_validation.payload_sha256
    # as computed by plan_issue_scope_rollup.py. Recomputing it here from the
    # embedded payload is a genuine cryptographic binding, not a
    # self-report: a runner cannot fabricate verify_status: verified without
    # also supplying a payload whose hash matches result_sha256.
    if _compute_payload_sha256(payload) != str(result_block.get("result_sha256", "")):
        return "rejected", "scope_rollup_marker_malformed", "result_sha_mismatch", False

    payload_schema_version = payload.get("schema_version")
    if payload_schema_version != 2:
        return "rejected", "scope_rollup_marker_malformed", "verify_status_not_verified", False

    capture_sidecar = _load_capture_sidecar(capture_sidecar_file)
    if capture_sidecar is None:
        return "rejected", "scope_rollup_marker_malformed", "capture_sidecar_missing", False

    expected_capture_path = str(assistant_output_file.resolve())
    capture_path = str(capture_sidecar.get("capture_path", ""))
    if capture_sidecar.get("capture_mode") != "subagent_stop_hook":
        return "rejected", "scope_rollup_marker_malformed", "capture_mode_invalid", False
    if capture_sidecar.get("capture_status") != "captured":
        return "rejected", "scope_rollup_marker_malformed", "capture_status_invalid", False
    if capture_sidecar.get("agent_type") != "scope-rollup-runner":
        return "rejected", "scope_rollup_marker_malformed", "capture_agent_type_mismatch", False
    if str(capture_sidecar.get("invocation_id", "")) != str(expected_invocation_id):
        return "rejected", "scope_rollup_marker_malformed", "capture_invocation_id_mismatch", False
    if capture_sidecar.get("capture_source") != "last_assistant_message":
        return "rejected", "scope_rollup_marker_malformed", "capture_source_invalid", False
    if capture_path != expected_capture_path:
        return "rejected", "scope_rollup_marker_malformed", "capture_path_mismatch", False
    if capture_sidecar.get("capture_routing_action", capture_sidecar.get("routing_action")) != "continue":
        return "rejected", "scope_rollup_marker_malformed", "capture_routing_action_invalid", False
    if not assistant_output_file.exists():
        return "rejected", "scope_rollup_marker_malformed", "capture_file_missing", False
    capture_sha = str(capture_sidecar.get("capture_sha256", ""))
    actual_capture_sha = _sha256_file(assistant_output_file)
    if capture_sha != actual_capture_sha:
        return "rejected", "scope_rollup_marker_malformed", "capture_sha_mismatch", False

    # Defense in depth: also run the shared in-memory verifier
    # (verify_scope_rollup_result.verify_payload) against the embedded
    # payload re-assembled with a self_validation block carrying the
    # already-verified sha/schema fields, so schema_name/script_file_sha256
    # (not covered by the result_sha256 check above) are cross-checked too.
    verifier = _load_issue_refinement_verifier()
    if verifier is None:
        return "rejected", "scope_rollup_marker_malformed", "verify_status_not_verified", False

    reconstructed = dict(payload)
    reconstructed["self_validation"] = {
        "schema_name": result_block.get("plan_schema_name") or result_block.get("plan_schema"),
        "schema_version": payload_schema_version,
        "payload_sha256": str(result_block.get("result_sha256", "")),
        "script_file_sha256": expected_script_sha,
    }
    _verify_output, verify_code = verifier.verify_payload(reconstructed)
    if verify_code != 0:
        return "rejected", "scope_rollup_marker_malformed", "verify_status_not_verified", False

    return "ok", None, None, True


def _format_result(
    status: str,
    routing_action: str,
    termination_cause: str | None,
    reject_reason: str | None,
    raw_plan_location_allowed: bool,
) -> dict[str, Any]:
    return {
        OUTPUT_MARKER: {
            "status": status,
            "routing_action": routing_action,
            "termination_cause": termination_cause,
            "reject_reason": reject_reason,
            "raw_plan_location_allowed": raw_plan_location_allowed,
        },
    }


def parse_scope_rollup_output(
    *,
    assistant_output: str,
    assistant_output_file: Path,
    capture_sidecar_file: Path,
    repo: str,
    issue_number: int,
    invocation_id: str,
    expected_script_sha: str,
    requested_at: str,
) -> dict[str, Any]:
    marker_blocks = _extract_marker_blocks(assistant_output)

    if not marker_blocks:
        return _format_result(
            status="marker_missing",
            routing_action="stop_human",
            termination_cause="scope_rollup_marker_missing",
            reject_reason="marker_missing",
            raw_plan_location_allowed=False,
        )

    if len(marker_blocks) > 1:
        return _format_result(
            status="marker_ambiguous",
            routing_action="stop_human",
            termination_cause="scope_rollup_marker_malformed",
            reject_reason="marker_ambiguous",
            raw_plan_location_allowed=False,
        )

    marker_payload = marker_blocks[0]
    if "__parse_error__" in marker_payload or "__type_error__" in marker_payload:
        return _format_result(
            status="marker_malformed",
            routing_action="stop_human",
            termination_cause="scope_rollup_marker_malformed",
            reject_reason="marker_malformed",
            raw_plan_location_allowed=False,
        )

    parse_status, termination_cause, reject_reason, raw_allowed = _validate_marker_payload(
        marker_payload=marker_payload,
        assistant_output_file=assistant_output_file,
        capture_sidecar_file=capture_sidecar_file,
        expected_repo=repo,
        expected_issue_number=issue_number,
        expected_invocation_id=invocation_id,
        expected_script_sha=expected_script_sha,
        requested_at=requested_at,
    )

    if parse_status == "ok":
        return _format_result(
            status="ok",
            routing_action="continue",
            termination_cause=None,
            reject_reason=None,
            raw_plan_location_allowed=raw_allowed,
        )

    if parse_status == "runner_unavailable":
        return _format_result(
            status="runner_unavailable",
            routing_action="deferred",
            termination_cause=None,
            reject_reason=None,
            raw_plan_location_allowed=raw_allowed,
        )

    if parse_status == "failed":
        return _format_result(
            status="failed",
            routing_action="stop_human",
            termination_cause=termination_cause,
            reject_reason=reject_reason,
            raw_plan_location_allowed=raw_allowed,
        )

    return _format_result(
        status=parse_status,
        routing_action="stop_human",
        termination_cause=termination_cause,
        reject_reason=reject_reason,
        raw_plan_location_allowed=raw_allowed,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Parse ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1 output from scope-rollup-runner "
            "and validate deterministic marker requirements."
        )
    )
    parser.add_argument("--assistant-output-file", required=True)
    parser.add_argument("--capture-sidecar-file", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--expected-script-sha", required=True)
    parser.add_argument("--requested-at", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    output_file = Path(args.assistant_output_file)
    capture_sidecar_file = Path(args.capture_sidecar_file)
    if not output_file.exists():
        result = _format_result(
            status="marker_missing",
            routing_action="stop_human",
            termination_cause="scope_rollup_marker_missing",
            reject_reason="marker_missing",
            raw_plan_location_allowed=False,
        )
        print(yaml.safe_dump(result, sort_keys=False, allow_unicode=True).rstrip(), end="")
        return

    assistant_output = output_file.read_text(encoding="utf-8")
    try:
        result = parse_scope_rollup_output(
            assistant_output=assistant_output,
            assistant_output_file=output_file.resolve(),
            capture_sidecar_file=capture_sidecar_file.resolve(),
            repo=args.repo,
            issue_number=args.issue_number,
            invocation_id=args.invocation_id,
            expected_script_sha=args.expected_script_sha,
            requested_at=args.requested_at,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        result = _format_result(
            status="marker_malformed",
            routing_action="stop_human",
            termination_cause="scope_rollup_marker_malformed",
            reject_reason=f"unexpected_error: {type(exc).__name__}: {exc}",
            raw_plan_location_allowed=False,
        )

    print(yaml.safe_dump(result, sort_keys=False, allow_unicode=True).rstrip(), end="")


if __name__ == "__main__":
    main()

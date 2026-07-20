#!/usr/bin/env python3
"""Unit tests for parse_scope_rollup_run_result.py

PR #1560 OWNER fix_delta (P0-1): the marker's `result` block no longer
carries a `raw_plan_location` file path (it is fixed to `null`); instead the
full plan payload (candidates + metadata, `self_validation` excluded) is
embedded directly as `result.payload`, and `result_sha256` is the sha256 of
that payload's canonical JSON encoding. These tests exercise the new,
artifact-free contract end to end.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
PARSE_SCRIPT = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "parse_scope_rollup_run_result.py"
)
PLAN_SCRIPT = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "issue-refinement-loop"
    / "scripts"
    / "plan_issue_scope_rollup.py"
)


def _load_script_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_payload_sha256(payload: Dict[str, Any]) -> str:
    canonical_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def _default_payload() -> Dict[str, Any]:
    return {
        "schema_version": 2,
        "repo": "squne121/loop-protocol",
        "generated_at": "2026-06-13T10:01:00Z",
        "source": "plan_issue_scope_rollup",
        "body_sha256": "deadbeef",
        "input": {"completeness": "full", "warnings": []},
        "candidates": [],
    }


def _render_marker(
    *,
    status: str = "ok",
    include_result: bool = True,
    requested_at: str = "2026-06-13T10:00:00+00:00",
    generated_at: str = "2026-06-13T10:01:00+00:00",
    overrides: Dict[str, Any] | None = None,
    script_sha: str | None = None,
    raw_plan_location: Any = None,
    payload: Dict[str, Any] | None = None,
    result_sha: str | None = None,
    result_overrides: Dict[str, Any] | None = None,
) -> str:
    script_sha = script_sha or _load_script_sha(PLAN_SCRIPT)
    marker: Dict[str, Any] = {
        "status": status,
        "schema_version": 1,
        "repo": "squne121/loop-protocol",
        "current_issue": 841,
        "invocation_id": "inv-2026-06-13",
        "requested_at": requested_at,
        "generated_at": generated_at,
        "git_head_sha": "0000000000000000000000000000000000000000",
        "script_path": str(PLAN_SCRIPT),
        "script_blob_sha256": script_sha,
    }

    if include_result:
        effective_payload = payload if payload is not None else _default_payload()
        effective_result_sha = (
            result_sha if result_sha is not None else _canonical_payload_sha256(effective_payload)
        )
        result_block: Dict[str, Any] = {
            "plan_schema": "ISSUE_SCOPE_ROLLUP_PLAN_V2",
            "plan_schema_name": "ISSUE_SCOPE_ROLLUP_PLAN_V2",
            "plan_schema_version": 2,
            "raw_plan_location": raw_plan_location,
            "result_sha256": effective_result_sha,
            "verify_status": "verified",
            "suggested_actions_summary": "No action\n",
            "candidate_count": 0,
            "high_confidence_count": 0,
            "payload": effective_payload,
        }
        if result_overrides:
            result_block.update(result_overrides)
        marker["result"] = result_block

    if overrides:
        marker.update(overrides)

    return (
        "```yaml\n"
        + yaml.safe_dump(
            {"ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1": marker},
            sort_keys=False,
            allow_unicode=True,
        ).rstrip()
        + "\n```"
    )


def _render_capture_sidecar(
    assistant_output_path: Path,
    *,
    invocation_id: str = "inv-2026-06-13",
    capture_status: str = "captured",
    capture_mode: str = "subagent_stop_hook",
    agent_type: str = "scope-rollup-runner",
    capture_source: str = "last_assistant_message",
    routing_action: str = "continue",
) -> str:
    return yaml.safe_dump(
        {
            "SCOPE_ROLLUP_CAPTURE_RESULT_V1": {
                "capture_mode": capture_mode,
                "capture_status": capture_status,
                "parser_status": "ok",
                "capture_routing_action": routing_action,
                "routing_action": routing_action,
                "agent_type": agent_type,
                "invocation_id": invocation_id,
                "capture_path": str(assistant_output_path.resolve()),
                "capture_sha256": hashlib.sha256(assistant_output_path.read_bytes()).hexdigest(),
                "capture_source": capture_source,
            }
        },
        sort_keys=False,
        allow_unicode=True,
    )


def _run_parser_cli(
    output_path: Path,
    sidecar_path: Path,
    *,
    invocation_id: str = "inv-2026-06-13",
    issue_number: int = 841,
    expected_script_sha: str,
    requested_at: str,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(PARSE_SCRIPT),
            "--assistant-output-file",
            str(output_path),
            "--capture-sidecar-file",
            str(sidecar_path),
            "--repo",
            "squne121/loop-protocol",
            "--issue-number",
            str(issue_number),
            "--invocation-id",
            invocation_id,
            "--expected-script-sha",
            expected_script_sha,
            "--requested-at",
            requested_at,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _run_parser(
    assistant_output: str,
    *,
    invocation_id: str = "inv-2026-06-13",
    issue_number: int = 841,
    expected_script_sha: str,
    requested_at: str,
    capture_sidecar_text: str | None = None,
) -> Dict[str, Any]:
    with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(assistant_output)
        tmp_path = Path(tmp.name)

    sidecar_path = tmp_path.with_suffix(".capture.yaml")
    if capture_sidecar_text is not None:
        sidecar_path.write_text(capture_sidecar_text, encoding="utf-8")

    try:
        result = _run_parser_cli(
            tmp_path,
            sidecar_path,
            invocation_id=invocation_id,
            issue_number=issue_number,
            expected_script_sha=expected_script_sha,
            requested_at=requested_at,
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
        if sidecar_path.exists():
            sidecar_path.unlink()

    assert result.returncode == 0, f"Parser command failed: {result.stderr}"
    parsed = yaml.safe_load(result.stdout)
    assert isinstance(parsed, dict)
    assert "SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1" in parsed
    return parsed["SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"]


def test_marker_missing_when_no_marker_block(tmp_path):
    result = _run_parser(
        "No fenced marker blocks.",
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "marker_missing"
    assert result["routing_action"] == "stop_human"
    assert result["reject_reason"] == "marker_missing"


def test_marker_missing_when_marker_appears_in_prose_only(tmp_path):
    result = _run_parser(
        "The runner returned ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1 but not in fenced YAML.",
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "marker_missing"


def test_marker_malformed_for_bad_yaml(tmp_path):
    result = _run_parser(
        "```yaml\n{ ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1: [invalid`\n```",
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "marker_malformed"
    assert result["routing_action"] == "stop_human"


def test_marker_ambiguous_when_duplicate_markers_present(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(script_sha=script_sha)
    result = _run_parser(
        f"{marker}\n{marker}",
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "marker_ambiguous"
    assert result["routing_action"] == "stop_human"
    assert result["reject_reason"] == "marker_ambiguous"


def test_rejected_when_invocation_id_mismatch(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(overrides={"invocation_id": "other-invocation"}, script_sha=script_sha)
    result = _run_parser(
        marker,
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "invocation_id_mismatch"


def test_rejected_when_result_sha_mismatch(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(
        script_sha=script_sha,
        raw_plan_location=None,
        result_sha="deadbeef" + "00" * 30,
    )
    result = _run_parser(
        marker,
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "result_sha_mismatch"


def test_rejected_when_raw_plan_location_is_not_null(tmp_path):
    """Issue #1547 fix_delta: raw_plan_location must always be null -- a
    non-null value (the pre-fix_delta contract) is now itself a rejection
    reason, not a path to validate."""
    marker = _render_marker(
        raw_plan_location="/tmp/scope_rollup_result.json",
        script_sha=_load_script_sha(PLAN_SCRIPT),
    )
    result = _run_parser(
        marker,
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "raw_plan_location_invalid"


def test_non_escalation_for_runner_unavailable_marker(tmp_path):
    marker = _render_marker(
        status="runner_unavailable",
        include_result=False,
        script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
        generated_at="2026-06-13T10:01:00+00:00",
    )
    result = _run_parser(
        marker,
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
        capture_sidecar_text="SCOPE_ROLLUP_CAPTURE_RESULT_V1:\n  capture_mode: unsupported\n",
    )
    assert result["status"] == "runner_unavailable"
    assert result["routing_action"] == "deferred"


def test_failed_marker_stops_human():
    marker = _render_marker(
        status="failed",
        include_result=False,
        script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
        generated_at="2026-06-13T10:01:00+00:00",
    )
    result = _run_parser(
        marker,
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
        capture_sidecar_text="SCOPE_ROLLUP_CAPTURE_RESULT_V1:\n  capture_mode: unsupported\n",
    )
    assert result["status"] == "failed"
    assert result["routing_action"] == "stop_human"


def test_requested_at_mismatch_rejected(tmp_path):
    marker = _render_marker(
        script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
    )
    result = _run_parser(
        marker,
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T09:59:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "requested_at_mismatch"


def test_rejected_when_capture_sidecar_is_missing(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(script_sha=script_sha)
    result = _run_parser(
        marker,
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "capture_sidecar_missing"


def test_ok_when_capture_sidecar_file_matches_assistant_output(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    # Legacy v2 runner markers carried the original inputs block but did not
    # declare query_schema_version or the v3 completeness fields.
    marker = _render_marker(
        script_sha=script_sha,
        overrides={
            "inputs": {
                "current_issue_sha256": "deadbeef",
                "issues_all_sha256": "deadbeef",
                "prs_all_sha256": "deadbeef",
                "issue_count": 0,
                "pr_count": 0,
            }
        },
    )
    with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(marker)
        output_path = Path(tmp.name)
    sidecar_path = output_path.with_suffix(".capture.yaml")
    sidecar_path.write_text(_render_capture_sidecar(output_path), encoding="utf-8")
    try:
        result = _run_parser_cli(
            output_path,
            sidecar_path,
            expected_script_sha=script_sha,
            requested_at="2026-06-13T10:00:00+00:00",
        )
        assert result.returncode == 0, result.stderr
        parsed = yaml.safe_load(result.stdout)["SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"]
        assert parsed["status"] == "ok"
        assert parsed["routing_action"] == "continue"
        assert parsed["raw_plan_location_allowed"] is True
    finally:
        output_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)


def test_ok_marker_carries_full_candidates_payload(tmp_path):
    """P0-1 point 5: candidate details survive the executor boundary and are
    readable from the parsed/validated marker payload."""
    script_sha = _load_script_sha(PLAN_SCRIPT)
    payload = _default_payload()
    payload["candidates"] = [
        {"kind": "issue", "number": 999, "confidence": "high", "suggested_action": "merge_into_current_pr"}
    ]
    marker = _render_marker(script_sha=script_sha, payload=payload)
    with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(marker)
        output_path = Path(tmp.name)
    sidecar_path = output_path.with_suffix(".capture.yaml")
    sidecar_path.write_text(_render_capture_sidecar(output_path), encoding="utf-8")
    try:
        result = _run_parser_cli(
            output_path,
            sidecar_path,
            expected_script_sha=script_sha,
            requested_at="2026-06-13T10:00:00+00:00",
        )
        assert result.returncode == 0, result.stderr
        parsed = yaml.safe_load(result.stdout)["SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"]
        assert parsed["status"] == "ok"
    finally:
        output_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)


def test_rejected_when_capture_status_is_not_captured(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(script_sha=script_sha)
    with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(marker)
        output_path = Path(tmp.name)
    sidecar_path = output_path.with_suffix(".capture.yaml")
    sidecar_path.write_text(
        _render_capture_sidecar(output_path, capture_status="duplicate_invocation", routing_action="stop_human"),
        encoding="utf-8",
    )
    try:
        result = _run_parser_cli(
            output_path,
            sidecar_path,
            expected_script_sha=script_sha,
            requested_at="2026-06-13T10:00:00+00:00",
        )
        parsed = yaml.safe_load(result.stdout)["SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"]
        assert parsed["status"] == "rejected"
        assert parsed["reject_reason"] == "capture_status_invalid"
    finally:
        output_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)


def test_rejected_when_capture_sha_mismatches(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(script_sha=script_sha)
    with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(marker)
        output_path = Path(tmp.name)
    sidecar_path = output_path.with_suffix(".capture.yaml")
    sidecar = yaml.safe_load(_render_capture_sidecar(output_path))
    sidecar["SCOPE_ROLLUP_CAPTURE_RESULT_V1"]["capture_sha256"] = "deadbeef"
    sidecar_path.write_text(yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8")
    try:
        result = _run_parser_cli(
            output_path,
            sidecar_path,
            expected_script_sha=script_sha,
            requested_at="2026-06-13T10:00:00+00:00",
        )
        parsed = yaml.safe_load(result.stdout)["SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"]
        assert parsed["status"] == "rejected"
        assert parsed["reject_reason"] == "capture_sha_mismatch"
    finally:
        output_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)


def _full_v3_completeness_block(item_count: int = 2) -> Dict[str, Any]:
    return {
        "page_count": 1,
        "item_count": item_count,
        "total_count": item_count,
        "pagination_complete": True,
        "sha256": "a" * 64,
    }


def _full_v3_budget_block() -> Dict[str, Any]:
    return {
        "page_count": 3,
        "response_bytes": 1234,
        "inventory_items": 2,
        "max_transaction_pages": 200,
        "max_response_bytes": 32_000_000,
        "max_inventory_items": 10_000,
        "deadline_seconds": 120.0,
    }


def test_rejected_when_marker_schema_version_3_has_no_inputs_block(tmp_path):
    """PR #1643 review (P0-3): the CURRENT runner always stamps
    marker_schema_version: 3. A marker declaring that discriminator must
    never be accepted as a "legacy v2" marker merely because `inputs` was
    stripped entirely -- that is exactly the silent-downgrade this contract
    must close."""
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(
        script_sha=script_sha,
        overrides={"marker_schema_version": 3},
    )
    result = _run_parser(
        marker,
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "inventory_completeness_contract_invalid"


def test_rejected_when_marker_schema_version_3_strips_only_query_schema_version(tmp_path):
    """A marker_schema_version: 3 marker that keeps every OTHER v3
    completeness/budget field but deletes just `query_schema_version` from
    `inputs` must still be rejected -- this is the exact gap the old
    `"query_schema_version" in inputs` gate left open."""
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(
        script_sha=script_sha,
        overrides={
            "marker_schema_version": 3,
            "inputs": {
                "current_issue_sha256": "deadbeef",
                "issues_all_sha256": "deadbeef",
                "prs_all_sha256": "deadbeef",
                "issue_count": 2,
                "pr_count": 0,
                # "query_schema_version": 3,  -- deliberately stripped
                "issues_completeness": _full_v3_completeness_block(2),
                "pull_requests_completeness": _full_v3_completeness_block(0),
                "transaction_budget": _full_v3_budget_block(),
            },
        },
    )
    result = _run_parser(
        marker,
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "inventory_completeness_contract_invalid"


def test_rejected_when_marker_schema_version_unsupported(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(
        script_sha=script_sha,
        overrides={"marker_schema_version": 99},
    )
    result = _run_parser(
        marker,
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "marker_schema_version_unsupported"


def test_ok_when_marker_schema_version_3_has_full_completeness_contract(tmp_path):
    """Positive counterpart: a marker_schema_version: 3 marker WITH the full
    completeness/budget contract present is accepted."""
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(
        script_sha=script_sha,
        overrides={
            "marker_schema_version": 3,
            "inputs": {
                "current_issue_sha256": "deadbeef",
                "issues_all_sha256": "deadbeef",
                "prs_all_sha256": "deadbeef",
                "issue_count": 2,
                "pr_count": 0,
                "query_schema_version": 3,
                "issues_completeness": _full_v3_completeness_block(2),
                "pull_requests_completeness": _full_v3_completeness_block(0),
                "transaction_budget": _full_v3_budget_block(),
            },
        },
    )
    with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(marker)
        output_path = Path(tmp.name)
    sidecar_path = output_path.with_suffix(".capture.yaml")
    sidecar_path.write_text(_render_capture_sidecar(output_path), encoding="utf-8")
    try:
        result = _run_parser_cli(
            output_path,
            sidecar_path,
            expected_script_sha=script_sha,
            requested_at="2026-06-13T10:00:00+00:00",
        )
        assert result.returncode == 0, result.stderr
        parsed = yaml.safe_load(result.stdout)["SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"]
        assert parsed["status"] == "ok"
    finally:
        output_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)


def test_ok_when_marker_schema_version_2_explicit_legacy_without_completeness(tmp_path):
    """An explicit marker_schema_version: 2 marker is accepted without the
    v3 completeness contract (explicit legacy discriminator, distinct from
    the absent-field legacy path)."""
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(
        script_sha=script_sha,
        overrides={
            "marker_schema_version": 2,
            "inputs": {
                "current_issue_sha256": "deadbeef",
                "issues_all_sha256": "deadbeef",
                "prs_all_sha256": "deadbeef",
                "issue_count": 0,
                "pr_count": 0,
            },
        },
    )
    with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(marker)
        output_path = Path(tmp.name)
    sidecar_path = output_path.with_suffix(".capture.yaml")
    sidecar_path.write_text(_render_capture_sidecar(output_path), encoding="utf-8")
    try:
        result = _run_parser_cli(
            output_path,
            sidecar_path,
            expected_script_sha=script_sha,
            requested_at="2026-06-13T10:00:00+00:00",
        )
        assert result.returncode == 0, result.stderr
        parsed = yaml.safe_load(result.stdout)["SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"]
        assert parsed["status"] == "ok"
    finally:
        output_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)

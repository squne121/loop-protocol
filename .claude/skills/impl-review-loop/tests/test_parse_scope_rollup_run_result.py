#!/usr/bin/env python3
"""Unit tests for parse_scope_rollup_run_result.py"""

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


def _build_plan_file(path: Path) -> tuple[str, str]:
    """Build a minimal valid ISSUE_SCOPE_ROLLUP_PLAN_V2 fixture and return expected sha.

    Returns:
        tuple[result_sha, payload_sha]
    """
    body = {
        "scope_rollup": "v2-fixture",
        "meta": {"source": "test"},
    }
    payload_sha = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()

    script_sha = _load_script_sha(PLAN_SCRIPT)

    payload = {
        "self_validation": {
            "schema_name": "ISSUE_SCOPE_ROLLUP_PLAN_V2",
            "schema_version": 2,
            "payload_sha256": payload_sha,
            "script_file_sha256": script_sha,
        },
        **body,
    }

    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(payload_text, encoding="utf-8")
    result_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return result_sha, payload_sha


def _render_marker(
    *,
    status: str = "ok",
    include_result: bool = True,
    requested_at: str = "2026-06-13T10:00:00+00:00",
    generated_at: str = "2026-06-13T10:01:00+00:00",
    overrides: Dict[str, Any] | None = None,
    script_sha: str | None = None,
    raw_plan_location: str = "/tmp/scope_rollup_inv-2026-06-13.json",
    result_sha: str | None = None,
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
        marker["result"] = {
            "plan_schema": "ISSUE_SCOPE_ROLLUP_PLAN_V2",
            "plan_schema_name": "ISSUE_SCOPE_ROLLUP_PLAN_V2",
            "plan_schema_version": 2,
            "raw_plan_location": raw_plan_location,
            "result_sha256": result_sha or "0000000000",
            "verify_status": "verified",
            "suggested_actions_summary": "No action\n",
            "candidate_count": 0,
            "high_confidence_count": 0,
        }

    if overrides:
        marker.update(overrides)

    return "```yaml\n" + yaml.safe_dump(
        {"ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1": marker},
        sort_keys=False,
        allow_unicode=True,
    ).rstrip() + "\n```"


def _run_parser(
    assistant_output: str,
    *,
    invocation_id: str = "inv-2026-06-13",
    issue_number: int = 841,
    expected_script_sha: str,
    requested_at: str,
) -> Dict[str, Any]:
    with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(assistant_output)
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            [
                sys.executable,
                str(PARSE_SCRIPT),
                "--assistant-output-file",
                str(tmp_path),
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

    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    assert result.returncode == 0, f"Parser command failed: {result.stderr}"

    parsed = yaml.safe_load(result.stdout)
    assert isinstance(parsed, dict)
    assert "SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1" in parsed
    return parsed["SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"]


def test_marker_missing_when_no_marker_block(tmp_path):
    output = "No fenced marker blocks."
    result = _run_parser(
        output,
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "marker_missing"
    assert result["routing_action"] == "stop_human"
    assert result["reject_reason"] == "marker_missing"


def test_marker_missing_when_marker_appears_in_prose_only(tmp_path):
    output = "The runner returned ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1 but not in fenced YAML."
    result = _run_parser(
        output,
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "marker_missing"


def test_marker_malformed_for_bad_yaml(tmp_path):
    output = "```yaml\n{ ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1: [invalid`\n```"
    result = _run_parser(
        output,
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "marker_malformed"
    assert result["routing_action"] == "stop_human"


def test_marker_ambiguous_when_duplicate_markers_present(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    marker = _render_marker(script_sha=script_sha)
    assistant_output = f"{marker}\n{marker}"

    result = _run_parser(
        assistant_output,
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "marker_ambiguous"
    assert result["routing_action"] == "stop_human"
    assert result["reject_reason"] == "marker_ambiguous"


def test_marker_malformed_when_single_block_has_duplicate_key():
    output = """```yaml
ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1:
  repo: squne121/loop-protocol
  status: ok
  current_issue: 841
  invocation_id: inv-2026-06-13
  requested_at: 2026-06-13T10:00:00+00:00
  generated_at: 2026-06-13T10:01:00+00:00
  script_blob_sha256: deadbeef
ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1:
  status: ok
  repo: squne121/loop-protocol
  current_issue: 841
  invocation_id: inv-2026-06-13
  requested_at: 2026-06-13T10:00:00+00:00
  generated_at: 2026-06-13T10:01:00+00:00
  script_blob_sha256: deadbeef
```
"""
    result = _run_parser(
        output,
        expected_script_sha="deadbeef",
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "marker_malformed"
    assert result["reject_reason"] == "marker_malformed"


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
    plan_path = tmp_path / "scope_rollup_inv-2026-06-13_result.json"
    result_sha, _ = _build_plan_file(plan_path)

    marker = _render_marker(
        script_sha=script_sha,
        raw_plan_location=str(plan_path),
        result_sha="deadbeef" + "00" * 30,
    )

    assert len(marker) > 0
    result = _run_parser(
        marker,
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "result_sha_mismatch"


def test_rejected_when_raw_plan_location_is_invalid(tmp_path):
    marker = _render_marker(
        raw_plan_location="/tmpfoo/scope_rollup_result.json",
        script_sha=_load_script_sha(PLAN_SCRIPT),
        result_sha="0000000000000000000000000000000000000000000000000000000000000000",
    )

    result = _run_parser(
        marker,
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "raw_plan_location_invalid"


def test_non_escation_for_runner_unavailable_marker(tmp_path):
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
    )
    assert result["status"] == "failed"
    assert result["routing_action"] == "stop_human"


def test_requested_at_mismatch_rejected(tmp_path):
    plan_path = tmp_path / "scope_rollup_inv-2026-06-13_result.json"
    result_sha, _ = _build_plan_file(plan_path)
    marker = _render_marker(
        script_sha=_load_script_sha(PLAN_SCRIPT),
        raw_plan_location=str(plan_path),
        result_sha=result_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )

    result = _run_parser(
        marker,
        expected_script_sha=_load_script_sha(PLAN_SCRIPT),
        requested_at="2026-06-13T09:59:00+00:00",
    )
    assert result["status"] == "rejected"
    assert result["reject_reason"] == "requested_at_mismatch"


def test_ok_for_valid_ok_marker(tmp_path):
    script_sha = _load_script_sha(PLAN_SCRIPT)
    plan_path = tmp_path / "scope_rollup_inv-2026-06-13_result.json"
    result_sha, _ = _build_plan_file(plan_path)

    marker = _render_marker(
        script_sha=script_sha,
        raw_plan_location=str(plan_path),
        result_sha=result_sha,
    )

    result = _run_parser(
        marker,
        expected_script_sha=script_sha,
        requested_at="2026-06-13T10:00:00+00:00",
    )
    assert result["status"] == "ok"
    assert result["routing_action"] == "continue"
    assert result["raw_plan_location_allowed"] is True

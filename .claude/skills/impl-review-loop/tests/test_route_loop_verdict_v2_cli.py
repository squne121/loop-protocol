"""
Behavior tests for the route_loop_verdict_v2.py CLI wrapper and the
LOOP_VERDICT_V2 fenced-YAML-block extraction helper.

Issue #1869 fix_delta (P0-1): the previously documented shell grep/sed
extraction in step-5-mergeability-handling.md only inspected the FIRST
fenced ```yaml block in a PR comment body. These tests exercise the real
production module (subprocess + import), not string-grep assertions, to
prove that:

  1. extract_latest_loop_verdict_v2 enumerates ALL fenced yaml blocks and
     selects the one containing the LOOP_VERDICT_V2 key, even when an
     unrelated yaml block precedes it in the same comment body.
  2. The CLI wrapper exits 0 for a well-formed invocation regardless of
     the resolved `route` (fail_closed / conflict_hard_stop are data, not
     process failures).
  3. mergeable == "CONFLICTING" and merge_state_status == "DIRTY" both
     resolve to conflict_hard_stop end-to-end through the CLI.
  4. merge_state_status == "CONFLICTING" is rejected as schema_invalid.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

IMPL_REVIEW_LOOP_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = IMPL_REVIEW_LOOP_DIR / "scripts"
CLI_SCRIPT = SCRIPTS_DIR / "route_loop_verdict_v2.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from route_loop_verdict_v2 import extract_latest_loop_verdict_v2  # noqa: E402


# ---------------------------------------------------------------------------
# extract_latest_loop_verdict_v2 (pure function)
# ---------------------------------------------------------------------------


def test_extraction_skips_leading_unrelated_yaml_block():
    body = (
        "Some prose before.\n\n"
        "```yaml\n"
        "unrelated_block: true\n"
        "```\n\n"
        "Here is the verdict:\n\n"
        "```yaml\n"
        "LOOP_VERDICT_V2:\n"
        "  verdict: APPROVE\n"
        "  merge_ready: true\n"
        "  reviewed_head_sha: abc123\n"
        "  mergeability:\n"
        "    mergeable: MERGEABLE\n"
        "    merge_state_status: CLEAN\n"
        "  required_auto_actions: []\n"
        "```\n"
    )
    loop_verdict, error = extract_latest_loop_verdict_v2(body)
    assert error is None
    assert loop_verdict is not None
    assert loop_verdict["verdict"] == "APPROVE"


def test_extraction_selects_last_matching_block_among_multiple():
    body = (
        "```yaml\n"
        "LOOP_VERDICT_V2:\n"
        "  verdict: REQUEST_CHANGES\n"
        "  merge_ready: false\n"
        "  reviewed_head_sha: old000\n"
        "  mergeability: {mergeable: MERGEABLE, merge_state_status: CLEAN}\n"
        "  required_auto_actions: []\n"
        "```\n\n"
        "```yaml\n"
        "LOOP_VERDICT_V2:\n"
        "  verdict: APPROVE\n"
        "  merge_ready: true\n"
        "  reviewed_head_sha: new111\n"
        "  mergeability: {mergeable: MERGEABLE, merge_state_status: CLEAN}\n"
        "  required_auto_actions: []\n"
        "```\n"
    )
    loop_verdict, error = extract_latest_loop_verdict_v2(body)
    assert error is None
    assert loop_verdict["reviewed_head_sha"] == "new111"


def test_extraction_reports_no_block_found():
    loop_verdict, error = extract_latest_loop_verdict_v2("no yaml blocks here at all")
    assert loop_verdict is None
    assert error == "no_fenced_yaml_block_found"


def test_extraction_reports_no_loop_verdict_v2_key():
    body = "```yaml\nsomething_else: true\n```\n"
    loop_verdict, error = extract_latest_loop_verdict_v2(body)
    assert loop_verdict is None
    assert error == "no_loop_verdict_v2_key_in_any_fenced_block"


# ---------------------------------------------------------------------------
# CLI wrapper (subprocess end-to-end)
# ---------------------------------------------------------------------------


def _run_cli(tmp_path: Path, body: str) -> dict:
    body_file = tmp_path / "comment.txt"
    body_file.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "--body-file", str(body_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"CLI exited {proc.returncode}, stderr: {proc.stderr}"
    return json.loads(proc.stdout)


def test_cli_exits_zero_and_routes_conflict_hard_stop_for_mergeable_conflicting(tmp_path):
    body = (
        "```yaml\n"
        "LOOP_VERDICT_V2:\n"
        "  verdict: APPROVE\n"
        "  merge_ready: false\n"
        "  reviewed_head_sha: abc123\n"
        "  mergeability:\n"
        "    mergeable: CONFLICTING\n"
        "    merge_state_status: DIRTY\n"
        "  required_auto_actions: []\n"
        "```\n"
    )
    output = _run_cli(tmp_path, body)
    assert output["route"] == "conflict_hard_stop"
    assert output["fail_closed"] is False
    assert output["reason_code"].startswith("conflict_mergeable_CONFLICTING")


def test_cli_exits_zero_and_routes_conflict_hard_stop_for_merge_state_status_dirty(tmp_path):
    body = (
        "```yaml\n"
        "LOOP_VERDICT_V2:\n"
        "  verdict: REQUEST_CHANGES\n"
        "  merge_ready: false\n"
        "  reviewed_head_sha: abc123\n"
        "  mergeability:\n"
        "    mergeable: UNKNOWN\n"
        "    merge_state_status: DIRTY\n"
        "  required_auto_actions: []\n"
        "```\n"
    )
    output = _run_cli(tmp_path, body)
    assert output["route"] == "conflict_hard_stop"
    assert output["reason_code"].startswith("conflict_merge_state_status_DIRTY")


def test_cli_rejects_merge_state_status_conflicting_as_schema_invalid(tmp_path):
    body = (
        "```yaml\n"
        "LOOP_VERDICT_V2:\n"
        "  verdict: APPROVE\n"
        "  merge_ready: false\n"
        "  reviewed_head_sha: abc123\n"
        "  mergeability:\n"
        "    mergeable: MERGEABLE\n"
        "    merge_state_status: CONFLICTING\n"
        "  required_auto_actions: []\n"
        "```\n"
    )
    output = _run_cli(tmp_path, body)
    assert output["route"] == "fail_closed"
    assert output["fail_closed"] is True
    assert "schema_invalid_merge_state_status_value:CONFLICTING" in output["reason_code"]


def test_cli_exits_zero_even_when_no_loop_verdict_v2_block_present(tmp_path):
    """extraction failure is data (fail_closed route), not a process crash."""
    output = _run_cli(tmp_path, "no verdict here")
    assert output["route"] == "fail_closed"
    assert output["fail_closed"] is True
    assert output["extraction_error"] == "no_fenced_yaml_block_found"


def test_cli_nonzero_exit_when_body_file_missing(tmp_path):
    missing = tmp_path / "does-not-exist.txt"
    proc = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "--body-file", str(missing)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0

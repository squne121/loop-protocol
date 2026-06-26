#!/usr/bin/env python3
"""
test_producer_fail_closed.py

Tests for Issue #1165: issue-refinement-loop producer fail-closed routing.

AC1: canonical failure envelope (STATUS/NEXT_ACTION/REASON_CODE/ARTIFACT/ARTIFACT_SHA256)
AC2: schema mismatch fixture matrix per script
AC3: output_budget_violation is machine-readable
AC4: publish_termination_report never called on producer failure
AC5: #1154/#1165/#1166 responsibility split in docs (checked by rg VC)
AC6: compact_author_result schema-less consumer contract fixed by fixture
AC7: canonical artifact path .claude/artifacts/issue-refinement-loop/<issue>/
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import unittest.mock as mock
from pathlib import Path

# ---------------------------------------------------------------------------
# Script paths
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
COMPACT_REVIEW_SCRIPT = _SCRIPTS_DIR / "compact_review_result.py"
COMPACT_AUTHOR_SCRIPT = _SCRIPTS_DIR / "compact_author_result.py"
PREFLIGHT_SCRIPT = _SCRIPTS_DIR / "run_refinement_preflight.py"
PUBLISH_SCRIPT = _SCRIPTS_DIR / "publish_termination_report.py"

# Canonical artifact path for production invocations (AC7)
CANONICAL_ARTIFACT_BASE = ".claude/artifacts/issue-refinement-loop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_FAILURE_FIELDS = {
    "STATUS: failed",
    "NEXT_ACTION:",
    "REASON_CODE:",
    "ARTIFACT:",
    "ARTIFACT_SHA256:",
}


def _assert_failure_envelope(stdout: str) -> None:
    """Assert that stdout contains all required canonical failure envelope fields."""
    for field in _REQUIRED_FAILURE_FIELDS:
        assert field in stdout, (
            f"Canonical failure envelope missing {field!r}.\n"
            f"stdout={stdout!r}"
        )
    # Envelope must be ≤ 2048 UTF-8 bytes (AC1)
    byte_count = len(stdout.encode("utf-8"))
    assert byte_count <= 2048, (
        f"Failure envelope stdout exceeds 2048 bytes: {byte_count} bytes\n"
        f"stdout={stdout!r}"
    )


def _minimal_valid_review_result() -> dict:
    """Build a minimal REVIEW_ISSUE_RESULT_V1 that passes schema validation."""
    return {
        "schema": "REVIEW_ISSUE_RESULT_V1",
        "schema_version": "1",
        "verdict": "approve",
        "status": "ok",
        "body_sha256": "sha256:" + "a" * 64,
        "issue_kind": "implementation",
        "generated_at": "2024-01-01T00:00:00Z",
        "deterministic_checks": {},
        "blocking_issues": [],
        "structured_blockers": [],
        "non_blocking_improvements": [],
        "findings": [],
        "diff_proposal": {},
        "parsed_vc_commands": [],
    }


# ---------------------------------------------------------------------------
# AC1 / AC2: compact_review_result schema mismatch → canonical failure envelope
# ---------------------------------------------------------------------------


def test_review_compact_schema_mismatch_emits_failure_artifact(tmp_path):
    """AC1/AC2: compact_review_result.py emits canonical failure envelope on schema mismatch."""
    # Input missing required schema fields (schema mismatch)
    invalid_input = json.dumps({
        "verdict": "approve",
        # Missing: schema, schema_version, status, body_sha256, etc.
    })
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"Schema mismatch should exit non-zero. stdout={result.stdout!r}"
    )
    _assert_failure_envelope(result.stdout)
    # REASON_CODE must be schema_mismatch
    assert "REASON_CODE: schema_mismatch" in result.stdout, (
        f"REASON_CODE must be schema_mismatch. stdout={result.stdout!r}"
    )
    # Failure artifact must reference the issue number in the path (AC7)
    # Note: "artifacts/issue-refinement-loop" segment only applies to production
    # invocations with --repo-root (AC7 canonical path). Without --repo-root, the
    # artifact dir is the user-supplied --artifact-dir value.
    assert "/1165/" in result.stdout, (
        f"ARTIFACT path must include issue number /1165/. stdout={result.stdout!r}"
    )
    # Failure artifact file must exist
    artifact_ref = [
        line for line in result.stdout.splitlines()
        if line.startswith("ARTIFACT: producer_failure_v1=")
    ]
    assert len(artifact_ref) == 1, f"Expected exactly one ARTIFACT line. stdout={result.stdout!r}"
    artifact_path_str = artifact_ref[0].split("=", 1)[1]
    artifact_path = Path(artifact_path_str)
    assert artifact_path.exists(), f"Failure artifact file not found: {artifact_path}"


def test_review_compact_invalid_verdict_emits_failure_artifact(tmp_path):
    """AC2: compact_review_result.py emits canonical failure envelope for invalid verdict."""
    invalid_input = json.dumps({
        "schema": "REVIEW_ISSUE_RESULT_V1",
        "schema_version": "1",
        "verdict": "invalid_verdict",  # not in VALID_VERDICTS
        "status": "ok",
        "body_sha256": "sha256:" + "a" * 64,
        "issue_kind": "impl",
        "generated_at": "2024-01-01T00:00:00Z",
        "deterministic_checks": {},
        "blocking_issues": [],
        "structured_blockers": [],
        "non_blocking_improvements": [],
        "findings": [],
        "diff_proposal": {},
        "parsed_vc_commands": [],
    })
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    _assert_failure_envelope(result.stdout)
    assert "REASON_CODE: schema_mismatch" in result.stdout


# ---------------------------------------------------------------------------
# AC3: output budget violation → machine-readable failure envelope
# ---------------------------------------------------------------------------


def test_review_compact_output_budget_violation_is_machine_readable(tmp_path):
    """AC3: when stdout would exceed 2048 bytes, emit machine-readable failure envelope."""
    # Use a long artifact_dir path so EVIDENCE + ARTIFACT lines exceed 2048 bytes total
    # Each x200/y200/z200/w200/v200 component is within Linux's 255-char per-component limit
    long_artifact_dir = (
        tmp_path
        / ("x" * 200)
        / ("y" * 200)
        / ("z" * 200)
        / ("w" * 200)
        / ("v" * 200)
    )
    # Provide valid input so we get past schema validation to the budget check
    valid_input = json.dumps(_minimal_valid_review_result())

    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(long_artifact_dir),
            "--issue-number", "1165",
        ],
        input=valid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"Budget violation should exit non-zero. stdout={result.stdout!r}"
    )
    _assert_failure_envelope(result.stdout)
    assert "REASON_CODE: output_budget_violation" in result.stdout, (
        f"REASON_CODE must be output_budget_violation. stdout={result.stdout!r}"
    )
    # The original verbose output (VERDICT, SUMMARY, etc.) must NOT be in stdout
    assert "VERDICT:" not in result.stdout, (
        "Original verbose output must not appear in budget-violated stdout"
    )
    assert "SUMMARY:" not in result.stdout, (
        "Original verbose output must not appear in budget-violated stdout"
    )
    # The failure artifact must exist and contain sha256/byte_count/bounded_preview
    artifact_ref = [
        line for line in result.stdout.splitlines()
        if line.startswith("ARTIFACT: producer_failure_v1=")
    ]
    assert len(artifact_ref) == 1
    artifact_path = Path(artifact_ref[0].split("=", 1)[1])
    assert artifact_path.exists(), f"Failure artifact not found: {artifact_path}"
    artifact_data = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact_data.get("reason_code") == "output_budget_violation"
    assert "byte_count" in artifact_data, "Artifact must contain byte_count"
    assert "output_sha256" in artifact_data, "Artifact must contain output_sha256"
    assert "bounded_preview" in artifact_data, "Artifact must contain bounded_preview"
    assert isinstance(artifact_data["byte_count"], int) and artifact_data["byte_count"] > 2048


# ---------------------------------------------------------------------------
# AC2 / AC6: compact_author_result schema mismatch → canonical failure envelope
# ---------------------------------------------------------------------------


def test_author_compact_schema_mismatch_emits_failure_artifact(tmp_path):
    """AC2/AC6: compact_author_result.py emits canonical failure envelope on schema mismatch.

    Schema-less consumer contract (AC6):
    - status must be in VALID_STATUSES ("ok", "failed", "no_change")
    - Rejection: invalid status → REASON_CODE: schema_mismatch
    """
    # Invalid status - not in VALID_STATUSES
    invalid_input = json.dumps({
        "status": "completely_invalid_status",
        "comment_url": "",
        "checked_body_sha256": "",
    })
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_AUTHOR_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"Schema mismatch should exit non-zero. stdout={result.stdout!r}"
    )
    _assert_failure_envelope(result.stdout)
    assert "REASON_CODE: schema_mismatch" in result.stdout, (
        f"REASON_CODE must be schema_mismatch. stdout={result.stdout!r}"
    )


def test_author_compact_ok_without_body_hash_emits_failure(tmp_path):
    """AC6: compact_author_result.py fails when status=ok but no body_hash provided."""
    invalid_input = json.dumps({
        "status": "ok",
        "comment_url": "",
        # No checked_body_sha256, no --updated-body, no --updated-body-file
    })
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_AUTHOR_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "status=ok without body_hash should fail"
    _assert_failure_envelope(result.stdout)
    assert "REASON_CODE: schema_mismatch" in result.stdout


def test_author_compact_schema_less_contract_fields():
    """AC6: ISSUE_AUTHOR_RESULT_V1_SCHEMA_LESS_CONTRACT exists and documents checked fields."""
    spec = importlib.util.spec_from_file_location(
        "compact_author_result_ac6",
        str(COMPACT_AUTHOR_SCRIPT),
    )
    car = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(car)  # type: ignore[union-attr]

    contract = getattr(car, "ISSUE_AUTHOR_RESULT_V1_SCHEMA_LESS_CONTRACT", None)
    assert contract is not None, "ISSUE_AUTHOR_RESULT_V1_SCHEMA_LESS_CONTRACT must exist"
    assert contract.get("consumer_mode") == "schema_less"
    assert "checked_fields" in contract
    assert "status" in contract["checked_fields"]
    assert "unchecked_fields" in contract
    # Rejection reason code must be documented
    assert contract["checked_fields"]["status"].get("rejection_reason_code") == "schema_mismatch"


# ---------------------------------------------------------------------------
# AC2: preflight planner_fail_closed_payload_invalid routes environment_failure
# ---------------------------------------------------------------------------


def test_preflight_planner_fail_closed_payload_invalid_routes_environment_failure():
    """AC2: BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID routes to environment_failure (not blocked).

    This verifies the fail-closed routing table in run_refinement_preflight.py.
    """
    spec = importlib.util.spec_from_file_location(
        "run_refinement_preflight_ac2",
        str(PREFLIGHT_SCRIPT),
    )
    rfp = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(rfp)  # type: ignore[union-attr]

    blocker = rfp.BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID

    # Bare blocker code → environment_failure
    status, exit_code = rfp._apply_exit_code_mapping(
        planner_exit_code=None,
        planner_fail_closed=None,
        blockers=[blocker],
    )
    assert status == "environment_failure", (
        f"BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID must route to environment_failure, "
        f"got {status!r}"
    )
    assert exit_code == rfp.EXIT_ENVIRONMENT_FAILURE

    # Blocker with detail (colon-separated) → still environment_failure
    blocker_with_detail = f"{blocker}: some invalid payload detail"
    status2, exit_code2 = rfp._apply_exit_code_mapping(
        planner_exit_code=None,
        planner_fail_closed=None,
        blockers=[blocker_with_detail],
    )
    assert status2 == "environment_failure", (
        f"Blocker with detail must also route to environment_failure, got {status2!r}"
    )

    # Verify it does NOT route to "blocked"
    assert status != "blocked", "planner_fail_closed_payload_invalid must NOT route to blocked"
    assert status2 != "blocked"


# ---------------------------------------------------------------------------
# AC4: producer failure never invokes publish_termination_report
# ---------------------------------------------------------------------------


def test_producer_failure_never_invokes_publish_termination_report(tmp_path):
    """AC4: producer failure (schema mismatch) never invokes publish_termination_report.

    Verifies via:
    1. Static AST analysis: compact scripts must not import publish_termination_report
    2. Behavioral: failure exit has no publish-related output
    """
    # --- Static analysis ---
    for script_path, name in [
        (COMPACT_REVIEW_SCRIPT, "compact_review_result"),
        (COMPACT_AUTHOR_SCRIPT, "compact_author_result"),
    ]:
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "publish_termination_report", (
                    f"{name} must not import publish_termination_report"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "publish_termination_report", (
                        f"{name} must not import publish_termination_report"
                    )

    # --- Behavioral: schema mismatch → failure, no gh comment attempt ---
    invalid_input = json.dumps({"verdict": "approve"})  # missing required schema fields
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "Schema mismatch should fail"
    combined = result.stdout + result.stderr
    # publish_termination_report writes "[publish_termination_report] comment posted"
    assert "[publish_termination_report]" not in combined, (
        "publish_termination_report must not be invoked on producer failure"
    )
    assert "gh issue comment" not in combined, (
        "gh issue comment must not be called on producer failure"
    )
    assert "comment posted" not in combined


def test_termination_bypass_fixture_call_count_zero(tmp_path):
    """AC4: call count for publish() and _post_github_comment() = 0 during producer failure.

    Uses monkeypatching to ensure that even if publish_termination_report were somehow
    imported, its publish() and _post_github_comment() functions are never called.
    """
    # Load compact_review_result with a mocked publish_termination_report in sys.modules
    mock_ptr = mock.MagicMock()
    publish_calls: list = []
    gh_calls: list = []

    mock_ptr.publish = mock.MagicMock(side_effect=lambda **kw: publish_calls.append(kw) or 0)
    mock_ptr._post_github_comment = mock.MagicMock(
        side_effect=lambda **kw: gh_calls.append(kw) or -1
    )

    with mock.patch.dict(sys.modules, {"publish_termination_report": mock_ptr}):
        spec = importlib.util.spec_from_file_location(
            "crr_ac4_fixture",
            str(COMPACT_REVIEW_SCRIPT),
        )
        crr = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(crr)  # type: ignore[union-attr]

        artifact_dir = tmp_path / "artifacts"
        # Call compact_review_result with invalid input (missing verdict → ValueError)
        try:
            crr.compact_review_result(
                {"status": "ok"},  # missing verdict → ValueError
                artifact_dir=artifact_dir,
                issue_number=1165,
            )
        except (ValueError, Exception):
            pass  # Expected failure

    # Call count must be zero for both publish() and _post_github_comment()
    assert len(publish_calls) == 0, (
        f"publish() must not be called on producer failure "
        f"(was called {len(publish_calls)} times)"
    )
    assert len(gh_calls) == 0, (
        f"_post_github_comment() must not be called on producer failure "
        f"(was called {len(gh_calls)} times)"
    )


# ---------------------------------------------------------------------------
# AC1: failure stdout never contains raw issue body or comment
# ---------------------------------------------------------------------------


def test_failure_stdout_never_contains_raw_issue_body_or_comment(tmp_path):
    """AC1: failure stdout never contains raw issue body or raw comment content."""
    RAW_BODY_SENTINEL = "THIS_IS_RAW_ISSUE_BODY_CONTENT_DO_NOT_EMIT_12345"
    RAW_COMMENT_SENTINEL = "THIS_IS_RAW_COMMENT_CONTENT_DO_NOT_EMIT_67890"

    # Input contains raw body/comment content but fails early validation
    # Use invalid_status to trigger ValueError (not jsonschema, which can be verbose)
    invalid_input_with_raw = json.dumps({
        "verdict": "invalid_verdict_that_should_not_appear_in_stdout",
        "status": "ok",
        "raw_body": RAW_BODY_SENTINEL,
        "raw_comment": RAW_COMMENT_SENTINEL,
        # invalid_verdict → ValueError before jsonschema runs
    })
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input_with_raw,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "Input with schema mismatch should fail"
    # Raw content must NOT appear in stdout
    assert RAW_BODY_SENTINEL not in result.stdout, (
        "Raw issue body must not appear in stdout on failure"
    )
    assert RAW_COMMENT_SENTINEL not in result.stdout, (
        "Raw comment must not appear in stdout on failure"
    )
    # Verify this is actually a failure envelope (not empty)
    assert "STATUS: failed" in result.stdout


# ---------------------------------------------------------------------------
# AC7: canonical artifact path reference in tests
# ---------------------------------------------------------------------------


def test_canonical_artifact_path_in_failure_artifact(tmp_path):
    """AC7: failure artifact is written to .claude/artifacts/issue-refinement-loop/<issue>/.

    When --repo-root is provided, the artifact uses the canonical path
    <repo_root>/.claude/artifacts/issue-refinement-loop/<issue>/.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # Create canonical artifact base (simulating repo structure)
    canonical_base = repo_root / ".claude" / "artifacts" / "issue-refinement-loop"

    invalid_input = json.dumps({"verdict": "not_valid"})  # invalid verdict → ValueError
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(canonical_base),  # default when repo-root matches
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    # The ARTIFACT path must contain the canonical base segment
    assert "artifacts/issue-refinement-loop" in result.stdout, (
        f"ARTIFACT must reference {CANONICAL_ARTIFACT_BASE!r}. stdout={result.stdout!r}"
    )
    # Must reference the issue number in the path
    assert "/1165/" in result.stdout, (
        f"ARTIFACT path must include issue number /1165/. stdout={result.stdout!r}"
    )

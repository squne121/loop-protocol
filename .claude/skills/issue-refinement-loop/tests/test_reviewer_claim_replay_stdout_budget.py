"""Tests for reviewer_claim_replay.py AC15 (Issue #1515): stdout OUTPUT_BUDGET_V1.

`next_state` (embedded as `REPLAY_NEXT_STATE` by issue-reviewer.md) must be
included in reviewer_claim_replay.py's CLI stdout while the overall stdout
still respects the pre-existing 2048 UTF-8 byte budget (via the existing
trim path), and must itself stay small enough to leave headroom for the
other ISSUE_REVIEW_RESULT_COMPACT_V1 fields sharing the same budget.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "reviewer_claim_replay.py"

IDENTITY_ARGS = [
    "--repository-full-name",
    "squne121/loop-protocol",
    "--issue-number",
    "1021",
    "--refinement-session-id",
    "session-abcdef1234567890",
]


def _run_cli(
    tmp_path: Path,
    review: dict,
    readiness: dict,
    *,
    previous_state_inline: dict | None = None,
    with_identity: bool = True,
) -> subprocess.CompletedProcess[str]:
    review_path = tmp_path / "review.json"
    readiness_path = tmp_path / "readiness.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
    args = [
        sys.executable,
        str(SCRIPT_PATH),
        "--review-result-file",
        str(review_path),
        "--readiness-result-file",
        str(readiness_path),
    ]
    if previous_state_inline is not None:
        args += ["--previous-state-inline", json.dumps(previous_state_inline)]
    if with_identity:
        args += IDENTITY_ARGS
    return subprocess.run(args, capture_output=True, text=True, timeout=15)


def test_next_state_included_within_output_budget(tmp_path: Path):
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [{"code": "C4", "message": "missing $ prefix"}],
        "structured_blockers": [],
    }
    readiness = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": "sha256:body-a",
        "errors": [],
    }
    proc = _run_cli(tmp_path, review, readiness, previous_state_inline={})
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert "next_state" in payload
    assert len(proc.stdout.encode("utf-8")) <= 2048

    next_state_bytes = len(
        json.dumps(payload["next_state"], separators=(",", ":")).encode("utf-8")
    )
    # Headroom check: next_state alone must stay well under the shared
    # 2048-byte budget so the other ISSUE_REVIEW_RESULT_COMPACT_V1 fields
    # (STATUS/VERDICT/SUMMARY/BLOCKERS/NEXT_ACTION/MUST_READ/EVIDENCE/
    # ARTIFACT + REPLAY_* fields) it is embedded alongside still fit.
    assert next_state_bytes <= 800


def test_next_state_survives_oversize_trimming(tmp_path: Path):
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [
            {"code": f"z_unregistered_{i}", "message": "x" * 200} for i in range(6)
        ],
        "structured_blockers": [],
    }
    readiness = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": "sha256:body-a",
        "errors": [],
    }
    proc = _run_cli(tmp_path, review, readiness, previous_state_inline={})
    assert proc.returncode == 0
    assert len(proc.stdout.encode("utf-8")) <= 2048
    payload = json.loads(proc.stdout)
    assert "next_state" in payload
    assert payload["next_state"]["schema"] == "REVIEWER_CLAIM_REPLAY_STATE_V2"


def test_next_state_present_without_identity_args_too(tmp_path: Path):
    """Legacy (no identity args, no --previous-state-inline) invocation
    still includes next_state in stdout (backward-compatible V1 shape)."""
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [{"code": "C4", "message": "missing $ prefix"}],
        "structured_blockers": [],
    }
    readiness = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": "sha256:body-a",
        "errors": [],
    }
    proc = _run_cli(tmp_path, review, readiness, with_identity=False)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert "next_state" in payload
    assert payload["next_state"]["schema"] == "REVIEWER_CLAIM_REPLAY_STATE_V1"
    assert len(proc.stdout.encode("utf-8")) <= 2048

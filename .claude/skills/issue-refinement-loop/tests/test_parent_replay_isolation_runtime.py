"""
test_parent_replay_isolation_runtime.py

Runtime E2E (Issue #1532 AC6, `runtime-verification: true`):
actual subprocess boundary between a simulated "child" (isolation worktree)
process that only sees its own private inventory and produces a bounded
REVIEWER_BLOCKER_CLAIM_V1 -- and a "parent" (orchestrator) process that
independently gathers its OWN parent-owned deterministic-checker inventory
in a SEPARATE directory the child process never has access to, replays
`parent_replay_binding.py` as a real child OS process, and validates the
assembled V2 envelope, including a tamper matrix that must fail closed to
`human_judgment_required`.

Per `docs/dev/runtime-verification-policy.md`, if the isolation-worktree
runtime cannot be started at all (e.g. `sys.executable` missing / import
failure of the production modules under test), tests SKIP with stdout
starting `SKIP:` and exit 77 semantics (pytest.skip here maps to that
policy at the pytest layer -- this module never converts a SKIP into a
PASS, and the fallback is never treated as pass/success).
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

PARENT_REPLAY_BINDING_SCRIPT = SCRIPTS_DIR / "parent_replay_binding.py"
VALIDATE_SCRIPT = SCRIPTS_DIR / "validate_review_compact_output.py"

if not PARENT_REPLAY_BINDING_SCRIPT.exists() or not VALIDATE_SCRIPT.exists():
    pytest.skip(
        "SKIP: parent_replay_binding.py / validate_review_compact_output.py not found "
        "-- isolation runtime cannot be started",
        allow_module_level=True,
    )

from parent_replay_binding import canonical_replay_next_state_line  # noqa: E402
from validate_review_compact_output import validate_review_compact_output_v2  # noqa: E402

READINESS_LP001 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "LP001",
            "source_check": "validate_issue_body",
            "category": "body_lint",
            "line_start": 1,
            "line_end": 1,
        }
    ],
}

COMPACT_MISSING_SECTION = {
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1532",
    "body_sha256": "sha256:body-a",
    "blocking_issues": [{"code": "missing_section", "message": "missing section"}],
    "structured_blockers": [],
    "findings": [],
}


def _run_child_process(child_dir: Path) -> dict:
    """Simulate the isolation-worktree child: it only has access to
    `child_dir` (its own private inventory), never to the parent's
    inventory directory. It returns a bounded REVIEWER_BLOCKER_CLAIM_V1
    (in this harness, represented as the child's own claimed
    `review_result` JSON dict) purely from its own private files -- an
    actual separate OS process (`subprocess.run`), not an in-process call.
    """
    child_script = child_dir / "child_claim.py"
    child_script.write_text(
        "import json, pathlib, sys\n"
        "review = json.loads((pathlib.Path(__file__).parent / 'private_review.json').read_text())\n"
        "sys.stdout.write(json.dumps(review))\n",
        encoding="utf-8",
    )
    (child_dir / "private_review.json").write_text(
        json.dumps(COMPACT_MISSING_SECTION), encoding="utf-8"
    )
    proc = subprocess.run(
        [sys.executable, str(child_script)],
        capture_output=True,
        text=True,
        cwd=str(child_dir),
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_parent_replay_binding_process(
    *, parent_dir: Path, review_result: dict, readiness_result: dict
) -> dict:
    """Real child OS process running the actual `parent_replay_binding.py`
    production script -- genuine subprocess isolation, not a mocked call.
    All inputs are written into `parent_dir` (a directory the simulated
    child process above never touched)."""
    review_file = parent_dir / "review_result.json"
    readiness_file = parent_dir / "readiness_result.json"
    review_file.write_text(json.dumps(review_result), encoding="utf-8")
    readiness_file.write_text(json.dumps(readiness_result), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(PARENT_REPLAY_BINDING_SCRIPT),
            "--review-result-file",
            str(review_file),
            "--readiness-result-file",
            str(readiness_file),
            "--previous-state-inline",
            "{}",
            "--repository-full-name",
            "squne121/loop-protocol",
            "--issue-number",
            "1532",
            "--refinement-session-id",
            "session-e2e",
            "--iteration-id",
            "iteration-e2e",
        ],
        capture_output=True,
        text=True,
        cwd=str(parent_dir),
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestParentChildBindingRuntimeE2E:
    """Issue #1532 AC6: actual isolation boundary runtime E2E, real
    subprocess, tamper matrix fails closed to human_judgment_required."""

    def test_parent_child_binding_runtime_e2e(self, tmp_path: Path):
        child_dir = tmp_path / "child_isolation_worktree"
        parent_dir = tmp_path / "parent_owned_inventory"
        child_dir.mkdir()
        parent_dir.mkdir()

        # 1) Child (isolation worktree, separate process) returns its
        #    bounded claim from files ONLY it can see.
        child_claim_review_result = _run_child_process(child_dir)

        # 2) Parent independently owns readiness_result (never read from
        #    the child's directory) and runs the REAL parent_replay_binding.py
        #    script as a separate OS process against a DIFFERENT directory.
        parent_binding_artifact = _run_parent_replay_binding_process(
            parent_dir=parent_dir,
            review_result=child_claim_review_result,
            readiness_result=READINESS_LP001,
        )
        assert parent_binding_artifact["schema"] == "PARENT_REPLAY_BINDING_ARTIFACT_V1"
        assert parent_binding_artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"

        # 3) Parent assembles the V2 envelope (child's V1 needs-fix text +
        #    parent-owned REPLAY_NEXT_STATE / REPLAY_PARENT_BINDING_DIGEST).
        replay_next_state_line = canonical_replay_next_state_line(parent_binding_artifact)
        v2_envelope = _assemble_v2_envelope(
            replay_artifact_digest="sha256:" + "1" * 64,
            replay_next_state_line=replay_next_state_line,
            replay_parent_binding_digest=parent_binding_artifact["binding_digest"],
        )

        result = validate_review_compact_output_v2(
            v2_envelope,
            issue_number=1532,
            expected_replay_next_state=replay_next_state_line,
            expected_parent_binding_digest=parent_binding_artifact["binding_digest"],
        )
        assert result["validation_status"] == "valid"

    def test_tampered_binding_digest_from_isolation_boundary_fails_closed(self, tmp_path: Path):
        """If a child (or an attacker impersonating the parent) supplies a
        REPLAY_PARENT_BINDING_DIGEST that does not match the orchestrator's
        OWN independently-computed digest, validation fails closed."""
        child_dir = tmp_path / "child_isolation_worktree"
        parent_dir = tmp_path / "parent_owned_inventory"
        child_dir.mkdir()
        parent_dir.mkdir()

        child_claim_review_result = _run_child_process(child_dir)
        parent_binding_artifact = _run_parent_replay_binding_process(
            parent_dir=parent_dir,
            review_result=child_claim_review_result,
            readiness_result=READINESS_LP001,
        )
        replay_next_state_line = canonical_replay_next_state_line(parent_binding_artifact)

        forged_digest = "sha256:" + "f" * 64
        v2_envelope = _assemble_v2_envelope(
            replay_artifact_digest="sha256:" + "1" * 64,
            replay_next_state_line=replay_next_state_line,
            replay_parent_binding_digest=forged_digest,
        )

        result = validate_review_compact_output_v2(
            v2_envelope,
            issue_number=1532,
            expected_replay_next_state=replay_next_state_line,
            expected_parent_binding_digest=parent_binding_artifact["binding_digest"],
        )
        assert result["validation_status"] == "invalid"
        assert result["next_action"] == "human_judgment_required"
        assert "replay_parent_binding_digest_mismatch" in {v["code"] for v in result["violations"]}

    def test_cross_issue_replay_binding_produces_a_different_digest(self, tmp_path: Path):
        """A binding artifact computed for a DIFFERENT issue_number must not
        collide with the one bound to the active issue (cross-issue replay
        rejection groundwork)."""
        parent_dir_a = tmp_path / "parent_a"
        parent_dir_b = tmp_path / "parent_b"
        parent_dir_a.mkdir()
        parent_dir_b.mkdir()

        review_file_a = parent_dir_a / "review_result.json"
        readiness_file_a = parent_dir_a / "readiness_result.json"
        review_file_a.write_text(json.dumps(COMPACT_MISSING_SECTION), encoding="utf-8")
        readiness_file_a.write_text(json.dumps(READINESS_LP001), encoding="utf-8")

        def _run(issue_number: str, parent_dir: Path, review_file: Path, readiness_file: Path) -> dict:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PARENT_REPLAY_BINDING_SCRIPT),
                    "--review-result-file",
                    str(review_file),
                    "--readiness-result-file",
                    str(readiness_file),
                    "--previous-state-inline",
                    "{}",
                    "--repository-full-name",
                    "squne121/loop-protocol",
                    "--issue-number",
                    issue_number,
                    "--refinement-session-id",
                    "session-e2e",
                    "--iteration-id",
                    "iteration-e2e",
                ],
                capture_output=True,
                text=True,
                cwd=str(parent_dir),
                timeout=15,
            )
            assert proc.returncode == 0, proc.stderr
            return json.loads(proc.stdout)

        artifact_1532 = _run("1532", parent_dir_a, review_file_a, readiness_file_a)

        review_file_b = parent_dir_b / "review_result.json"
        readiness_file_b = parent_dir_b / "readiness_result.json"
        review_result_b = copy.deepcopy(COMPACT_MISSING_SECTION)
        review_result_b["issue_url"] = "https://github.com/squne121/loop-protocol/issues/9999"
        review_file_b.write_text(json.dumps(review_result_b), encoding="utf-8")
        readiness_file_b.write_text(json.dumps(READINESS_LP001), encoding="utf-8")
        artifact_9999 = _run("9999", parent_dir_b, review_file_b, readiness_file_b)

        assert artifact_1532["binding_digest"] != artifact_9999["binding_digest"]


def _assemble_v2_envelope(
    *, replay_artifact_digest: str, replay_next_state_line: str, replay_parent_binding_digest: str
) -> str:
    artifact_path = ".claude/artifacts/issue-refinement-loop/1532/compact_review_result_20260716T000000Z.json"
    lines = [
        "STATUS: ok",
        "VERDICT: needs-fix",
        "SUMMARY: 1 blocker(s)",
        "BLOCKERS: 1",
        "NEXT_ACTION: request_changes",
        "MUST_READ: ",
        f"EVIDENCE: {artifact_path}",
        f"ARTIFACT: compact_review_result_v1={artifact_path}",
        "REPLAY_VERDICT: deterministic_fail_confirmed",
        "REPLAY_ROUTING: proceed_to_rewrite",
        "REPLAY_SHOULD_CONSUME: true",
        "REPLAY_BODY_SHA256: sha256:" + "0" * 64,
        f"REPLAY_ARTIFACT_DIGEST: {replay_artifact_digest}",
        f"REPLAY_NEXT_STATE: {replay_next_state_line}",
        f"REPLAY_PARENT_BINDING_DIGEST: {replay_parent_binding_digest}",
    ]
    return "\n".join(lines)

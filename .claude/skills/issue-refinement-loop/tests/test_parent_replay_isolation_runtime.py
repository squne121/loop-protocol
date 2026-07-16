"""
test_parent_replay_isolation_runtime.py

Runtime E2E (Issue #1532 AC6, `runtime-verification: true`):
actual subprocess boundary between a simulated "child" (isolation worktree)
process that only sees its own private inventory and produces a bounded,
schema-shaped `REVIEWER_BLOCKER_CLAIM_V1` via the REAL
`compact_review_result.py` production CLI -- and a "parent" (orchestrator)
process that independently gathers its OWN parent-owned deterministic-
checker inventory in a SEPARATE directory the child process never has
access to, replays `parent_replay_binding.py` as a real child OS process,
and validates the assembled V2 envelope via the REAL
`validate_review_compact_output.py --v2` CLI, including a tamper matrix
that must fail closed to `human_judgment_required`.

Issue #1532 Blocker 3.4: unlike a prior iteration of this test, the
simulated child does NOT fabricate its claim via ad-hoc JSON echo, and the
validator step is invoked through the actual production CLI (subprocess),
not a bare Python function call -- this exercises the real
producer -> parent replay -> V2 validator -> `--write-v2` command chain.

Per `docs/dev/runtime-verification-policy.md`, if the isolation-worktree
runtime cannot be started at all (e.g. `sys.executable` missing / import
failure of the production modules under test), tests SKIP with stdout
starting `SKIP:` and exit 77 semantics (pytest.skip here maps to that
policy at the pytest layer -- this module never converts a SKIP into a
PASS, and the fallback is never treated as pass/success).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
FIXTURES_DIR = SKILL_ROOT / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

COMPACT_REVIEW_RESULT_SCRIPT = SCRIPTS_DIR / "compact_review_result.py"
PARENT_REPLAY_BINDING_SCRIPT = SCRIPTS_DIR / "parent_replay_binding.py"
VALIDATE_SCRIPT = SCRIPTS_DIR / "validate_review_compact_output.py"
STATE_STORE_SCRIPT = SCRIPTS_DIR / "reviewer_claim_replay_state_store.py"

_REQUIRED_SCRIPTS = (
    COMPACT_REVIEW_RESULT_SCRIPT,
    PARENT_REPLAY_BINDING_SCRIPT,
    VALIDATE_SCRIPT,
    STATE_STORE_SCRIPT,
)
if not all(p.exists() for p in _REQUIRED_SCRIPTS):
    pytest.skip(
        "SKIP: production skill scripts not found -- isolation runtime cannot be started",
        allow_module_level=True,
    )

def _review_result_needs_fix(body_sha256: str) -> dict:
    """Real REVIEW_ISSUE_RESULT_V1-shaped fixture (schema-valid against
    `review-issue/schemas/review_issue_result_v1.json`, per the existing
    `review_result_needs_fix.json` fixture used elsewhere in this skill's
    test suite), with `body_sha256` and `blocking_issues` overridden to a
    taxonomy-known code so the parent's replay is deterministically
    backed."""
    raw = json.loads((FIXTURES_DIR / "review_result_needs_fix.json").read_text(encoding="utf-8"))
    raw["body_sha256"] = body_sha256
    raw["blocking_issues"] = [{"code": "missing_section", "message": "missing section"}]
    return raw


def _readiness_lp001(body_sha256: str) -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": body_sha256,
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


def _run_child_compact_review_result(
    *, child_dir: Path, review_result: dict
) -> tuple[str, dict]:
    """REAL child (isolation worktree) OS process: runs the production
    `compact_review_result.py` CLI against a private input file only this
    process sees. Returns (stdout_text, parsed REVIEWER_BLOCKER_CLAIM_V1)."""
    input_file = child_dir / "raw_review_result.json"
    input_file.write_text(json.dumps(review_result), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(COMPACT_REVIEW_RESULT_SCRIPT),
            "--input-file",
            str(input_file),
            "--issue-number",
            "1532",
            "--repo-root",
            str(child_dir),
        ],
        capture_output=True,
        text=True,
        cwd=str(child_dir),
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    stdout_text = proc.stdout.rstrip("\n")
    claim_line = next(
        line for line in stdout_text.split("\n") if line.startswith("REVIEWER_BLOCKER_CLAIM: ")
    )
    claim = json.loads(claim_line[len("REVIEWER_BLOCKER_CLAIM: ") :])
    return stdout_text, claim


def _run_parent_replay_binding_process(
    *,
    parent_dir: Path,
    reviewer_blocker_claim: dict,
    readiness_result: dict,
    current_body_bytes: bytes,
    issue_number: str = "1532",
    iteration_id: str = "iteration-e2e",
) -> dict:
    """Real child OS process running the actual `parent_replay_binding.py`
    production script -- genuine subprocess isolation, not a mocked call.
    All inputs are written into `parent_dir` (a directory the simulated
    child process above never touched)."""
    claim_file = parent_dir / "reviewer_blocker_claim.json"
    readiness_file = parent_dir / "readiness_result.json"
    body_file = parent_dir / "current_body.txt"
    claim_file.write_text(json.dumps(reviewer_blocker_claim), encoding="utf-8")
    readiness_file.write_text(json.dumps(readiness_result), encoding="utf-8")
    body_file.write_bytes(current_body_bytes)

    proc = subprocess.run(
        [
            sys.executable,
            str(PARENT_REPLAY_BINDING_SCRIPT),
            "--reviewer-blocker-claim-file",
            str(claim_file),
            "--readiness-result-file",
            str(readiness_file),
            "--previous-state-inline",
            "{}",
            "--current-body-file",
            str(body_file),
            "--issue-url",
            "https://github.com/squne121/loop-protocol/issues/1532",
            "--repository-full-name",
            "squne121/loop-protocol",
            "--issue-number",
            issue_number,
            "--refinement-session-id",
            "session-e2e",
            "--iteration-id",
            iteration_id,
        ],
        capture_output=True,
        text=True,
        cwd=str(parent_dir),
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _assemble_v2_envelope(*, child_stdout_text: str, binding_artifact: dict) -> str:
    import parent_replay_binding as _pb

    replay_result = binding_artifact["replay_result"]
    extra = [
        f"PARENT_REPLAY_VERDICT: {replay_result['verdict']}",
        f"PARENT_REPLAY_ROUTING: {replay_result['routing']}",
        "PARENT_REPLAY_SHOULD_CONSUME: "
        + ("true" if replay_result["should_consume_iteration"] else "false"),
        f"PARENT_REPLAY_BODY_SHA256: {replay_result['body_sha256']}",
        f"PARENT_REPLAY_NEXT_STATE: {_pb.canonical_replay_next_state_line(binding_artifact)}",
        f"PARENT_REPLAY_BINDING_DIGEST: {binding_artifact['binding_digest']}",
    ]
    return child_stdout_text + "\n" + "\n".join(extra)


def _run_validator_cli_v2(
    *,
    run_dir: Path,
    envelope_text: str,
    binding_artifact: dict,
    current_body_bytes: bytes,
    issue_number: str = "1532",
    iteration_id: str = "iteration-e2e",
) -> tuple[int, dict]:
    """REAL `validate_review_compact_output.py --v2` production CLI --
    subprocess, not a bare function call (Blocker 3.4)."""
    binding_file = run_dir / "binding_artifact.json"
    body_file = run_dir / "current_body.txt"
    input_file = run_dir / "envelope.txt"
    binding_file.write_text(json.dumps(binding_artifact), encoding="utf-8")
    body_file.write_bytes(current_body_bytes)
    input_file.write_text(envelope_text, encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATE_SCRIPT),
            "--v2",
            "--issue-number",
            issue_number,
            "--input-file",
            str(input_file),
            "--binding-artifact-file",
            str(binding_file),
            "--repository-full-name",
            "squne121/loop-protocol",
            "--refinement-session-id",
            "session-e2e",
            "--iteration-id",
            iteration_id,
            "--current-body-file",
            str(body_file),
        ],
        capture_output=True,
        text=True,
        cwd=str(run_dir),
        timeout=15,
    )
    return proc.returncode, json.loads(proc.stdout)


def _run_state_write_v2_cli(
    *, run_dir: Path, validation_result_v2: dict, expected_parent_binding_digest: str | None = None
) -> tuple[int, dict]:
    state_dir = run_dir / "state"
    state_dir.mkdir(exist_ok=True)
    normalized_payload = validation_result_v2.get("normalized_payload") or {}
    digest = expected_parent_binding_digest or normalized_payload.get(
        "PARENT_REPLAY_BINDING_DIGEST", "sha256:" + ("0" * 64)
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(STATE_STORE_SCRIPT),
            "--write-v2",
            "--state-dir",
            str(state_dir),
            "--repository-full-name",
            "squne121/loop-protocol",
            "--issue-number",
            "1532",
            "--refinement-session-id",
            "session-e2e",
            "--validation-result-v2-inline",
            json.dumps(validation_result_v2),
            "--expected-parent-binding-digest",
            digest,
        ],
        capture_output=True,
        text=True,
        cwd=str(run_dir),
        timeout=15,
    )
    return proc.returncode, json.loads(proc.stdout)


class TestParentChildBindingRuntimeE2E:
    """Issue #1532 AC6: actual isolation boundary runtime E2E through the
    REAL production command chain -- child compact_review_result.py CLI ->
    parent parent_replay_binding.py CLI -> validate_review_compact_output.py
    --v2 CLI -> reviewer_claim_replay_state_store.py --write-v2 CLI -- with
    a tamper matrix that fails closed to human_judgment_required."""

    def test_parent_child_binding_runtime_e2e_through_write_v2(self, tmp_path: Path):
        child_dir = tmp_path / "child_isolation_worktree"
        parent_dir = tmp_path / "parent_owned_inventory"
        validate_dir = tmp_path / "validate_run"
        child_dir.mkdir()
        parent_dir.mkdir()
        validate_dir.mkdir()

        current_body_bytes = b"the current live Issue body snapshot"
        current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
        review_result = _review_result_needs_fix(current_body_sha256)
        readiness_result = _readiness_lp001(current_body_sha256)

        # 1) CHILD (isolation worktree, separate process, REAL production
        #    CLI): returns its bounded REVIEWER_BLOCKER_CLAIM_V1 claim from
        #    files ONLY it can see.
        child_stdout_text, reviewer_blocker_claim = _run_child_compact_review_result(
            child_dir=child_dir, review_result=review_result
        )
        assert reviewer_blocker_claim["schema"] == "REVIEWER_BLOCKER_CLAIM_V1"
        assert "findings" not in reviewer_blocker_claim
        assert "checker_evidence" not in reviewer_blocker_claim

        # 2) PARENT (separate process, REAL production CLI, DIFFERENT
        #    directory): independently owns readiness_result and its own
        #    current-body snapshot; never reads the child's directory.
        binding_artifact = _run_parent_replay_binding_process(
            parent_dir=parent_dir,
            reviewer_blocker_claim=reviewer_blocker_claim,
            readiness_result=readiness_result,
            current_body_bytes=current_body_bytes,
        )
        assert binding_artifact["schema"] == "PARENT_REPLAY_BINDING_ARTIFACT_V1"
        assert binding_artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"

        # 3) ASSEMBLER: parent appends its own computed PARENT_REPLAY_*
        #    fields to the child's real stdout text.
        v2_envelope = _assemble_v2_envelope(
            child_stdout_text=child_stdout_text, binding_artifact=binding_artifact
        )

        # 4) VALIDATOR (REAL production CLI, subprocess).
        rc, validation_result = _run_validator_cli_v2(
            run_dir=validate_dir,
            envelope_text=v2_envelope,
            binding_artifact=binding_artifact,
            current_body_bytes=current_body_bytes,
        )
        assert rc == 0, validation_result
        assert validation_result["validation_status"] == "valid"

        # 5) STATE WRITE (REAL production CLI, subprocess) -- the final
        #    state bytes are verified.
        rc, write_result = _run_state_write_v2_cli(
            run_dir=validate_dir, validation_result_v2=validation_result
        )
        assert rc == 0, write_result
        assert write_result["status"] == "ok"
        assert write_result["state"]["consecutive_unbacked_count"] == 0
        state_file = validate_dir / "state" / "reviewer_claim_replay_state.json"
        assert state_file.exists()
        persisted = json.loads(state_file.read_text(encoding="utf-8"))
        assert persisted == binding_artifact["replay_next_state"]

    def test_tampered_binding_digest_from_isolation_boundary_fails_closed(self, tmp_path: Path):
        """If an attacker impersonating the parent supplies a
        PARENT_REPLAY_BINDING_DIGEST that does not match the orchestrator's
        OWN independently-computed digest, validation fails closed and the
        state file is never written."""
        child_dir = tmp_path / "child_isolation_worktree"
        parent_dir = tmp_path / "parent_owned_inventory"
        validate_dir = tmp_path / "validate_run"
        child_dir.mkdir()
        parent_dir.mkdir()
        validate_dir.mkdir()

        current_body_bytes = b"the current live Issue body snapshot"
        current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
        review_result = _review_result_needs_fix(current_body_sha256)
        readiness_result = _readiness_lp001(current_body_sha256)

        child_stdout_text, reviewer_blocker_claim = _run_child_compact_review_result(
            child_dir=child_dir, review_result=review_result
        )
        binding_artifact = _run_parent_replay_binding_process(
            parent_dir=parent_dir,
            reviewer_blocker_claim=reviewer_blocker_claim,
            readiness_result=readiness_result,
            current_body_bytes=current_body_bytes,
        )
        forged_artifact = dict(binding_artifact)
        forged_artifact["binding_digest"] = "sha256:" + "f" * 64
        v2_envelope = _assemble_v2_envelope(
            child_stdout_text=child_stdout_text, binding_artifact=forged_artifact
        )

        rc, validation_result = _run_validator_cli_v2(
            run_dir=validate_dir,
            envelope_text=v2_envelope,
            binding_artifact=forged_artifact,
            current_body_bytes=current_body_bytes,
        )
        assert rc == 1
        assert validation_result["validation_status"] == "invalid"
        assert validation_result["next_action"] == "human_judgment_required"
        assert "binding_artifact_digest_self_inconsistent" in {
            v["code"] for v in validation_result["violations"]
        }

        rc, write_result = _run_state_write_v2_cli(
            run_dir=validate_dir, validation_result_v2=validation_result
        )
        assert rc == 1
        assert write_result["status"] == "rejected"
        assert not (validate_dir / "state" / "reviewer_claim_replay_state.json").exists()

    def test_cross_issue_replay_binding_produces_a_different_digest(self, tmp_path: Path):
        """A binding artifact computed for a DIFFERENT issue_number must not
        collide with the one bound to the active issue (cross-issue replay
        rejection groundwork)."""
        current_body_bytes = b"the current live Issue body snapshot"
        current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
        review_result = _review_result_needs_fix(current_body_sha256)
        readiness_result = _readiness_lp001(current_body_sha256)

        child_dir = tmp_path / "child"
        child_dir.mkdir()
        _stdout, claim = _run_child_compact_review_result(child_dir=child_dir, review_result=review_result)

        parent_dir_a = tmp_path / "parent_a"
        parent_dir_b = tmp_path / "parent_b"
        parent_dir_a.mkdir()
        parent_dir_b.mkdir()

        artifact_1532 = _run_parent_replay_binding_process(
            parent_dir=parent_dir_a,
            reviewer_blocker_claim=claim,
            readiness_result=readiness_result,
            current_body_bytes=current_body_bytes,
            issue_number="1532",
        )
        artifact_9999 = _run_parent_replay_binding_process(
            parent_dir=parent_dir_b,
            reviewer_blocker_claim=claim,
            readiness_result=readiness_result,
            current_body_bytes=current_body_bytes,
            issue_number="9999",
        )
        assert artifact_1532["binding_digest"] != artifact_9999["binding_digest"]

    def test_cross_session_and_cross_iteration_binding_produces_different_digests(
        self, tmp_path: Path
    ):
        """A verifier must reject an artifact bound to a different
        refinement_session_id / iteration_id -- proven here at the digest
        level (an artifact recomputed for a DIFFERENT session/iteration
        never matches the one the validator expects)."""
        current_body_bytes = b"the current live Issue body snapshot"
        current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
        review_result = _review_result_needs_fix(current_body_sha256)
        readiness_result = _readiness_lp001(current_body_sha256)

        child_dir = tmp_path / "child"
        child_dir.mkdir()
        _stdout, claim = _run_child_compact_review_result(child_dir=child_dir, review_result=review_result)

        parent_dir_a = tmp_path / "parent_a"
        parent_dir_b = tmp_path / "parent_b"
        parent_dir_a.mkdir()
        parent_dir_b.mkdir()

        artifact_iter1 = _run_parent_replay_binding_process(
            parent_dir=parent_dir_a,
            reviewer_blocker_claim=claim,
            readiness_result=readiness_result,
            current_body_bytes=current_body_bytes,
            iteration_id="iteration-1",
        )
        artifact_iter2 = _run_parent_replay_binding_process(
            parent_dir=parent_dir_b,
            reviewer_blocker_claim=claim,
            readiness_result=readiness_result,
            current_body_bytes=current_body_bytes,
            iteration_id="iteration-2",
        )
        assert artifact_iter1["binding_digest"] != artifact_iter2["binding_digest"]

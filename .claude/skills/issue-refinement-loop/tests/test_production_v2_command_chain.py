"""
test_production_v2_command_chain.py

Issue #1541 AC7: production E2E through the REAL parent-owned command
chain -- child `compact_review_result.py` CLI -> `emit_parent_review_envelope_v2.py`
production CLI (NOT the test-only `_assemble_v2_envelope()` f-string helper
from `test_parent_replay_isolation_runtime.py`) -> `validate_review_compact_output.py
--v2` CLI -> `reviewer_claim_replay_state_store.py --write-v2` CLI.

Each step is a genuine subprocess invocation of the production script, not a
bare Python function call -- this exercises the real producer -> parent
binding -> emitter -> V2 validator -> state writer chain end to end,
including a tamper case that must fail closed before any state write.
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

COMPACT_REVIEW_RESULT_SCRIPT = SCRIPTS_DIR / "compact_review_result.py"
PARENT_REPLAY_BINDING_SCRIPT = SCRIPTS_DIR / "parent_replay_binding.py"
EMIT_SCRIPT = SCRIPTS_DIR / "emit_parent_review_envelope_v2.py"
VALIDATE_SCRIPT = SCRIPTS_DIR / "validate_review_compact_output.py"
STATE_STORE_SCRIPT = SCRIPTS_DIR / "reviewer_claim_replay_state_store.py"

_REQUIRED_SCRIPTS = (
    COMPACT_REVIEW_RESULT_SCRIPT,
    PARENT_REPLAY_BINDING_SCRIPT,
    EMIT_SCRIPT,
    VALIDATE_SCRIPT,
    STATE_STORE_SCRIPT,
)
if not all(p.exists() for p in _REQUIRED_SCRIPTS):
    pytest.skip(
        "SKIP: production skill scripts not found -- command chain cannot be started",
        allow_module_level=True,
    )

REPO = "squne121/loop-protocol"
ISSUE_NUMBER = "1541"
SESSION_ID = "session-1541-chain"
ITERATION_ID = "iteration-1541-chain"


def _review_result_needs_fix(body_sha256: str) -> dict:
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


def _run_child_compact_review_result(*, child_dir: Path, review_result: dict) -> tuple[str, dict]:
    input_file = child_dir / "raw_review_result.json"
    input_file.write_text(json.dumps(review_result), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(COMPACT_REVIEW_RESULT_SCRIPT),
            "--input-file",
            str(input_file),
            "--issue-number",
            ISSUE_NUMBER,
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
    *, parent_dir: Path, reviewer_blocker_claim: dict, readiness_result: dict, current_body_bytes: bytes
) -> dict:
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
            f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
            "--repository-full-name",
            REPO,
            "--issue-number",
            ISSUE_NUMBER,
            "--refinement-session-id",
            SESSION_ID,
            "--iteration-id",
            ITERATION_ID,
        ],
        capture_output=True,
        text=True,
        cwd=str(parent_dir),
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_emit_v2_cli(
    *, run_dir: Path, child_stdout_text: str, binding_artifact: dict, current_body_bytes: bytes
) -> tuple[int, bytes, str]:
    """REAL `emit_parent_review_envelope_v2.py` production CLI (subprocess)
    -- the production replacement for the test-only `_assemble_v2_envelope()`
    f-string helper."""
    binding_file = run_dir / "binding_artifact.json"
    body_file = run_dir / "current_body.txt"
    binding_file.write_text(json.dumps(binding_artifact), encoding="utf-8")
    body_file.write_bytes(current_body_bytes)

    proc = subprocess.run(
        [
            sys.executable,
            str(EMIT_SCRIPT),
            "--issue-number",
            ISSUE_NUMBER,
            "--binding-artifact-file",
            str(binding_file),
            "--repository-full-name",
            REPO,
            "--refinement-session-id",
            SESSION_ID,
            "--iteration-id",
            ITERATION_ID,
            "--current-body-file",
            str(body_file),
        ],
        input=(child_stdout_text + "\n").encode("utf-8"),
        capture_output=True,
        cwd=str(run_dir),
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace")


def _run_validator_cli_v2(
    *, run_dir: Path, envelope_bytes: bytes, binding_artifact: dict, current_body_bytes: bytes
) -> tuple[int, dict]:
    binding_file = run_dir / "binding_artifact.json"
    body_file = run_dir / "current_body.txt"
    input_file = run_dir / "envelope.txt"
    binding_file.write_text(json.dumps(binding_artifact), encoding="utf-8")
    body_file.write_bytes(current_body_bytes)
    input_file.write_bytes(envelope_bytes)

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATE_SCRIPT),
            "--v2",
            "--issue-number",
            ISSUE_NUMBER,
            "--input-file",
            str(input_file),
            "--binding-artifact-file",
            str(binding_file),
            "--repository-full-name",
            REPO,
            "--refinement-session-id",
            SESSION_ID,
            "--iteration-id",
            ITERATION_ID,
            "--current-body-file",
            str(body_file),
        ],
        capture_output=True,
        text=True,
        cwd=str(run_dir),
        timeout=15,
    )
    return proc.returncode, json.loads(proc.stdout)


def _run_state_write_v2_cli(*, run_dir: Path, validation_result_v2: dict) -> tuple[int, dict]:
    state_dir = run_dir / "state"
    state_dir.mkdir(exist_ok=True)
    normalized_payload = validation_result_v2.get("normalized_payload") or {}
    digest = normalized_payload.get("PARENT_REPLAY_BINDING_DIGEST", "sha256:" + ("0" * 64))
    proc = subprocess.run(
        [
            sys.executable,
            str(STATE_STORE_SCRIPT),
            "--write-v2",
            "--state-dir",
            str(state_dir),
            "--repository-full-name",
            REPO,
            "--issue-number",
            ISSUE_NUMBER,
            "--refinement-session-id",
            SESSION_ID,
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


def test_production_v2_command_chain(tmp_path: Path):
    """Issue #1541 AC7: the FULL production chain, using the emitter CLI
    (not a test-only assembler), from child stdout through to a persisted
    V2 state file."""
    child_dir = tmp_path / "child_isolation_worktree"
    parent_dir = tmp_path / "parent_owned_inventory"
    emit_dir = tmp_path / "emit_run"
    validate_dir = tmp_path / "validate_run"
    child_dir.mkdir()
    parent_dir.mkdir()
    emit_dir.mkdir()
    validate_dir.mkdir()

    current_body_bytes = b"the current live Issue #1541 body snapshot for the production chain"
    current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
    review_result = _review_result_needs_fix(current_body_sha256)
    readiness_result = _readiness_lp001(current_body_sha256)

    # 1) CHILD: real production CLI, private directory.
    child_stdout_text, reviewer_blocker_claim = _run_child_compact_review_result(
        child_dir=child_dir, review_result=review_result
    )
    assert reviewer_blocker_claim["schema"] == "REVIEWER_BLOCKER_CLAIM_V1"
    assert "findings" not in reviewer_blocker_claim

    # 2) PARENT BINDING: real production CLI, separate directory.
    binding_artifact = _run_parent_replay_binding_process(
        parent_dir=parent_dir,
        reviewer_blocker_claim=reviewer_blocker_claim,
        readiness_result=readiness_result,
        current_body_bytes=current_body_bytes,
    )
    assert binding_artifact["schema"] == "PARENT_REPLAY_BINDING_ARTIFACT_V1"
    assert binding_artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"

    # 3) EMITTER: real production CLI -- NOT `_assemble_v2_envelope()`.
    rc, envelope_bytes, emit_stderr = _run_emit_v2_cli(
        run_dir=emit_dir,
        child_stdout_text=child_stdout_text,
        binding_artifact=binding_artifact,
        current_body_bytes=current_body_bytes,
    )
    assert rc == 0, emit_stderr
    assert emit_stderr == ""
    assert envelope_bytes != b""
    envelope_text = envelope_bytes.decode("utf-8")
    assert envelope_text.count("\n") == 15
    assert "PARENT_REPLAY_BINDING_DIGEST: " + binding_artifact["binding_digest"] in envelope_text

    # 4) VALIDATOR: real production CLI, independent binding artifact copy.
    rc, validation_result = _run_validator_cli_v2(
        run_dir=validate_dir,
        envelope_bytes=envelope_bytes,
        binding_artifact=binding_artifact,
        current_body_bytes=current_body_bytes,
    )
    assert rc == 0, validation_result
    assert validation_result["validation_status"] == "valid"
    assert validation_result["envelope_kind"] == "needs_fix_v2"

    # 5) STATE WRITE: real production CLI.
    rc, write_result = _run_state_write_v2_cli(run_dir=validate_dir, validation_result_v2=validation_result)
    assert rc == 0, write_result
    assert write_result["status"] == "ok"
    state_file = validate_dir / "state" / "reviewer_claim_replay_state.json"
    assert state_file.exists()
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted == binding_artifact["replay_next_state"]


def test_production_v2_command_chain_tampered_binding_fails_before_state_write(tmp_path: Path):
    """A tampered binding artifact must be rejected by the emitter itself
    (contract-invalid, exit 1, empty stdout) -- the production chain never
    reaches the validator or the state writer with forged content."""
    child_dir = tmp_path / "child_isolation_worktree"
    parent_dir = tmp_path / "parent_owned_inventory"
    emit_dir = tmp_path / "emit_run"
    child_dir.mkdir()
    parent_dir.mkdir()
    emit_dir.mkdir()

    current_body_bytes = b"the current live Issue #1541 body snapshot for the tamper case"
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

    rc, envelope_bytes, emit_stderr = _run_emit_v2_cli(
        run_dir=emit_dir,
        child_stdout_text=child_stdout_text,
        binding_artifact=forged_artifact,
        current_body_bytes=current_body_bytes,
    )
    assert rc == 1
    assert envelope_bytes == b""
    diagnostic = json.loads(emit_stderr)
    assert diagnostic["schema"] == "EMIT_PARENT_REVIEW_ENVELOPE_V2_FAILURE"
    assert diagnostic["reason_code"] == "contract_invalid"
    assert any(v["code"] == "binding_artifact_digest_self_inconsistent" for v in diagnostic["violations"])

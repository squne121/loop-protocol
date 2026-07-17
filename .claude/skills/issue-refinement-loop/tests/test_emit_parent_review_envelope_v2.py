"""
test_emit_parent_review_envelope_v2.py

Issue #1541: unit/contract tests for `emit_parent_review_envelope_v2.py`,
the deterministic production replacement for the test-only
`_assemble_v2_envelope()` helper (see `test_parent_replay_isolation_runtime.py`).

Test functions are top-level (not class-nested) so that the Issue #1541
Verification Commands' pytest node ids
(`test_emit_parent_review_envelope_v2.py::test_<name>`) resolve exactly.
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

import emit_parent_review_envelope_v2 as emit_mod  # noqa: E402
import validate_review_compact_output as v1  # noqa: E402

REPO = "squne121/loop-protocol"
ISSUE_NUMBER = 1541
SESSION_ID = "session-1541"
ITERATION_ID = "iteration-1541"
CURRENT_BODY_BYTES = b"the current live Issue #1541 body snapshot"
CURRENT_BODY_SHA256 = f"sha256:{hashlib.sha256(CURRENT_BODY_BYTES).hexdigest()}"


def _review_result_needs_fix() -> dict:
    raw = json.loads((FIXTURES_DIR / "review_result_needs_fix.json").read_text(encoding="utf-8"))
    raw["body_sha256"] = CURRENT_BODY_SHA256
    raw["blocking_issues"] = [{"code": "missing_section", "message": "missing section"}]
    return raw


def _readiness_lp001() -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": CURRENT_BODY_SHA256,
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


def _run_child_compact_review_result(tmp_path: Path) -> tuple[str, dict]:
    child_dir = tmp_path / "child"
    child_dir.mkdir(exist_ok=True)
    input_file = child_dir / "raw_review_result.json"
    input_file.write_text(json.dumps(_review_result_needs_fix()), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(COMPACT_REVIEW_RESULT_SCRIPT),
            "--input-file",
            str(input_file),
            "--issue-number",
            str(ISSUE_NUMBER),
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


def _run_parent_replay_binding(tmp_path: Path, reviewer_blocker_claim: dict) -> dict:
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir(exist_ok=True)
    claim_file = parent_dir / "reviewer_blocker_claim.json"
    readiness_file = parent_dir / "readiness_result.json"
    body_file = parent_dir / "current_body.txt"
    claim_file.write_text(json.dumps(reviewer_blocker_claim), encoding="utf-8")
    readiness_file.write_text(json.dumps(_readiness_lp001()), encoding="utf-8")
    body_file.write_bytes(CURRENT_BODY_BYTES)

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
            str(ISSUE_NUMBER),
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


def _build_valid_pair(tmp_path: Path) -> tuple[str, dict]:
    """Return (child_needs_fix_intermediate_stdout_text, binding_artifact)."""
    child_stdout_text, claim = _run_child_compact_review_result(tmp_path)
    binding_artifact = _run_parent_replay_binding(tmp_path, claim)
    assert binding_artifact["schema"] == "PARENT_REPLAY_BINDING_ARTIFACT_V1"
    assert binding_artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"
    return child_stdout_text, binding_artifact


def _build_valid_approve_intermediate() -> str:
    return "\n".join(
        [
            "STATUS: ok",
            "VERDICT: approve",
            "SUMMARY: contract ready",
            "BLOCKERS: 0",
            "NEXT_ACTION: proceed",
            "MUST_READ: ",
            (
                f"EVIDENCE: .claude/artifacts/issue-refinement-loop/{ISSUE_NUMBER}/"
                f"compact_review_result_20260717T000000Z.json"
            ),
            (
                f"ARTIFACT: compact_review_result_v1=.claude/artifacts/issue-refinement-loop/"
                f"{ISSUE_NUMBER}/compact_review_result_20260717T000000Z.json"
            ),
        ]
    ) + "\n"


def test_rejects_invalid_child_intermediate():
    # Missing REVIEWER_BLOCKER_CLAIM on an otherwise-needs-fix envelope.
    bad = "\n".join(
        [
            "STATUS: ok",
            "VERDICT: needs-fix",
            "SUMMARY: 1 blocker(s)",
            "BLOCKERS: 1",
            "NEXT_ACTION: request_changes",
            "MUST_READ: ",
            (
                f"EVIDENCE: .claude/artifacts/issue-refinement-loop/{ISSUE_NUMBER}/"
                f"compact_review_result_20260717T000000Z.json"
            ),
            (
                f"ARTIFACT: compact_review_result_v1=.claude/artifacts/issue-refinement-loop/"
                f"{ISSUE_NUMBER}/compact_review_result_20260717T000000Z.json"
            ),
        ]
    ) + "\n"
    result = emit_mod.validate_child_intermediate(bad, issue_number=ISSUE_NUMBER)
    assert result["validation_status"] == "invalid"

    # A V2 FINAL envelope (with PARENT_REPLAY_* fields) submitted AS a
    # child intermediate must also be rejected -- those fields are
    # parent-only and unknown in the child grammar (Issue #1532 Blocker 2).
    forged = bad.rstrip("\n") + "\nPARENT_REPLAY_VERDICT: deterministic_fail_confirmed\n"
    forged_result = emit_mod.validate_child_intermediate(forged, issue_number=ISSUE_NUMBER)
    assert forged_result["validation_status"] == "invalid"

    # Prose injected before the envelope.
    prose = "Here is my review:\n" + _build_valid_approve_intermediate()
    prose_result = emit_mod.validate_child_intermediate(prose, issue_number=ISSUE_NUMBER)
    assert prose_result["validation_status"] == "invalid"

    # Code fence.
    fenced = "```\n" + _build_valid_approve_intermediate()
    fenced_result = emit_mod.validate_child_intermediate(fenced, issue_number=ISSUE_NUMBER)
    assert fenced_result["validation_status"] == "invalid"

    # Duplicate field.
    dup = _build_valid_approve_intermediate().rstrip("\n") + "\nSTATUS: ok\n"
    dup_result = emit_mod.validate_child_intermediate(dup, issue_number=ISSUE_NUMBER)
    assert dup_result["validation_status"] == "invalid"


def test_needs_fix_emission_is_byte_deterministic(tmp_path):
    child_stdout_text, binding_artifact = _build_valid_pair(tmp_path)
    result1 = emit_mod.emit_parent_review_envelope_v2(
        child_stdout_text,
        issue_number=ISSUE_NUMBER,
        binding_artifact=binding_artifact,
        repository_full_name=REPO,
        refinement_session_id=SESSION_ID,
        iteration_id=ITERATION_ID,
        current_body_bytes=CURRENT_BODY_BYTES,
    )
    result2 = emit_mod.emit_parent_review_envelope_v2(
        child_stdout_text,
        issue_number=ISSUE_NUMBER,
        binding_artifact=binding_artifact,
        repository_full_name=REPO,
        refinement_session_id=SESSION_ID,
        iteration_id=ITERATION_ID,
        current_body_bytes=CURRENT_BODY_BYTES,
    )
    assert result1["envelope_bytes"] == result2["envelope_bytes"]
    text = result1["envelope_bytes"].decode("utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert "\r" not in text
    # No BOM.
    assert not result1["envelope_bytes"].startswith(b"\xef\xbb\xbf")
    lines = text[:-1].split("\n")
    assert len(lines) == 15
    assert [line.split(":", 1)[0] for line in lines] == emit_mod.FINAL_V2_FIELDS


def test_needs_fix_uses_only_parent_replay_fields(tmp_path):
    child_stdout_text, binding_artifact = _build_valid_pair(tmp_path)
    result = emit_mod.emit_parent_review_envelope_v2(
        child_stdout_text,
        issue_number=ISSUE_NUMBER,
        binding_artifact=binding_artifact,
        repository_full_name=REPO,
        refinement_session_id=SESSION_ID,
        iteration_id=ITERATION_ID,
        current_body_bytes=CURRENT_BODY_BYTES,
    )
    text = result["envelope_bytes"].decode("utf-8")
    line_keys = [line.split(":", 1)[0] for line in text.rstrip("\n").split("\n")]
    # Old V1 child self-report fields must NEVER appear (Issue #1532
    # Blocker 2). Checked as exact field-line keys, not substrings --
    # "PARENT_REPLAY_VERDICT" legitimately CONTAINS "REPLAY_VERDICT" as
    # a substring, so a naive `"REPLAY_VERDICT:" not in text` assertion
    # would be a false positive here.
    assert "REPLAY_VERDICT" not in line_keys
    assert "REPLAY_ROUTING" not in line_keys
    assert "REPLAY_SHOULD_CONSUME" not in line_keys
    assert "REPLAY_BODY_SHA256" not in line_keys
    assert "REPLAY_ARTIFACT_DIGEST" not in line_keys
    replay_result = binding_artifact["replay_result"]
    assert f"PARENT_REPLAY_VERDICT: {replay_result['verdict']}" in text
    assert f"PARENT_REPLAY_ROUTING: {replay_result['routing']}" in text
    assert f"PARENT_REPLAY_BINDING_DIGEST: {binding_artifact['binding_digest']}" in text


def test_emitted_v2_envelope_validates_against_independent_binding(tmp_path):
    child_stdout_text, binding_artifact = _build_valid_pair(tmp_path)
    result = emit_mod.emit_parent_review_envelope_v2(
        child_stdout_text,
        issue_number=ISSUE_NUMBER,
        binding_artifact=binding_artifact,
        repository_full_name=REPO,
        refinement_session_id=SESSION_ID,
        iteration_id=ITERATION_ID,
        current_body_bytes=CURRENT_BODY_BYTES,
    )
    # Independently re-validate via the V2 validator, with a FRESH deep
    # copy of the binding artifact (never the SAME in-memory object the
    # emitter used) to prove no shared-state shortcut.
    independent_binding_artifact = json.loads(json.dumps(binding_artifact))
    validation = v1.validate_review_compact_output_v2(
        result["envelope_bytes"].decode("utf-8"),
        issue_number=ISSUE_NUMBER,
        binding_artifact=independent_binding_artifact,
        repository_full_name=REPO,
        refinement_session_id=SESSION_ID,
        iteration_id=ITERATION_ID,
        current_body_sha256=CURRENT_BODY_SHA256,
    )
    assert validation["validation_status"] == "valid", validation["violations"]
    assert validation["envelope_kind"] == "needs_fix_v2"


def test_v2_tamper_matrix_fails_closed(tmp_path):
    child_stdout_text, binding_artifact = _build_valid_pair(tmp_path)

    def _expect_contract_invalid(*, child_text=None, artifact=None, **kwargs):
        call_kwargs = dict(
            issue_number=ISSUE_NUMBER,
            binding_artifact=artifact if artifact is not None else binding_artifact,
            repository_full_name=REPO,
            refinement_session_id=SESSION_ID,
            iteration_id=ITERATION_ID,
            current_body_bytes=CURRENT_BODY_BYTES,
        )
        call_kwargs.update(kwargs)
        with pytest.raises(emit_mod.EmitContractError):
            emit_mod.emit_parent_review_envelope_v2(
                child_text if child_text is not None else child_stdout_text, **call_kwargs
            )

    # 1) claim tampered: swap in a claim-consistent-looking but DIFFERENT
    #    child stdout (different reviewer_blocker_code) than the one the
    #    binding artifact was built from.
    tampered_claim_line_child = child_stdout_text.replace("missing_section", "different_code")
    _expect_contract_invalid(child_text=tampered_claim_line_child)

    # 2) verdict tampered (binding artifact digest now self-inconsistent).
    verdict_tampered = json.loads(json.dumps(binding_artifact))
    verdict_tampered["replay_result"]["verdict"] = "reviewer_false_positive_suspected"
    _expect_contract_invalid(artifact=verdict_tampered)

    # 3) routing tampered.
    routing_tampered = json.loads(json.dumps(binding_artifact))
    routing_tampered["replay_result"]["routing"] = "human_escalation"
    _expect_contract_invalid(artifact=routing_tampered)

    # 4) should-consume tampered.
    should_consume_tampered = json.loads(json.dumps(binding_artifact))
    should_consume_tampered["replay_result"]["should_consume_iteration"] = (
        not should_consume_tampered["replay_result"]["should_consume_iteration"]
    )
    _expect_contract_invalid(artifact=should_consume_tampered)

    # 5) body hash tampered.
    body_hash_tampered = json.loads(json.dumps(binding_artifact))
    body_hash_tampered["current_body_sha256"] = "sha256:" + ("a" * 64)
    _expect_contract_invalid(artifact=body_hash_tampered)

    # 6) next state tampered.
    next_state_tampered = json.loads(json.dumps(binding_artifact))
    next_state_tampered["replay_next_state"] = dict(next_state_tampered["replay_next_state"])
    next_state_tampered["replay_next_state"]["consecutive_unbacked_count"] = 999
    _expect_contract_invalid(artifact=next_state_tampered)

    # 7) binding digest tampered directly (no recompute).
    digest_tampered = json.loads(json.dumps(binding_artifact))
    digest_tampered["binding_digest"] = "sha256:" + "f" * 64
    _expect_contract_invalid(artifact=digest_tampered)

    # 8) identity tampered (issue number mismatch vs binding artifact).
    with pytest.raises(emit_mod.EmitContractError):
        emit_mod.emit_parent_review_envelope_v2(
            child_stdout_text,
            issue_number=9999,
            binding_artifact=binding_artifact,
            repository_full_name=REPO,
            refinement_session_id=SESSION_ID,
            iteration_id=ITERATION_ID,
            current_body_bytes=CURRENT_BODY_BYTES,
        )


def test_approve_preserves_eight_lines_without_parent_replay():
    approve_text = _build_valid_approve_intermediate()
    result = emit_mod.emit_parent_review_envelope_v2(
        approve_text,
        issue_number=ISSUE_NUMBER,
        binding_artifact={"poison": "should never be read"},
        repository_full_name="poison/poison",
        refinement_session_id="poison-session",
        iteration_id="poison-iteration",
        current_body_bytes=b"poison body",
    )
    assert result["envelope_kind"] == "approve"
    out_text = result["envelope_bytes"].decode("utf-8")
    lines = out_text[:-1].split("\n") if out_text.endswith("\n") else out_text.split("\n")
    assert [line.split(":", 1)[0] for line in lines] == v1.APPROVE_FIELDS
    assert "REVIEWER_BLOCKER_CLAIM" not in out_text
    assert "PARENT_REPLAY_" not in out_text
    # Byte-identical to the (re-rendered) input content.
    assert out_text == approve_text

    # Also valid against the plain V1 validator (approve grammar unchanged).
    validation = v1.validate_review_compact_output(out_text, issue_number=ISSUE_NUMBER)
    assert validation["validation_status"] == "valid"
    assert validation["envelope_kind"] == "approve"


def test_failure_contract_never_writes_partial_stdout(tmp_path):
    # contract-invalid: malformed child intermediate -> CLI exit 1, empty stdout.
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "emit_parent_review_envelope_v2.py"),
            "--issue-number",
            str(ISSUE_NUMBER),
        ],
        input="not a valid envelope at all\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.strip() != ""
    diagnostic = json.loads(proc.stderr)
    assert diagnostic["schema"] == "EMIT_PARENT_REVIEW_ENVELOPE_V2_FAILURE"
    assert diagnostic["reason_code"] == "contract_invalid"

    # runtime error: needs-fix intermediate but missing binding artifact file -> exit 2, empty stdout.
    child_stdout_text, _claim = _run_child_compact_review_result(tmp_path)
    proc2 = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "emit_parent_review_envelope_v2.py"),
            "--issue-number",
            str(ISSUE_NUMBER),
        ],
        input=child_stdout_text + "\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc2.returncode == 2
    assert proc2.stdout == ""
    assert proc2.stderr.strip() != ""
    diagnostic2 = json.loads(proc2.stderr)
    assert diagnostic2["schema"] == "EMIT_PARENT_REVIEW_ENVELOPE_V2_FAILURE"
    assert diagnostic2["reason_code"] == "runtime_error"

    # Success case, for contrast: exit 0, stdout == full envelope, stderr empty.
    child_stdout_text2, binding_artifact = _build_valid_pair(tmp_path)
    run_dir = tmp_path / "success_run"
    run_dir.mkdir()
    binding_file = run_dir / "binding_artifact.json"
    body_file = run_dir / "current_body.txt"
    binding_file.write_text(json.dumps(binding_artifact), encoding="utf-8")
    body_file.write_bytes(CURRENT_BODY_BYTES)
    proc3 = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "emit_parent_review_envelope_v2.py"),
            "--issue-number",
            str(ISSUE_NUMBER),
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
        input=child_stdout_text2 + "\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc3.returncode == 0, proc3.stderr
    assert proc3.stdout != ""
    assert proc3.stderr == ""

"""
test_compact_review_result_reviewer_blocker_claim.py

Issue #1554: `compact_review_result()`'s `REVIEWER_BLOCKER_CLAIM_V1` builder
must prefer `raw_result["structured_blockers"][].code` (the deterministic
checker's own code) over the human-readable `blocking_issues` prose, falling
back to `blocking_issues` ONLY when `structured_blockers` is empty.

GIVEN/WHEN/THEN:
  - AC1: structured_blockers non-empty + competing blocking_issues prose
    WHEN compact_review_result() builds the claim THEN the claim's code
    comes from structured_blockers, never from the prose.
  - AC2: structured_blockers empty + blocking_issues non-empty WHEN
    compact_review_result() builds the claim THEN the legacy
    blocking_issues fallback still produces a code (no regression).
  - AC3: the built claim's schema shape (schema/body_sha256/
    blockers[].reviewer_blocker_code/message/line_start/line_end) is
    unchanged -- verified by actually passing it through
    `parent_replay_binding.validate_reviewer_blocker_claim()`.
  - AC4: multiple structured_blockers (including a duplicate code) WHEN
    compacted THEN the claim preserves input order/duplication and never
    mixes in blocking_issues order/prose.
  - AC5/AC6: production CLI chain (compact_review_result.py ->
    parent_replay_binding.py) -- deterministic_fail_confirmed only when
    PARENT-OWNED evidence backs the same taxonomy entry; child-side
    checker_evidence alone is never suffient (trust boundary, PR #1535).
  - AC7: full production chain (compact_review_result.py ->
    parent_replay_binding.py -> validate_review_compact_output.py --v2)
    ends in validation_status: valid.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILLS_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from compact_review_result import compact_review_result  # noqa: E402
from parent_replay_binding import (  # noqa: E402
    canonical_replay_next_state_line,
    validate_reviewer_blocker_claim,
)

COMPACT_SCRIPT = SCRIPTS_DIR / "compact_review_result.py"
PARENT_REPLAY_SCRIPT = SCRIPTS_DIR / "parent_replay_binding.py"
VALIDATE_V2_SCRIPT = SCRIPTS_DIR / "validate_review_compact_output.py"

BODY_BYTES = b"issue-1554-fixture-body-c9"
BODY_SHA256_HEX = hashlib.sha256(BODY_BYTES).hexdigest()
BODY_SHA256 = f"sha256:{BODY_SHA256_HEX}"

REPO_FULL_NAME = "squne121/loop-protocol"
ISSUE_NUMBER = 1554
ISSUE_URL = f"https://github.com/{REPO_FULL_NAME}/issues/{ISSUE_NUMBER}"


def _checker_evidence(**overrides: Any) -> dict[str, Any]:
    base = {
        "source_check": "contract_readiness_check",
        "rule_id": "RVA001",
        "category": "rva_immediate_field_missing",
        "artifact_path": ".claude/skills/issue-refinement-loop/scripts/contract_readiness_check.py",
        "artifact_schema": "CHECK_ISSUE_CONTRACT_V1",
        "body_sha256": BODY_SHA256,
        "iteration_id": "iter-1",
        "line_start": 10,
        "line_end": 12,
    }
    base.update(overrides)
    return base


# PR #1319-fixed producer shape: finding_kind == deterministic_domain_blocker,
# deterministic_domain_key set, blocking == true, full checker_evidence.
STRUCTURED_C9 = {
    "code": "C9",
    "message": "Runtime Verification Applicability セクションに decision フィールドがありません",
    "finding_kind": "deterministic_domain_blocker",
    "deterministic_domain_key": "runtime_applicability",
    "blocking": True,
    "checker_evidence": [_checker_evidence()],
}

# Competing human-readable prose (#1549 failure shape): a plain string, not
# an object, and deliberately carries a DIFFERENT message than STRUCTURED_C9.
BLOCKING_ISSUES_PROSE_C9 = (
    "C9: Runtime Verification Applicability セクションがない、または decision "
    "フィールドが欠落しています。人間向けにセクションを追加してください。"
)


def _base_raw_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "REVIEW_ISSUE_RESULT_V1",
        "schema_version": "review_issue_result/v1",
        "verdict": "needs-fix",
        "status": "ok",
        "body_sha256": BODY_SHA256,
        "issue_kind": "implementation",
        "generated_at": "2026-07-18T00:00:00Z",
        "issue_url": ISSUE_URL,
        "deterministic_checks": {"C9_runtime_applicability_present": "fail"},
        "blocking_issues": [BLOCKING_ISSUES_PROSE_C9],
        "structured_blockers": [STRUCTURED_C9],
        "non_blocking_improvements": [],
        "findings": [
            {
                "finding_kind": "deterministic_domain_blocker",
                "deterministic_domain_key": "runtime_applicability",
                "blocking": True,
                "checker_evidence": [_checker_evidence()],
                "message": "runtime_applicability",
            }
        ],
        "diff_proposal": {},
        "parsed_vc_commands": [],
    }
    base.update(overrides)
    return base


def _extract_claim(stdout_lines: list[str]) -> dict[str, Any]:
    for line in stdout_lines:
        if line.startswith("REVIEWER_BLOCKER_CLAIM: "):
            return json.loads(line[len("REVIEWER_BLOCKER_CLAIM: ") :])
    raise AssertionError(f"REVIEWER_BLOCKER_CLAIM line not found in: {stdout_lines}")


def _extract_claim_from_text(stdout_text: str) -> dict[str, Any]:
    for line in stdout_text.splitlines():
        if line.startswith("REVIEWER_BLOCKER_CLAIM: "):
            return json.loads(line[len("REVIEWER_BLOCKER_CLAIM: ") :])
    raise AssertionError(f"REVIEWER_BLOCKER_CLAIM line not found in stdout: {stdout_text!r}")


# ---------------------------------------------------------------------------
# AC1: structured_blockers takes priority over blocking_issues prose
# ---------------------------------------------------------------------------


def test_reviewer_blocker_claim_prefers_structured_blockers_code(tmp_path):
    """GIVEN structured_blockers (code=C9, full checker_evidence) and a
    DIFFERENT prose blocking_issues[0] WHEN compact_review_result() builds
    the claim THEN blockers[0].reviewer_blocker_code == 'C9' and the prose
    value is never used as the code."""
    raw_result = _base_raw_result()
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _compact, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=ISSUE_NUMBER
    )

    claim = _extract_claim(stdout_lines)
    assert len(claim["blockers"]) == 1
    assert claim["blockers"][0]["reviewer_blocker_code"] == "C9"
    assert claim["blockers"][0]["message"] == STRUCTURED_C9["message"]
    # The prose blocking_issues string must never appear as a code.
    assert claim["blockers"][0]["reviewer_blocker_code"] != BLOCKING_ISSUES_PROSE_C9
    for blocker in claim["blockers"]:
        assert blocker["reviewer_blocker_code"] != BLOCKING_ISSUES_PROSE_C9


# ---------------------------------------------------------------------------
# AC2: blocking_issues fallback preserved when structured_blockers empty
# ---------------------------------------------------------------------------


def test_reviewer_blocker_claim_falls_back_to_blocking_issues_when_structured_blockers_empty(
    tmp_path,
):
    """GIVEN structured_blockers == [] and blocking_issues non-empty WHEN
    compact_review_result() builds the claim THEN the legacy blocking_issues
    fallback still produces a code (back-compat, AC2)."""
    raw_result = _base_raw_result(
        structured_blockers=[],
        blocking_issues=[{"code": "LP001", "message": "必須セクションが不足しています"}],
    )
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _compact, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=ISSUE_NUMBER
    )

    claim = _extract_claim(stdout_lines)
    assert len(claim["blockers"]) == 1
    assert claim["blockers"][0]["reviewer_blocker_code"] == "LP001"
    assert claim["blockers"][0]["message"] == "必須セクションが不足しています"


def test_reviewer_blocker_claim_falls_back_to_string_blocking_issues(tmp_path):
    """GIVEN structured_blockers == [] and a STRING blocking_issues entry
    WHEN compact_review_result() builds the claim THEN the string itself
    becomes reviewer_blocker_code (pre-existing string fallback path)."""
    raw_result = _base_raw_result(
        structured_blockers=[],
        blocking_issues=["some_legacy_string_code"],
    )
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _compact, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=ISSUE_NUMBER
    )

    claim = _extract_claim(stdout_lines)
    assert claim["blockers"][0]["reviewer_blocker_code"] == "some_legacy_string_code"
    assert claim["blockers"][0]["message"] is None


# ---------------------------------------------------------------------------
# AC3: schema shape unchanged
# ---------------------------------------------------------------------------


def test_reviewer_blocker_claim_schema_shape_unchanged(tmp_path):
    """GIVEN a structured_blockers-backed claim WHEN passed through
    parent_replay_binding.validate_reviewer_blocker_claim() THEN it is
    accepted with EXACTLY the schema/body_sha256/blockers[].
    {reviewer_blocker_code,message,line_start,line_end} shape (no extra,
    no missing, no empty code, no type violation)."""
    raw_result = _base_raw_result()
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _compact, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=ISSUE_NUMBER
    )
    claim = _extract_claim(stdout_lines)

    # Must not raise -- fail-closed shape validation from the untrusted-claim
    # boundary (Issue #1532 Blocker 1).
    normalized = validate_reviewer_blocker_claim(claim)

    assert normalized["schema"] == "REVIEWER_BLOCKER_CLAIM_V1"
    assert normalized["body_sha256"] == BODY_SHA256
    assert len(normalized["blockers"]) == 1
    blocker = normalized["blockers"][0]
    assert set(blocker.keys()) == {"reviewer_blocker_code", "message", "line_start", "line_end"}
    assert blocker["reviewer_blocker_code"] == "C9"
    assert isinstance(blocker["reviewer_blocker_code"], str) and blocker["reviewer_blocker_code"].strip()


# ---------------------------------------------------------------------------
# AC4: multi-blocker order/duplicate preservation
# ---------------------------------------------------------------------------


def test_reviewer_blocker_claim_preserves_multi_blocker_order(tmp_path):
    """GIVEN structured_blockers == [B, A, B] (duplicate code B, distinct
    message/line per entry) WHEN compact_review_result() builds the claim
    THEN blockers preserves that exact order/duplication, and none of the
    competing blocking_issues prose/order leaks in."""
    blocker_b1 = {
        "code": "B",
        "message": "first B",
        "finding_kind": "deterministic_domain_blocker",
        "deterministic_domain_key": "vc_command_format",
        "blocking": True,
        "checker_evidence": [_checker_evidence(rule_id="VCS001", category="non_dollar_command")],
        "line_start": 10,
        "line_end": 12,
    }
    blocker_a = {
        "code": "A",
        "message": "the A",
        "finding_kind": "deterministic_domain_blocker",
        "deterministic_domain_key": "required_sections",
        "blocking": True,
        "checker_evidence": [_checker_evidence(rule_id="LP001", category="body_lint")],
        # no top-level line_start/line_end -- must transcribe as null
    }
    blocker_b2 = {
        "code": "B",
        "message": "second B",
        "finding_kind": "deterministic_domain_blocker",
        "deterministic_domain_key": "vc_command_format",
        "blocking": True,
        "checker_evidence": [_checker_evidence(rule_id="VCS001", category="non_dollar_command")],
        "line_start": 30,
        "line_end": 32,
    }
    raw_result = _base_raw_result(
        structured_blockers=[blocker_b1, blocker_a, blocker_b2],
        blocking_issues=["Z: unrelated prose", "Y: another unrelated prose"],
        findings=[],
    )
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _compact, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=ISSUE_NUMBER
    )
    claim = _extract_claim(stdout_lines)

    assert claim["blockers"] == [
        {"reviewer_blocker_code": "B", "message": "first B", "line_start": 10, "line_end": 12},
        {"reviewer_blocker_code": "A", "message": "the A", "line_start": None, "line_end": None},
        {"reviewer_blocker_code": "B", "message": "second B", "line_start": 30, "line_end": 32},
    ]
    codes = [b["reviewer_blocker_code"] for b in claim["blockers"]]
    assert "Z" not in codes and "Y" not in codes
    assert "Z: unrelated prose" not in [b["message"] for b in claim["blockers"]]


# ---------------------------------------------------------------------------
# CLI production-chain helpers (AC5/AC6/AC7)
# ---------------------------------------------------------------------------


def _run_compact_review_result_cli(
    raw_result: dict[str, Any], tmp_path: Path, issue_number: int = ISSUE_NUMBER
) -> tuple[int, str]:
    """Run the REAL compact_review_result.py CLI with `tmp_path` as an
    isolated cwd surrogate for the repo root, so the emitted ARTIFACT field
    is a relative `.claude/artifacts/issue-refinement-loop/<issue>/<file>`
    path (matching validate_review_compact_output.py's lexical
    _ARTIFACT_PATH_RE) without ever writing into the real repo tree."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_file = tmp_path / "raw_result.json"
    input_file.write_text(json.dumps(raw_result), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(COMPACT_SCRIPT),
            "--input-file",
            str(input_file),
            "--artifact-dir",
            ".claude/artifacts/issue-refinement-loop",
            "--issue-number",
            str(issue_number),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"compact_review_result.py CLI failed: {result.stdout} {result.stderr}"
    return result.returncode, result.stdout


def _run_parent_replay_binding_cli(
    *,
    claim: dict[str, Any],
    readiness_result: dict[str, Any],
    tmp_path: Path,
    refinement_session_id: str,
    iteration_id: str,
) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    claim_file = tmp_path / "claim.json"
    claim_file.write_text(json.dumps(claim), encoding="utf-8")
    readiness_file = tmp_path / "readiness.json"
    readiness_file.write_text(json.dumps(readiness_result), encoding="utf-8")
    body_file = tmp_path / "current_body.txt"
    body_file.write_bytes(BODY_BYTES)

    result = subprocess.run(
        [
            sys.executable,
            str(PARENT_REPLAY_SCRIPT),
            "--reviewer-blocker-claim-file",
            str(claim_file),
            "--readiness-result-file",
            str(readiness_file),
            "--current-body-file",
            str(body_file),
            "--issue-url",
            ISSUE_URL,
            "--repository-full-name",
            REPO_FULL_NAME,
            "--issue-number",
            str(ISSUE_NUMBER),
            "--refinement-session-id",
            refinement_session_id,
            "--iteration-id",
            iteration_id,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"parent_replay_binding.py CLI failed: {result.stdout} {result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# AC5: production chain confirms deterministic_fail_confirmed with
# matching parent-owned evidence
# ---------------------------------------------------------------------------


def test_parent_replay_confirms_deterministic_fail_with_matching_parent_owned_evidence(tmp_path):
    """GIVEN a structured_blockers-backed claim (code=C9) produced by the
    REAL compact_review_result.py CLI, and a PARENT-OWNED readiness_result
    with a matching rva_immediate_field_missing error, WHEN
    parent_replay_binding.py (REAL CLI) replays THEN
    PARENT_REPLAY_VERDICT == deterministic_fail_confirmed."""
    raw_result = _base_raw_result()
    _rc, stdout = _run_compact_review_result_cli(raw_result, tmp_path / "step1")
    claim = _extract_claim_from_text(stdout)
    assert claim["blockers"][0]["reviewer_blocker_code"] == "C9"

    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": BODY_SHA256,
        "errors": [
            {
                "rule_id": "RVA001",
                "source_check": "contract_readiness_check",
                "category": "rva_immediate_field_missing",
                "line_start": 1,
                "line_end": 2,
            }
        ],
    }
    binding_artifact = _run_parent_replay_binding_cli(
        claim=claim,
        readiness_result=readiness_result,
        tmp_path=tmp_path / "step2",
        refinement_session_id="session-ac5",
        iteration_id="iteration-ac5",
    )

    assert binding_artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"
    assert binding_artifact["replay_result"]["routing"] == "proceed_to_rewrite"


# ---------------------------------------------------------------------------
# AC6: trust boundary does not reverse -- child checker_evidence alone
# never confirms deterministic_fail_confirmed without parent-owned evidence
# ---------------------------------------------------------------------------


def test_parent_replay_does_not_confirm_without_parent_owned_evidence(tmp_path):
    """GIVEN the SAME claim as AC5 (child's structured_blockers[].
    checker_evidence still exists in the review artifact, just not passed
    to the parent) but a readiness_result with NO matching parent-owned
    error, WHEN parent_replay_binding.py (REAL CLI) replays THEN
    PARENT_REPLAY_VERDICT != deterministic_fail_confirmed (trust boundary
    has not reversed; PR #1535)."""
    raw_result = _base_raw_result()
    _rc, stdout = _run_compact_review_result_cli(raw_result, tmp_path / "step1")
    claim = _extract_claim_from_text(stdout)
    assert claim["blockers"][0]["reviewer_blocker_code"] == "C9"

    readiness_result_no_match = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": BODY_SHA256,
        "errors": [],
    }
    binding_artifact = _run_parent_replay_binding_cli(
        claim=claim,
        readiness_result=readiness_result_no_match,
        tmp_path=tmp_path / "step2",
        refinement_session_id="session-ac6",
        iteration_id="iteration-ac6",
    )

    assert binding_artifact["replay_result"]["verdict"] != "deterministic_fail_confirmed"


# ---------------------------------------------------------------------------
# AC7: full production chain -- compact_review_result.py ->
# parent_replay_binding.py -> validate_review_compact_output.py --v2
# ---------------------------------------------------------------------------


def test_production_chain_compact_to_parent_replay_to_v2_validation(tmp_path):
    """GIVEN a schema-valid producer result (blocking_issues prose,
    structured_blockers full evidence shape) WHEN the REAL
    compact_review_result.py CLI -> REAL parent_replay_binding.py CLI ->
    REAL validate_review_compact_output.py --v2 CLI chain runs THEN
    validation_status == valid."""
    raw_result = _base_raw_result()
    _rc, compact_stdout = _run_compact_review_result_cli(raw_result, tmp_path / "step1")
    claim = _extract_claim_from_text(compact_stdout)

    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": BODY_SHA256,
        "errors": [
            {
                "rule_id": "RVA001",
                "source_check": "contract_readiness_check",
                "category": "rva_immediate_field_missing",
                "line_start": 1,
                "line_end": 2,
            }
        ],
    }
    refinement_session_id = "session-ac7"
    iteration_id = "iteration-ac7"
    binding_artifact = _run_parent_replay_binding_cli(
        claim=claim,
        readiness_result=readiness_result,
        tmp_path=tmp_path / "step2",
        refinement_session_id=refinement_session_id,
        iteration_id=iteration_id,
    )
    assert binding_artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"

    # The V2 envelope is orchestrator-assembled: the 9 canonical lines from
    # compact_review_result.py's real stdout, plus the 6 PARENT_REPLAY_*
    # fields derived from the parent's own binding artifact (never from the
    # child's REPLAY self-report -- Issue #1532 Blocker 2).
    compact_lines = compact_stdout.rstrip("\n").split("\n")
    assert len(compact_lines) == 9  # 8 approve fields + REVIEWER_BLOCKER_CLAIM

    replay_result = binding_artifact["replay_result"]
    should_consume_literal = "true" if replay_result["should_consume_iteration"] else "false"
    parent_replay_lines = [
        f"PARENT_REPLAY_VERDICT: {replay_result['verdict']}",
        f"PARENT_REPLAY_ROUTING: {replay_result['routing']}",
        f"PARENT_REPLAY_SHOULD_CONSUME: {should_consume_literal}",
        f"PARENT_REPLAY_BODY_SHA256: {replay_result['body_sha256']}",
        f"PARENT_REPLAY_NEXT_STATE: {canonical_replay_next_state_line(binding_artifact)}",
        f"PARENT_REPLAY_BINDING_DIGEST: {binding_artifact['binding_digest']}",
    ]
    envelope_text = "\n".join(compact_lines + parent_replay_lines) + "\n"

    step3_dir = tmp_path / "step3"
    step3_dir.mkdir()
    envelope_file = step3_dir / "envelope.txt"
    envelope_file.write_bytes(envelope_text.encode("utf-8"))
    binding_artifact_file = step3_dir / "binding_artifact.json"
    binding_artifact_file.write_text(json.dumps(binding_artifact), encoding="utf-8")
    body_file = step3_dir / "current_body.txt"
    body_file.write_bytes(BODY_BYTES)

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATE_V2_SCRIPT),
            "--input-file",
            str(envelope_file),
            "--issue-number",
            str(ISSUE_NUMBER),
            "--v2",
            "--binding-artifact-file",
            str(binding_artifact_file),
            "--repository-full-name",
            REPO_FULL_NAME,
            "--refinement-session-id",
            refinement_session_id,
            "--iteration-id",
            iteration_id,
            "--current-body-file",
            str(body_file),
        ],
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["validation_status"] == "valid", payload
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert payload["envelope_kind"] == "needs_fix_v2"

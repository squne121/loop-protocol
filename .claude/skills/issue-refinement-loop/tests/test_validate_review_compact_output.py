"""
test_validate_review_compact_output.py

Pytest coverage for `validate_review_compact_output.py` (Issue #1507).

GIVEN/WHEN/THEN style tests covering AC1-AC9, AC12, AC13 of Issue #1507:
  - AC1/AC2/AC3: exact envelope shape acceptance / rejection
  - AC4: missing / duplicate / unknown / out-of-order field rejection
  - AC5: prose / code fence / blank line / ANSI / control char / byte budget
  - AC6: value constraints + REPLAY_VERDICT x REPLAY_ROUTING x
    REPLAY_SHOULD_CONSUME matrix
  - AC7: ARTIFACT lexical validation (no filesystem access)
  - AC8: REVIEW_COMPACT_VALIDATION_RESULT_V1 contains input_sha256 /
    normalized_payload
  - AC9: producer-failure + injected fake-approve rejection
  - AC12: parity with real compact_review_result.py / reviewer_claim_replay.py
    output
  - AC13: subprocess E2E rejection of malformed input
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_ROOT / "scripts"
FIXTURES_DIR = SKILLS_ROOT / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

from validate_review_compact_output import (  # noqa: E402
    build_result,
    validate_review_compact_output,
)

VALIDATOR_PATH = SCRIPTS_DIR / "validate_review_compact_output.py"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _fake_sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _approve_envelope(
    *,
    status: str = "ok",
    verdict: str = "approve",
    summary: str = "contract ready",
    blockers: str = "0",
    next_action: str = "proceed",
    must_read: str = "",
    artifact_path: str = ".claude/artifacts/issue-refinement-loop/1507/compact_review_result_20260714T113303Z.json",
) -> str:
    lines = [
        f"STATUS: {status}",
        f"VERDICT: {verdict}",
        f"SUMMARY: {summary}",
        f"BLOCKERS: {blockers}",
        f"NEXT_ACTION: {next_action}",
        f"MUST_READ: {must_read}",
        f"EVIDENCE: {artifact_path}",
        f"ARTIFACT: compact_review_result_v1={artifact_path}",
    ]
    return "\n".join(lines)


def _needs_fix_envelope(
    *,
    status: str = "ok",
    blockers: str = "2",
    next_action: str = "request_changes",
    artifact_path: str = ".claude/artifacts/issue-refinement-loop/1507/compact_review_result_20260714T113304Z.json",
    replay_verdict: str = "deterministic_fail_confirmed",
    replay_routing: str = "proceed_to_rewrite",
    replay_should_consume: str = "true",
    replay_body_sha256: str | None = None,
    replay_artifact_digest: str | None = None,
) -> str:
    replay_body_sha256 = replay_body_sha256 or f"sha256:{_fake_sha256('body')}"
    replay_artifact_digest = replay_artifact_digest or f"sha256:{_fake_sha256('artifact')}"
    base = _approve_envelope(
        status=status,
        verdict="needs-fix",
        summary="2 blocker(s)",
        blockers=blockers,
        next_action=next_action,
        artifact_path=artifact_path,
    )
    extra = [
        f"REPLAY_VERDICT: {replay_verdict}",
        f"REPLAY_ROUTING: {replay_routing}",
        f"REPLAY_SHOULD_CONSUME: {replay_should_consume}",
        f"REPLAY_BODY_SHA256: {replay_body_sha256}",
        f"REPLAY_ARTIFACT_DIGEST: {replay_artifact_digest}",
    ]
    return base + "\n" + "\n".join(extra)


def _producer_failure_envelope(
    *,
    status: str = "failed",
    next_action: str = "human_judgment_required",
    reason_code: str = "schema_mismatch",
    artifact_path: str = (
        ".claude/artifacts/issue-refinement-loop/1501/"
        "producer_failure_schema_mismatch_20260713T215634Z.json"
    ),
    artifact_sha256: str | None = None,
) -> str:
    artifact_sha256 = artifact_sha256 or _fake_sha256("producer-failure-artifact")
    lines = [
        f"STATUS: {status}",
        f"NEXT_ACTION: {next_action}",
        f"REASON_CODE: {reason_code}",
        f"ARTIFACT: producer_failure_v1={artifact_path}",
        f"ARTIFACT_SHA256: {artifact_sha256}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AC1: exact 8-line approve envelope
# ---------------------------------------------------------------------------


class TestApproveEnvelope:
    def test_approve_exact_8_lines_valid(self):
        """GIVEN exact 8-line approve envelope WHEN validated THEN valid/exit-0-equivalent."""
        text = _approve_envelope()
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "valid"
        assert result["envelope_kind"] == "approve"
        assert result["violations"] == []
        assert result["next_action"] == "proceed"
        assert result["normalized_payload"]["VERDICT"] == "approve"

    def test_approve_with_replay_field_present_is_rejected(self):
        """GIVEN approve envelope with a stray REPLAY_* field WHEN validated THEN invalid."""
        text = _approve_envelope() + "\nREPLAY_VERDICT: deterministic_fail_confirmed"
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"

    def test_approve_with_nonzero_blockers_rejected(self):
        """GIVEN approve envelope with BLOCKERS != 0 WHEN validated THEN invalid."""
        text = _approve_envelope(blockers="1")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "approve_blockers_must_be_zero" in codes


# ---------------------------------------------------------------------------
# AC2: exact 13-line needs-fix envelope
# ---------------------------------------------------------------------------


class TestNeedsFixEnvelope:
    def test_needs_fix_exact_13_lines_valid(self):
        """GIVEN exact 13-line needs-fix envelope WHEN validated THEN valid."""
        text = _needs_fix_envelope()
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "valid"
        assert result["envelope_kind"] == "needs_fix"
        assert result["violations"] == []
        assert result["normalized_payload"]["REPLAY_VERDICT"] == "deterministic_fail_confirmed"

    def test_needs_fix_missing_replay_field_is_invalid(self):
        """GIVEN needs-fix envelope missing REPLAY_ARTIFACT_DIGEST THEN invalid."""
        text = _needs_fix_envelope()
        # Drop the last line (REPLAY_ARTIFACT_DIGEST)
        truncated = "\n".join(text.split("\n")[:-1])
        result = validate_review_compact_output(truncated)
        assert result["validation_status"] == "invalid"


# ---------------------------------------------------------------------------
# AC3: canonical producer-failure envelope
# ---------------------------------------------------------------------------


class TestProducerFailureEnvelope:
    def test_producer_failure_envelope_invalid_human_judgment(self):
        """GIVEN canonical 5-line producer-failure envelope WHEN validated
        THEN parseable but always validation_status=invalid, next_action
        human_judgment_required."""
        text = _producer_failure_envelope()
        result = validate_review_compact_output(text)
        assert result["envelope_kind"] == "producer_failure"
        assert result["validation_status"] == "invalid"
        assert result["next_action"] == "human_judgment_required"
        # Envelope IS parseable: normalized_payload is populated for
        # diagnostics even though validation_status stays invalid.
        assert result["normalized_payload"] is not None
        assert result["normalized_payload"]["REASON_CODE"] == "schema_mismatch"

    def test_producer_failure_wrong_status_is_flagged(self):
        """GIVEN producer-failure shaped envelope with STATUS != failed THEN violation recorded."""
        text = _producer_failure_envelope(status="ok")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "producer_failure_status_must_be_failed" in codes


# ---------------------------------------------------------------------------
# AC4: missing / duplicate / unknown / out-of-order fields
# ---------------------------------------------------------------------------


class TestFieldStructureViolations:
    def test_missing_field(self):
        """GIVEN approve envelope missing SUMMARY WHEN validated THEN missing_field violation."""
        lines = _approve_envelope().split("\n")
        del lines[2]  # remove SUMMARY line
        text = "\n".join(lines)
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "missing_field" in codes

    def test_duplicate_field(self):
        """GIVEN approve envelope with duplicated VERDICT line WHEN validated THEN duplicate_field violation."""
        lines = _approve_envelope().split("\n")
        lines.insert(2, "VERDICT: approve")
        text = "\n".join(lines)
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "duplicate_field" in codes

    def test_unknown_field(self):
        """GIVEN approve envelope with an EXTRA_FIELD line WHEN validated THEN unknown_field violation."""
        text = _approve_envelope() + "\nEXTRA_FIELD: surprise"
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "unknown_field" in codes

    def test_out_of_order_field(self):
        """GIVEN approve envelope with VERDICT/STATUS swapped WHEN validated THEN out_of_order_field violation."""
        lines = _approve_envelope().split("\n")
        lines[0], lines[1] = lines[1], lines[0]
        text = "\n".join(lines)
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "out_of_order_field" in codes


# ---------------------------------------------------------------------------
# AC5: prose / fence / blank line / ANSI / control char / byte budget
# ---------------------------------------------------------------------------


class TestLexicalRejections:
    def test_prose_prefix_suffix(self):
        """GIVEN leading and trailing prose around a valid envelope WHEN validated THEN prose violations."""
        text = "Here is my review:\n" + _approve_envelope() + "\nThanks!"
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "prose_prefix" in codes
        assert "prose_suffix" in codes

    def test_code_fence(self):
        """GIVEN a Markdown code fence wrapping the envelope WHEN validated THEN code_fence_detected."""
        text = "```\n" + _approve_envelope() + "\n```"
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "code_fence_detected" in codes

    def test_blank_line(self):
        """GIVEN a blank line inserted mid-envelope WHEN validated THEN blank_line_detected."""
        lines = _approve_envelope().split("\n")
        lines.insert(3, "")
        text = "\n".join(lines)
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "blank_line_detected" in codes

    def test_ansi_escape(self):
        """GIVEN an ANSI escape sequence embedded in a value WHEN validated THEN ansi_escape_detected."""
        text = _approve_envelope(summary="contract ready \x1b[31m")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "ansi_escape_detected" in codes

    def test_control_char(self):
        """GIVEN a NUL byte embedded in a value WHEN validated THEN control_char_detected."""
        text = _approve_envelope(summary="contract\x00ready")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "control_char_detected" in codes

    def test_byte_budget_exceeded(self):
        """GIVEN input exceeding 2048 UTF-8 bytes WHEN validated THEN byte_budget_exceeded."""
        text = _approve_envelope(summary="x" * 3000)
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "byte_budget_exceeded" in codes

    def test_crlf_detected(self):
        """GIVEN CRLF line endings WHEN validated THEN crlf_detected."""
        text = _approve_envelope().replace("\n", "\r\n")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "crlf_detected" in codes

    def test_trailing_whitespace_in_value_rejected(self):
        """GIVEN trailing whitespace in a value WHEN validated THEN value_whitespace_violation."""
        text = _approve_envelope(summary="contract ready ")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "value_whitespace_violation" in codes

    def test_empty_must_read_value_is_allowed(self):
        """GIVEN empty MUST_READ value (canonical shape) WHEN validated THEN no whitespace violation."""
        text = _approve_envelope(must_read="")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "valid"


# ---------------------------------------------------------------------------
# AC6: value constraints + REPLAY_VERDICT x REPLAY_ROUTING x REPLAY_SHOULD_CONSUME matrix
# ---------------------------------------------------------------------------


class TestValueConstraints:
    def test_value_constraints_unknown_status_rejected(self):
        """GIVEN an unknown STATUS value WHEN validated THEN status_value_invalid."""
        text = _approve_envelope(status="weird")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "status_value_invalid" in codes

    def test_value_constraints_blockers_leading_zero_rejected(self):
        """GIVEN BLOCKERS with a leading zero WHEN validated THEN blockers_invalid_format."""
        text = _needs_fix_envelope(blockers="01")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "blockers_invalid_format" in codes

    def test_value_constraints_blockers_negative_rejected(self):
        """GIVEN a negative BLOCKERS value WHEN validated THEN blockers_invalid_format."""
        text = _needs_fix_envelope(blockers="-1")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "blockers_invalid_format" in codes

    @pytest.mark.parametrize(
        "verdict,routing,should_consume",
        [
            ("deterministic_fail_confirmed", "proceed_to_rewrite", "true"),
            ("checker_artifact_inconsistency", "fix_checker_artifact", "false"),
            (
                "reviewer_claim_unbacked_by_deterministic_checker",
                "downgrade_to_non_blocking",
                "false",
            ),
            ("reviewer_false_positive_suspected", "human_escalation", "false"),
            ("input_or_runtime_error", "human_judgment_required", "false"),
        ],
    )
    def test_replay_verdict_routing_matrix_accepts_canonical_pairs(
        self, verdict, routing, should_consume
    ):
        """GIVEN each of the 5 canonical REPLAY_VERDICT/ROUTING/SHOULD_CONSUME
        combinations WHEN validated THEN valid."""
        text = _needs_fix_envelope(
            replay_verdict=verdict,
            replay_routing=routing,
            replay_should_consume=should_consume,
        )
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "valid", result["violations"]

    def test_replay_verdict_routing_matrix_rejects_mismatched_pair(self):
        """GIVEN REPLAY_VERDICT=checker_artifact_inconsistency paired with the
        wrong routing WHEN validated THEN replay_verdict_routing_mismatch."""
        text = _needs_fix_envelope(
            replay_verdict="checker_artifact_inconsistency",
            replay_routing="proceed_to_rewrite",
            replay_should_consume="true",
        )
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "replay_verdict_routing_mismatch" in codes

    def test_replay_verdict_invalid_enum_rejected(self):
        """GIVEN an out-of-enum REPLAY_VERDICT value WHEN validated THEN replay_verdict_invalid_enum."""
        text = _needs_fix_envelope(
            replay_verdict="totally_made_up_verdict",
            replay_routing="proceed_to_rewrite",
            replay_should_consume="true",
        )
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "replay_verdict_invalid_enum" in codes


# ---------------------------------------------------------------------------
# AC7: ARTIFACT lexical validation
# ---------------------------------------------------------------------------


class TestArtifactLexicalValidation:
    def test_artifact_lexical_valid(self):
        """GIVEN a canonical repo-relative ARTIFACT path WHEN validated THEN no artifact violation."""
        text = _approve_envelope(
            artifact_path=".claude/artifacts/issue-refinement-loop/1507/compact_review_result_20260714T113303Z.json"
        )
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "valid"
        assert result["artifact_path_policy"]["status"] == "valid"

    def test_artifact_absolute_path_rejected(self):
        """GIVEN an absolute ARTIFACT path WHEN validated THEN artifact_absolute_path_rejected."""
        text = _approve_envelope(
            artifact_path="/etc/passwd"
        )
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "artifact_absolute_path_rejected" in codes

    def test_artifact_parent_traversal_rejected(self):
        """GIVEN an ARTIFACT path containing .. WHEN validated THEN artifact_parent_traversal_rejected."""
        text = _approve_envelope(
            artifact_path=".claude/artifacts/issue-refinement-loop/../../etc/passwd"
        )
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "artifact_parent_traversal_rejected" in codes

    def test_artifact_does_not_check_filesystem_existence(self, tmp_path):
        """GIVEN a syntactically valid but nonexistent ARTIFACT path WHEN
        validated THEN validation succeeds (lexical-only, #1472 boundary)."""
        text = _approve_envelope(
            artifact_path=".claude/artifacts/issue-refinement-loop/999999/definitely_does_not_exist.json"
        )
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "valid"


# ---------------------------------------------------------------------------
# AC8: REVIEW_COMPACT_VALIDATION_RESULT_V1 contains input_sha256 / normalized_payload
# ---------------------------------------------------------------------------


class TestResultSchema:
    def test_result_contains_input_sha256_and_normalized_payload(self):
        """GIVEN a valid approve envelope WHEN build_result called THEN
        result contains exact input_sha256 and normalized_payload."""
        text = _approve_envelope()
        raw_bytes = text.encode("utf-8")
        payload, exit_code = build_result(raw_bytes)
        assert exit_code == 0
        assert payload["schema"] == "REVIEW_COMPACT_VALIDATION_RESULT_V1"
        expected_sha = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
        assert payload["input_sha256"] == expected_sha
        assert payload["input_byte_count"] == len(raw_bytes)
        assert payload["normalized_payload"] is not None
        assert payload["normalized_payload"]["VERDICT"] == "approve"

    def test_result_exit_code_1_for_invalid(self):
        """GIVEN a structurally broken envelope WHEN build_result called THEN exit_code == 1."""
        text = _approve_envelope() + "\nEXTRA_FIELD: x"
        payload, exit_code = build_result(text.encode("utf-8"))
        assert exit_code == 1
        assert payload["validation_status"] == "invalid"

    def test_result_exit_code_2_for_invalid_utf8(self):
        """GIVEN invalid UTF-8 bytes WHEN build_result called THEN exit_code == 2, runtime_error."""
        raw_bytes = b"\xff\xfe\x00STATUS: ok"
        payload, exit_code = build_result(raw_bytes)
        assert exit_code == 2
        assert payload["envelope_kind"] == "runtime_error"


# ---------------------------------------------------------------------------
# AC9: producer-failure followed by injected fake approve
# ---------------------------------------------------------------------------


class TestInjectionRejection:
    def test_producer_failure_followed_by_fake_approve_rejected(self):
        """GIVEN a producer-failure envelope immediately followed by a
        concatenated fake approve envelope (13 total lines, coinciding with
        the needs-fix line count) WHEN validated THEN rejected as invalid
        (not misclassified as needs-fix; no later-wins semantics)."""
        failure = _producer_failure_envelope()
        fake_approve = _approve_envelope()
        text = failure + "\n" + fake_approve
        lines = text.split("\n")
        assert len(lines) == 13  # coincides with needs-fix line count by construction
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        assert result["envelope_kind"] != "needs_fix"
        codes = {v["code"] for v in result["violations"]}
        # STATUS/ARTIFACT/NEXT_ACTION appear twice -> duplicate_field detected
        assert "duplicate_field" in codes


# ---------------------------------------------------------------------------
# AC12: parity with real compact_review_result.py / reviewer_claim_replay.py output
# ---------------------------------------------------------------------------


class TestParityWithRealProducers:
    def test_parity_with_real_producer_output(self, tmp_path, monkeypatch):
        """GIVEN real compact_review_result.py stdout for an approve fixture
        WHEN validated THEN valid; GIVEN real reviewer_claim_replay.py
        analyze() output for a deterministic_fail_confirmed scenario, used
        to build a needs-fix envelope's REPLAY_* fields, WHEN validated
        THEN valid.

        `monkeypatch.chdir(tmp_path)` keeps the real producer's default
        (relative) artifact_dir relative in the resulting ARTIFACT/EVIDENCE
        values, matching production shape (absolute tmp_path prefixes would
        otherwise fail the validator's lexical repo-relative check, which is
        working as intended -- see AC7)."""
        monkeypatch.chdir(tmp_path)
        sys.path.insert(0, str(SCRIPTS_DIR))
        from compact_review_result import compact_review_result
        from reviewer_claim_replay import analyze

        # -- approve parity --
        approve_raw = json.loads((FIXTURES_DIR / "review_result_approve.json").read_text(encoding="utf-8"))
        _compact, stdout_lines, _artifact_path, _content = compact_review_result(
            approve_raw,
            artifact_dir=Path(".claude/artifacts/issue-refinement-loop"),
            issue_number=1507,
            repo_root=tmp_path,
        )
        approve_text = "\n".join(stdout_lines)
        approve_result = validate_review_compact_output(approve_text)
        assert approve_result["validation_status"] == "valid", approve_result["violations"]
        assert approve_result["envelope_kind"] == "approve"

        # -- needs-fix parity: real reviewer_claim_replay.py analyze() output --
        body_sha = f"sha256:{_fake_sha256('parity-body')}"
        review = {
            "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
            "issue_url": "https://github.com/squne121/loop-protocol/issues/1507",
            "body_sha256": body_sha,
            "blocking_issues": [{"code": "LP010", "message": "ac/vc mismatch"}],
            "structured_blockers": [],
        }
        readiness = {
            "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
            "body_sha256": body_sha,
            "errors": [
                {
                    "rule_id": "LP010",
                    "source_check": "validate_issue_body",
                    "category": "body_lint",
                    "line_start": 5,
                    "line_end": 5,
                }
            ],
        }
        replay_result, _next_state = analyze(
            review_result=review,
            readiness_result=readiness,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state={},
        )
        assert replay_result["verdict"] == "deterministic_fail_confirmed"
        assert replay_result["routing"] == "proceed_to_rewrite"
        assert replay_result["should_consume_iteration"] is True

        needs_fix_raw = json.loads((FIXTURES_DIR / "review_result_needs_fix.json").read_text(encoding="utf-8"))
        needs_fix_raw = {**needs_fix_raw, "body_sha256": body_sha}
        _compact_nf, stdout_lines_nf, _artifact_path_nf, _content_nf = compact_review_result(
            needs_fix_raw,
            artifact_dir=Path(".claude/artifacts/issue-refinement-loop"),
            issue_number=1507,
            repo_root=tmp_path,
        )
        replay_artifact_digest = (
            f"sha256:{hashlib.sha256(json.dumps(replay_result, sort_keys=True).encode('utf-8')).hexdigest()}"
        )
        should_consume_literal = "true" if replay_result["should_consume_iteration"] else "false"
        replay_lines = [
            f"REPLAY_VERDICT: {replay_result['verdict']}",
            f"REPLAY_ROUTING: {replay_result['routing']}",
            f"REPLAY_SHOULD_CONSUME: {should_consume_literal}",
            f"REPLAY_BODY_SHA256: {replay_result['body_sha256']}",
            f"REPLAY_ARTIFACT_DIGEST: {replay_artifact_digest}",
        ]
        needs_fix_text = "\n".join(stdout_lines_nf) + "\n" + "\n".join(replay_lines)
        needs_fix_result = validate_review_compact_output(needs_fix_text)
        assert needs_fix_result["validation_status"] == "valid", needs_fix_result["violations"]
        assert needs_fix_result["envelope_kind"] == "needs_fix"
        assert needs_fix_result["normalized_payload"]["REPLAY_VERDICT"] == "deterministic_fail_confirmed"

    def test_replay_verdict_enum_matches_reviewer_claim_replay_legacy_map(self):
        """GIVEN reviewer_claim_replay.py's own _LEGACY_VERDICT_MAP_V1 WHEN
        compared to validator's VALID_REPLAY_VERDICTS THEN the value sets
        are identical (no enum drift, #1507 P0-5)."""
        from reviewer_claim_replay import _LEGACY_VERDICT_MAP_V1
        from validate_review_compact_output import VALID_REPLAY_VERDICTS

        assert set(_LEGACY_VERDICT_MAP_V1.values()) == VALID_REPLAY_VERDICTS


# ---------------------------------------------------------------------------
# AC13: subprocess E2E rejects malformed input
# ---------------------------------------------------------------------------


class TestE2ESubprocess:
    def _run(self, stdin_text: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(VALIDATOR_PATH)],
            input=stdin_text.encode("utf-8"),
            capture_output=True,
            timeout=30,
        )

    def test_e2e_subprocess_accepts_valid_approve(self):
        """GIVEN validator invoked as a real subprocess with a valid approve
        envelope on stdin WHEN it runs THEN exit code 0 and JSON validation_status valid."""
        proc = self._run(_approve_envelope())
        assert proc.returncode == 0
        payload = json.loads(proc.stdout.decode("utf-8"))
        assert payload["validation_status"] == "valid"

    def test_e2e_subprocess_rejects_malformed_input(self):
        """GIVEN validator invoked as a real subprocess with multiple
        malformed inputs (missing field / duplicate field / injection /
        prose) on stdin WHEN each runs THEN exit code 1 and
        validation_status invalid."""
        malformed_inputs = [
            "\n".join(_approve_envelope().split("\n")[:-1]),  # missing ARTIFACT
            _approve_envelope() + "\nSTATUS: ok",  # duplicate field
            _producer_failure_envelope() + "\n" + _approve_envelope(),  # injection
            "Dear reviewer,\n" + _approve_envelope(),  # prose prefix
        ]
        for stdin_text in malformed_inputs:
            proc = self._run(stdin_text)
            assert proc.returncode == 1, f"expected exit 1 for {stdin_text!r}, got {proc.returncode}"
            payload = json.loads(proc.stdout.decode("utf-8"))
            assert payload["validation_status"] == "invalid"

    def test_e2e_subprocess_stdout_is_single_json_object(self):
        """GIVEN validator invoked as subprocess WHEN it runs THEN stdout is
        exactly one JSON object (no extra prose lines)."""
        proc = self._run(_approve_envelope())
        stdout_text = proc.stdout.decode("utf-8")
        # Exactly one JSON object followed by a single trailing newline.
        assert stdout_text.endswith("\n")
        json.loads(stdout_text)  # must parse as a single JSON document

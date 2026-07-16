"""
test_validate_review_compact_output.py

Pytest coverage for `validate_review_compact_output.py` (Issue #1507).

GIVEN/WHEN/THEN style tests covering AC1-AC9, AC12, AC13, AC15-AC21 of Issue #1507:
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
  - AC15/AC16: --issue-number active issue namespace binding (mismatch /
    unknown / 0 / leading-zero rejection)
  - AC17-AC20: MUST_READ / EVIDENCE / ARTIFACT filename / SUMMARY
    producer-derived invariants
  - AC21: mutation testing against real compact_review_result.py output
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
    validate_review_compact_output_v2,
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
        validated THEN validation succeeds (lexical-only, #1472 boundary).

        Uses a canonical-shape but never-generated timestamp
        (20990101T000000Z) so the filename still matches the AC19
        compact_review_result_YYYYMMDDTHHMMSSZ.json pattern while remaining
        guaranteed absent from disk.
        """
        text = _approve_envelope(
            artifact_path=".claude/artifacts/issue-refinement-loop/999999/compact_review_result_20990101T000000Z.json"
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
        WHEN validated THEN valid; GIVEN the REAL production chain -- real
        `compact_review_result.py` needs-fix stdout (child, carrying only
        REVIEWER_BLOCKER_CLAIM) parsed into a claim, replayed through the
        real `parent_replay_binding.build_parent_replay_binding()` (parent),
        assembled into a V2 envelope, and validated via the real
        `validate_review_compact_output_v2` -- WHEN validated THEN valid
        (Issue #1532 Blocker 3.1/3.4).

        `monkeypatch.chdir(tmp_path)` keeps the real producer's default
        (relative) artifact_dir relative in the resulting ARTIFACT/EVIDENCE
        values, matching production shape (absolute tmp_path prefixes would
        otherwise fail the validator's lexical repo-relative check, which is
        working as intended -- see AC7)."""
        monkeypatch.chdir(tmp_path)
        sys.path.insert(0, str(SCRIPTS_DIR))
        from compact_review_result import compact_review_result

        # -- approve parity --
        approve_raw = json.loads((FIXTURES_DIR / "review_result_approve.json").read_text(encoding="utf-8"))
        _compact, stdout_lines, _artifact_path, _content = compact_review_result(
            approve_raw,
            artifact_dir=Path(".claude/artifacts/issue-refinement-loop"),
            issue_number=1507,
            repo_root=tmp_path,
        )
        approve_text = "\n".join(stdout_lines)
        approve_result = validate_review_compact_output(approve_text, issue_number=1507)
        assert approve_result["validation_status"] == "valid", approve_result["violations"]
        assert approve_result["envelope_kind"] == "approve"

        # -- needs-fix parity: REAL production chain --
        body_bytes = b"parity-body-current-snapshot"
        body_sha = f"sha256:{hashlib.sha256(body_bytes).hexdigest()}"
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

        # 1) CHILD step: the real compact_review_result.py producer builds
        #    the needs-fix stdout, including the bounded REVIEWER_BLOCKER_CLAIM
        #    field (no REPLAY_* fields -- those are retired for children).
        needs_fix_raw = json.loads((FIXTURES_DIR / "review_result_needs_fix.json").read_text(encoding="utf-8"))
        needs_fix_raw = {
            **needs_fix_raw,
            "body_sha256": body_sha,
            "blocking_issues": [{"code": "LP010", "message": "ac/vc mismatch"}],
        }
        _compact_nf, stdout_lines_nf, _artifact_path_nf, _content_nf = compact_review_result(
            needs_fix_raw,
            artifact_dir=Path(".claude/artifacts/issue-refinement-loop"),
            issue_number=1507,
            repo_root=tmp_path,
        )
        child_text = "\n".join(stdout_lines_nf)
        claim_line = _compact_nf["REVIEWER_BLOCKER_CLAIM"]
        reviewer_blocker_claim = json.loads(claim_line)

        # 2) PARENT step: real parent_replay_binding.py replay over
        #    parent-owned readiness_result + the (validated) child claim.
        binding_artifact = _pb.build_parent_replay_binding(
            reviewer_blocker_claim=reviewer_blocker_claim,
            readiness_result=readiness,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
            current_body_bytes=body_bytes,
            issue_url="https://github.com/squne121/loop-protocol/issues/1507",
            repository_full_name="squne121/loop-protocol",
            issue_number=1507,
            refinement_session_id="session-parity",
            iteration_id="iteration-parity",
        )
        replay_result = binding_artifact["replay_result"]
        assert replay_result["verdict"] == "deterministic_fail_confirmed"
        assert replay_result["routing"] == "proceed_to_rewrite"
        assert replay_result["should_consume_iteration"] is True

        # 3) ASSEMBLER step: orchestrator appends its own parent-computed
        #    fields to the child's real stdout text.
        v2_text = child_text + "\n" + "\n".join(
            [
                f"PARENT_REPLAY_VERDICT: {replay_result['verdict']}",
                f"PARENT_REPLAY_ROUTING: {replay_result['routing']}",
                "PARENT_REPLAY_SHOULD_CONSUME: "
                + ("true" if replay_result["should_consume_iteration"] else "false"),
                f"PARENT_REPLAY_BODY_SHA256: {replay_result['body_sha256']}",
                f"PARENT_REPLAY_NEXT_STATE: {_pb.canonical_replay_next_state_line(binding_artifact)}",
                f"PARENT_REPLAY_BINDING_DIGEST: {binding_artifact['binding_digest']}",
            ]
        )

        # 4) VALIDATOR step: real validate_review_compact_output_v2.
        needs_fix_result = validate_review_compact_output_v2(
            v2_text,
            issue_number=1507,
            binding_artifact=binding_artifact,
            repository_full_name="squne121/loop-protocol",
            refinement_session_id="session-parity",
            iteration_id="iteration-parity",
            current_body_sha256=body_sha,
        )
        assert needs_fix_result["validation_status"] == "valid", needs_fix_result["violations"]
        assert needs_fix_result["envelope_kind"] == "needs_fix_v2"
        assert needs_fix_result["normalized_payload"]["PARENT_REPLAY_VERDICT"] == "deterministic_fail_confirmed"

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
    def _run(self, stdin_text: str, *, issue_number: int = 1507) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), "--issue-number", str(issue_number)],
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

    def test_e2e_subprocess_requires_issue_number(self):
        """GIVEN validator invoked without --issue-number WHEN it runs THEN
        argparse fails closed (exit code 2, no stdout JSON emitted)."""
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH)],
            input=_approve_envelope().encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 2
        assert b"--issue-number" in proc.stderr


# ---------------------------------------------------------------------------
# AC15/AC16: active issue namespace binding
# ---------------------------------------------------------------------------


class TestIssueNumberBinding:
    def test_artifact_issue_number_mismatch_rejected(self):
        """GIVEN an approve envelope whose ARTIFACT issue segment (1507)
        does not match the bound --issue-number (9999) WHEN validated THEN
        artifact_issue_number_mismatch."""
        text = _approve_envelope()
        result = validate_review_compact_output(text, issue_number=9999)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "artifact_issue_number_mismatch" in codes

    def test_artifact_issue_number_match_is_valid(self):
        """GIVEN an approve envelope whose ARTIFACT issue segment matches
        the bound --issue-number WHEN validated THEN valid."""
        text = _approve_envelope()
        result = validate_review_compact_output(text, issue_number=1507)
        assert result["validation_status"] == "valid"

    def test_artifact_unknown_namespace_rejected(self):
        """GIVEN ARTIFACT issue segment 'unknown' WHEN validated THEN
        artifact_issue_segment_unknown_rejected, regardless of --issue-number."""
        text = _approve_envelope(
            artifact_path=".claude/artifacts/issue-refinement-loop/unknown/compact_review_result_20260714T113303Z.json"
        )
        result = validate_review_compact_output(text, issue_number=1507)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "artifact_issue_segment_unknown_rejected" in codes

    @pytest.mark.parametrize("segment", ["0", "0001507"])
    def test_artifact_zero_or_leading_zero_namespace_rejected(self, segment):
        """GIVEN ARTIFACT issue segment '0' or a leading-zero segment WHEN
        validated THEN artifact_issue_segment_zero_or_leading_zero_rejected,
        regardless of --issue-number."""
        text = _approve_envelope(
            artifact_path=f".claude/artifacts/issue-refinement-loop/{segment}/compact_review_result_20260714T113303Z.json"
        )
        result = validate_review_compact_output(text, issue_number=1507)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "artifact_issue_segment_zero_or_leading_zero_rejected" in codes


# ---------------------------------------------------------------------------
# AC17-AC20: producer-derived field invariants
# ---------------------------------------------------------------------------


class TestProducerDerivedInvariants:
    def test_must_read_non_empty_rejected(self):
        """GIVEN MUST_READ is non-empty WHEN validated THEN
        must_read_non_empty_rejected."""
        text = _approve_envelope(must_read="docs/dev/some-file.md")
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "must_read_non_empty_rejected" in codes

    def test_evidence_artifact_mismatch_rejected(self):
        """GIVEN EVIDENCE does not match the ARTIFACT path (with the
        compact_review_result_v1= prefix stripped) WHEN validated THEN
        evidence_artifact_mismatch."""
        artifact_path = ".claude/artifacts/issue-refinement-loop/1507/compact_review_result_20260714T113303Z.json"
        lines = _approve_envelope(artifact_path=artifact_path).split("\n")
        # Tamper EVIDENCE (index 6) only, leaving ARTIFACT (index 7) intact.
        lines[6] = "EVIDENCE: .claude/artifacts/issue-refinement-loop/1507/compact_review_result_99999999T999999Z.json"
        text = "\n".join(lines)
        result = validate_review_compact_output(text, issue_number=1507)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "evidence_artifact_mismatch" in codes

    def test_artifact_filename_pattern_rejected(self):
        """GIVEN the ARTIFACT filename does not match
        compact_review_result_YYYYMMDDTHHMMSSZ.json WHEN validated THEN
        artifact_filename_pattern_invalid."""
        text = _approve_envelope(
            artifact_path=".claude/artifacts/issue-refinement-loop/1507/some_other_file.json"
        )
        result = validate_review_compact_output(text, issue_number=1507)
        assert result["validation_status"] == "invalid"
        codes = {v["code"] for v in result["violations"]}
        assert "artifact_filename_pattern_invalid" in codes

    def test_summary_invariant_rejected(self):
        """GIVEN SUMMARY does not match the expected invariant for its
        envelope kind (approve: 'contract ready' exact; needs-fix:
        'N blocker(s)(; first=<code>)?') WHEN validated THEN
        summary_invariant_invalid."""
        approve_text = _approve_envelope(summary="looks fine to me")
        approve_result = validate_review_compact_output(approve_text)
        assert approve_result["validation_status"] == "invalid"
        approve_codes = {v["code"] for v in approve_result["violations"]}
        assert "summary_invariant_invalid" in approve_codes

        needs_fix_lines = _needs_fix_envelope().split("\n")
        needs_fix_lines[2] = "SUMMARY: something is wrong"
        needs_fix_text = "\n".join(needs_fix_lines)
        needs_fix_result = validate_review_compact_output(needs_fix_text)
        assert needs_fix_result["validation_status"] == "invalid"
        needs_fix_codes = {v["code"] for v in needs_fix_result["violations"]}
        assert "summary_invariant_invalid" in needs_fix_codes

    def test_summary_invariant_needs_fix_first_code_suffix_is_valid(self):
        """GIVEN needs-fix SUMMARY matches 'N blocker(s); first=<code>' WHEN
        validated THEN no summary_invariant_invalid violation."""
        lines = _needs_fix_envelope().split("\n")
        lines[2] = "SUMMARY: 2 blocker(s); first=LP010"
        text = "\n".join(lines)
        result = validate_review_compact_output(text)
        assert result["validation_status"] == "valid", result["violations"]


# ---------------------------------------------------------------------------
# AC21: mutation testing against real compact_review_result.py output
# ---------------------------------------------------------------------------


class TestProducerOutputMutationTesting:
    def test_producer_output_mutation_testing(self, tmp_path, monkeypatch):
        """GIVEN real compact_review_result.py approve/needs-fix stdout
        WHEN validated THEN valid; WHEN MUST_READ / EVIDENCE / ARTIFACT
        filename / ARTIFACT issue segment are each individually mutated
        THEN each mutation independently causes validation_status invalid
        (Issue #1507 AC21)."""
        monkeypatch.chdir(tmp_path)
        sys.path.insert(0, str(SCRIPTS_DIR))
        from compact_review_result import compact_review_result

        approve_raw = json.loads((FIXTURES_DIR / "review_result_approve.json").read_text(encoding="utf-8"))
        _compact, stdout_lines, _artifact_path, _content = compact_review_result(
            approve_raw,
            artifact_dir=Path(".claude/artifacts/issue-refinement-loop"),
            issue_number=1507,
            repo_root=tmp_path,
        )
        baseline_text = "\n".join(stdout_lines)
        baseline_result = validate_review_compact_output(baseline_text, issue_number=1507)
        assert baseline_result["validation_status"] == "valid", baseline_result["violations"]

        field_index = {line.split(": ", 1)[0]: i for i, line in enumerate(stdout_lines)}

        # Mutation 1: MUST_READ non-empty
        mutated = list(stdout_lines)
        mutated[field_index["MUST_READ"]] = "MUST_READ: docs/dev/some-file.md"
        result = validate_review_compact_output("\n".join(mutated), issue_number=1507)
        assert result["validation_status"] == "invalid"
        assert "must_read_non_empty_rejected" in {v["code"] for v in result["violations"]}

        # Mutation 2: EVIDENCE diverges from ARTIFACT
        mutated = list(stdout_lines)
        mutated[field_index["EVIDENCE"]] = (
            "EVIDENCE: .claude/artifacts/issue-refinement-loop/1507/compact_review_result_99999999T999999Z.json"
        )
        result = validate_review_compact_output("\n".join(mutated), issue_number=1507)
        assert result["validation_status"] == "invalid"
        assert "evidence_artifact_mismatch" in {v["code"] for v in result["violations"]}

        # Mutation 3: ARTIFACT filename no longer matches the canonical shape
        mutated = list(stdout_lines)
        artifact_line = mutated[field_index["ARTIFACT"]]
        prefix, _, path = artifact_line.partition("=")
        mutated_path = str(Path(path).parent / "not_a_compact_result.json")
        mutated[field_index["ARTIFACT"]] = f"{prefix}={mutated_path}"
        result = validate_review_compact_output("\n".join(mutated), issue_number=1507)
        assert result["validation_status"] == "invalid"
        assert "artifact_filename_pattern_invalid" in {v["code"] for v in result["violations"]}

        # Mutation 4: ARTIFACT issue segment diverges from the bound --issue-number
        mutated = list(stdout_lines)
        artifact_line = mutated[field_index["ARTIFACT"]]
        prefix, _, path = artifact_line.partition("=")
        p = Path(path)
        mutated_path = p.parent.parent / "1508" / p.name
        mutated[field_index["ARTIFACT"]] = f"{prefix}={mutated_path}"
        result = validate_review_compact_output("\n".join(mutated), issue_number=1507)
        assert result["validation_status"] == "invalid"
        assert "artifact_issue_number_mismatch" in {v["code"] for v in result["violations"]}


# ---------------------------------------------------------------------------
# Issue #1532 (V2): parent-local replay integrity binding envelope
# ---------------------------------------------------------------------------

import parent_replay_binding as _pb  # noqa: E402

_V2_BODY_BYTES = b"issue body used for the V2 parity fixture"
_V2_BODY_SHA256 = f"sha256:{hashlib.sha256(_V2_BODY_BYTES).hexdigest()}"
_V2_IDENTITY = dict(
    repository_full_name="squne121/loop-protocol",
    issue_number=1507,
    refinement_session_id="session-v2",
    iteration_id="iteration-v2",
)


def _v2_claim(*, blockers=None) -> dict:
    return {
        "schema": "REVIEWER_BLOCKER_CLAIM_V1",
        "body_sha256": _V2_BODY_SHA256,
        "blockers": blockers
        if blockers is not None
        else [{"reviewer_blocker_code": "LP010", "message": "ac/vc mismatch", "line_start": 5, "line_end": 5}],
    }


def _v2_readiness() -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": _V2_BODY_SHA256,
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


def _v2_binding_artifact(**overrides) -> dict:
    kwargs = dict(
        reviewer_blocker_claim=_v2_claim(),
        readiness_result=_v2_readiness(),
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=None,
        current_body_bytes=_V2_BODY_BYTES,
        issue_url="https://github.com/squne121/loop-protocol/issues/1507",
        **_V2_IDENTITY,
    )
    kwargs.update(overrides)
    return _pb.build_parent_replay_binding(**kwargs)


def _v2_needs_fix_envelope(
    *,
    artifact_path: str = ".claude/artifacts/issue-refinement-loop/1507/compact_review_result_20260714T113304Z.json",
    binding_artifact: dict | None = None,
    reviewer_blocker_claim_line: str | None = None,
    parent_replay_verdict: str | None = None,
    parent_replay_routing: str | None = None,
    parent_replay_should_consume: str | None = None,
    parent_replay_body_sha256: str | None = None,
    parent_replay_next_state: str | None = None,
    parent_replay_binding_digest: str | None = None,
) -> tuple[str, dict]:
    binding_artifact = binding_artifact if binding_artifact is not None else _v2_binding_artifact()
    replay_result = binding_artifact["replay_result"]
    claim_line = reviewer_blocker_claim_line or _pb.canonical_json_bytes(
        _pb.validate_reviewer_blocker_claim(_v2_claim())
    ).decode("utf-8")
    base = _approve_envelope(
        verdict="needs-fix",
        summary="1 blocker(s)",
        blockers="1",
        next_action="request_changes",
        artifact_path=artifact_path,
    )
    extra = [
        f"REVIEWER_BLOCKER_CLAIM: {claim_line}",
        f"PARENT_REPLAY_VERDICT: {parent_replay_verdict or replay_result['verdict']}",
        f"PARENT_REPLAY_ROUTING: {parent_replay_routing or replay_result['routing']}",
        "PARENT_REPLAY_SHOULD_CONSUME: "
        + (
            parent_replay_should_consume
            if parent_replay_should_consume is not None
            else ("true" if replay_result["should_consume_iteration"] else "false")
        ),
        f"PARENT_REPLAY_BODY_SHA256: {parent_replay_body_sha256 or replay_result['body_sha256']}",
        "PARENT_REPLAY_NEXT_STATE: "
        + (parent_replay_next_state or _pb.canonical_replay_next_state_line(binding_artifact)),
        f"PARENT_REPLAY_BINDING_DIGEST: {parent_replay_binding_digest or binding_artifact['binding_digest']}",
    ]
    return base + "\n" + "\n".join(extra), binding_artifact


def _validate_v2(envelope: str, binding_artifact: dict, **overrides) -> dict:
    kwargs = dict(
        issue_number=_V2_IDENTITY["issue_number"],
        binding_artifact=binding_artifact,
        repository_full_name=_V2_IDENTITY["repository_full_name"],
        refinement_session_id=_V2_IDENTITY["refinement_session_id"],
        iteration_id=_V2_IDENTITY["iteration_id"],
        current_body_sha256=_V2_BODY_SHA256,
    )
    kwargs.update(overrides)
    return validate_review_compact_output_v2(envelope, **kwargs)


class TestV2ParentBindingEnvelope:
    """GIVEN an ISSUE_REVIEW_RESULT_COMPACT_V2 needs-fix envelope THEN the
    V2 validator accepts the exact 15-line grammar (child's bounded claim +
    parent-computed PARENT_REPLAY_* fields) ONLY against a REQUIRED,
    independently-supplied binding artifact, and fails closed on any tamper
    (Issue #1532 AC4/High-1)."""

    def test_valid_v2_envelope_is_accepted(self):
        envelope, binding_artifact = _v2_needs_fix_envelope()
        result = _validate_v2(envelope, binding_artifact)
        assert result["validation_status"] == "valid", result["violations"]
        assert result["envelope_kind"] == "needs_fix_v2"
        assert "PARENT_REPLAY_NEXT_STATE" in result["normalized_payload"]
        assert "PARENT_REPLAY_BINDING_DIGEST" in result["normalized_payload"]
        # The retired child self-report fields never appear in V2 output.
        assert "REPLAY_VERDICT" not in result["normalized_payload"]

    def test_v1_approve_envelopes_are_unaffected_by_v2_validator(self):
        """GIVEN a plain V1 approve envelope THEN validate_review_compact_output_v2
        delegates to the V1 result unchanged (V2 is purely additive for approve)."""
        envelope = _approve_envelope()
        v1_result = validate_review_compact_output(envelope, issue_number=1507)
        binding_artifact = _v2_binding_artifact()
        v2_result = _validate_v2(envelope, binding_artifact)
        assert v2_result == v1_result

    def test_v1_needs_fix_envelope_is_never_valid_under_v2(self):
        """Issue #1532 Blocker 2: the retired V1 13-line REPLAY_* child
        self-report envelope must NOT be accepted by the V2 validator --
        there is no producer that emits it anymore, and it must not
        silently satisfy the V2 15-line grammar either."""
        envelope = _needs_fix_envelope()
        binding_artifact = _v2_binding_artifact()
        result = _validate_v2(envelope, binding_artifact)
        assert result["validation_status"] == "invalid"
        assert result["envelope_kind"] == "unknown"

    def test_missing_expected_binding_artifact_is_a_type_error_at_call_site(self):
        """High-1: there is no optional binding_artifact code path -- a
        caller omitting it entirely fails to construct a valid call."""
        envelope, _binding_artifact = _v2_needs_fix_envelope()
        with pytest.raises(TypeError):
            validate_review_compact_output_v2(  # type: ignore[call-arg]
                envelope,
                issue_number=1507,
                repository_full_name=_V2_IDENTITY["repository_full_name"],
                refinement_session_id=_V2_IDENTITY["refinement_session_id"],
                iteration_id=_V2_IDENTITY["iteration_id"],
                current_body_sha256=_V2_BODY_SHA256,
            )

    def test_binding_artifact_digest_tamper_fails_closed(self):
        envelope, binding_artifact = _v2_needs_fix_envelope()
        tampered_artifact = dict(binding_artifact)
        tampered_artifact["binding_digest"] = f"sha256:{_fake_sha256('a-different-value')}"
        result = _validate_v2(envelope, tampered_artifact)
        assert result["validation_status"] == "invalid"
        assert result["next_action"] == "human_judgment_required"
        assert "binding_artifact_digest_self_inconsistent" in {v["code"] for v in result["violations"]}

    def test_envelope_parent_replay_binding_digest_mismatch_fails_closed(self):
        envelope, binding_artifact = _v2_needs_fix_envelope(
            parent_replay_binding_digest=f"sha256:{_fake_sha256('forged')}"
        )
        result = _validate_v2(envelope, binding_artifact)
        assert result["validation_status"] == "invalid"
        assert "parent_replay_binding_digest_mismatch" in {v["code"] for v in result["violations"]}

    def test_envelope_parent_replay_next_state_mismatch_fails_closed(self):
        envelope, binding_artifact = _v2_needs_fix_envelope(
            parent_replay_next_state='{"schema":"different"}'
        )
        result = _validate_v2(envelope, binding_artifact)
        assert result["validation_status"] == "invalid"
        assert "parent_replay_next_state_mismatch" in {v["code"] for v in result["violations"]}

    def test_malformed_parent_replay_next_state_json_fails_closed(self):
        envelope, binding_artifact = _v2_needs_fix_envelope(parent_replay_next_state="not-json{{")
        result = _validate_v2(envelope, binding_artifact)
        assert result["validation_status"] == "invalid"
        assert "parent_replay_next_state_invalid_json" in {v["code"] for v in result["violations"]}

    def test_malformed_parent_replay_binding_digest_format_fails_closed(self):
        envelope, binding_artifact = _v2_needs_fix_envelope(parent_replay_binding_digest="not-a-digest")
        result = _validate_v2(envelope, binding_artifact)
        assert result["validation_status"] == "invalid"
        assert "parent_replay_binding_digest_invalid_format" in {v["code"] for v in result["violations"]}

    def test_missing_v2_field_falls_back_to_unknown(self):
        """Dropping PARENT_REPLAY_BINDING_DIGEST leaves a 14-line sequence
        that matches neither the V1 13-line needs-fix template nor the V2
        15-line template -- fail-closed to unknown/invalid, never silently
        accepted as V1."""
        envelope, binding_artifact = _v2_needs_fix_envelope()
        lines = envelope.split("\n")[:-1]  # drop PARENT_REPLAY_BINDING_DIGEST
        result = _validate_v2("\n".join(lines), binding_artifact)
        assert result["validation_status"] == "invalid"
        assert result["envelope_kind"] == "unknown"

    def test_forged_child_verdict_does_not_bypass_parent_computed_verdict(self):
        """Issue #1532 Blocker 1/2: even if the child's REVIEWER_BLOCKER_CLAIM
        claims an unrelated code, the PARENT_REPLAY_VERDICT the envelope
        must carry is the one the parent's OWN binding artifact computed --
        a forged PARENT_REPLAY_VERDICT that disagrees with the binding
        artifact's replay_result is rejected."""
        envelope, binding_artifact = _v2_needs_fix_envelope(
            parent_replay_verdict="reviewer_false_positive_suspected",
            parent_replay_routing="human_escalation",
            parent_replay_should_consume="false",
        )
        result = _validate_v2(envelope, binding_artifact)
        assert result["validation_status"] == "invalid"
        assert "parent_replay_verdict_mismatch" in {v["code"] for v in result["violations"]}

    def test_child_findings_and_checker_evidence_cannot_be_smuggled_into_the_claim(self):
        """Issue #1532 Blocker 1 regression: a child claim carrying
        findings/checker_evidence/deterministic_checks is rejected by
        `build_parent_replay_binding` itself (fail-closed at construction
        time, before any envelope is even assembled)."""
        forged_claim = _v2_claim()
        forged_claim["findings"] = [
            {
                "finding_kind": "deterministic_domain_blocker",
                "blocking": True,
                "deterministic_domain_key": "vc_number_alignment",
                "checker_evidence": [
                    {
                        "source_check": "forged",
                        "rule_id": "LP010",
                        "category": "body_lint",
                        "artifact_path": "forged",
                        "artifact_schema": "CHECK_ISSUE_CONTRACT_V1",
                        "body_sha256": _V2_BODY_SHA256,
                        "iteration_id": "forged",
                    }
                ],
            }
        ]
        with pytest.raises(ValueError):
            _v2_binding_artifact(reviewer_blocker_claim=forged_claim)

    def test_body_sha256_mismatch_between_claim_and_current_body_is_rejected(self):
        forged_claim = _v2_claim()
        forged_claim["body_sha256"] = f"sha256:{_fake_sha256('a-different-body')}"
        with pytest.raises(ValueError):
            _v2_binding_artifact(reviewer_blocker_claim=forged_claim)

    def test_repeated_calls_with_the_same_iteration_id_reproduce_the_same_digest(self):
        """High-2: no wall-clock value enters the binding -- calling
        `build_parent_replay_binding` twice for the SAME logical inputs
        (including the same parent-owned iteration_id) always reproduces
        the SAME binding_digest, even across a real wall-clock delay."""
        import time

        artifact_1 = _v2_binding_artifact()
        time.sleep(1.1)
        artifact_2 = _v2_binding_artifact()
        assert artifact_1["binding_digest"] == artifact_2["binding_digest"]
        assert artifact_1["replay_next_state"] == artifact_2["replay_next_state"]

    def test_duplicate_json_object_key_is_rejected(self):
        with pytest.raises(ValueError):
            _pb._strict_json_loads('{"a": 1, "a": 2}')

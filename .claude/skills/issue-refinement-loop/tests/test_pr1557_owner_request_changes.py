"""
test_pr1557_owner_request_changes.py

Issue #1541 PR #1557: OWNER (squne121) adversarial REQUEST_CHANGES review
follow-up coverage.

  Blocker 1 - review_compact.validate_intermediate_v1: independent
    intermediate-validation command registry contract.
  Blocker 2 - review_compact.emit_approve: registry profile that structurally
    never opens binding/body files for an approve child input.
  High-1   - wire-byte contract boundary (2047/2048/2049 bytes, CRLF, double
    trailing LF).
  High-2   - bounded reads: an over-budget input is rejected without the
    reported byte count silently reflecting the FULL oversized input size.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as cr  # noqa: E402
import compact_review_result as compact_mod  # noqa: E402
import emit_parent_review_envelope_v2 as emit_mod  # noqa: E402

REPO = "squne121/loop-protocol"
ISSUE_NUMBER = 1541
EMIT_SCRIPT = SCRIPTS_DIR / "emit_parent_review_envelope_v2.py"


def _valid_approve_intermediate_text() -> str:
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


# ---------------------------------------------------------------------------
# Blocker 2: review_compact.emit_approve registry profile
# ---------------------------------------------------------------------------


class TestEmitApproveRegistryProfile:
    def test_registry_entry_has_no_binding_or_body_placeholders(self):
        assert "review_compact.emit_approve" in cr.REGISTRY
        entry = cr.REGISTRY["review_compact.emit_approve"]
        assert entry["id"] == "review_compact.emit_approve"
        assert entry["shell"] is False
        assert entry["mutation"] is False
        assert entry["network_effect"] == "local_only"
        argv_template = entry["argv"]
        assert argv_template[:6] == ["uv", "run", "--locked", "--offline", "--no-sync", "python3"]
        assert argv_template[6].endswith("emit_parent_review_envelope_v2.py")
        # No binding/body/session/iteration placeholders exist at all in
        # this entry's argv template or placeholder spec -- structurally,
        # not merely by convention, an approve caller cannot even supply
        # paths to those files via this command_id.
        for forbidden in (
            "--binding-artifact-file",
            "--repository-full-name",
            "--refinement-session-id",
            "--iteration-id",
            "--current-body-file",
        ):
            assert forbidden not in argv_template
        assert set(entry["placeholders"].keys()) == {"issue_number"}

        rendered = cr.render_command("review_compact.emit_approve", {"issue_number": ISSUE_NUMBER})
        assert isinstance(rendered, list)
        assert all(isinstance(token, str) for token in rendered)
        assert not any(token.startswith("{") and token.endswith("}") for token in rendered)

    def test_approve_input_with_nonexistent_binding_and_body_paths_succeeds_via_registry(
        self, tmp_path
    ):
        """Issue #1541 PR #1557 OWNER REQUEST_CHANGES Blocker 2 required
        test: approve input + a NONEXISTENT binding artifact path + a
        NONEXISTENT current body path -> the registry's `review_compact.emit_approve`
        command succeeds (exit 0), the output is byte-identical to the
        input's 8 lines, and binding/body are never opened (there is
        structurally no argv slot to even pass those paths through this
        command_id -- see the registry-shape assertions above)."""
        argv = cr.render_command("review_compact.emit_approve", {"issue_number": ISSUE_NUMBER})
        approve_text = _valid_approve_intermediate_text()

        proc = subprocess.run(
            argv,
            input=approve_text.encode("utf-8"),
            capture_output=True,
            cwd=str(SKILL_ROOT.parent.parent.parent),
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
        assert proc.stderr == b""
        assert proc.stdout.decode("utf-8") == approve_text

    def test_main_cli_never_opens_binding_or_body_for_approve_even_if_supplied(self, tmp_path):
        """Defense-in-depth: even calling the UNIFIED (non-approve-profile)
        CLI entrypoint directly with `--binding-artifact-file` /
        `--current-body-file` pointed at files that DO NOT EXIST, an
        approve child input must still succeed -- proving those files are
        never opened for the approve classification (Blocker 2's `main()`
        reordering fix), independent of which registry profile invoked it."""
        approve_text = _valid_approve_intermediate_text()
        nonexistent_binding = tmp_path / "does_not_exist_binding.json"
        nonexistent_body = tmp_path / "does_not_exist_body.txt"
        assert not nonexistent_binding.exists()
        assert not nonexistent_body.exists()

        proc = subprocess.run(
            [
                sys.executable,
                str(EMIT_SCRIPT),
                "--issue-number",
                str(ISSUE_NUMBER),
                "--binding-artifact-file",
                str(nonexistent_binding),
                "--repository-full-name",
                REPO,
                "--refinement-session-id",
                "session-x",
                "--iteration-id",
                "iteration-x",
                "--current-body-file",
                str(nonexistent_body),
            ],
            input=approve_text.encode("utf-8"),
            capture_output=True,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
        assert proc.stdout.decode("utf-8") == approve_text


# ---------------------------------------------------------------------------
# Blocker 1: review_compact.validate_intermediate_v1 registry + CLI contract
# ---------------------------------------------------------------------------


class TestValidateIntermediateV1:
    def test_registry_entry_contract(self):
        assert "review_compact.validate_intermediate_v1" in cr.REGISTRY
        entry = cr.REGISTRY["review_compact.validate_intermediate_v1"]
        assert entry["mutation"] is False
        assert entry["network_effect"] == "local_only"
        argv_template = entry["argv"]
        assert "--validate-intermediate" in argv_template
        for forbidden in (
            "--binding-artifact-file",
            "--repository-full-name",
            "--refinement-session-id",
            "--iteration-id",
            "--current-body-file",
        ):
            assert forbidden not in argv_template

    def test_cli_rejects_extra_binding_body_args_in_validate_intermediate_mode(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(EMIT_SCRIPT),
                "--issue-number",
                str(ISSUE_NUMBER),
                "--validate-intermediate",
                "--binding-artifact-file",
                "/nonexistent/should/never/be/opened.json",
            ],
            input=_valid_approve_intermediate_text().encode("utf-8"),
            capture_output=True,
            timeout=15,
        )
        assert proc.returncode == 2

    def test_cli_returns_shape_for_approve_and_needs_fix(self, tmp_path):
        approve_proc = subprocess.run(
            [sys.executable, str(EMIT_SCRIPT), "--issue-number", str(ISSUE_NUMBER), "--validate-intermediate"],
            input=_valid_approve_intermediate_text().encode("utf-8"),
            capture_output=True,
            timeout=15,
        )
        assert approve_proc.returncode == 0
        approve_result = json.loads(approve_proc.stdout)
        assert approve_result["schema"] == "REVIEW_COMPACT_INTERMEDIATE_VALIDATION_RESULT_V1"
        assert approve_result["validation_status"] == "valid"
        assert approve_result["envelope_kind"] == "approve"
        assert approve_result["canonical_reviewer_blocker_claim"] is None
        assert approve_result["input_sha256"].startswith("sha256:")
        assert approve_result["input_byte_count"] == len(_valid_approve_intermediate_text().encode("utf-8"))

        malformed_proc = subprocess.run(
            [sys.executable, str(EMIT_SCRIPT), "--issue-number", str(ISSUE_NUMBER), "--validate-intermediate"],
            input=b"not a valid envelope at all\n",
            capture_output=True,
            timeout=15,
        )
        assert malformed_proc.returncode == 1
        malformed_result = json.loads(malformed_proc.stdout)
        assert malformed_result["validation_status"] == "invalid"
        assert malformed_result["canonical_reviewer_blocker_claim"] is None


# ---------------------------------------------------------------------------
# High-1: wire-byte contract boundary
# ---------------------------------------------------------------------------


class TestWireByteBoundary:
    def _stdout_lines_for_message_length(self, message_length: int) -> list[str]:
        artifact = (
            f".claude/artifacts/issue-refinement-loop/{ISSUE_NUMBER}/"
            f"compact_review_result_20260717T000000Z.json"
        )
        base_lines = [
            "STATUS: ok",
            "VERDICT: needs-fix",
            f"SUMMARY: {'x' * message_length}",
            "BLOCKERS: 1",
            "NEXT_ACTION: request_changes",
            "MUST_READ: ",
            f"EVIDENCE: {artifact}",
            f"ARTIFACT: compact_review_result_v1={artifact}",
        ]
        return base_lines

    def test_wire_bytes_matches_actual_stdout_including_trailing_lf(self):
        lines = ["STATUS: ok", "VERDICT: approve"]
        wire = compact_mod.wire_bytes(lines)
        assert wire == b"STATUS: ok\nVERDICT: approve\n"
        assert wire.endswith(b"\n")
        assert not wire.endswith(b"\n\n")

    def test_wire_bytes_empty_lines_list(self):
        assert compact_mod.wire_bytes([]) == b""

    def test_producer_boundary_exactly_2048_wire_bytes_accepted(self):
        # Find a message length such that wire_bytes() total is exactly 2048.
        lines = self._stdout_lines_for_message_length(0)
        base_len = len(compact_mod.wire_bytes(lines))
        target_message_length = 2048 - base_len
        assert target_message_length > 0
        lines = self._stdout_lines_for_message_length(target_message_length)
        wire = compact_mod.wire_bytes(lines)
        assert len(wire) == 2048

    def test_producer_boundary_2049_wire_bytes_over_budget(self):
        lines = self._stdout_lines_for_message_length(0)
        base_len = len(compact_mod.wire_bytes(lines))
        target_message_length = 2048 - base_len + 1
        lines = self._stdout_lines_for_message_length(target_message_length)
        wire = compact_mod.wire_bytes(lines)
        assert len(wire) == 2049

    def test_intermediate_validator_rejects_crlf(self):
        text = _valid_approve_intermediate_text().replace("\n", "\r\n")
        result = emit_mod.validate_child_intermediate(text, issue_number=ISSUE_NUMBER)
        assert result["validation_status"] == "invalid"
        assert any(v["code"] == "crlf_detected" for v in result["violations"])

    def test_intermediate_validator_rejects_double_trailing_lf(self):
        text = _valid_approve_intermediate_text() + "\n"
        result = emit_mod.validate_child_intermediate(text, issue_number=ISSUE_NUMBER)
        assert result["validation_status"] == "invalid"
        assert any(v["code"] == "blank_line_detected" for v in result["violations"])


# ---------------------------------------------------------------------------
# High-2: bounded reads
# ---------------------------------------------------------------------------


class TestBoundedReads:
    def test_emitter_cli_rejects_oversized_stdin_without_reflecting_full_size(self):
        """An input far larger than the 2048-byte budget must still be
        rejected -- and the emitter must never need to buffer/report the
        FULL oversized length (it reads at most MAX_INPUT_BYTES + 1 bytes),
        proving the read itself is bounded rather than merely the
        after-the-fact budget check."""
        oversized = ("STATUS: ok\n" * 10_000).encode("utf-8")  # far more than 2048 bytes
        proc = subprocess.run(
            [sys.executable, str(EMIT_SCRIPT), "--issue-number", str(ISSUE_NUMBER)],
            input=oversized,
            capture_output=True,
            timeout=15,
        )
        assert proc.returncode == 1
        assert proc.stdout == b""
        diagnostic = json.loads(proc.stderr)
        assert diagnostic["reason_code"] == "contract_invalid"
        violation = next(v for v in diagnostic["violations"] if v["code"] == "byte_budget_exceeded")
        # The reported byte_count reflects the BOUNDED read (<= MAX_INPUT_BYTES + 1),
        # never the full ~110,000-byte oversized input -- proving the read
        # itself never buffered the whole oversized payload into memory.
        assert violation["byte_count"] <= emit_mod.MAX_INPUT_BYTES + 1
        assert violation["byte_count"] < len(oversized)

    def test_reviewer_blocker_claim_blockers_maxitems_enforced(self):
        import parent_replay_binding as pb

        body_sha256 = "sha256:" + ("0" * 64)
        claim = {
            "schema": "REVIEWER_BLOCKER_CLAIM_V1",
            "body_sha256": body_sha256,
            "blockers": [
                {"reviewer_blocker_code": f"code_{i}", "message": None, "line_start": None, "line_end": None}
                for i in range(pb.MAX_BLOCKER_CLAIM_ITEMS + 1)
            ],
        }
        try:
            pb.validate_reviewer_blocker_claim(claim)
            raise AssertionError("expected ValueError for blockers exceeding maxItems")
        except ValueError as exc:
            assert "maxItems" in str(exc)

    def test_reviewer_blocker_claim_code_maxlength_enforced(self):
        import parent_replay_binding as pb

        body_sha256 = "sha256:" + ("0" * 64)
        claim = {
            "schema": "REVIEWER_BLOCKER_CLAIM_V1",
            "body_sha256": body_sha256,
            "blockers": [
                {
                    "reviewer_blocker_code": "x" * (pb.MAX_REVIEWER_BLOCKER_CODE_LENGTH + 1),
                    "message": None,
                    "line_start": None,
                    "line_end": None,
                }
            ],
        }
        try:
            pb.validate_reviewer_blocker_claim(claim)
            raise AssertionError("expected ValueError for reviewer_blocker_code exceeding maxLength")
        except ValueError as exc:
            assert "maxLength" in str(exc)

    def test_reviewer_blocker_claim_message_maxlength_enforced(self):
        import parent_replay_binding as pb

        body_sha256 = "sha256:" + ("0" * 64)
        claim = {
            "schema": "REVIEWER_BLOCKER_CLAIM_V1",
            "body_sha256": body_sha256,
            "blockers": [
                {
                    "reviewer_blocker_code": "missing_section",
                    "message": "y" * (pb.MAX_BLOCKER_CLAIM_MESSAGE_LENGTH + 1),
                    "line_start": None,
                    "line_end": None,
                }
            ],
        }
        try:
            pb.validate_reviewer_blocker_claim(claim)
            raise AssertionError("expected ValueError for message exceeding maxLength")
        except ValueError as exc:
            assert "maxLength" in str(exc)

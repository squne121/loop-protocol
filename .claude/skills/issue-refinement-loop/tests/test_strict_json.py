from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_refinement_phase_state as phase_state_builder  # noqa: E402
import emit_parent_review_envelope_v2 as _emit2  # noqa: E402


def _real_approve_child_bytes(issue_number: int) -> bytes:
    """Issue #1755 fix_delta P0 (OWNER REQUEST_CHANGES, PR #1826): a REAL,
    grammar-valid 8-line child-intermediate approve envelope (the exact shape
    `review_compact.validate_intermediate_v1` accepts), used so a genuine
    receipt can be produced by actually RUNNING the real validator against
    these bytes -- the review-phase gate now re-runs this same validator and
    rejects any hand-crafted receipt that does not match its output."""
    artifact_path = (
        f".claude/artifacts/issue-refinement-loop/{issue_number}/"
        "compact_review_result_20260728T000000Z.json"
    )
    lines = [
        "STATUS: ok",
        "VERDICT: approve",
        "SUMMARY: contract ready",
        "BLOCKERS: 0",
        "NEXT_ACTION: proceed",
        "MUST_READ: ",
        f"EVIDENCE: {artifact_path}",
        f"ARTIFACT: compact_review_result_v1={artifact_path}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_build_refinement_phase_state_rejects_nan_on_write(tmp_path, monkeypatch):
    """GIVEN phase state containing NaN WHEN CLI writes JSON THEN ValueError (strict JSON)."""
    source_path = tmp_path / "source.json"
    output_path = tmp_path / "phase_state.json"
    source_path.write_text("{}", encoding="utf-8")

    def _bad_phase_state(*args, **kwargs):
        return {"schema_version": "ISSUE_REFINEMENT_PHASE_STATE_V1", "bad": float("nan")}

    monkeypatch.setattr(phase_state_builder, "build_phase_state", _bad_phase_state)

    with pytest.raises(ValueError):
        phase_state_builder.main(
            [
                "--phase",
                "review",
                "--source-kind",
                "issue_review_result_compact_v1",
                "--source-path",
                str(source_path),
                "--review-result-path",
                str(source_path),
                # Issue #1507 AC24 / Issue #1755: review + issue_review_result_compact_v1
                # now requires --review-validation-result-path and --issue-number.
                # build_phase_state itself is monkeypatched away above, so these
                # arguments only need to satisfy argparse; their content is
                # never consulted.
                "--review-validation-result-path",
                str(source_path),
                "--issue-number",
                "1",
                "--output-path",
                str(output_path),
            ]
        )


def test_build_refinement_phase_state_cli_writes_strict_json(tmp_path):
    """GIVEN normal CLI usage WHEN output written THEN file contains parseable strict JSON.

    Issue #1755 fix_delta P0 (OWNER REQUEST_CHANGES, PR #1826): the
    review-phase gate now re-runs the REAL
    review_compact.validate_intermediate_v1 validator against --source-path's
    actual bytes and rejects any validation result that does not match its
    output exactly. This test therefore uses a REAL, grammar-valid child
    intermediate approve envelope as --source-path content, and produces the
    validation result by actually invoking the real validator function
    (never a hand-crafted "claimed valid" payload)."""
    source_path = tmp_path / "source.json"
    # Issue #1755 fix_delta: --source-path is now bound to REAL child
    # intermediate text (not JSON), so --review-result-path (a separate,
    # generically strict-JSON-validated argument) needs its OWN valid-JSON
    # file rather than reusing source_path.
    review_result_path = tmp_path / "review_result.json"
    output_path = tmp_path / "phase_state.json"
    validation_path = tmp_path / "validation.json"
    issue_number = 1755
    source_bytes = _real_approve_child_bytes(issue_number)
    source_path.write_bytes(source_bytes)
    review_result_path.write_text("{}", encoding="utf-8")
    validation_result = _emit2.build_validate_intermediate_result(
        source_bytes, issue_number=issue_number
    )
    assert validation_result["validation_status"] == "valid", validation_result
    validation_path.write_text(
        json.dumps(validation_result),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "build_refinement_phase_state.py"),
            "--phase",
            "review",
            "--source-kind",
            "issue_review_result_compact_v1",
            "--source-path",
            str(source_path),
            "--review-result-path",
            str(review_result_path),
            # Issue #1507 AC24 / Issue #1755: required for --phase review +
            # --source-kind issue_review_result_compact_v1.
            "--review-validation-result-path",
            str(validation_path),
            "--issue-number",
            "1755",
            "--output-path",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "ISSUE_REFINEMENT_PHASE_STATE_V1"


def test_build_refinement_phase_state_cli_rejects_nan_in_source_input(tmp_path):
    """GIVEN source JSON containing NaN WHEN CLI runs THEN it fails closed.

    Issue #1755: uses phase="preflight" / source_kind="refinement_preflight_result_v1"
    (rather than phase="review" / source_kind="issue_review_result_compact_v1")
    because the review-phase gate now intentionally skips strict-JSON
    validation of --source-path for that specific (phase, source_kind)
    combination -- --source-path there is bound to the raw child stdout
    BYTES fed to review_compact.validate_intermediate_v1 (plain
    field:value text, not JSON); see
    test_refinement_phase_gate_validation_seam_gates.py for that gate's
    dedicated fail-closed coverage. This test's intent (generic
    --source-path strict-JSON rejection) is orthogonal to the review-phase
    gate and is preserved unchanged via a non-gated (phase, source_kind)."""
    source_path = tmp_path / "source.json"
    output_path = tmp_path / "phase_state.json"
    source_path.write_text('{"bad": NaN}', encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "build_refinement_phase_state.py"),
            "--phase",
            "preflight",
            "--source-kind",
            "refinement_preflight_result_v1",
            "--source-path",
            str(source_path),
            "--output-path",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "strict json validation error" in proc.stdout


def test_build_refinement_phase_state_cli_rejects_infinity_in_review_input(tmp_path):
    """GIVEN review-result JSON containing Infinity WHEN CLI runs THEN it fails closed."""
    source_path = tmp_path / "source.json"
    review_path = tmp_path / "review.json"
    output_path = tmp_path / "phase_state.json"
    source_path.write_text("{}", encoding="utf-8")
    review_path.write_text('{"bad": Infinity}', encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "build_refinement_phase_state.py"),
            "--phase",
            "review",
            "--source-kind",
            "issue_review_result_compact_v1",
            "--source-path",
            str(source_path),
            "--review-result-path",
            str(review_path),
            "--output-path",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "strict json validation error" in proc.stdout

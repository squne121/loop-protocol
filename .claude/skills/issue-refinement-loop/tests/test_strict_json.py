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
                # Issue #1507 AC24: review + issue_review_result_compact_v1 now
                # requires --review-validation-result-path. build_phase_state
                # itself is monkeypatched away above, so this argument only
                # needs to satisfy argparse; its content is never consulted.
                "--review-validation-result-path",
                str(source_path),
                "--output-path",
                str(output_path),
            ]
        )


def test_build_refinement_phase_state_cli_writes_strict_json(tmp_path):
    """GIVEN normal CLI usage WHEN output written THEN file contains parseable strict JSON."""
    source_path = tmp_path / "source.json"
    output_path = tmp_path / "phase_state.json"
    validation_path = tmp_path / "validation.json"
    source_path.write_text("{}", encoding="utf-8")
    validation_path.write_text(
        json.dumps({"validation_status": "valid"}), encoding="utf-8"
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
            str(source_path),
            # Issue #1507 AC24: required for --phase review +
            # --source-kind issue_review_result_compact_v1.
            "--review-validation-result-path",
            str(validation_path),
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
    """GIVEN source JSON containing NaN WHEN CLI runs THEN it fails closed."""
    source_path = tmp_path / "source.json"
    output_path = tmp_path / "phase_state.json"
    source_path.write_text('{"bad": NaN}', encoding="utf-8")

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

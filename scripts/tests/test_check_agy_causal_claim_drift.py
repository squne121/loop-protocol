"""Tests for `check_agy_causal_claim_drift.py` (Issue #1778 AC3/AC6/AC7).

Covers:
- AC3: against the real repo files, `Issue #1758` references in
  `agy_permission_policy.py` are detected as drift (no
  `# SUPERSEDED (Issue #M): ...` marker) because
  `references/agy-headless-tool-use-investigation.md`
  (`status: resolved`) already mentions `#1758`, and the script fail-closes
  (non-zero exit).
- Hermetic positive/negative fixtures so the detector's logic -- not just
  this repo's current state -- is under test: a resolved doc mentioning the
  referenced Issue without a SUPERSEDED marker must be flagged; the same
  setup WITH a SUPERSEDED marker, or with a non-resolving doc status, must
  not be flagged.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "check_agy_causal_claim_drift.py"
)
_REAL_AGY_PERMISSION_POLICY = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skills"
    / "gemini-cli-headless-delegation"
    / "scripts"
    / "agy_permission_policy.py"
)
_REAL_REFERENCES_DIR = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skills"
    / "gemini-cli-headless-delegation"
    / "references"
)
_MODULE_NAME = "check_agy_causal_claim_drift_1778_test"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


app = _load_module()


# ---------------------------------------------------------------------------
# AC3: real-repo detection of the Issue #1758 drift case
# ---------------------------------------------------------------------------


def test_issue_1758_drift_detected_against_real_repo_files() -> None:
    manifest = app.build_manifest(
        [_REAL_AGY_PERMISSION_POLICY], _REAL_REFERENCES_DIR
    )
    matching = [
        f for f in manifest["findings"] if f["issue_number"] == 1758
    ]
    assert matching, "expected at least one Issue #1758 drift finding"
    for finding in matching:
        assert finding["kind"] == "causal_claim_drift"
        assert finding["severity"] == "p0"
        assert finding["doc_status"] == "resolved"
        assert "agy-headless-tool-use-investigation.md" in finding["doc_path"]


def test_issue_1758_drift_fail_closes_main_exit_code() -> None:
    exit_code = app.main(
        [
            "--code-target",
            str(_REAL_AGY_PERMISSION_POLICY),
            "--references-dir",
            str(_REAL_REFERENCES_DIR),
        ]
    )
    assert exit_code == 1


# ---------------------------------------------------------------------------
# Hermetic positive / negative fixtures
# ---------------------------------------------------------------------------


def _write_doc(
    references_dir: Path, filename: str, status: str, body: str
) -> Path:
    references_dir.mkdir(parents=True, exist_ok=True)
    doc_path = references_dir / filename
    doc_path.write_text(
        f"---\nissue: 999\nstatus: {status}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return doc_path


def test_drift_flagged_when_resolved_doc_mentions_issue_without_marker(
    tmp_path: Path,
) -> None:
    code = tmp_path / "fixture_code.py"
    code.write_text(
        "# Issue #4242: some historical design rationale\nVALUE = 1\n",
        encoding="utf-8",
    )
    references_dir = tmp_path / "references"
    _write_doc(
        references_dir,
        "fixture-investigation.md",
        "resolved",
        "## Live confirmation (Issue #4242)\n\nThis confirms the finding.",
    )
    manifest = app.build_manifest([code], references_dir)
    assert manifest["status"] == "findings_detected"
    assert any(f["issue_number"] == 4242 for f in manifest["findings"])


def test_drift_not_flagged_when_superseded_marker_present(
    tmp_path: Path,
) -> None:
    code = tmp_path / "fixture_code.py"
    code.write_text(
        "# Issue #4242: some historical design rationale\n"
        "# SUPERSEDED (Issue #5000): see fixture-investigation.md\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    references_dir = tmp_path / "references"
    _write_doc(
        references_dir,
        "fixture-investigation.md",
        "resolved",
        "## Live confirmation (Issue #4242)\n\nThis confirms the finding.",
    )
    manifest = app.build_manifest([code], references_dir)
    assert manifest["status"] == "ok"
    assert manifest["findings"] == []


def test_drift_not_flagged_when_doc_status_not_resolving(
    tmp_path: Path,
) -> None:
    code = tmp_path / "fixture_code.py"
    code.write_text(
        "# Issue #4242: some historical design rationale\nVALUE = 1\n",
        encoding="utf-8",
    )
    references_dir = tmp_path / "references"
    _write_doc(
        references_dir,
        "fixture-investigation.md",
        "in_progress_custom_status",
        "## Live confirmation (Issue #4242)\n\nThis confirms the finding.",
    )
    manifest = app.build_manifest([code], references_dir)
    assert manifest["status"] == "ok"
    assert manifest["findings"] == []


def test_drift_not_flagged_when_no_doc_mentions_issue(tmp_path: Path) -> None:
    code = tmp_path / "fixture_code.py"
    code.write_text(
        "# Issue #4242: some historical design rationale\nVALUE = 1\n",
        encoding="utf-8",
    )
    references_dir = tmp_path / "references"
    _write_doc(
        references_dir,
        "fixture-investigation.md",
        "resolved",
        "## Unrelated finding\n\nNo issue number mentioned here.",
    )
    manifest = app.build_manifest([code], references_dir)
    assert manifest["status"] == "ok"
    assert manifest["findings"] == []


# ---------------------------------------------------------------------------
# AC6/AC7: CLI entry point
# ---------------------------------------------------------------------------


def test_main_returns_zero_on_no_drift(tmp_path: Path) -> None:
    code = tmp_path / "fixture_code.py"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    references_dir = tmp_path / "references"
    references_dir.mkdir(parents=True, exist_ok=True)
    exit_code = app.main(
        [
            "--code-target",
            str(code),
            "--references-dir",
            str(references_dir),
        ]
    )
    assert exit_code == 0


def test_main_returns_two_on_missing_code_target(tmp_path: Path) -> None:
    exit_code = app.main(
        [
            "--code-target",
            str(tmp_path / "does_not_exist_1778.py"),
        ]
    )
    assert exit_code == 2

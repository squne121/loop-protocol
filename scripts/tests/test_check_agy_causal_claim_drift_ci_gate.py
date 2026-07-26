"""Integration tests for the Issue #1788 CI gate integration of
`check_agy_causal_claim_drift.py`.

Covers:
- AC4: a hermetic fixture with an intentional (unbaselined) causal-claim
  drift, invoked the same way the CI step invokes the script (subprocess,
  `--apply-baseline`), fails closed (exit code 1).
- AC5: a hermetic fixture with no drift (SUPERSEDED marker present) passes
  (exit code 0) under the same invocation.
- AC3 / "drift_gate_does_not_block_unrelated_pr": against the REAL repo
  target files, `--apply-baseline` produces exit code 0 (the 54 pre-existing
  findings are all in the baseline, so an unrelated PR is not blocked),
  while the same invocation WITHOUT `--apply-baseline` still fails closed
  with all pre-existing findings reported (proving the baseline flag does
  not change detection logic, only the gate's blocking decision -- Issue
  #1788 Out of Scope).

Uses `subprocess.run` (not the in-process `app.main()` helper used by
`test_check_agy_causal_claim_drift.py`) so this test exercises the exact
CLI invocation the CI workflow step performs (Issue #1788 AC4/AC5 require
"CI 相当のスクリプト実行").
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_agy_causal_claim_drift.py"


def _run_gate(*extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), *extra_args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_doc(references_dir: Path, status: str, body: str) -> Path:
    references_dir.mkdir(parents=True, exist_ok=True)
    doc_path = references_dir / "fixture-investigation.md"
    doc_path.write_text(
        f"---\nissue: 999\nstatus: {status}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return doc_path


# ---------------------------------------------------------------------------
# AC4: hermetic fixture with intentional drift fails closed
# ---------------------------------------------------------------------------


def test_drift_fixture_fails_closed(tmp_path: Path) -> None:
    code = tmp_path / "fixture_code.py"
    code.write_text(
        "# Issue #4242: some historical design rationale\nVALUE = 1\n",
        encoding="utf-8",
    )
    references_dir = tmp_path / "references"
    _write_doc(
        references_dir,
        "resolved",
        "## Live confirmation (Issue #4242)\n\nThis confirms the finding.",
    )

    result = _run_gate(
        "--code-target",
        str(code),
        "--references-dir",
        str(references_dir),
        "--apply-baseline",
    )

    assert result.returncode == 1, (
        f"expected exit 1 for unbaselined drift fixture, got "
        f"{result.returncode}: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    manifest = json.loads(result.stdout)
    assert manifest["status"] == "findings_detected"
    assert manifest["new_finding_count"] == 1
    assert manifest["baseline_count"] == 0


# ---------------------------------------------------------------------------
# AC5: hermetic fixture with no drift (SUPERSEDED marker) passes clean
# ---------------------------------------------------------------------------


def test_drift_fixture_passes_clean(tmp_path: Path) -> None:
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
        "resolved",
        "## Live confirmation (Issue #4242)\n\nThis confirms the finding.",
    )

    result = _run_gate(
        "--code-target",
        str(code),
        "--references-dir",
        str(references_dir),
        "--apply-baseline",
    )

    assert result.returncode == 0, (
        f"expected exit 0 for clean fixture, got {result.returncode}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    manifest = json.loads(result.stdout)
    assert manifest["status"] == "ok"
    assert manifest["new_finding_count"] == 0


# ---------------------------------------------------------------------------
# AC3: gate does not block an unrelated PR given the real repo's
# pre-#1788 baseline, while detection itself stays unchanged.
# ---------------------------------------------------------------------------


def test_drift_gate_does_not_block_unrelated_pr() -> None:
    baselined_result = _run_gate("--apply-baseline")
    assert baselined_result.returncode == 0, (
        "expected the CI-gate invocation (--apply-baseline) against the "
        "real repo's current state to exit 0 (all pre-existing findings "
        "are baselined), so an unrelated PR touching neither AGY script "
        "is not blocked. "
        f"stdout={baselined_result.stdout!r} "
        f"stderr={baselined_result.stderr!r}"
    )
    baselined_manifest = json.loads(baselined_result.stdout)
    assert baselined_manifest["baseline_applied"] is True
    assert baselined_manifest["new_finding_count"] == 0
    assert baselined_manifest["baseline_count"] >= 1

    raw_result = _run_gate()
    assert raw_result.returncode == 1, (
        "expected the raw invocation (no --apply-baseline) to still fail "
        "closed, proving --apply-baseline changes only the exit-code "
        "decision, not the underlying detection logic (Issue #1788 Out "
        "of Scope)."
    )
    raw_manifest = json.loads(raw_result.stdout)
    assert raw_manifest["baseline_applied"] is False
    assert raw_manifest["status"] == "findings_detected"
    assert len(raw_manifest["findings"]) == baselined_manifest["baseline_count"]

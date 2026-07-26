"""Unit tests for scripts/check-visual-artifact-pipeline.py (Issue #1387).

The module under test has a hyphenated filename (matches the existing CLI
naming convention across the repo), so it cannot be `import`ed normally; it
is loaded via `importlib.util.spec_from_file_location` instead.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-visual-artifact-pipeline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_visual_artifact_pipeline", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _base_capture(**overrides) -> dict:
    capture = {
        "capture_id": "spec.ts::name.png",
        "spec_file": "tests/e2e/spec.ts",
        "screenshot_name": "name.png",
        "registry_id": None,
        "directory": "tests/e2e/__screenshots__/spec.ts/",
        "browser": "chromium",
        "project": "chromium",
        "viewport": "1280x720",
        "device_scale_factor": 1,
        "comparator_kind": "maxDiffPixels",
        "comparator_value": "1",
        "style_path": False,
        "artifact_scope": "job",
        "digest_env": "TEST_RESULTS_DIGEST",
        "retention_days": 30,
    }
    capture.update(overrides)
    return capture


# ---------------------------------------------------------------------------
# GIVEN a declared capture that matches the derived ground truth
# WHEN cross_validate_active_captures runs
# THEN it reports no failures (positive baseline case)
# ---------------------------------------------------------------------------
def test_positive_case_matching_declared_and_derived_passes():
    declared = [_base_capture()]
    derived = [_base_capture()]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert failures == []


# ---------------------------------------------------------------------------
# AC4 — hard fail (not soft warn) when the declared summary drifts from the
# real spec-derived directory.
# ---------------------------------------------------------------------------
def test_hard_fail_when_directory_drifts_from_real_spec():
    declared = [_base_capture(directory="tests/e2e/__screenshots__/wrong-spec.ts/")]
    derived = [_base_capture()]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("directory drift" in f for f in failures)


# ---------------------------------------------------------------------------
# AC9(c) — active capture directory drift is a hard fail (distinct wording
# check from the AC4 test above, using an explicit mismatch value).
# ---------------------------------------------------------------------------
def test_ac9c_directory_drift_between_declared_and_derived_fails():
    declared = [_base_capture(directory="tests/e2e/__screenshots__/other.spec.ts/")]
    derived = [_base_capture(directory="tests/e2e/__screenshots__/spec.ts/")]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("does not match derived directory" in f for f in failures)


# ---------------------------------------------------------------------------
# AC5 / AC9(a) — comparator exclusivity: maxDiffPixelRatio mislabeled as
# maxDiffPixels (kind mismatch even though the underlying capture is real).
# ---------------------------------------------------------------------------
def test_exclusivity_fails_when_comparator_kind_mislabeled():
    declared = [_base_capture(comparator_kind="maxDiffPixels", comparator_value="0.08")]
    derived = [_base_capture(comparator_kind="maxDiffPixelRatio", comparator_value="0.08")]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("comparator_kind" in f and "does not match derived" in f for f in failures)


# ---------------------------------------------------------------------------
# AC5 / AC9(b) — comparator exclusivity: both maxDiffPixels and
# maxDiffPixelRatio declared in the same options blob must fail at
# extraction time (mutually exclusive per Playwright's own API).
# ---------------------------------------------------------------------------
def test_exclusivity_fails_when_both_comparators_declared():
    kind, value, errors = mod._parse_comparator("maxDiffPixels: 1, maxDiffPixelRatio: 0.08")
    assert kind is None
    assert value is None
    assert any("mutually exclusive" in e for e in errors)


def test_exclusivity_fails_when_neither_comparator_declared():
    kind, value, errors = mod._parse_comparator("animations: 'disabled'")
    assert kind is None
    assert any("neither" in e for e in errors)


# ---------------------------------------------------------------------------
# AC9(d) — duplicate capture_id in the declared list must fail.
# ---------------------------------------------------------------------------
def test_ac9d_duplicate_declared_capture_id_fails():
    declared = [_base_capture(), _base_capture()]
    derived = [_base_capture()]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("duplicate declared capture_id" in f for f in failures)


# ---------------------------------------------------------------------------
# AC9(e) — a pending-baseline registryId must never be declared as an active
# capture (it fails closed at runtime in visual-utils.ts and must never be
# treated as an active, CI-gated baseline).
# ---------------------------------------------------------------------------
def test_ac9e_pending_baseline_registered_as_active_fails():
    declared = [_base_capture(registry_id="running-hud-paused")]
    derived: list[dict] = []
    registry_maturity = {"running-hud-paused": "pending-baseline"}
    failures = mod.cross_validate_active_captures(declared, derived, registry_maturity)
    assert any("pending-baseline" in f for f in failures)


# ---------------------------------------------------------------------------
# AC9(f) — Playwright and Vitest component VRT baseline roots must not mix.
# ---------------------------------------------------------------------------
def test_ac9f_playwright_vitest_baseline_root_mixing_fails():
    declared = [
        _base_capture(directory="tests/component/__screenshots__/widget.spec.ts/")
    ]
    derived: list[dict] = []
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("reserved Vitest component snapshot root" in f for f in failures)


def test_ac9f_directory_outside_known_snapshot_roots_fails():
    declared = [_base_capture(directory="tests/somewhere-else/__screenshots__/x/")]
    derived: list[dict] = []
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("outside the Playwright snapshot root" in f for f in failures)


# ---------------------------------------------------------------------------
# AC9(g) — artifact digest wiring must be present per declared capture.
# ---------------------------------------------------------------------------
def test_ac9g_missing_digest_env_fails():
    declared = [_base_capture(digest_env=None)]
    derived = [_base_capture()]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("missing digest_env" in f for f in failures)


# ---------------------------------------------------------------------------
# AC7 — artifact absence-state classification wiring.
# ---------------------------------------------------------------------------
def test_artifact_status_wiring_requires_all_four_states():
    incomplete_summary = (
        "steps.upload-test-results.outcome steps.upload-playwright-report.outcome uploaded"
    )
    failures = mod.check_artifact_status_wiring(incomplete_summary)
    assert any("no_files" in f for f in failures)
    assert any("step_not_run" in f for f in failures)
    assert any("upload_failed" in f for f in failures)


def test_artifact_status_wiring_passes_when_all_tokens_present():
    complete_summary = " ".join(mod.ARTIFACT_STATUS_REQUIRED_TOKENS)
    failures = mod.check_artifact_status_wiring(complete_summary)
    assert failures == []


# ---------------------------------------------------------------------------
# extract_declared_active_captures: parses the CAPTURES literal embedded
# between the ACTIVE_VRT_CAPTURES_BEGIN/_END markers.
# ---------------------------------------------------------------------------
def test_extract_declared_active_captures_parses_marker_block():
    workflow_text = """
          # ACTIVE_VRT_CAPTURES_BEGIN
          CAPTURES = [
              {
                  "capture_id": "a.spec.ts::x.png",
                  "directory": "tests/e2e/__screenshots__/a.spec.ts/",
                  "comparator_kind": "maxDiffPixels",
                  "comparator_value": "1",
                  "style_path": False,
                  "registry_id": None,
                  "digest_env": "TEST_RESULTS_DIGEST",
              },
          ]
          # ACTIVE_VRT_CAPTURES_END
"""
    captures, errors = mod.extract_declared_active_captures(workflow_text)
    assert errors == []
    assert len(captures) == 1
    assert captures[0]["capture_id"] == "a.spec.ts::x.png"


def test_extract_declared_active_captures_fails_closed_when_markers_missing():
    captures, errors = mod.extract_declared_active_captures("no markers here")
    assert captures == []
    assert errors


# ---------------------------------------------------------------------------
# extract_derived_active_captures against a synthetic fixture spec tree:
# guard-only (`.rejects.toThrow`) call sites and pending-baseline registryIds
# must never be counted as active captures.
# ---------------------------------------------------------------------------
def test_extract_derived_active_captures_skips_guard_only_and_pending(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    spec = e2e_dir / "fixture.spec.ts"
    spec.write_text(
        """
test('real capture', async ({ page }) => {
  await expectDomOverlayScreenshot(overlay, 'real.png', 'active-id', { maxDiffPixels: 5 })
})

test('guard only', async ({ page }) => {
  await expect(
    expectDomOverlayScreenshot(overlay, 'guard.png', 'active-id', { maxDiffPixels: 5 }),
  ).rejects.toThrow(/rejected/)
})

test('pending scenario', async ({ page }) => {
  await expectDomOverlayScreenshot(overlay, 'pending.png', 'pending-id', { maxDiffPixels: 5 })
})
""",
        encoding="utf-8",
    )
    registry_maturity = {"active-id": "legacy-current", "pending-id": "pending-baseline"}
    pw_snapshot_config = {
        "test_dir": "tests/e2e",
        "snapshot_path_template": "{testDir}/__screenshots__/{testFilePath}/{arg}{ext}",
    }
    captures, errors = mod.extract_derived_active_captures(e2e_dir, pw_snapshot_config, registry_maturity)
    assert errors == []
    names = {c["screenshot_name"] for c in captures}
    assert names == {"real.png"}


# ---------------------------------------------------------------------------
# Integration: the real repo's spec files / registry / config extract
# cleanly (no parse errors), proving the extraction logic is not brittle
# against the actual production sources this Issue targets.
# ---------------------------------------------------------------------------
def test_extract_derived_active_captures_against_real_repo_spec_files():
    e2e_dir = REPO_ROOT / "tests" / "e2e"
    visual_utils_text = (e2e_dir / "visual-utils.ts").read_text(encoding="utf-8")
    pw_config_text = (REPO_ROOT / "playwright.config.ts").read_text(encoding="utf-8")
    registry_maturity = mod.parse_registry_maturity(visual_utils_text)
    pw_snapshot_config = mod.parse_playwright_snapshot_config(pw_config_text)
    captures, errors = mod.extract_derived_active_captures(e2e_dir, pw_snapshot_config, registry_maturity)
    assert errors == []
    capture_ids = {c["capture_id"] for c in captures}
    assert "m2-combat-mvp.spec.ts::m2-timeout-overlay-baseline.png" in capture_ids
    assert "m2-combat-mvp.spec.ts::m2-running-hud-baseline.png" in capture_ids
    assert "visual-overlay.spec.ts::vrt-running-hud-overlay.png" in capture_ids


# ---------------------------------------------------------------------------
# End-to-end: the real, currently-committed ci.yml passes the full
# validator (structural wiring + active-capture cross-validation), proving
# the CI-embedded CAPTURES literal has not drifted from the real sources.
# ---------------------------------------------------------------------------
def test_main_passes_against_real_ci_yml():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status: pass" in result.stdout


def test_main_fails_closed_on_missing_workflow_file(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(tmp_path / "missing.yml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "status: error" in result.stdout


@pytest.mark.parametrize(
    "template,test_dir,spec_filename,expected",
    [
        (
            "{testDir}/__screenshots__/{testFilePath}/{arg}{ext}",
            "tests/e2e",
            "a.spec.ts",
            "tests/e2e/__screenshots__/a.spec.ts/",
        ),
    ],
)
def test_resolve_capture_directory(template, test_dir, spec_filename, expected):
    assert mod.resolve_capture_directory(test_dir, template, spec_filename) == expected

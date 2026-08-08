"""Unit tests for scripts/check-visual-artifact-pipeline.py (Issue #1387).

The module under test has a hyphenated filename (matches the existing CLI
naming convention across the repo), so it cannot be `import`ed normally; it
is loaded via `importlib.util.spec_from_file_location` instead.
"""

from __future__ import annotations

import base64
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-visual-artifact-pipeline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_visual_artifact_pipeline", SCRIPT_PATH)
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
    declared = [_base_capture(directory="tests/component/__screenshots__/widget.spec.ts/")]
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
# PR #1813 review fix, P1 Blocker 1 / Blocker 7 (marge condition 7): every
# other declared field the validator can independently re-derive is tampered
# ONE AT A TIME and must be caught as a hard fail — closing the gap where
# only directory/comparator_kind/comparator_value/style_path were checked.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("spec_file", "tests/e2e/tampered.spec.ts"),
        ("screenshot_name", "tampered.png"),
        ("registry_id", "tampered-registry-id"),
        ("browser", "firefox"),
        ("project", "firefox"),
        ("viewport", "1920x1080"),
        ("device_scale_factor", 2),
        ("artifact_scope", "suite"),
        ("digest_env", "PLAYWRIGHT_REPORT_DIGEST"),
        ("retention_days", 14),
    ],
)
def test_field_level_tampering_is_hard_failed(field, bad_value):
    declared = [_base_capture(**{field: bad_value})]
    derived = [_base_capture()]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any(field in f for f in failures), f"tampering {field!r} was not hard-failed: {failures}"


def test_directory_tampering_is_hard_failed():
    declared = [_base_capture(directory="tests/e2e/__screenshots__/tampered.spec.ts/")]
    derived = [_base_capture()]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("directory" in f for f in failures)


def test_comparator_value_tampering_is_hard_failed():
    declared = [_base_capture(comparator_value="999")]
    derived = [_base_capture()]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("comparator_value" in f for f in failures)


def test_style_path_tampering_is_hard_failed():
    declared = [_base_capture(style_path=True)]
    derived = [_base_capture(style_path=False)]
    failures = mod.cross_validate_active_captures(declared, derived, {})
    assert any("style_path" in f for f in failures)


# ---------------------------------------------------------------------------
# AC7 — artifact absence-state classification wiring.
# ---------------------------------------------------------------------------
def test_artifact_status_wiring_requires_all_four_states():
    incomplete_summary = "steps.upload-test-results.outcome steps.upload-playwright-report.outcome uploaded"
    failures = mod.check_artifact_status_wiring(incomplete_summary)
    assert any("no_files" in f for f in failures)
    assert any("step_not_run" in f for f in failures)
    assert any("upload_failed" in f for f in failures)


def test_artifact_status_wiring_passes_when_all_tokens_present():
    complete_summary = " ".join(mod.ARTIFACT_STATUS_REQUIRED_TOKENS)
    failures = mod.check_artifact_status_wiring(complete_summary)
    assert failures == []


def test_artifact_status_wiring_requires_artifact_id_tokens():
    # PR #1813 review fix, P1 Blocker 3: classification must be based on the
    # upload step's own artifact-id output, not a post-hoc directory scan.
    assert "steps.upload-playwright-report.outputs.artifact-id" in mod.ARTIFACT_STATUS_REQUIRED_TOKENS
    assert "steps.upload-test-results.outputs.artifact-id" in mod.ARTIFACT_STATUS_REQUIRED_TOKENS


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
        "browser": "chromium",
        "project": "chromium",
        "viewport": "1280x720",
        "device_scale_factor": 1,
    }
    captures, errors = mod.extract_derived_active_captures(e2e_dir, pw_snapshot_config, registry_maturity)
    assert errors == []
    names = {c["screenshot_name"] for c in captures}
    assert names == {"real.png"}


# ---------------------------------------------------------------------------
# PR #1813 review fix, P2 Blocker 5 — the TS-lite parser must not be
# fail-open: subdirectories are walked (rglob), double-quoted / array names
# are supported, comments/strings never desynchronise paren-balance
# counting, and calls this validator genuinely cannot resolve to a
# capture_id (options-only, no-args) are hard-failed rather than silently
# skipped.
# ---------------------------------------------------------------------------
def _default_pw_snapshot_config() -> dict:
    return {
        "test_dir": "tests/e2e",
        "snapshot_path_template": "{testDir}/__screenshots__/{testFilePath}/{arg}{ext}",
        "browser": "chromium",
        "project": "chromium",
        "viewport": "1280x720",
        "device_scale_factor": 1,
    }


def test_extract_derived_active_captures_walks_nested_subdirectories(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    nested_dir = e2e_dir / "nested"
    nested_dir.mkdir(parents=True)
    spec = nested_dir / "nested.spec.ts"
    spec.write_text(
        "test('nested', async ({ page }) => {\n"
        "  await expect(page.locator('canvas')).toHaveScreenshot('nested.png', { maxDiffPixels: 1 })\n"
        "})\n",
        encoding="utf-8",
    )
    captures, errors = mod.extract_derived_active_captures(e2e_dir, _default_pw_snapshot_config(), {})
    assert errors == []
    assert {"nested.png"} == {c["screenshot_name"] for c in captures}


def test_extract_derived_active_captures_supports_double_quoted_name(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    spec = e2e_dir / "fixture.spec.ts"
    spec.write_text(
        "test('double quoted', async ({ page }) => {\n"
        "  await expect(page.locator('canvas')).toHaveScreenshot(\"double-quoted.png\", { maxDiffPixels: 1 })\n"
        "})\n",
        encoding="utf-8",
    )
    captures, errors = mod.extract_derived_active_captures(e2e_dir, _default_pw_snapshot_config(), {})
    assert errors == []
    assert {"double-quoted.png"} == {c["screenshot_name"] for c in captures}


def test_extract_derived_active_captures_supports_array_name(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    spec = e2e_dir / "fixture.spec.ts"
    spec.write_text(
        "test('array name', async ({ page }) => {\n"
        "  await expect(page.locator('canvas')).toHaveScreenshot(['group', 'array-name.png'], { maxDiffPixels: 1 })\n"
        "})\n",
        encoding="utf-8",
    )
    captures, errors = mod.extract_derived_active_captures(e2e_dir, _default_pw_snapshot_config(), {})
    assert errors == []
    assert {"group/array-name.png"} == {c["screenshot_name"] for c in captures}


def test_extract_derived_active_captures_hard_fails_on_options_only_call(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    spec = e2e_dir / "fixture.spec.ts"
    spec.write_text(
        "test('options only', async ({ page }) => {\n"
        "  await expect(page.locator('canvas')).toHaveScreenshot({ maxDiffPixels: 1 })\n"
        "})\n",
        encoding="utf-8",
    )
    captures, errors = mod.extract_derived_active_captures(e2e_dir, _default_pw_snapshot_config(), {})
    assert captures == []
    assert any("options-only" in e for e in errors)


def test_extract_derived_active_captures_hard_fails_on_zero_arg_call(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    spec = e2e_dir / "fixture.spec.ts"
    spec.write_text(
        "test('implicit name', async ({ page }) => {\n  await expect(page.locator('canvas')).toHaveScreenshot()\n})\n",
        encoding="utf-8",
    )
    captures, errors = mod.extract_derived_active_captures(e2e_dir, _default_pw_snapshot_config(), {})
    assert captures == []
    assert any("no arguments" in e for e in errors)


def test_extract_derived_active_captures_ignores_call_site_inside_comment(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    spec = e2e_dir / "fixture.spec.ts"
    spec.write_text(
        "// example: toHaveScreenshot('fake-from-comment.png', { maxDiffPixels: 1 })\n"
        "test('real', async ({ page }) => {\n"
        "  await expect(page.locator('canvas')).toHaveScreenshot('real.png', { maxDiffPixels: 1 })\n"
        "})\n",
        encoding="utf-8",
    )
    captures, errors = mod.extract_derived_active_captures(e2e_dir, _default_pw_snapshot_config(), {})
    assert errors == []
    assert {"real.png"} == {c["screenshot_name"] for c in captures}


def test_extract_derived_active_captures_pathtemplate_override_changes_directory(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    spec = e2e_dir / "fixture.spec.ts"
    spec.write_text(
        "test('custom path template', async ({ page }) => {\n"
        "  await expect(page.locator('canvas')).toHaveScreenshot('custom.png', {\n"
        "    maxDiffPixels: 1,\n"
        "    pathTemplate: '{testDir}/__custom_screenshots__/{testFilePath}/{arg}{ext}',\n"
        "  })\n"
        "})\n",
        encoding="utf-8",
    )
    captures, errors = mod.extract_derived_active_captures(e2e_dir, _default_pw_snapshot_config(), {})
    assert errors == []
    assert captures[0]["directory"] == "tests/e2e/__custom_screenshots__/fixture.spec.ts/"


def test_parse_registry_filename_to_id_extracts_mapping_from_table():
    registry_md_text = (
        "| id | kind | maturity | artifact/test | spec |\n"
        "|---|---|---|---|---|\n"
        "| timeout-overlay | screenshot-baseline | frozen | "
        "`tests/e2e/__screenshots__/m2-combat-mvp.spec.ts/m2-timeout-overlay-baseline.png` | #1 |\n"
        "| defeat-overlay | pixel-contract | predicate-only | `getImageData` smoke | #2 |\n"
    )
    mapping = mod.parse_registry_filename_to_id(registry_md_text)
    assert mapping == {"m2-timeout-overlay-baseline.png": "timeout-overlay"}


def test_extract_derived_active_captures_resolves_registry_id_for_raw_call_via_registry_doc(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    spec = e2e_dir / "fixture.spec.ts"
    spec.write_text(
        "test('raw call with registry mapping', async ({ page }) => {\n"
        "  await expect(page.locator('canvas')).toHaveScreenshot('mapped.png', { maxDiffPixels: 1 })\n"
        "})\n",
        encoding="utf-8",
    )
    registry_filename_to_id = {"mapped.png": "some-registry-id"}
    captures, errors = mod.extract_derived_active_captures(
        e2e_dir, _default_pw_snapshot_config(), {}, registry_filename_to_id
    )
    assert errors == []
    assert captures[0]["registry_id"] == "some-registry-id"


# ---------------------------------------------------------------------------
# Integration: the real repo's spec files / registry / config extract
# cleanly (no parse errors), proving the extraction logic is not brittle
# against the actual production sources this Issue targets.
# ---------------------------------------------------------------------------
def test_extract_derived_active_captures_against_real_repo_spec_files():
    e2e_dir = REPO_ROOT / "tests" / "e2e"
    visual_utils_text = (e2e_dir / "visual-utils.ts").read_text(encoding="utf-8")
    pw_config_text = (REPO_ROOT / "playwright.config.ts").read_text(encoding="utf-8")
    registry_doc_text = (REPO_ROOT / "docs" / "dev" / "visual-baseline-registry.md").read_text(encoding="utf-8")
    registry_maturity = mod.parse_registry_maturity(visual_utils_text)
    registry_filename_to_id = mod.parse_registry_filename_to_id(registry_doc_text)
    pw_snapshot_config = mod.parse_playwright_snapshot_config(pw_config_text)
    captures, errors = mod.extract_derived_active_captures(
        e2e_dir, pw_snapshot_config, registry_maturity, registry_filename_to_id
    )
    assert errors == []
    capture_ids = {c["capture_id"] for c in captures}
    assert "m2-combat-mvp.spec.ts::m2-timeout-overlay-baseline.png" in capture_ids
    assert "m2-combat-mvp.spec.ts::m2-running-hud-baseline.png" in capture_ids
    assert "visual-overlay.spec.ts::vrt-running-hud-overlay.png" in capture_ids


def test_parse_playwright_snapshot_config_resolves_project_fields_from_real_config():
    pw_config_text = (REPO_ROOT / "playwright.config.ts").read_text(encoding="utf-8")
    result = mod.parse_playwright_snapshot_config(pw_config_text)
    assert result["browser"] == "chromium"
    assert result["project"] == "chromium"
    assert result["viewport"] == "1280x720"
    assert result["device_scale_factor"] == 1


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


# ---------------------------------------------------------------------------
# PR #1813 review fix, P1 Blocker 3 / marge condition 8 — integration test:
# the standard e2e lane's `test-results/` VRT evidence must never be
# clobbered by the AC9 preview-namespace lane that runs immediately after it
# in the same job. This is asserted against playwright.config.ts's ACTUAL
# `outputDir` formula (not a hardcoded pair of directory names), so a
# regression that re-merges the two lanes' output directories fails this
# test even if the literal directory names it uses happen to change.
# ---------------------------------------------------------------------------
def _parse_output_dir_by_lane(pw_config_text: str) -> tuple[str, str]:
    m = re.search(
        r"outputDir:\s*PREVIEW_NAMESPACE_LANE\s*\?\s*'([^']+)'\s*:\s*'([^']+)'",
        pw_config_text,
    )
    assert m, "playwright.config.ts must declare a lane-specific outputDir (Issue #1387 P1 Blocker 3)"
    return m.group(1), m.group(2)


def test_preview_namespace_and_standard_lane_use_distinct_output_dirs():
    pw_config_text = (REPO_ROOT / "playwright.config.ts").read_text(encoding="utf-8")
    preview_dir_name, standard_dir_name = _parse_output_dir_by_lane(pw_config_text)
    assert preview_dir_name != standard_dir_name


def test_preview_namespace_lane_cleanup_does_not_clobber_standard_lane_artifacts(tmp_path):
    pw_config_text = (REPO_ROOT / "playwright.config.ts").read_text(encoding="utf-8")
    preview_dir_name, standard_dir_name = _parse_output_dir_by_lane(pw_config_text)

    # Simulate the standard e2e lane having already run and left VRT evidence
    # (actual/expected/diff PNGs on a mismatch, or just trace files) in its
    # own outputDir.
    standard_dir = tmp_path / standard_dir_name
    standard_dir.mkdir()
    marker = standard_dir / "m2-timeout-overlay-baseline-actual.png"
    marker.write_bytes(b"standard-lane-evidence")

    # Simulate Playwright's own "clean outputDir at run start" behavior for
    # the SECOND (preview-namespace) lane that runs afterwards in the same
    # CI job — it must only ever touch its OWN outputDir.
    preview_dir = tmp_path / preview_dir_name
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    preview_dir.mkdir()
    (preview_dir / "some-preview-lane-file.txt").write_bytes(b"preview-lane-only")

    assert marker.exists(), (
        "preview-namespace lane cleanup clobbered the standard lane's test-results "
        "evidence (Issue #1387 / PR #1813 review fix, P1 Blocker 3)"
    )
    assert marker.read_bytes() == b"standard-lane-evidence"


# ---------------------------------------------------------------------------
# AC10 (Issue #1389) — Vitest Browser Mode component VRT comparator support.
# Additive audit path, independent of the jobs.e2e-only tests above.
#
# PR #1977 review fix (OWNER REQUEST_CHANGES) rewrote this section:
#   - P0 item 2: directory derivation now reads a real
#     `vitest.visual.config.ts` (not a self-generated constant) and
#     cross-checks against a real committed PNG on disk.
#   - P0 item 3: `_parse_vitest_comparator` (regex-anywhere, mutually
#     exclusive px/ratio) was replaced by `_parse_vitest_comparator_options`
#     (masked/structural, allows px+ratio to coexist, range/type validated,
#     comment-immune).
#   - P1 item 4: `cross_validate_component_vrt_captures` 1:1 registry
#     cross-validation, zero-capture / missing-PNG / orphan-PNG / duplicate
#     hard fails.
# ---------------------------------------------------------------------------

# Minimal valid 1x1 PNG (standard fixture bytes), used as a stand-in
# committed baseline so tests can assert `png_exists is True` / `errors ==
# []` without needing a real screenshot.
_MINIMAL_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)

_PRODUCTION_INCLUDE_PATTERN = "tests/component/*.vrt.test.ts"
_REAL_VITEST_CONFIG = mod.parse_vitest_visual_config(
    (REPO_ROOT / "vitest.visual.config.ts").read_text(encoding="utf-8")
)


def _default_vitest_config(**overrides) -> dict:
    config = {
        "screenshot_directory": "__screenshots__",
        "include_patterns": [_PRODUCTION_INCLUDE_PATTERN],
    }
    config.update(overrides)
    return config


def _write_component_vrt_test(
    tmp_path: Path,
    filename: str,
    body: str,
    *,
    subdir: str | None = None,
) -> Path:
    component_dir = tmp_path / "tests" / "component"
    component_dir.mkdir(parents=True, exist_ok=True)
    target_dir = component_dir / subdir if subdir else component_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    spec_path = target_dir / filename
    spec_path.write_text(body, encoding="utf-8")
    return component_dir


def _write_baseline_png(component_dir: Path, spec_filename: str, screenshot_name: str) -> Path:
    name_stem, _, ext = screenshot_name.rpartition(".")
    name_stem = name_stem or screenshot_name
    ext = f".{ext}" if ext else ".png"
    png_dir = component_dir / "__screenshots__" / spec_filename
    png_dir.mkdir(parents=True, exist_ok=True)
    png_path = png_dir / f"{name_stem}-chromium-linux{ext}"
    png_path.write_bytes(_MINIMAL_PNG_BYTES)
    return png_path


# --- parse_vitest_visual_config -------------------------------------------


def test_parse_vitest_visual_config_reads_real_config():
    config = _REAL_VITEST_CONFIG
    assert config["screenshot_directory"] == "__screenshots__"
    assert _PRODUCTION_INCLUDE_PATTERN in config["include_patterns"]
    assert any("__negative_control__" in p for p in config["include_patterns"])


def test_parse_vitest_visual_config_shorthand_form():
    config = mod.parse_vitest_visual_config(
        "export default defineConfig({ test: { browser: { screenshotDirectory: '__screenshots__' } } })"
    )
    assert config["screenshot_directory"] == "__screenshots__"


def test_parse_vitest_visual_config_missing_screenshot_directory():
    config = mod.parse_vitest_visual_config("export default defineConfig({ test: {} })")
    assert config["screenshot_directory"] is None


def test_parse_vitest_visual_config_const_without_resolver_is_not_explicit():
    # A bare `const X = '__screenshots__'` with no `resolveScreenshotPath`
    # anywhere in the file is NOT treated as an explicit screenshot
    # directory declaration (PR #1977 review fix, P0 item 2) -- it could be
    # an unrelated constant.
    config = mod.parse_vitest_visual_config("const SOME_OTHER_CONSTANT = '__screenshots__'")
    assert config["screenshot_directory"] is None


# --- _parse_vitest_comparator_options --------------------------------------


def test_parse_vitest_comparator_options_recognizes_allowed_mismatched_pixel_ratio():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: { allowedMismatchedPixelRatio: 0.02 } }"
    )
    assert comparator == {"allowedMismatchedPixelRatio": "0.02"}
    assert errors == []


def test_parse_vitest_comparator_options_recognizes_allowed_mismatched_pixels():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: { allowedMismatchedPixels: 5 } }"
    )
    assert comparator == {"allowedMismatchedPixels": "5"}
    assert errors == []


def test_parse_vitest_comparator_options_recognizes_threshold():
    comparator, errors = mod._parse_vitest_comparator_options("{ comparatorOptions: { threshold: 0.1 } }")
    assert comparator == {"threshold": "0.1"}
    assert errors == []


def test_parse_vitest_comparator_options_fails_closed_when_none_declared():
    comparator, errors = mod._parse_vitest_comparator_options("{ comparatorOptions: {} }")
    assert comparator == {}
    assert any("neither" in e for e in errors)


def test_parse_vitest_comparator_options_fails_closed_when_no_comparator_options_object():
    comparator, errors = mod._parse_vitest_comparator_options("{ animations: 'disabled' }")
    assert comparator == {}
    assert any("no comparatorOptions object found" in e for e in errors)


# AC10 (Issue #1389 P0 item 3, OWNER REQUEST_CHANGES) — Vitest allows BOTH
# allowedMismatchedPixels and allowedMismatchedPixelRatio together (applies
# the stricter of the two); the prior validator incorrectly rejected this
# as mutually exclusive.
def test_parse_vitest_comparator_options_allows_pixels_and_ratio_to_coexist():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: { allowedMismatchedPixels: 5, allowedMismatchedPixelRatio: 0.02 } }"
    )
    assert comparator == {"allowedMismatchedPixels": "5", "allowedMismatchedPixelRatio": "0.02"}
    assert errors == []


# AC10 (P0 item 3) — a commented-out comparator option must NOT be detected
# as present (regression test for the prior regex-anywhere bug).
def test_parse_vitest_comparator_options_ignores_commented_out_option():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: {\n  // allowedMismatchedPixelRatio: 0.02,\n } }"
    )
    assert comparator == {}
    assert any("neither" in e for e in errors)


# AC10 (P0 item 3) — a comparator key that only appears OUTSIDE the
# `comparatorOptions` object (e.g. a sibling field, or a string literal
# elsewhere in the options blob) must not be detected as present.
def test_parse_vitest_comparator_options_ignores_key_outside_comparator_options_object():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ allowedMismatchedPixelRatio: 0.02, comparatorOptions: {} }"
    )
    assert comparator == {}
    assert any("neither" in e for e in errors)


def test_parse_vitest_comparator_options_validates_ratio_range():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: { allowedMismatchedPixelRatio: 1.5 } }"
    )
    assert comparator == {}
    assert any("out of range" in e for e in errors)


def test_parse_vitest_comparator_options_validates_threshold_range():
    comparator, errors = mod._parse_vitest_comparator_options("{ comparatorOptions: { threshold: -0.1 } }")
    assert comparator == {}
    assert any("out of range" in e for e in errors)


def test_parse_vitest_comparator_options_validates_pixels_non_negative_integer():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: { allowedMismatchedPixels: -1 } }"
    )
    assert comparator == {}
    assert any("non-negative integer" in e for e in errors)


def test_parse_vitest_comparator_options_validates_pixels_not_fractional():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: { allowedMismatchedPixels: 1.5 } }"
    )
    assert comparator == {}
    assert any("non-negative integer" in e for e in errors)


# AC10 (P0 item 3) — a variable reference / expression value fails closed
# rather than being silently ignored or accepted.
def test_parse_vitest_comparator_options_fails_closed_on_variable_reference():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: { allowedMismatchedPixelRatio: SOME_TOLERANCE } }"
    )
    assert comparator == {}
    assert any("not a plain numeric literal" in e for e in errors)


def test_parse_vitest_comparator_options_fails_closed_on_spread():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: { ...sharedComparatorOptions, threshold: 0.1 } }"
    )
    assert comparator == {"threshold": "0.1"}
    assert any("spread syntax" in e for e in errors)


def test_parse_vitest_comparator_options_ignores_unrelated_keys():
    comparator, errors = mod._parse_vitest_comparator_options(
        "{ comparatorOptions: { threshold: 0.1, someUnrelatedOption: true } }"
    )
    assert comparator == {"threshold": "0.1"}
    assert errors == []


# --- extract_derived_vitest_component_captures -----------------------------


def test_extract_derived_vitest_component_captures_parses_real_call_site(tmp_path):
    component_dir = _write_component_vrt_test(
        tmp_path,
        "combat-hud-running.vrt.test.ts",
        """
        test('x', async () => {
          await expect(page.elementLocator(el)).toMatchScreenshot('combat-hud-running.png', {
            comparatorOptions: {
              allowedMismatchedPixelRatio: 0.02,
            },
          })
        })
        """,
    )
    _write_baseline_png(component_dir, "combat-hud-running.vrt.test.ts", "combat-hud-running.png")
    captures, errors = mod.extract_derived_vitest_component_captures(component_dir, _default_vitest_config())
    assert errors == []
    assert len(captures) == 1
    capture = captures[0]
    assert capture["capture_id"] == "combat-hud-running.vrt.test.ts::combat-hud-running.png"
    assert capture["directory"] == "tests/component/__screenshots__/combat-hud-running.vrt.test.ts/"
    assert capture["comparator"] == {"allowedMismatchedPixelRatio": "0.02"}
    assert capture["comparator_kind"] == "allowedMismatchedPixelRatio"
    assert capture["comparator_value"] == "0.02"
    assert capture["png_exists"] is True


def test_extract_derived_vitest_component_captures_returns_empty_when_dir_missing(tmp_path):
    captures, errors = mod.extract_derived_vitest_component_captures(
        tmp_path / "does-not-exist", _default_vitest_config()
    )
    assert captures == []
    assert errors == []


def test_extract_derived_vitest_component_captures_hard_fails_when_screenshot_directory_not_explicit(tmp_path):
    component_dir = _write_component_vrt_test(
        tmp_path,
        "combat-hud-running.vrt.test.ts",
        "test('x', async () => {})",
    )
    captures, errors = mod.extract_derived_vitest_component_captures(
        component_dir, {"screenshot_directory": None, "include_patterns": [_PRODUCTION_INCLUDE_PATTERN]}
    )
    assert captures == []
    assert any("does not explicitly declare a screenshot directory" in e for e in errors)


def test_extract_derived_vitest_component_captures_hard_fails_recursive_include(tmp_path):
    component_dir = _write_component_vrt_test(
        tmp_path,
        "combat-hud-running.vrt.test.ts",
        "test('x', async () => {})",
    )
    captures, errors = mod.extract_derived_vitest_component_captures(
        component_dir,
        _default_vitest_config(include_patterns=["tests/component/**/*.vrt.test.ts"]),
    )
    assert captures == []
    assert any("is recursive" in e for e in errors)


def test_extract_derived_vitest_component_captures_hard_fails_unexpected_include_pattern(tmp_path):
    component_dir = _write_component_vrt_test(
        tmp_path,
        "combat-hud-running.vrt.test.ts",
        "test('x', async () => {})",
    )
    captures, errors = mod.extract_derived_vitest_component_captures(
        component_dir,
        _default_vitest_config(include_patterns=["tests/component/*.other.test.ts"]),
    )
    assert captures == []
    assert any("is not the expected flat production pattern" in e for e in errors)


def test_extract_derived_vitest_component_captures_excludes_negative_control_pattern(tmp_path):
    component_dir = _write_component_vrt_test(
        tmp_path,
        "hidden-attachment-negative-control.vrt.test.ts",
        """
        test('x', async () => {
          await expect(page.elementLocator(el)).toMatchScreenshot('hidden-attachment-negative-control.png', {
            comparatorOptions: { allowedMismatchedPixels: 0 },
          })
        })
        """,
        subdir="__negative_control__",
    )
    captures, errors = mod.extract_derived_vitest_component_captures(
        component_dir,
        _default_vitest_config(
            include_patterns=[
                _PRODUCTION_INCLUDE_PATTERN,
                "tests/component/__negative_control__/*.vrt.test.ts",
            ]
        ),
    )
    assert captures == []
    assert errors == []


def test_extract_derived_vitest_component_captures_hard_fails_missing_comparator(tmp_path):
    component_dir = _write_component_vrt_test(
        tmp_path,
        "broken.vrt.test.ts",
        """
        test('x', async () => {
          await expect(page.elementLocator(el)).toMatchScreenshot('broken.png', {
            comparatorOptions: {},
          })
        })
        """,
    )
    captures, errors = mod.extract_derived_vitest_component_captures(component_dir, _default_vitest_config())
    assert len(captures) == 1
    assert any("neither" in e for e in errors)


def test_extract_derived_vitest_component_captures_skips_guard_only_call(tmp_path):
    component_dir = _write_component_vrt_test(
        tmp_path,
        "guard.vrt.test.ts",
        """
        test('x', async () => {
          await expect(bad()).rejects.toThrow()
          await expect(page.elementLocator(el)).toMatchScreenshot('real.png', {
            comparatorOptions: { threshold: 0.1 },
          })
        })
        """,
    )
    _write_baseline_png(component_dir, "guard.vrt.test.ts", "real.png")
    captures, errors = mod.extract_derived_vitest_component_captures(component_dir, _default_vitest_config())
    assert errors == []
    assert [c["capture_id"] for c in captures] == ["guard.vrt.test.ts::real.png"]


# AC10 (P0 item 2, OWNER REQUEST_CHANGES) — mutation test: moving the
# committed PNG to a location that no longer matches the real
# `vitest.visual.config.ts`-derived directory must hard-fail. This proves
# the validator is no longer circular (comparing a self-generated string
# against itself) -- it now depends on genuinely independent inputs (the
# config's declared screenshot directory + the actual PNG on disk).
def test_extract_derived_vitest_component_captures_mutation_wrong_screenshot_directory_fails(tmp_path):
    component_dir = _write_component_vrt_test(
        tmp_path,
        "combat-hud-running.vrt.test.ts",
        """
        test('x', async () => {
          await expect(page.elementLocator(el)).toMatchScreenshot('combat-hud-running.png', {
            comparatorOptions: { allowedMismatchedPixelRatio: 0.02 },
          })
        })
        """,
    )
    # Committed PNG lives at the CORRECT location for `__screenshots__`.
    _write_baseline_png(component_dir, "combat-hud-running.vrt.test.ts", "combat-hud-running.png")

    # A drifted config declares a DIFFERENT screenshot directory.
    mutated_config = _default_vitest_config(screenshot_directory="__wrong_dir__")
    captures, errors = mod.extract_derived_vitest_component_captures(component_dir, mutated_config)
    assert len(captures) == 1
    assert captures[0]["png_exists"] is False
    assert any("expected committed PNG not found" in e for e in errors)


def test_extract_derived_vitest_component_captures_detects_orphan_png(tmp_path):
    component_dir = _write_component_vrt_test(
        tmp_path,
        "combat-hud-running.vrt.test.ts",
        """
        test('x', async () => {
          await expect(page.elementLocator(el)).toMatchScreenshot('combat-hud-running.png', {
            comparatorOptions: { allowedMismatchedPixelRatio: 0.02 },
          })
        })
        """,
    )
    _write_baseline_png(component_dir, "combat-hud-running.vrt.test.ts", "combat-hud-running.png")
    # A stale PNG with no corresponding toMatchScreenshot() call site.
    _write_baseline_png(component_dir, "combat-hud-running.vrt.test.ts", "stale-orphan.png")
    captures, errors = mod.extract_derived_vitest_component_captures(component_dir, _default_vitest_config())
    assert any("orphan committed PNG" in e for e in errors)


def test_extract_derived_vitest_component_captures_against_real_repo_spec_files():
    component_dir = REPO_ROOT / "tests" / "component"
    captures, errors = mod.extract_derived_vitest_component_captures(component_dir, _REAL_VITEST_CONFIG)
    assert errors == []
    capture_ids = {c["capture_id"] for c in captures}
    assert capture_ids == {
        "combat-hud-running.vrt.test.ts::combat-hud-running.png",
        "combat-hud-running.vrt.test.ts::combat-hud-critical.png",
    }
    assert mod.validate_vitest_component_captures(captures) == []


# --- validate_vitest_component_captures ------------------------------------


def test_validate_vitest_component_captures_hard_fails_wrong_root():
    captures = [
        {
            "capture_id": "x.vrt.test.ts::x.png",
            "directory": "tests/e2e/__screenshots__/x.vrt.test.ts/",
            "comparator_kind": "threshold",
            "comparator_value": "0.1",
        }
    ]
    failures = mod.validate_vitest_component_captures(captures)
    assert any("outside the reserved Vitest component snapshot root" in f for f in failures)


def test_validate_vitest_component_captures_hard_fails_unknown_comparator_kind():
    captures = [
        {
            "capture_id": "x.vrt.test.ts::x.png",
            "directory": "tests/component/__screenshots__/x.vrt.test.ts/",
            "comparator_kind": "maxDiffPixels",
            "comparator_value": "1",
        }
    ]
    failures = mod.validate_vitest_component_captures(captures)
    assert any("comparator_kind must be one of" in f for f in failures)


def test_validate_vitest_component_captures_passes_for_valid_capture():
    captures = [
        {
            "capture_id": "x.vrt.test.ts::x.png",
            "directory": "tests/component/__screenshots__/x.vrt.test.ts/",
            "comparator_kind": "allowedMismatchedPixelRatio",
            "comparator_value": "0.02",
        }
    ]
    assert mod.validate_vitest_component_captures(captures) == []


def test_validate_vitest_component_captures_passes_for_multi_key_comparator_dict():
    captures = [
        {
            "capture_id": "x.vrt.test.ts::x.png",
            "directory": "tests/component/__screenshots__/x.vrt.test.ts/",
            "comparator": {"allowedMismatchedPixels": "5", "allowedMismatchedPixelRatio": "0.02"},
        }
    ]
    assert mod.validate_vitest_component_captures(captures) == []


def test_validate_vitest_component_captures_hard_fails_missing_png():
    captures = [
        {
            "capture_id": "x.vrt.test.ts::x.png",
            "directory": "tests/component/__screenshots__/x.vrt.test.ts/",
            "comparator": {"threshold": "0.1"},
            "png_exists": False,
            "png_path": "tests/component/__screenshots__/x.vrt.test.ts/x-chromium-linux.png",
        }
    ]
    failures = mod.validate_vitest_component_captures(captures)
    assert any("missing committed PNG" in f for f in failures)


# --- parse_component_vrt_registry_entries / cross_validate_component_vrt_captures ---


def test_parse_component_vrt_registry_entries_reads_real_registry_doc():
    registry_text = (REPO_ROOT / "docs" / "dev" / "visual-baseline-registry.md").read_text(encoding="utf-8")
    entries = mod.parse_component_vrt_registry_entries(registry_text)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["spec_file"] == "tests/component/combat-hud-running.vrt.test.ts"
    assert entry["directory"] == "tests/component/__screenshots__/combat-hud-running.vrt.test.ts/"
    assert entry["comparator_kind"] == "allowedMismatchedPixelRatio"
    assert entry["comparator_value"] == "0.02"


def _registry_entry(**overrides) -> dict:
    entry = {
        "id": "combat-hud-running (component VRT)",
        "directory": "tests/component/__screenshots__/combat-hud-running.vrt.test.ts/",
        "spec_file": "tests/component/combat-hud-running.vrt.test.ts",
        "comparator_kind": "allowedMismatchedPixelRatio",
        "comparator_value": "0.02",
    }
    entry.update(overrides)
    return entry


def _component_capture(**overrides) -> dict:
    capture = {
        "capture_id": "combat-hud-running.vrt.test.ts::combat-hud-running.png",
        "spec_file": "tests/component/combat-hud-running.vrt.test.ts",
        "screenshot_name": "combat-hud-running.png",
        "directory": "tests/component/__screenshots__/combat-hud-running.vrt.test.ts/",
        "comparator": {"allowedMismatchedPixelRatio": "0.02"},
        "comparator_kind": "allowedMismatchedPixelRatio",
        "comparator_value": "0.02",
        "png_path": (
            "tests/component/__screenshots__/combat-hud-running.vrt.test.ts/"
            "combat-hud-running-chromium-linux.png"
        ),
        "png_exists": True,
    }
    capture.update(overrides)
    return capture


def test_cross_validate_component_vrt_captures_passes_for_matching_pair():
    failures = mod.cross_validate_component_vrt_captures([_component_capture()], [_registry_entry()])
    assert failures == []


# AC10 (P1 item 4, OWNER REQUEST_CHANGES) — zero captures must hard-fail
# (not silent-pass) when the registry declares component VRT entries.
def test_cross_validate_component_vrt_captures_hard_fails_zero_captures():
    failures = mod.cross_validate_component_vrt_captures([], [_registry_entry()])
    assert any("zero component VRT captures found" in f for f in failures)


def test_cross_validate_component_vrt_captures_passes_when_no_registry_entries_declared():
    # No registry entries declared yet -- nothing to cross-validate against
    # (distinct from the "registry declares entries but zero captures
    # found" hard-fail case above).
    assert mod.cross_validate_component_vrt_captures([], []) == []


def test_cross_validate_component_vrt_captures_hard_fails_duplicate_capture_id():
    captures = [_component_capture(), _component_capture()]
    failures = mod.cross_validate_component_vrt_captures(captures, [_registry_entry()])
    assert any("duplicate component VRT capture_id" in f for f in failures)


def test_cross_validate_component_vrt_captures_hard_fails_missing_registry_entry():
    failures = mod.cross_validate_component_vrt_captures(
        [_component_capture(spec_file="tests/component/unregistered.vrt.test.ts")],
        [_registry_entry()],
    )
    assert any("no matching component VRT registry entry" in f for f in failures)
    assert any(
        "no matching active capture" in f for f in failures
    )  # the registry entry itself is also unmatched


def test_cross_validate_component_vrt_captures_hard_fails_comparator_drift():
    failures = mod.cross_validate_component_vrt_captures(
        [_component_capture(comparator={"allowedMismatchedPixelRatio": "0.99"}, comparator_value="0.99")],
        [_registry_entry()],
    )
    assert any("does not match registry-declared" in f for f in failures)


def test_cross_validate_component_vrt_captures_hard_fails_directory_drift():
    failures = mod.cross_validate_component_vrt_captures(
        [_component_capture(directory="tests/component/__screenshots__/other.vrt.test.ts/")],
        [_registry_entry()],
    )
    assert any("directory" in f and "does not match" in f for f in failures)


def test_cross_validate_component_vrt_captures_against_real_repo_state():
    component_dir = REPO_ROOT / "tests" / "component"
    captures, errors = mod.extract_derived_vitest_component_captures(component_dir, _REAL_VITEST_CONFIG)
    assert errors == []
    registry_text = (REPO_ROOT / "docs" / "dev" / "visual-baseline-registry.md").read_text(encoding="utf-8")
    entries = mod.parse_component_vrt_registry_entries(registry_text)
    assert mod.cross_validate_component_vrt_captures(captures, entries) == []


def test_main_passes_against_real_ci_yml_including_component_vrt_audit():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status: pass" in result.stdout


def test_main_hard_fails_when_component_vrt_capture_missing_comparator(tmp_path):
    workflow_src = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    workflow_path = tmp_path / "ci.yml"
    workflow_path.write_text(workflow_src, encoding="utf-8")
    pw_config = REPO_ROOT / "playwright.config.ts"
    e2e_dir = REPO_ROOT / "tests" / "e2e"
    visual_utils = REPO_ROOT / "tests" / "e2e" / "visual-utils.ts"
    registry_doc = REPO_ROOT / "docs" / "dev" / "visual-baseline-registry.md"
    component_dir = _write_component_vrt_test(
        tmp_path,
        "combat-hud-running.vrt.test.ts",
        """
        test('x', async () => {
          await expect(page.elementLocator(el)).toMatchScreenshot('combat-hud-running.png', {})
        })
        """,
    )
    vitest_config_path = tmp_path / "vitest.visual.config.ts"
    vitest_config_path.write_text(
        "export default { test: { include: ['tests/component/*.vrt.test.ts'], "
        "browser: { screenshotDirectory: '__screenshots__' } } }",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(workflow_path),
            str(pw_config),
            str(e2e_dir),
            str(visual_utils),
            str(registry_doc),
            str(component_dir),
            str(vitest_config_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "status: fail" in result.stdout
    assert "component vrt capture" in result.stdout

"""test_resolve_visual_impact.py (Issue #2019, AC4 / AC5 / AC24)

GIVEN/WHEN/THEN tests for scripts/agent-ops/resolve_visual_impact.py, the
Python orchestration layer that reads docs/dev/visual-surfaces.yml and
delegates static import-graph resolution to resolve_visual_impact.mjs
(TypeScript compiler API).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2019"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.yml"
SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.schema.json"
MJS_PATH = REPO_ROOT / "scripts" / "agent-ops" / "resolve_visual_impact.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required for resolve_visual_impact.mjs")


def test_ac4_combat_hud_module_change_flags_combat_hud_running():
    """GIVEN a diff touching src/ui/combatHud.ts WHEN resolved THEN
    combat-hud-running is reported as an affected surface."""
    result = rvi.resolve(
        changed_paths=["src/ui/combatHud.ts"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    assert not result.errors
    affected_ids = {entry["surface_id"] for entry in result.affected_surfaces}
    assert "combat-hud-running" in affected_ids


def test_ac24_regression_1958_combathud_change_without_baseline_update_fails():
    """GIVEN the #1958 regression shape (src/ui/combatHud.ts changed, baseline
    PNG NOT changed) WHEN resolved THEN combat-hud-running is affected and
    therefore requires a disposition -- it must never be silently no-impact."""
    baseline_path = (
        "tests/component/__screenshots__/combat-hud-running.vrt.test.ts/"
        "combat-hud-running-chromium-linux.png"
    )
    result = rvi.resolve(
        changed_paths=["src/ui/combatHud.ts"],  # deliberately NOT including baseline_path
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    affected_ids = {entry["surface_id"] for entry in result.affected_surfaces}
    assert "combat-hud-running" in affected_ids
    assert baseline_path not in result.changed_paths


def test_transitive_dependency_change_flags_affected_surface():
    """GIVEN a changed file that is only transitively reachable (via a
    fixture entry module's import graph, resolved by the TS compiler API
    layer) WHEN resolved THEN the surface is still reported as affected."""
    fixture_dir = REPO_ROOT / "scripts" / "agent-ops" / "tests" / "fixtures" / "visual_impact" / "vite_deterministic"
    registry_doc = {
        "schema_version": 1,
        "global_invalidators": [],
        "coverage_roots": ["scripts/agent-ops/tests/fixtures/visual_impact/vite_deterministic/**"],
        "surfaces": {
            "fixture-surface": {
                "producers": {
                    "modules": [str((fixture_dir / "entry.ts").relative_to(REPO_ROOT))],
                    "styles": [],
                    "assets": [],
                    "config": [],
                },
                "contracts": {
                    "runner": "vitest-browser-mode",
                    "spec": "fixture-spec.vrt.test.ts",
                    "baseline": "fixture-baseline.png",
                    "job": "component-vrt-report",
                    "update_command_id": "vitest_component_vrt_update",
                    "verify_command_id": "vitest_component_vrt_verify",
                    "maturity": "provisional",
                },
                "policy": {"disposition_required": True},
            }
        },
    }
    tmp_registry = fixture_dir / "_tmp_registry_for_transitive_test.yml"
    tmp_registry.write_text(yaml.safe_dump(registry_doc), encoding="utf-8")
    try:
        result = rvi.resolve(
            changed_paths=[str((fixture_dir / "styles" / "base.css").relative_to(REPO_ROOT))],
            registry_path=tmp_registry,
            schema_path=SCHEMA_PATH,
            mjs_path=MJS_PATH,
            repo_root=REPO_ROOT,
        )
        assert not result.errors
        affected_ids = {entry["surface_id"] for entry in result.affected_surfaces}
        assert "fixture-surface" in affected_ids
    finally:
        tmp_registry.unlink(missing_ok=True)


def test_ac5_registry_union_detects_deleted_producer_mapping():
    """GIVEN a base registry that maps a producer and a head registry that
    removed that mapping WHEN diffed THEN the surface is reported affected
    (mapping deletion is treated as an impact, not a bypass)."""
    fixtures = REPO_ROOT / "scripts" / "agent-ops" / "tests" / "fixtures" / "visual_impact" / "registry_union"
    base_doc = yaml.safe_load((fixtures / "base_registry.yml").read_text(encoding="utf-8"))
    head_doc = yaml.safe_load((fixtures / "head_registry_deleted_mapping.yml").read_text(encoding="utf-8"))
    affected = rvi.diff_producer_mappings(base_doc, head_doc)
    assert "fixture-surface" in affected


def test_ac5_registry_union_no_diff_when_mapping_unchanged():
    """GIVEN identical base/head registries WHEN diffed THEN no surface is
    reported as mapping-deleted."""
    fixtures = REPO_ROOT / "scripts" / "agent-ops" / "tests" / "fixtures" / "visual_impact" / "registry_union"
    base_doc = yaml.safe_load((fixtures / "base_registry.yml").read_text(encoding="utf-8"))
    affected = rvi.diff_producer_mappings(base_doc, base_doc)
    assert affected == set()


def test_unmapped_visual_candidate_fails_closed():
    """GIVEN a changed path under coverage_roots that maps to NO surface
    WHEN resolved THEN it is reported as unmapped_visual_candidate (never
    silently no-impact)."""
    result = rvi.resolve(
        changed_paths=["src/ui/debugPause.ts"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    assert "src/ui/debugPause.ts" in result.unmapped_visual_candidates
    assert result.affected_surfaces == [] or all(
        e["surface_id"] != "unmapped" for e in result.affected_surfaces
    )


def test_global_invalidator_affects_all_surfaces():
    """GIVEN src/style.css (a registered global_invalidator) changes WHEN
    resolved THEN every surface in the registry is affected."""
    result = rvi.resolve(
        changed_paths=["src/style.css"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    head_doc = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    all_surface_ids = set(head_doc["surfaces"].keys())
    affected_ids = {entry["surface_id"] for entry in result.affected_surfaces}
    assert all_surface_ids <= affected_ids


def test_command_id_map_resolves_known_ids_only():
    """GIVEN COMMAND_ID_MAP WHEN inspected THEN it only contains the closed
    enum values declared in docs/dev/visual-surfaces.schema.json (no raw
    shell strings leak from the registry itself)."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    update_enum = set(
        schema["properties"]["surfaces"]["additionalProperties"]["properties"]["contracts"]["properties"][
            "update_command_id"
        ]["enum"]
    )
    verify_enum = set(
        schema["properties"]["surfaces"]["additionalProperties"]["properties"]["contracts"]["properties"][
            "verify_command_id"
        ]["enum"]
    )
    known_ids = update_enum | verify_enum
    assert set(rvi.COMMAND_ID_MAP.keys()) == known_ids
    for argv in rvi.COMMAND_ID_MAP.values():
        assert isinstance(argv, list)
        assert all(isinstance(part, str) for part in argv)

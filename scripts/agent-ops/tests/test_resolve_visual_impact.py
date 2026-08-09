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
from datetime import date
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


def _mjs_dependencies_available() -> bool:
    """`node` alone is not sufficient: resolve_visual_impact.mjs resolves the
    ``typescript`` package via Node ESM module resolution rooted at
    REPO_ROOT. The `python-test-core` CI lane is intentionally Python-only
    (no `pnpm install`; see docs/dev/test-lane-policy.md / #1760), so
    ``node_modules/typescript`` is absent there and a plain `node` presence
    check would let the subprocess hard-crash with ERR_MODULE_NOT_FOUND
    instead of cleanly skipping. AC4/AC5 behavior is still exercised end to
    end (real resolve_visual_impact.py + resolve_visual_impact.mjs
    subprocess, not a reimplementation) by the Vitest suite under
    tests/agent-ops/resolve-visual-impact-*.test.ts, which runs in the
    `test` job where `pnpm install` has already populated node_modules.
    """
    if shutil.which("node") is None:
        return False
    return (REPO_ROOT / "node_modules" / "typescript").is_dir()


pytestmark = pytest.mark.skipif(
    not _mjs_dependencies_available(),
    reason="node + node_modules/typescript is required for resolve_visual_impact.mjs",
)


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


def test_pr2045_meta_policy_path_change_affects_all_surfaces():
    """PR #2045 OWNER fix_delta P0-4/P0-6: a change to the evaluator/registry
    itself (e.g. resolve_visual_impact.py) must never be treated as
    no-impact -- it invalidates trust in every other affected-surface
    determination in the same diff, so ALL registered surfaces become
    affected (`meta_policy_change`)."""
    result = rvi.resolve(
        changed_paths=["scripts/agent-ops/resolve_visual_impact.py"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    head_doc = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    all_surface_ids = set(head_doc["surfaces"].keys())
    affected = {e["surface_id"]: e["reason"] for e in result.affected_surfaces}
    assert all_surface_ids <= set(affected.keys())
    assert affected["combat-hud-running"] == "meta_policy_change"


def test_pr2045_baseline_only_change_marks_surface_affected():
    """PR #2045 OWNER fix_delta P0-4: changing ONLY the registered baseline
    PNG (no producer module touched) must still mark the surface affected
    -- this is the exact "baseline PNG regenerated with no disposition"
    bypass shape the OWNER flagged (baseline/spec are outside
    `coverage_roots`, which is producer-source scoped)."""
    baseline_path = (
        "tests/component/__screenshots__/combat-hud-running.vrt.test.ts/"
        "combat-hud-running-chromium-linux.png"
    )
    result = rvi.resolve(
        changed_paths=[baseline_path],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    affected_ids = {entry["surface_id"] for entry in result.affected_surfaces}
    assert "combat-hud-running" in affected_ids


def test_pr2045_spec_only_change_marks_surface_affected():
    """PR #2045 OWNER fix_delta P0-4: changing ONLY the registered VRT spec
    file must also mark the surface affected."""
    result = rvi.resolve(
        changed_paths=["tests/component/combat-hud-running.vrt.test.ts"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    affected_ids = {entry["surface_id"] for entry in result.affected_surfaces}
    assert "combat-hud-running" in affected_ids
    assert "combat-hud-critical" in affected_ids


def test_pr2045_resolve_result_carries_validated_head_doc():
    """PR #2045 OWNER fix_delta P0-2: resolve() exposes the single validated
    head registry document so callers (e.g. _run_policy_check) never
    re-parse/re-validate a second, potentially divergent copy."""
    result = rvi.resolve(
        changed_paths=["src/ui/combatHud.ts"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    assert result.head_doc is not None
    assert "combat-hud-running" in result.head_doc.get("surfaces", {})


def test_pr2045_mjs_returncode_nonzero_is_a_resolver_error(tmp_path):
    """PR #2045 OWNER fix_delta P0-2: a non-zero resolve_visual_impact.mjs
    exit code must be surfaced as a resolver error even when stdout happens
    to parse as valid JSON (crash-with-partial-output must never degrade to
    "fully resolved, zero impact")."""
    fake_mjs = tmp_path / "fake_resolve_visual_impact.mjs"
    fake_mjs.write_text(
        "#!/usr/bin/env node\n"
        "process.stdin.resume();\n"
        "process.stdin.on('end', () => {\n"
        "  process.stdout.write(JSON.stringify({\n"
        "    schema: 'RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1',\n"
        "    resolver_version: '1',\n"
        "    surfaces: { 'combat-hud-running': { reachable_files: [], unknown_impact: [] }, "
        "'combat-hud-critical': { reachable_files: [], unknown_impact: [] } },\n"
        "    errors: ['boom']\n"
        "  }));\n"
        "  process.exitCode = 1;\n"
        "});\n",
        encoding="utf-8",
    )
    result = rvi.resolve(
        changed_paths=["src/ui/combatHud.ts"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=fake_mjs,
        repo_root=REPO_ROOT,
    )
    assert result.errors, "a non-zero mjs exit must be recorded as a resolver error"
    assert any("exited 1" in e for e in result.errors)


def test_pr2045_mjs_surface_key_mismatch_is_a_resolver_error(tmp_path):
    """PR #2045 OWNER fix_delta P0-2: the mjs result's `surfaces` key set
    must match the request's -- a resolver that silently drops a requested
    surface from its output must never be treated as "that surface has no
    impact"."""
    fake_mjs = tmp_path / "fake_resolve_visual_impact.mjs"
    fake_mjs.write_text(
        "#!/usr/bin/env node\n"
        "process.stdin.resume();\n"
        "process.stdin.on('end', () => {\n"
        "  process.stdout.write(JSON.stringify({\n"
        "    schema: 'RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1',\n"
        "    resolver_version: '1',\n"
        "    surfaces: {},\n"
        "    errors: []\n"
        "  }));\n"
        "  process.exitCode = 0;\n"
        "});\n",
        encoding="utf-8",
    )
    result = rvi.resolve(
        changed_paths=["src/ui/combatHud.ts"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=fake_mjs,
        repo_root=REPO_ROOT,
    )
    assert result.errors
    assert any("surface key set mismatch" in e for e in result.errors)


def test_pr2045_resolver_error_fails_policy_closed():
    """PR #2045 OWNER fix_delta P0-2: `evaluate_pr_policy` must fail closed
    when `resolve_result.errors` is non-empty, even if `affected_surfaces`
    happens to be empty -- previously a broken resolver run silently
    produced an unconditional PASS."""
    broken_result = rvi.ResolveResult(
        changed_paths=["src/ui/combatHud.ts"],
        affected_surfaces=[],
        errors=["mjs crashed"],
    )
    policy_result = rvi.evaluate_pr_policy(
        resolve_result=broken_result,
        declaration_doc=None,
        registry_doc={"surfaces": {}},
        evidence_manifest=None,
        head_sha="a" * 40,
        changed_paths=["src/ui/combatHud.ts"],
        actor="squne121",
        authorized_owners=set(),
        today=date(2026, 8, 9),
    )
    assert policy_result["ok"] is False
    assert any("resolver_error" in f for f in policy_result["failures"])


def test_pr2045_p0_1_pipefail_negative_integration(tmp_path):
    """PR #2045 OWNER fix_delta P0-1: reproduce the exact shell shape the
    `visual-impact-policy` CI job uses (`<evaluator> | tee <file>`) and
    prove that WITHOUT `set -o pipefail` a non-zero evaluator exit is
    silently swallowed (step exit 0), while WITH `set -o pipefail` (the
    fix) the step correctly exits non-zero. This is a real subprocess/shell
    integration test, not a mock of the evaluator's return value."""
    import subprocess

    # An affected surface with no VISUAL_IMPACT_DECLARATION_V1 in the PR
    # body -> guaranteed policy-check failure (exit 1), independent of mjs
    # availability (pure Python failure path -- missing declaration entry).
    pr_body_file = tmp_path / "pr_body.md"
    pr_body_file.write_text("no declaration here", encoding="utf-8")
    changed_paths_file = tmp_path / "changed_paths.txt"
    changed_paths_file.write_text("src/style.css\n", encoding="utf-8")  # global_invalidator

    cmd = (
        f"uv run --locked python3 {MJS_PATH.parent / 'resolve_visual_impact.py'} "
        f"--mode policy-check --changed-paths-file {changed_paths_file} "
        f"--pr-body-file {pr_body_file} --head-sha {'a' * 40} "
        f"| tee {tmp_path / 'out.json'}"
    )

    without_pipefail = subprocess.run(
        ["bash", "-c", f"set +o pipefail; {cmd}"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert without_pipefail.returncode == 0, (
        "documents the pre-fix bug shape: without pipefail, `tee`'s own exit "
        "status (0) masks the evaluator's real non-zero exit"
    )

    with_pipefail = subprocess.run(
        ["bash", "-c", f"set -o pipefail; {cmd}"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert with_pipefail.returncode != 0, (
        "PR #2045 P0-1 fix: `set -o pipefail` must make the pipeline exit "
        "non-zero when the evaluator itself fails"
    )


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

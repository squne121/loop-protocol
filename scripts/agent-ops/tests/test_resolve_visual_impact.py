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


def test_pr2045_bootstrap_base_ref_predating_registry_is_not_a_resolver_error():
    """PR #2045 OWNER fix_delta P0-2/P0-3: enabling `--base-ref`/`--head-ref`
    in production (this PR's own CI wiring fix) exposed a real bug: a base
    ref that predates the registry file entirely (this Issue's own PR,
    whose base branch commit -- before merge -- has no
    docs/dev/visual-surfaces.yml) synthesizes an empty registry doc that was
    then incorrectly run through schema validation (which requires
    `surfaces` to be non-empty) and raised `RegistryError`, poisoning every
    resolve() call with a spurious `base registry invalid` failure. The
    legitimate bootstrap case must never schema-fail."""
    import subprocess

    # A commit before this Issue's registry file existed.
    pre_registry_sha = subprocess.run(
        ["git", "log", "--format=%H", "--", "docs/dev/visual-surfaces.yml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()[-1]
    pre_registry_parent = subprocess.run(
        ["git", "rev-parse", f"{pre_registry_sha}~1"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if pre_registry_parent.returncode != 0:
        pytest.skip("no ancestor commit predating docs/dev/visual-surfaces.yml available locally")
    base_ref = pre_registry_parent.stdout.strip()

    result = rvi.resolve(
        changed_paths=["src/ui/combatHud.ts"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
        base_ref=base_ref,
        head_ref=None,
    )
    assert not any("base registry invalid" in e for e in result.errors), result.errors
    affected_ids = {entry["surface_id"] for entry in result.affected_surfaces}
    assert "combat-hud-running" in affected_ids


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


def _p2_contracts(spec: str, baseline: str) -> dict:
    return {
        "runner": "vitest-browser-mode",
        "spec": spec,
        "baseline": baseline,
        "job": "component-vrt-report",
        "update_command_id": "vitest_component_vrt_update",
        "verify_command_id": "vitest_component_vrt_verify",
        "maturity": "provisional",
    }


def test_p2_e2e_verified_unchanged_vrt_evidence_success_path_real_subprocess(tmp_path):
    """Issue #2525 P2 fix_delta (OWNER REQUEST_CHANGES on PR #2548,
    2026-09-06): proves the ORDINARY (non-waiver) `verified_unchanged` +
    VRT-evidence success path end to end -- config files unchanged, only a
    dependency reachable exclusively through the fixture's unsupported
    `@app/*` tsconfig path alias changes -- through the REAL
    resolve_visual_impact.mjs subprocess and resolve_visual_impact.py's
    resolve() + evaluate_pr_policy(). AC3's existing coverage in
    test_resolve_visual_impact_fallback_policy.py only proves the
    all-surfaces-WAIVER success path with a mocked run_mjs; this proves the
    real verified_unchanged + evidence-manifest binding path with a real
    Node subprocess boundary, plus the missing-evidence and
    mismatched-evidence failure directions from the same scenario."""
    fixture_dir = (
        REPO_ROOT / "scripts" / "agent-ops" / "tests" / "fixtures" / "visual_impact" / "unsupported_resolution"
    )

    registry_doc = {
        "schema_version": 1,
        "global_invalidators": [],
        "coverage_roots": ["*.ts"],
        "surfaces": {
            "fixture-surface-a": {
                "producers": {"modules": ["entry.ts"], "styles": [], "assets": [], "config": []},
                "contracts": _p2_contracts("fixture-a.vrt.test.ts", "fixture-a-baseline.png"),
                "policy": {"disposition_required": True},
            },
            "fixture-surface-b": {
                "producers": {"modules": ["entry.ts"], "styles": [], "assets": [], "config": []},
                "contracts": _p2_contracts("fixture-b.vrt.test.ts", "fixture-b-baseline.png"),
                "policy": {"disposition_required": True},
            },
        },
    }
    registry_path = tmp_path / "registry.yml"
    registry_path.write_text(yaml.safe_dump(registry_doc), encoding="utf-8")

    result = rvi.resolve(
        changed_paths=["dependency.ts"],
        registry_path=registry_path,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=fixture_dir,
    )
    assert not result.errors, result.errors
    assert result.resolver_fallback_active is True
    affected = {e["surface_id"] for e in result.affected_surfaces}
    assert affected == {"fixture-surface-a", "fixture-surface-b"}

    head_sha = "a" * 40
    declaration_doc = {
        "surfaces": [
            {"surface_id": "fixture-surface-a", "disposition": "verified_unchanged"},
            {"surface_id": "fixture-surface-b", "disposition": "verified_unchanged"},
        ]
    }

    def _record(surface_id: str, *, mismatched_pixels: int = 0, verify_succeeded: bool = True) -> dict:
        surface_def = registry_doc["surfaces"][surface_id]
        return rvi.build_evidence_manifest_v2_record(
            surface_id=surface_id,
            contract_digest=rvi.compute_contract_digest(surface_def),
            head_sha=head_sha,
            workflow_run_id=1,
            check_run_id=None,
            check_suite_id=None,
            github_app_id=None,
            github_app_slug=None,
            check_conclusion=None,
            baseline_path=surface_def["contracts"]["baseline"],
            baseline_sha256="b" * 64,
            actual_sha256="b" * 64,
            mismatched_pixels=mismatched_pixels,
            verify_command_id="vitest_component_vrt_verify",
            verify_succeeded=verify_succeeded,
            update_command_id="vitest_component_vrt_update",
            update_executed=False,
            update_succeeded=False,
            expected_artifact_id="expected-1",
            actual_artifact_id="actual-1",
            diff_artifact_id="diff-1",
        )

    full_manifest = {
        "schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA,
        "surfaces": [_record("fixture-surface-a"), _record("fixture-surface-b")],
    }

    def _evaluate(evidence_manifest: dict) -> dict:
        return rvi.evaluate_pr_policy(
            resolve_result=result,
            declaration_doc=declaration_doc,
            registry_doc=registry_doc,
            evidence_manifest=evidence_manifest,
            head_sha=head_sha,
            changed_paths=["dependency.ts"],
            actor="squne121",
            authorized_owners=set(),
            today=date(2026, 9, 6),
            trusted_check_run_id="run-1",
            trusted_check_suite_id="suite-1",
            trusted_github_app_id="app-1",
            trusted_github_app_slug="github-actions",
            trusted_check_conclusion="success",
        )

    # Ordinary success path: both surfaces have a full, matching evidence
    # manifest record.
    policy_result = _evaluate(full_manifest)
    assert policy_result["ok"] is True, policy_result["failures"]
    assert len(policy_result["surface_results"]) == 2
    assert all(entry["ok"] for entry in policy_result["surface_results"])

    # Omitting the required evidence for just fixture-surface-b -> ok False.
    manifest_missing_b = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [full_manifest["surfaces"][0]]}
    result_missing = _evaluate(manifest_missing_b)
    assert result_missing["ok"] is False
    assert any("fixture-surface-b" in f for f in result_missing["failures"])

    # A VRT evidence mismatch for fixture-surface-b -> ok False.
    manifest_mismatch = {
        "schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA,
        "surfaces": [
            full_manifest["surfaces"][0],
            _record("fixture-surface-b", mismatched_pixels=42, verify_succeeded=False),
        ],
    }
    result_mismatch = _evaluate(manifest_mismatch)
    assert result_mismatch["ok"] is False
    assert any("fixture-surface-b" in f for f in result_mismatch["failures"])


def _write_css_global_invalidator_fixture(tmp_path) -> None:
    """Shared fixture for AC8/AC9: a `style.css` global invalidator that
    `@import`s a nested CSS file, which in turn references an image via a
    plain `url()` (never itself recursively walked as CSS). `entry.ts` is a
    schema-required non-empty `producers.modules` placeholder that imports
    nothing (so it never introduces an independent producer_reachable hit)."""
    (tmp_path / "styles").mkdir()
    (tmp_path / "assets").mkdir()
    (tmp_path / "entry.ts").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "style.css").write_text('@import "styles/nested.css";\n', encoding="utf-8")
    (tmp_path / "styles" / "nested.css").write_text(
        '.a { background: url("../assets/icon.svg"); }\n', encoding="utf-8"
    )
    (tmp_path / "assets" / "icon.svg").write_text("<svg></svg>\n", encoding="utf-8")


def test_ac8_css_global_invalidator_transitive_dependency_change_affects_registered_surfaces(tmp_path):
    """AC8: `src/style.css` itself is NOT changed -- only its transitive
    `@import`/`url()` dependency (`assets/icon.svg`, reached via
    `styles/nested.css`) is. `build_mjs_request()`/`resolve()` must add the
    registered CSS `global_invalidators` entry (`style.css`) as an extra
    graph root for every surface so this transitive change is detected as
    `producer_reachable` -- NEVER via a direct `global_invalidator` string
    match (the invalidator itself is deliberately excluded from
    `changed_paths` so a direct-match hit can never hide this gap)."""
    _write_css_global_invalidator_fixture(tmp_path)
    registry_doc = {
        "schema_version": 1,
        "global_invalidators": ["style.css"],
        "coverage_roots": ["unused/**"],
        "surfaces": {
            "combat-hud-running": {
                "producers": {"modules": ["entry.ts"], "styles": [], "assets": [], "config": []},
                "contracts": _p2_contracts("fixture-a.vrt.test.ts", "fixture-a-baseline.png"),
                "policy": {"disposition_required": True},
            },
            "combat-hud-critical": {
                "producers": {"modules": ["entry.ts"], "styles": [], "assets": [], "config": []},
                "contracts": _p2_contracts("fixture-b.vrt.test.ts", "fixture-b-baseline.png"),
                "policy": {"disposition_required": True},
            },
        },
    }
    registry_path = tmp_path / "registry.yml"
    registry_path.write_text(yaml.safe_dump(registry_doc), encoding="utf-8")

    result = rvi.resolve(
        changed_paths=["assets/icon.svg"],  # style.css itself is NOT in changed_paths
        registry_path=registry_path,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=tmp_path,
    )
    assert not result.errors, result.errors
    affected = {e["surface_id"]: e["reason"] for e in result.affected_surfaces}
    assert {"combat-hud-running", "combat-hud-critical"} <= set(affected.keys())
    assert affected["combat-hud-running"] != "global_invalidator"
    assert affected["combat-hud-critical"] != "global_invalidator"


def test_ac9_base_head_union_preserves_css_global_invalidator_transitive_reachability(tmp_path, monkeypatch):
    """AC9 (base/head union negative regression): the BASE registry
    registers `style.css` as a CSS `global_invalidators` entry; the HEAD
    registry has REMOVED that registration. `style.css` itself is never in
    `changed_paths` -- only a file it transitively `@import`s/references is
    changed. Because `resolve()`'s base/head UNION (PR #2045 OWNER
    fix_delta P0-3) is supposed to preserve base-side `global_invalidators`
    coverage, the registered surfaces must still be reported affected."""
    _write_css_global_invalidator_fixture(tmp_path)
    surfaces_doc = {
        "combat-hud-running": {
            "producers": {"modules": ["entry.ts"], "styles": [], "assets": [], "config": []},
            "contracts": _p2_contracts("fixture-a.vrt.test.ts", "fixture-a-baseline.png"),
            "policy": {"disposition_required": True},
        },
        "combat-hud-critical": {
            "producers": {"modules": ["entry.ts"], "styles": [], "assets": [], "config": []},
            "contracts": _p2_contracts("fixture-b.vrt.test.ts", "fixture-b-baseline.png"),
            "policy": {"disposition_required": True},
        },
    }
    base_doc = {
        "schema_version": 1,
        "global_invalidators": ["style.css"],
        "coverage_roots": ["unused/**"],
        "surfaces": surfaces_doc,
    }
    head_doc = {
        "schema_version": 1,
        "global_invalidators": [],  # removed on head
        "coverage_roots": ["unused/**"],
        "surfaces": surfaces_doc,
    }

    def fake_load(registry_path, schema_path, git_ref, repo_root):
        return head_doc if git_ref == "HEAD" else base_doc

    monkeypatch.setattr(rvi, "load_and_validate_registry", fake_load)

    result = rvi.resolve(
        changed_paths=["assets/icon.svg"],  # style.css itself is NOT in changed_paths
        registry_path=tmp_path / "unused_registry.yml",
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=tmp_path,
        base_ref="BASE",
        head_ref="HEAD",
    )
    assert not result.errors, result.errors
    affected = {e["surface_id"]: e["reason"] for e in result.affected_surfaces}
    assert {"combat-hud-running", "combat-hud-critical"} <= set(affected.keys())
    assert affected["combat-hud-running"] != "global_invalidator"
    assert affected["combat-hud-critical"] != "global_invalidator"


def test_ac10_vitest_visual_config_ts_is_a_global_invalidator():
    """AC10: `vitest.visual.config.ts` is registered in
    docs/dev/visual-surfaces.yml's `global_invalidators` -- changing ONLY
    that file must mark every registered surface affected."""
    result = rvi.resolve(
        changed_paths=["vitest.visual.config.ts"],
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    head_doc = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    all_surface_ids = set(head_doc["surfaces"].keys())
    affected = {e["surface_id"]: e["reason"] for e in result.affected_surfaces}
    assert all_surface_ids <= set(affected.keys())
    assert affected["combat-hud-running"] == "global_invalidator"


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

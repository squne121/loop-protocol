"""test_resolve_visual_impact_fallback_policy.py (Issue #2525)

GIVEN/WHEN/THEN tests for the all-registered-surfaces-affected fallback that
`scripts/agent-ops/resolve_visual_impact.py`'s `resolve()` and
`evaluate_pr_policy()` connect an unsupported resolution setting
(`resolve_visual_impact.mjs`'s `detectUnsupportedResolutionSettings()`) and
`unknown_impact` disposition to (Issue #2019 OWNER anchor comment / Issue
#2525 anchor comment, 2026-09-06).

Deliberately Node-INDEPENDENT (never spawns `node`, never requires
`node_modules/typescript`): `resolve()`'s call to `run_mjs()` is
monkeypatched with a synthetic `RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1`-shaped
dict, and `evaluate_pr_policy()` is exercised directly against synthetic
`ResolveResult` objects. This guarantees the module never SKIPs (unlike
`test_resolve_visual_impact.py`, which legitimately skips when `node`/
`node_modules/typescript` are unavailable) -- Runtime Verification
Applicability `skip_conditions` names this exact module as one of the two
completion-evidence sources that must PASS regardless (the other being the
Vitest suite, which DOES exercise the real Node subprocess boundary).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2525"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.schema.json"
MJS_PATH = REPO_ROOT / "scripts" / "agent-ops" / "resolve_visual_impact.mjs"  # unused -- run_mjs is mocked below

FIXTURE_DIR_REL = "scripts/agent-ops/tests/fixtures/visual_impact/unsupported_resolution"
FIXTURE_ENTRY = f"{FIXTURE_DIR_REL}/entry.ts"
FIXTURE_OTHER_ENTRY = f"{FIXTURE_DIR_REL}/other_entry.ts"
FIXTURE_DEPENDENCY = f"{FIXTURE_DIR_REL}/dependency.ts"

HEAD_SHA = "a" * 40


def _contracts(spec: str, baseline: str) -> dict[str, Any]:
    return {
        "runner": "vitest-browser-mode",
        "spec": spec,
        "baseline": baseline,
        "job": "component-vrt-report",
        "update_command_id": "vitest_component_vrt_update",
        "verify_command_id": "vitest_component_vrt_verify",
        "maturity": "provisional",
    }


def _two_surface_registry_doc() -> dict[str, Any]:
    """Two registered surfaces, neither of which lists `dependency.ts` as a
    producer -- it is only reachable (in the real fixture) through the
    unsupported `@app/*` tsconfig path alias (see entry.ts), which this
    resolver's bare-import walk does not follow. `coverage_roots` covers
    the whole fixture directory so an unmapped `dependency.ts` change would
    be a `unmapped_visual_candidate` absent the fallback."""
    return {
        "schema_version": 1,
        "global_invalidators": [],
        "coverage_roots": [f"{FIXTURE_DIR_REL}/**"],
        "surfaces": {
            "fixture-surface-a": {
                "producers": {"modules": [FIXTURE_ENTRY], "styles": [], "assets": [], "config": []},
                "contracts": _contracts("fixture-a.vrt.test.ts", "fixture-a-baseline.png"),
                "policy": {"disposition_required": True},
            },
            "fixture-surface-b": {
                "producers": {"modules": [FIXTURE_OTHER_ENTRY], "styles": [], "assets": [], "config": []},
                "contracts": _contracts("fixture-b.vrt.test.ts", "fixture-b-baseline.png"),
                "policy": {"disposition_required": True},
            },
        },
    }


def _write_registry(tmp_path: Path, doc: dict[str, Any]) -> Path:
    registry_path = tmp_path / "registry.yml"
    registry_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return registry_path


def _make_fake_run_mjs(
    *,
    unsupported_resolution_settings: list[str] | None = None,
    unknown_impact_by_surface: dict[str, list[dict[str, Any]]] | None = None,
):
    """Builds a synthetic `run_mjs()` replacement -- never spawns Node.
    Mirrors the real RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1 shape exactly
    (including the new `unsupported_resolution_settings` diagnostic field)
    so `resolve()`'s consumption of it is exercised for real."""

    def _fake_run_mjs(mjs_path: Path, request: dict[str, Any], node_bin: str = "node", timeout_seconds: float = 60):
        del mjs_path, node_bin, timeout_seconds
        surfaces_out: dict[str, Any] = {}
        for surface_id in request.get("surfaces", {}):
            unknown = list((unknown_impact_by_surface or {}).get(surface_id, []))
            surfaces_out[surface_id] = {"reachable_files": [], "unknown_impact": unknown}
        return {
            "schema": "RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1",
            "resolver_version": "1",
            "surfaces": surfaces_out,
            "errors": [],
            "unsupported_resolution_settings": list(unsupported_resolution_settings or []),
        }

    return _fake_run_mjs


# ---------------------------------------------------------------------------
# AC1 / AC4: resolve_visual_impact.mjs's unsupported_resolution_settings ->
# resolve()'s all-registered-surfaces-affected fallback.
# ---------------------------------------------------------------------------


def test_ac1_unsupported_resolution_settings_marks_all_registered_surfaces_affected(monkeypatch, tmp_path):
    """GIVEN resolve_visual_impact.mjs reports a non-empty
    unsupported_resolution_settings diagnostic WHEN resolved THEN every
    registered surface is affected (reason unsupported_resolution_fallback)
    and NO resolver-fatal error is recorded."""
    monkeypatch.setattr(
        rvi,
        "run_mjs",
        _make_fake_run_mjs(
            unsupported_resolution_settings=[
                'package.json "exports" string shorthand ("./entry.ts") is configured but not supported '
                "by this bare-import resolver",
            ]
        ),
    )
    registry_path = _write_registry(tmp_path, _two_surface_registry_doc())

    result = rvi.resolve(
        changed_paths=[FIXTURE_DEPENDENCY],
        registry_path=registry_path,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )

    assert result.errors == []
    assert result.resolver_fallback_active is True
    assert result.unsupported_resolution_settings
    affected = {e["surface_id"]: e["reason"] for e in result.affected_surfaces}
    assert affected == {
        "fixture-surface-a": "unsupported_resolution_fallback",
        "fixture-surface-b": "unsupported_resolution_fallback",
    }


def test_ac4_regression_dependency_only_change_still_triggers_fallback_without_config_change(monkeypatch, tmp_path):
    """AC4 regression: changed_paths contains ONLY `dependency.ts` -- none
    of the fixture's config files (package.json / tsconfig.json /
    tsconfig.base.json / vite.config.ts / config/shared_resolve.ts) are in
    the diff. The fallback must still fire (this models the real detector,
    which reads live filesystem state rather than diffing the config files
    themselves) -- a detector that only fired when a config file itself was
    the changed path would silently miss exactly this shape."""
    monkeypatch.setattr(
        rvi,
        "run_mjs",
        _make_fake_run_mjs(unsupported_resolution_settings=["tsconfig.base.json compilerOptions.paths is configured"]),
    )
    registry_path = _write_registry(tmp_path, _two_surface_registry_doc())

    changed_paths = [FIXTURE_DEPENDENCY]
    config_paths = {
        f"{FIXTURE_DIR_REL}/package.json",
        f"{FIXTURE_DIR_REL}/tsconfig.json",
        f"{FIXTURE_DIR_REL}/tsconfig.base.json",
        f"{FIXTURE_DIR_REL}/vite.config.ts",
        f"{FIXTURE_DIR_REL}/config/shared_resolve.ts",
    }
    assert not (set(changed_paths) & config_paths), "regression must exercise a config-file-UNCHANGED diff"

    result = rvi.resolve(
        changed_paths=changed_paths,
        registry_path=registry_path,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )

    affected_ids = {e["surface_id"] for e in result.affected_surfaces}
    assert affected_ids == {"fixture-surface-a", "fixture-surface-b"}
    # The diagnostic-only unmapped_visual_candidates signal is still
    # retained (never dropped) even though it is not independently
    # blocking (see evaluate_pr_policy() tests below).
    assert FIXTURE_DEPENDENCY in result.unmapped_visual_candidates


# ---------------------------------------------------------------------------
# AC1: py's unknown_impact -> the SAME all-registered-surfaces-affected
# fallback (never only the one surface whose own walk hit the construct).
# ---------------------------------------------------------------------------


def test_ac1_unknown_impact_marks_all_registered_surfaces_affected_not_only_the_hit_surface(monkeypatch, tmp_path):
    """GIVEN only fixture-surface-a's producer graph walk hits an
    unknown_impact construct WHEN resolved THEN fixture-surface-b (which
    hit no unknown_impact construct at all) is ALSO affected -- the
    analysis incompleteness invalidates trust in the whole diff's
    resolution, not just the one surface that happened to hit it."""
    monkeypatch.setattr(
        rvi,
        "run_mjs",
        _make_fake_run_mjs(
            unknown_impact_by_surface={
                "fixture-surface-a": [
                    {"file": FIXTURE_ENTRY, "kind": "import_meta_glob", "detail": "import.meta.glob('./**/*.ts')"}
                ],
            }
        ),
    )
    registry_path = _write_registry(tmp_path, _two_surface_registry_doc())

    result = rvi.resolve(
        changed_paths=[FIXTURE_DEPENDENCY],
        registry_path=registry_path,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )

    assert result.errors == []
    assert result.resolver_fallback_active is True
    affected = {e["surface_id"]: e["reason"] for e in result.affected_surfaces}
    assert affected == {
        "fixture-surface-a": "unknown_impact_fallback",
        "fixture-surface-b": "unknown_impact_fallback",
    }
    # Per-entry unknown_impact diagnostics are unchanged/kept.
    assert result.unknown_impact == [
        {
            "surface_id": "fixture-surface-a",
            "file": FIXTURE_ENTRY,
            "kind": "import_meta_glob",
            "detail": "import.meta.glob('./**/*.ts')",
        }
    ]


def test_no_fallback_when_neither_signal_present(monkeypatch, tmp_path):
    """GIVEN neither unsupported_resolution_settings nor any unknown_impact
    WHEN resolved THEN resolver_fallback_active is False and an unrelated
    changed path is neither affected nor swallowed."""
    monkeypatch.setattr(rvi, "run_mjs", _make_fake_run_mjs())
    registry_path = _write_registry(tmp_path, _two_surface_registry_doc())

    result = rvi.resolve(
        changed_paths=[FIXTURE_DEPENDENCY],
        registry_path=registry_path,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )

    assert result.resolver_fallback_active is False
    assert result.affected_surfaces == []
    assert FIXTURE_DEPENDENCY in result.unmapped_visual_candidates


# ---------------------------------------------------------------------------
# AC2: evaluate_pr_policy() never double-counts unmapped_visual_candidates
# as an independent blocking failure while resolver_fallback_active is
# True; it remains blocking when resolver_fallback_active is False.
# ---------------------------------------------------------------------------


def _base_policy_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        declaration_doc=None,
        registry_doc={"surfaces": {}},
        evidence_manifest=None,
        head_sha=HEAD_SHA,
        changed_paths=[FIXTURE_DEPENDENCY],
        actor="squne121",
        authorized_owners=set(),
        today=date(2026, 9, 6),
    )
    kwargs.update(overrides)
    return kwargs


def test_ac2_unmapped_visual_candidate_not_independently_blocking_when_fallback_active():
    resolve_result = rvi.ResolveResult(
        changed_paths=[FIXTURE_DEPENDENCY],
        affected_surfaces=[],
        unmapped_visual_candidates=[FIXTURE_DEPENDENCY],
        resolver_fallback_active=True,
        unsupported_resolution_settings=["tsconfig.base.json compilerOptions.paths is configured"],
    )
    policy_result = rvi.evaluate_pr_policy(resolve_result=resolve_result, **_base_policy_kwargs())
    assert policy_result["ok"] is True
    assert not any("unmapped_visual_candidate" in f for f in policy_result["failures"])


def test_ac2_unmapped_visual_candidate_still_blocks_when_fallback_not_active():
    """The 'fallback 非発火時に本当にどの surface にも mapping されない
    candidate は従来どおり blocking' half of AC2."""
    resolve_result = rvi.ResolveResult(
        changed_paths=["src/ui/debugPause.ts"],
        affected_surfaces=[],
        unmapped_visual_candidates=["src/ui/debugPause.ts"],
        resolver_fallback_active=False,
    )
    policy_result = rvi.evaluate_pr_policy(
        resolve_result=resolve_result, **_base_policy_kwargs(changed_paths=["src/ui/debugPause.ts"])
    )
    assert policy_result["ok"] is False
    assert any("unmapped_visual_candidate" in f for f in policy_result["failures"])


def test_ac1_unknown_impact_alone_never_unconditionally_fails_policy():
    """Issue #2525's central behavioral change: a non-empty
    `resolve_result.unknown_impact` must NOT, by itself, be an unconditional
    policy failure any more (previously `evaluate_pr_policy()` appended an
    `unknown_impact: ...` failure regardless of disposition/evidence)."""
    resolve_result = rvi.ResolveResult(
        changed_paths=[FIXTURE_DEPENDENCY],
        affected_surfaces=[],
        unknown_impact=[
            {"surface_id": "fixture-surface-a", "file": FIXTURE_ENTRY, "kind": "import_meta_glob", "detail": "x"}
        ],
        resolver_fallback_active=True,
    )
    policy_result = rvi.evaluate_pr_policy(resolve_result=resolve_result, **_base_policy_kwargs())
    assert policy_result["ok"] is True
    assert not any("unknown_impact" in f for f in policy_result["failures"])


# ---------------------------------------------------------------------------
# AC3: full evaluate_pr_policy() PASS/FAIL both directions for a
# fallback-triggered affected-surfaces set with real disposition + VRT
# evidence evaluation (never declaration/self-report alone).
# ---------------------------------------------------------------------------


def _fallback_resolve_result() -> "rvi.ResolveResult":
    return rvi.ResolveResult(
        changed_paths=[FIXTURE_DEPENDENCY],
        affected_surfaces=[
            {"surface_id": "fixture-surface-a", "reason": "unsupported_resolution_fallback"},
            {"surface_id": "fixture-surface-b", "reason": "unknown_impact_fallback"},
        ],
        unmapped_visual_candidates=[FIXTURE_DEPENDENCY],
        resolver_fallback_active=True,
        unsupported_resolution_settings=["tsconfig.base.json compilerOptions.paths is configured"],
    )


def _waiver_declaration_doc(
    *, tracking_issue: str | None = "123", expiry: str = "2099-01-01", reason: str = "ok"
) -> dict[str, Any]:
    waiver: dict[str, Any] = {"expiry": expiry, "reason": reason}
    if tracking_issue is not None:
        waiver["tracking_issue"] = tracking_issue
    return {
        "surfaces": [
            {"surface_id": "fixture-surface-a", "disposition": "waived", "waiver": dict(waiver)},
            {"surface_id": "fixture-surface-b", "disposition": "waived", "waiver": dict(waiver)},
        ]
    }


def test_ac3_pass_when_all_affected_surfaces_have_valid_waiver():
    registry_doc = _two_surface_registry_doc()
    policy_result = rvi.evaluate_pr_policy(
        resolve_result=_fallback_resolve_result(),
        declaration_doc=_waiver_declaration_doc(),
        registry_doc=registry_doc,
        evidence_manifest=None,
        head_sha=HEAD_SHA,
        changed_paths=[FIXTURE_DEPENDENCY],
        actor="squne121",
        authorized_owners={"@squne121"},
        today=date(2026, 9, 6),
        tracking_issue_checker=lambda _issue: True,
    )
    assert policy_result["ok"] is True, policy_result["failures"]
    assert len(policy_result["surface_results"]) == 2
    assert all(entry["ok"] for entry in policy_result["surface_results"])


def test_ac3_fail_when_declaration_missing_for_a_fallback_affected_surface():
    registry_doc = _two_surface_registry_doc()
    declaration_doc = _waiver_declaration_doc()
    declaration_doc["surfaces"] = [declaration_doc["surfaces"][0]]  # drop fixture-surface-b's entry entirely

    policy_result = rvi.evaluate_pr_policy(
        resolve_result=_fallback_resolve_result(),
        declaration_doc=declaration_doc,
        registry_doc=registry_doc,
        evidence_manifest=None,
        head_sha=HEAD_SHA,
        changed_paths=[FIXTURE_DEPENDENCY],
        actor="squne121",
        authorized_owners={"@squne121"},
        today=date(2026, 9, 6),
        tracking_issue_checker=lambda _issue: True,
    )
    assert policy_result["ok"] is False
    assert any("missing VISUAL_IMPACT_DECLARATION_V1 entry" in f for f in policy_result["failures"])


def test_ac3_fail_when_declaration_self_report_only_no_evidence_manifest():
    """declaration self-report alone (verified_unchanged, no independently
    observed evidence manifest at all) must never PASS -- the existing
    AC12 fail-closed guarantee is preserved for fallback-affected
    surfaces too."""
    registry_doc = _two_surface_registry_doc()
    declaration_doc = {
        "surfaces": [
            {"surface_id": "fixture-surface-a", "disposition": "verified_unchanged"},
            {"surface_id": "fixture-surface-b", "disposition": "verified_unchanged"},
        ]
    }
    policy_result = rvi.evaluate_pr_policy(
        resolve_result=_fallback_resolve_result(),
        declaration_doc=declaration_doc,
        registry_doc=registry_doc,
        evidence_manifest=None,
        head_sha=HEAD_SHA,
        changed_paths=[FIXTURE_DEPENDENCY],
        actor="squne121",
        authorized_owners=set(),
        today=date(2026, 9, 6),
    )
    assert policy_result["ok"] is False
    assert any("canonical_verify_lane_did_not_succeed_on_current_head" in f for f in policy_result["failures"])


def test_ac3_fail_when_vrt_evidence_reports_mismatch():
    """A full, correctly-bound evidence manifest that reports a nonzero
    pixel mismatch (a real VRT failure) must still fail the disposition,
    even though the surface only became affected via the fallback."""
    surface_def = _two_surface_registry_doc()["surfaces"]["fixture-surface-a"]
    contract_digest = rvi.compute_contract_digest(surface_def)
    manifest = {
        "schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA,
        "surfaces": [
            rvi.build_evidence_manifest_v2_record(
                surface_id="fixture-surface-a",
                contract_digest=contract_digest,
                head_sha=HEAD_SHA,
                workflow_run_id=1,
                check_run_id=None,
                check_suite_id=None,
                github_app_id=None,
                github_app_slug=None,
                check_conclusion=None,
                baseline_path="fixture-a-baseline.png",
                baseline_sha256="b" * 64,
                actual_sha256="c" * 64,
                mismatched_pixels=17,
                verify_command_id="vitest_component_vrt_verify",
                verify_succeeded=False,
                update_command_id="vitest_component_vrt_update",
                update_executed=False,
                update_succeeded=False,
                expected_artifact_id="expected-1",
                actual_artifact_id="actual-1",
                diff_artifact_id="diff-1",
            )
        ],
    }
    registry_doc = {"surfaces": {"fixture-surface-a": surface_def}}
    resolve_result = rvi.ResolveResult(
        changed_paths=[FIXTURE_DEPENDENCY],
        affected_surfaces=[{"surface_id": "fixture-surface-a", "reason": "unsupported_resolution_fallback"}],
        unmapped_visual_candidates=[FIXTURE_DEPENDENCY],
        resolver_fallback_active=True,
        unsupported_resolution_settings=["tsconfig.base.json compilerOptions.paths is configured"],
    )
    declaration_doc = {"surfaces": [{"surface_id": "fixture-surface-a", "disposition": "verified_unchanged"}]}

    policy_result = rvi.evaluate_pr_policy(
        resolve_result=resolve_result,
        declaration_doc=declaration_doc,
        registry_doc=registry_doc,
        evidence_manifest=manifest,
        head_sha=HEAD_SHA,
        changed_paths=[FIXTURE_DEPENDENCY],
        actor="squne121",
        authorized_owners=set(),
        today=date(2026, 9, 6),
        trusted_check_run_id="run-1",
        trusted_check_suite_id="suite-1",
        trusted_github_app_id="app-1",
        trusted_github_app_slug="github-actions",
        trusted_check_conclusion="success",
    )
    assert policy_result["ok"] is False
    assert any("failed" in f for f in policy_result["failures"])


def test_ac3_fail_when_waiver_invalid_missing_tracking_issue():
    registry_doc = _two_surface_registry_doc()
    declaration_doc = _waiver_declaration_doc(tracking_issue=None)
    policy_result = rvi.evaluate_pr_policy(
        resolve_result=_fallback_resolve_result(),
        declaration_doc=declaration_doc,
        registry_doc=registry_doc,
        evidence_manifest=None,
        head_sha=HEAD_SHA,
        changed_paths=[FIXTURE_DEPENDENCY],
        actor="squne121",
        authorized_owners={"@squne121"},
        today=date(2026, 9, 6),
    )
    assert policy_result["ok"] is False
    assert any("missing_tracking_issue" in f for f in policy_result["failures"])


# ---------------------------------------------------------------------------
# AC5: a genuinely broken resolver run is NEVER excused by
# resolver_fallback_active -- fail-closed pattern preserved (mirrors
# test_resolve_visual_impact.py's test_pr2045_resolver_error_fails_policy_closed).
# ---------------------------------------------------------------------------


def test_ac5_resolver_error_still_blocks_even_when_fallback_active():
    resolve_result = rvi.ResolveResult(
        changed_paths=[FIXTURE_DEPENDENCY],
        affected_surfaces=[],
        errors=["resolve_visual_impact.mjs exited 1: ['boom']"],
        resolver_fallback_active=True,
        unsupported_resolution_settings=["tsconfig.base.json compilerOptions.paths is configured"],
    )
    policy_result = rvi.evaluate_pr_policy(resolve_result=resolve_result, **_base_policy_kwargs())
    assert policy_result["ok"] is False
    assert any("resolver_error" in f for f in policy_result["failures"])


def test_ac5_mjs_crash_is_a_resolver_error_never_a_fallback_success(monkeypatch, tmp_path):
    """Node subprocess start-up failure (bounded here by monkeypatching
    run_mjs to raise, exactly as a real crashed/timed-out subprocess would
    surface through run_mjs()) must still populate `result.errors` and must
    NOT set resolver_fallback_active True on its own."""

    def _raising_run_mjs(mjs_path, request, node_bin="node", timeout_seconds=60):
        del mjs_path, request, node_bin, timeout_seconds
        raise rvi.RegistryError("resolve_visual_impact.mjs exceeded wall-clock timeout (60s)")

    monkeypatch.setattr(rvi, "run_mjs", _raising_run_mjs)
    registry_path = _write_registry(tmp_path, _two_surface_registry_doc())

    result = rvi.resolve(
        changed_paths=[FIXTURE_DEPENDENCY],
        registry_path=registry_path,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    assert result.errors
    assert any("timeout" in e for e in result.errors)
    assert result.resolver_fallback_active is False

    policy_result = rvi.evaluate_pr_policy(resolve_result=result, **_base_policy_kwargs())
    assert policy_result["ok"] is False
    assert any("resolver_error" in f for f in policy_result["failures"])


def test_ac5_invalid_head_registry_is_resolver_error_and_never_fallback_success(tmp_path):
    """A malformed registry document fails BEFORE resolve() ever reaches
    run_mjs() (schema/version-mismatch-adjacent case) -- must be a
    resolver error, never an all-surfaces-affected fallback success."""
    registry_path = tmp_path / "invalid_registry.yml"
    registry_path.write_text("surfaces: [this, is, not, a, mapping]\nschema_version: 1\n", encoding="utf-8")

    result = rvi.resolve(
        changed_paths=[FIXTURE_DEPENDENCY],
        registry_path=registry_path,
        schema_path=SCHEMA_PATH,
        mjs_path=MJS_PATH,
        repo_root=REPO_ROOT,
    )
    assert result.errors
    assert any("head registry invalid" in e for e in result.errors)
    assert result.resolver_fallback_active is False
    assert result.affected_surfaces == []


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ac5_mjs_schema_mismatch_is_a_resolver_error_never_a_fallback_success(monkeypatch):
    """AC5: `run_mjs()`'s OWN schema/version-mismatch validation (never
    resolve()'s handling of an already-raised exception -- see the
    `run_mjs`-monkeypatch tests above for that) must reject a resolver
    output with the wrong `schema`, even if that malformed output ALSO
    happens to carry an `unsupported_resolution_settings` field -- a
    mismatched envelope schema is never converted into an
    all-registered-surfaces-affected fallback success. subprocess.run is
    monkeypatched (never spawns Node) so this stays fully Node-independent
    while still exercising the real production validation code path."""

    def _fake_subprocess_run(cmd, input=None, capture_output=None, text=None, check=None, timeout=None):
        del cmd, input, capture_output, text, check, timeout
        payload = {
            "schema": "SOME_OTHER_SCHEMA_V1",
            "resolver_version": "1",
            "surfaces": {},
            "errors": [],
            "unsupported_resolution_settings": ["should never reach fallback"],
        }
        return _FakeCompletedProcess(0, json.dumps(payload))

    monkeypatch.setattr(rvi.subprocess, "run", _fake_subprocess_run)

    with pytest.raises(rvi.RegistryError, match="unexpected schema"):
        rvi.run_mjs(MJS_PATH, {"repo_root": ".", "surfaces": {}})


def test_ac5_mjs_malformed_json_output_is_a_resolver_error(monkeypatch):
    """AC5: malformed/incomplete resolver stdout (not valid JSON at all)
    must raise a resolver error from `run_mjs()` itself, never be silently
    treated as an empty/fallback-successful result."""

    def _fake_subprocess_run(cmd, input=None, capture_output=None, text=None, check=None, timeout=None):
        del cmd, input, capture_output, text, check, timeout
        return _FakeCompletedProcess(1, "{not valid json")

    monkeypatch.setattr(rvi.subprocess, "run", _fake_subprocess_run)

    with pytest.raises(rvi.RegistryError, match="invalid JSON"):
        rvi.run_mjs(MJS_PATH, {"repo_root": ".", "surfaces": {}})


def test_ac5_mjs_incomplete_per_surface_shape_is_rejected_fail_closed(monkeypatch):
    """AC5 fix_delta (OWNER REQUEST_CHANGES on PR #2548, 2026-09-06): a
    *successful* (right schema/version/surface-key-set/exit-code 0) result
    whose per-surface payload is missing `reachable_files`/`unknown_impact`
    entirely (e.g. `{"surfaces": {"fixture": {}}}`) must never be silently
    converted into "fully resolved, zero impact" fallback success -- it must
    raise the same RegistryError fail-closed path as any other malformed
    resolver output. subprocess.run is monkeypatched (never spawns Node) so
    this stays fully Node-independent while still exercising the real
    production validation code path (`_validate_mjs_success_result`)."""

    def _fake_subprocess_run(cmd, input=None, capture_output=None, text=None, check=None, timeout=None):
        del cmd, input, capture_output, text, check, timeout
        payload = {
            "schema": "RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1",
            "resolver_version": "1",
            "surfaces": {"fixture": {}},
            "errors": [],
            "unsupported_resolution_settings": ["alias configured"],
        }
        return _FakeCompletedProcess(0, json.dumps(payload))

    monkeypatch.setattr(rvi.subprocess, "run", _fake_subprocess_run)

    with pytest.raises(rvi.RegistryError, match="reachable_files"):
        rvi.run_mjs(MJS_PATH, {"repo_root": ".", "surfaces": {"fixture": {}}})


def test_ac5_mjs_unknown_impact_entry_missing_required_field_is_rejected_fail_closed(monkeypatch):
    """AC5 fix_delta: a per-surface `unknown_impact` entry missing one of
    its required string fields (file/kind/detail) must also be rejected
    fail-closed, not silently passed through with a defaulted/missing
    value."""

    def _fake_subprocess_run(cmd, input=None, capture_output=None, text=None, check=None, timeout=None):
        del cmd, input, capture_output, text, check, timeout
        payload = {
            "schema": "RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1",
            "resolver_version": "1",
            "surfaces": {
                "fixture": {
                    "reachable_files": [],
                    "unknown_impact": [{"file": "entry.ts", "kind": "import_meta_glob"}],  # missing "detail"
                }
            },
            "errors": [],
            "unsupported_resolution_settings": [],
        }
        return _FakeCompletedProcess(0, json.dumps(payload))

    monkeypatch.setattr(rvi.subprocess, "run", _fake_subprocess_run)

    with pytest.raises(rvi.RegistryError, match="unknown_impact entry"):
        rvi.run_mjs(MJS_PATH, {"repo_root": ".", "surfaces": {"fixture": {}}})

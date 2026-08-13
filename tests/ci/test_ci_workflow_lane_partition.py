"""
tests/ci/test_ci_workflow_lane_partition.py

Issue #2119 AC5/AC6/AC11/AC14: DAG topology, aggregate-job three-mode
failure differentiation, existing consumer authority, and the lane
selector's exclusive enum contract.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import types

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_VERDICT_SCRIPT = REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts" / "ci_verdict_summary_v2.py"
PLAYWRIGHT_BIN = REPO_ROOT / "node_modules" / ".bin" / "playwright"
PW_CONFIG = REPO_ROOT / "playwright.config.ts"


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_e2e_core_and_e2e_responsive_matrix_have_no_dag_cross_dependency():
    doc = _load_workflow()
    jobs = doc["jobs"]
    assert "e2e-core" in jobs, "static topology failure: jobs.e2e-core missing"
    assert "e2e-responsive-matrix" in jobs, "static topology failure: jobs.e2e-responsive-matrix missing"

    core_needs = jobs["e2e-core"].get("needs")
    responsive_needs = jobs["e2e-responsive-matrix"].get("needs")

    def _as_set(needs) -> set[str]:
        if needs is None:
            return set()
        if isinstance(needs, str):
            return {needs}
        return set(needs)

    assert "e2e-responsive-matrix" not in _as_set(core_needs)
    assert "e2e-core" not in _as_set(responsive_needs)


def test_aggregate_e2e_uses_needs_and_if_always_and_distinguishes_three_failure_modes():
    doc = _load_workflow()
    jobs = doc["jobs"]
    assert "e2e" in jobs, "static topology failure: jobs.e2e (aggregate) missing"
    aggregate = jobs["e2e"]

    needs = aggregate.get("needs")
    assert isinstance(needs, list), "jobs.e2e.needs must be a list"
    assert set(needs) == {"e2e-core", "e2e-responsive-matrix"}
    assert aggregate.get("if") == "always()"

    steps_text = json.dumps(aggregate.get("steps", []))
    # (b) runtime result failure: failure/cancelled/skipped are each
    # distinguished (not collapsed into one generic branch).
    for token in ("failure", "cancelled", "skipped"):
        assert f'"{token}"' in steps_text or f"'{token}'" in steps_text or token in steps_text, (
            f"aggregate e2e must distinguish runtime result '{token}'"
        )
    assert "needs.e2e-core.result" in steps_text
    assert "needs.e2e-responsive-matrix.result" in steps_text

    # (c) runtime evidence failure: the aggregate must not degrade to
    # success purely on `result == success` without any evidence binding —
    # each provider's own if-no-files-found: error step is the enforcement
    # mechanism; the aggregate step's comment/logic must reference it so a
    # future edit cannot silently drop that binding without touching this
    # job's own text.
    assert "if-no-files-found: error" in steps_text or "runtime evidence" in steps_text


def _load_ci_verdict_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("ci_verdict_summary_v2", CI_VERDICT_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_existing_visual_and_ci_verdict_consumers_treat_aggregate_e2e_as_authoritative():
    doc = _load_workflow()
    jobs = doc["jobs"]
    needs = jobs["ci-verdict-summary"]["needs"]
    assert "e2e" in needs, "ci-verdict-summary must still depend on the stable aggregate check name 'e2e'"
    assert "e2e-core" not in needs and "e2e-responsive-matrix" not in needs, (
        "ci-verdict-summary must reference the stable aggregate 'e2e', not the provider jobs directly"
    )

    v2 = _load_ci_verdict_module()
    assert v2.get_classification("ci", "e2e") in {"required", "evidence"}


def test_lane_selector_enum_rejects_invalid_multi_lane_combination():
    if not PLAYWRIGHT_BIN.is_file():
        pytest.skip("playwright binary not installed under node_modules/.bin — run `pnpm install` first")

    def _list(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
        import os

        env = dict(os.environ)
        env.update(env_overrides)
        env["CI"] = "true"
        return subprocess.run(
            [str(PLAYWRIGHT_BIN), "test", "--list", "--reporter=json", f"--config={PW_CONFIG}"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    ok = _list({"LOOP_E2E_LANE": "core"})
    assert ok.returncode == 0, f"LOOP_E2E_LANE=core must succeed: {ok.stderr}"

    multi = _list({"LOOP_E2E_LANE": "core,responsive"})
    assert multi.returncode != 0, "LOOP_E2E_LANE=core,responsive (multi-lane) must be rejected fail-closed"
    assert "LOOP_E2E_LANE" in multi.stderr

    unknown = _list({"LOOP_E2E_LANE": "bogus-lane"})
    assert unknown.returncode != 0, "LOOP_E2E_LANE=bogus-lane (unknown) must be rejected fail-closed"
    assert "LOOP_E2E_LANE" in unknown.stderr

    # AC8/AC14 fix_delta (PR #2137 review, iteration 1): `e2e-core` owns
    # preview-namespace-exactly-once, so `LOOP_E2E_LANE=core` combined with
    # `LOOP_E2E_PREVIEW_NAMESPACE_LANE=true` is a LEGITIMATE combination
    # (the e2e-core CI job runs both the standard core suite and, in a
    # later step, the dedicated preview-namespace spec under the same
    # job-level LOOP_E2E_LANE=core env var) and must NOT be rejected.
    # The preview-namespace spec itself requires LOOP_EXPECTED_STORAGE_KEY to
    # be set at module scope (unrelated to the LOOP_E2E_LANE selector under
    # test here) -- provide a valid non-production value so a --list
    # collection failure there can never be misread as a lane-selector
    # rejection.
    core_with_preview_namespace_flag = _list(
        {
            "LOOP_E2E_LANE": "core",
            "LOOP_E2E_PREVIEW_NAMESPACE_LANE": "true",
            "LOOP_EXPECTED_STORAGE_KEY": "loop-protocol.preview.pr-0.mvp.save",
        }
    )
    assert core_with_preview_namespace_flag.returncode == 0, (
        "LOOP_E2E_LANE=core + LOOP_E2E_PREVIEW_NAMESPACE_LANE=true must be "
        f"accepted (e2e-core owns preview-namespace-exactly-once): "
        f"{core_with_preview_namespace_flag.stderr}"
    )

    # `responsive` has its own dedicated, mutually exclusive spec selection,
    # so combining it with the preview-namespace flag remains a genuine,
    # fail-closed-rejected inconsistency.
    responsive_with_preview_namespace_flag = _list(
        {"LOOP_E2E_LANE": "responsive", "LOOP_E2E_PREVIEW_NAMESPACE_LANE": "true"}
    )
    assert responsive_with_preview_namespace_flag.returncode != 0, (
        "LOOP_E2E_LANE=responsive + LOOP_E2E_PREVIEW_NAMESPACE_LANE=true "
        "must be rejected fail-closed"
    )
    assert "LOOP_E2E_LANE" in responsive_with_preview_namespace_flag.stderr

"""scripts/ci/tests/test_dispatch_close_evidence_publication_smoke_v1.py

Issue #2555 AC8: `uv run pytest` wrapper for the bounded runtime-verification
smoke run (`scripts/ci/dispatch_close_evidence_publication_smoke_v1.py`).
The VC preflight allowlist does not permit an arbitrary
`uv run python3 <script>` invocation shape, so this pytest wrapper is the
canonical VC entry point (`uv run pytest
scripts/ci/tests/test_dispatch_close_evidence_publication_smoke_v1.py -v`).

When `GH_TOKEN`/`GITHUB_TOKEN` lacks `actions: read` scope, or no existing
non-expired Reliability close-grade artifact ID can be resolved (or the
current worktree's HEAD is not yet pushed to origin), the underlying module
raises `SkipCondition` -- this wrapper maps that to `pytest.skip()` (SKIP,
never a false PASS or a false FAIL; `docs/dev/runtime-verification-policy.md`
SKIP contract).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SMOKE_MODULE_PATH = REPO_ROOT / "scripts" / "ci" / "dispatch_close_evidence_publication_smoke_v1.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "ci_dispatch_close_evidence_publication_smoke_v1_under_test", SMOKE_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


smoke = _load_smoke_module()


def test_module_defines_bounded_retry_constants_never_unbounded_loops():
    """GIVEN the smoke module WHEN inspected THEN every poll/search loop has
    a finite, explicit bound (never an unbounded `while True`)."""
    assert smoke.RUN_POLL_MAX_ATTEMPTS > 0
    assert smoke.RUN_DISCOVERY_MAX_ATTEMPTS > 0
    assert smoke.ARTIFACT_SEARCH_PAGES > 0
    assert smoke.ARTIFACT_SEARCH_PER_PAGE > 0


def test_exit_codes_match_runtime_verification_policy_contract():
    """GIVEN the module's exit code constants WHEN compared to
    docs/dev/runtime-verification-policy.md's SKIP contract THEN PASS=0,
    FAIL=1, SKIP=77 (SKIP is never conflated with PASS)."""
    assert smoke.EXIT_PASS == 0
    assert smoke.EXIT_FAIL == 1
    assert smoke.EXIT_SKIP == 77


def test_target_job_and_artifact_names_match_ci_yml_contract():
    """GIVEN the smoke module's target job/artifact name constants WHEN
    compared to the `close-evidence-publication` job's own AC5/AC7 upload
    step names in .github/workflows/ci.yml THEN they are byte-identical
    (never a drifted duplicate string)."""
    ci_yml_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "if: ${{ github.event.inputs.close_evidence_source_artifact_id != '' }}" in ci_yml_text
    assert f"name: {smoke.ARTIFACT_A_NAME}" in ci_yml_text
    assert f"name: {smoke.ARTIFACT_B_NAME}" in ci_yml_text
    assert smoke.JOB_NAME == "close-evidence-publication"
    assert f"  {smoke.JOB_NAME}:" in ci_yml_text


def test_run_smoke_end_to_end_pass_fail_or_skip():
    """GIVEN the real repository/environment WHEN run_smoke() executes THEN
    it either (a) raises SkipCondition (mapped to pytest.skip() below) when
    a precondition -- GH token actions:read scope, a resolvable existing
    Reliability artifact, or a pushed matching ref -- cannot be resolved, or
    (b) returns a SmokeResult with status in {"pass", "fail"} after actually
    dispatching and polling the real `close-evidence-publication` job
    end-to-end (never a fabricated/simulated result)."""
    try:
        result = smoke.run_smoke()
    except smoke.SkipCondition as exc:
        pytest.skip(f"close_evidence_publication_smoke SKIP: {exc}")
        return

    assert result.status in ("pass", "fail")
    if result.status == "fail":
        pytest.fail(
            f"close_evidence_publication_smoke FAIL: {result.reason} "
            f"(run_id={result.run_id} run_url={result.run_url})"
        )
    # status == "pass": AC8 evidence -- record for PR body attachment.
    assert result.run_id is not None
    assert result.artifact_a_id is not None
    assert result.artifact_b_id is not None
    print(
        "close_evidence_publication_smoke PASS: "
        f"run_id={result.run_id} run_url={result.run_url} "
        f"artifact_a_id={result.artifact_a_id} artifact_b_id={result.artifact_b_id}"
    )

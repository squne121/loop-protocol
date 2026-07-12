"""Contract tests for `.github/workflows/deploy-pages.yml` pnpm bootstrap (Issue #1476).

These tests assert that `package.json#packageManager` is the single source of truth
for the pnpm version consumed by both the `deploy-main` and `deploy-pr` jobs, that
both jobs pin the same `pnpm/action-setup` commit SHA without a duplicate `version:`
input, and that both jobs assert the actually-resolved `pnpm --version` matches the
pinned version before running `pnpm install --frozen-lockfile` -- with no error
suppression anywhere in the bootstrap chain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy-pages.yml"
PACKAGE_JSON_PATH = REPO_ROOT / "package.json"

EXPECTED_PNPM_VERSION = "11.7.0"
EXPECTED_PACKAGE_MANAGER = f"pnpm@{EXPECTED_PNPM_VERSION}"
EXPECTED_ACTION_SETUP_USES = "pnpm/action-setup@0e279bb959325dab635dd2c09392533439d90093"
DEPLOY_JOBS = ("deploy-main", "deploy-pr")
INSTALL_RUN = "pnpm install --frozen-lockfile"


def _load_workflow() -> dict[str, Any]:
    with WORKFLOW_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_package_json() -> dict[str, Any]:
    return json.loads(PACKAGE_JSON_PATH.read_text(encoding="utf-8"))


def _job_steps(workflow: dict[str, Any], job_name: str) -> list[dict[str, Any]]:
    return workflow["jobs"][job_name]["steps"]


def _uses_action_setup(step: dict[str, Any]) -> bool:
    return str(step.get("uses", "")).startswith("pnpm/action-setup@")


def _step_has_error_suppression(step: dict[str, Any]) -> bool:
    run = str(step.get("run", ""))
    if "|| true" in run or "|| :" in run:
        return True
    if step.get("continue-on-error") is True:
        return True
    return False


def test_deploy_jobs_require_package_manager_pinned_bootstrap():
    """AC1: package.json#packageManager must be the exact, unversioned pnpm pin."""
    package_json = _load_package_json()
    package_manager = package_json.get("packageManager")
    assert package_manager == EXPECTED_PACKAGE_MANAGER, (
        f"package.json#packageManager must be exactly {EXPECTED_PACKAGE_MANAGER!r} "
        f"(no range/latest/wildcard), got {package_manager!r}"
    )


@pytest.mark.parametrize("job_name", DEPLOY_JOBS)
def test_action_setup_sha_is_pinned_without_duplicate_version_input(job_name: str):
    """AC2: both jobs use the pinned upstream SHA and do not redundantly set version:."""
    workflow = _load_workflow()
    steps = _job_steps(workflow, job_name)
    action_setup_steps = [s for s in steps if _uses_action_setup(s)]
    assert len(action_setup_steps) == 1, f"{job_name}: expected exactly one pnpm/action-setup step"

    step = action_setup_steps[0]
    uses = str(step["uses"])
    uses_ref = uses.split(" ", 1)[0]
    assert uses_ref == EXPECTED_ACTION_SETUP_USES, (
        f"{job_name}: pnpm/action-setup must pin {EXPECTED_ACTION_SETUP_USES}, got {uses_ref!r}"
    )

    with_block = step.get("with") or {}
    assert "version" not in with_block, (
        f"{job_name}: pnpm/action-setup must not declare a version: input; "
        "package.json#packageManager is the single source of truth"
    )


@pytest.mark.parametrize("job_name", DEPLOY_JOBS)
def test_pnpm_version_is_asserted_before_frozen_install_with_no_suppression(job_name: str):
    """AC3: pnpm --version is asserted == 11.7.0 before install, with no error suppression."""
    workflow = _load_workflow()
    steps = _job_steps(workflow, job_name)

    action_setup_index = next(
        (i for i, s in enumerate(steps) if _uses_action_setup(s)),
        None,
    )
    assert action_setup_index is not None, f"{job_name}: missing pnpm/action-setup step"

    install_index = next(
        (i for i, s in enumerate(steps) if str(s.get("run", "")).strip() == INSTALL_RUN),
        None,
    )
    assert install_index is not None, f"{job_name}: missing `{INSTALL_RUN}` step"

    version_assert_index = None
    version_assert_step = None
    for i, s in enumerate(steps):
        run = str(s.get("run", ""))
        if "pnpm --version" in run:
            version_assert_index = i
            version_assert_step = s
            break

    assert version_assert_index is not None, (
        f"{job_name}: missing a `pnpm --version` assertion step before `{INSTALL_RUN}`"
    )
    run = str(version_assert_step.get("run", ""))
    assert EXPECTED_PNPM_VERSION in run, (
        f"{job_name}: pnpm version assertion step must check for exact "
        f"{EXPECTED_PNPM_VERSION}, got run body without that literal: {run!r}"
    )
    assert not _step_has_error_suppression(version_assert_step), (
        f"{job_name}: pnpm --version assertion step must not suppress errors"
    )
    assert action_setup_index < version_assert_index < install_index, (
        f"{job_name}: bootstrap order must be action-setup -> version assertion -> "
        f"install (got action_setup={action_setup_index}, "
        f"version_assert={version_assert_index}, install={install_index})"
    )


def test_deploy_main_and_deploy_pr_bootstrap_steps_are_semantically_identical():
    """AC4: deploy-main / deploy-pr bootstrap (setup, version assertion, install) must match."""
    workflow = _load_workflow()

    def bootstrap_signature(job_name: str) -> dict[str, Any]:
        steps = _job_steps(workflow, job_name)
        action_setup = next(s for s in steps if _uses_action_setup(s))
        version_assert = next(s for s in steps if "pnpm --version" in str(s.get("run", "")))
        install = next(s for s in steps if str(s.get("run", "")).strip() == INSTALL_RUN)
        return {
            "action_setup_uses": action_setup["uses"],
            "action_setup_with": action_setup.get("with") or {},
            "version_assert_run": str(version_assert.get("run", "")).strip(),
            "install_run": str(install.get("run", "")).strip(),
        }

    main_sig = bootstrap_signature("deploy-main")
    pr_sig = bootstrap_signature("deploy-pr")
    assert main_sig == pr_sig, (
        "deploy-main and deploy-pr must run semantically identical pnpm bootstrap "
        f"steps (setup, version assertion, install); got main={main_sig} pr={pr_sig}"
    )


def test_no_error_suppression_anywhere_in_bootstrap_or_install_steps():
    """AC4: none of the bootstrap-related steps (setup/version-assert/install) suppress errors."""
    workflow = _load_workflow()
    for job_name in DEPLOY_JOBS:
        steps = _job_steps(workflow, job_name)
        for step in steps:
            run = str(step.get("run", ""))
            is_bootstrap_related = (
                _uses_action_setup(step)
                or "pnpm --version" in run
                or run.strip() == INSTALL_RUN
            )
            if not is_bootstrap_related:
                continue
            assert not _step_has_error_suppression(step), (
                f"{job_name}: bootstrap-related step must not suppress errors: {step!r}"
            )

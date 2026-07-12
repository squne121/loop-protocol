"""Contract tests for `.github/workflows/deploy-pages.yml` pnpm bootstrap (Issue #1476).

These tests assert that `package.json#packageManager` is the single source of truth
for the pnpm version consumed by every `pnpm/action-setup` consumer under
`.github/**/*.yml`, that `deploy-main` / `deploy-pr` pin the same `pnpm/action-setup`
commit SHA without a duplicate `version:` input, and that both jobs assert the
actually-resolved `pnpm --version` (derived from `package.json#packageManager` at
runtime) matches the pinned version before running `pnpm install --frozen-lockfile`
-- with no error suppression anywhere in the bootstrap chain.

This module doubles as a standalone CLI (`python3 test_deploy_pages_pnpm_setup.py
--root <dir>`) so that the pnpm-bootstrap contract can be evaluated against an
arbitrary root (e.g. a pre-fix fixture written to a pytest tmp_path) and produce a
deterministic non-zero exit code with a stable, machine-readable failure key -- as
opposed to a pytest collection error, which Issue #1476 explicitly disallows as a
baseline-red substitute.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
THIS_FILE = Path(__file__).resolve()
WORKFLOW_RELPATH = Path(".github") / "workflows" / "deploy-pages.yml"
PACKAGE_JSON_RELPATH = Path("package.json")

EXPECTED_PNPM_VERSION = "11.7.0"
EXPECTED_PACKAGE_MANAGER = f"pnpm@{EXPECTED_PNPM_VERSION}"
EXPECTED_ACTION_SETUP_USES = "pnpm/action-setup@0e279bb959325dab635dd2c09392533439d90093"
DEPLOY_JOBS = ("deploy-main", "deploy-pr")
INSTALL_RUN = "pnpm install --frozen-lockfile"

# Whitelist of files allowed to consume `pnpm/action-setup`. A new, unreviewed
# consumer added anywhere under `.github/**/*.yml` must fail this contract until
# it is deliberately added here (Issue #1476 P1: Scope Delta consumer coverage).
ALLOWED_PNPM_ACTION_SETUP_CONSUMERS = frozenset(
    {
        ".github/workflows/deploy-pages.yml",
        ".github/workflows/session-manifest.yml",
        ".github/workflows/agent-retro-index.yml",
        ".github/actions/setup-node-pnpm/action.yml",
    }
)

_PNPM_COMMAND_RE = re.compile(r"\bpnpm\s+(install|exec|run|--version)\b")


# ---------------------------------------------------------------------------
# Root-parameterized loaders (support fixture roots, not just REPO_ROOT)
# ---------------------------------------------------------------------------


def _load_workflow(root: Path = REPO_ROOT) -> dict[str, Any]:
    path = root / WORKFLOW_RELPATH
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_package_json(root: Path = REPO_ROOT) -> dict[str, Any]:
    path = root / PACKAGE_JSON_RELPATH
    return json.loads(path.read_text(encoding="utf-8"))


def _job_steps(workflow: dict[str, Any], job_name: str) -> list[dict[str, Any]]:
    return workflow["jobs"][job_name]["steps"]


def _uses_action_setup(step: dict[str, Any]) -> bool:
    return str(step.get("uses", "")).startswith("pnpm/action-setup@")


def _step_has_error_suppression(step: dict[str, Any]) -> bool:
    run = str(step.get("run", ""))
    if "|| true" in run or "|| :" in run:
        return True
    # `set +e` (possibly combined with other flags, e.g. `set -eo pipefail; set +e`)
    if re.search(r"(?m)^\s*set\s+(?:[a-zA-Z]*\s+)*\+e\b", run):
        return True
    continue_on_error = step.get("continue-on-error")
    # Any non-absent, non-`False` value (boolean true, or a `${{ ... }}`
    # expression string that could evaluate truthy) counts as suppression.
    if continue_on_error is not None and continue_on_error is not False:
        return True
    return False


def _extract_version_assert_step(job_name: str, root: Path = REPO_ROOT) -> dict[str, Any]:
    workflow = _load_workflow(root)
    steps = _job_steps(workflow, job_name)
    return next(s for s in steps if "pnpm --version" in str(s.get("run", "")))


# ---------------------------------------------------------------------------
# Baseline-red CLI: pure violation collection, independent of pytest collection
# ---------------------------------------------------------------------------


def _collect_violations(root: Path) -> list[str]:
    """Evaluate the pnpm bootstrap contract against `root` and return a list of
    stable, machine-readable failure keys. Empty list == contract satisfied.
    """
    violations: list[str] = []

    package_json_path = root / PACKAGE_JSON_RELPATH
    if not package_json_path.exists():
        violations.append("package_json_missing")
        package_manager = None
    else:
        try:
            package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            violations.append("package_json_invalid_json")
            package_json = {}
        package_manager = package_json.get("packageManager")

    if package_manager != EXPECTED_PACKAGE_MANAGER:
        violations.append("non_exact_pnpm_version")

    workflow_path = root / WORKFLOW_RELPATH
    if not workflow_path.exists():
        violations.append("deploy_pages_workflow_missing")
        return violations

    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = (workflow or {}).get("jobs") or {}
    for job_name in DEPLOY_JOBS:
        job = jobs.get(job_name)
        if job is None:
            violations.append(f"missing_job:{job_name}")
            continue
        steps = job.get("steps") or []
        action_setup_steps = [s for s in steps if _uses_action_setup(s)]
        if len(action_setup_steps) != 1:
            violations.append(f"missing_action_setup:{job_name}")
        else:
            with_block = action_setup_steps[0].get("with") or {}
            if "version" in with_block:
                violations.append(f"duplicate_version_input:{job_name}")
        version_assert = next(
            (s for s in steps if "pnpm --version" in str(s.get("run", ""))), None
        )
        if version_assert is None:
            violations.append(f"missing_version_assertion:{job_name}")

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the deploy-pages pnpm bootstrap contract against --root."
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    violations = _collect_violations(args.root)
    if violations:
        print(f"FAIL: {','.join(violations)}")
        return 1
    print("OK: pnpm bootstrap contract satisfied")
    return 0


# ---------------------------------------------------------------------------
# Pre-fix fixtures (embedded, not on-disk, so no new file outside Allowed Paths
# is required) -- AC4 baseline-red evidence, Issue #1476 P0-1.
# ---------------------------------------------------------------------------

PRE_FIX_PACKAGE_JSON = json.dumps(
    {
        "name": "loop-protocol",
        "private": True,
        "version": "0.0.0",
        "type": "module",
    },
    indent=2,
)

PRE_FIX_DEPLOY_PAGES_YML = """\
name: deploy-pages
on:
  push:
    branches: [main]
  pull_request:
    types: [opened, synchronize, reopened, closed]
jobs:
  deploy-main:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6
      - uses: pnpm/action-setup@0e279bb959325dab635dd2c09392533439d90093 # v6
        with:
          version: 11
      - run: pnpm install --frozen-lockfile
  deploy-pr:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6
      - uses: pnpm/action-setup@0e279bb959325dab635dd2c09392533439d90093 # v6
        with:
          version: 11
      - run: pnpm install --frozen-lockfile
"""


def test_pre_fix_fixture_reports_non_exact_pnpm_version_as_assertion_failure():
    """P0-1: a pre-fix root (no packageManager, `version:` input still present,
    no version-assertion step) must produce exit code 1 with a stable
    `non_exact_pnpm_version` failure key via the CLI entry point -- never a
    pytest collection error (exit code 4 / 0 collected items)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / "package.json").write_text(PRE_FIX_PACKAGE_JSON, encoding="utf-8")
        (root / ".github" / "workflows" / "deploy-pages.yml").write_text(
            PRE_FIX_DEPLOY_PAGES_YML, encoding="utf-8"
        )

        result = subprocess.run(
            [sys.executable, str(THIS_FILE), "--root", str(root)],
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 1, (
        f"pre-fix fixture must fail with exit code 1 (assertion failure), "
        f"got {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "non_exact_pnpm_version" in result.stdout, (
        f"pre-fix fixture failure output must contain the stable failure key "
        f"'non_exact_pnpm_version', got stdout={result.stdout!r}"
    )
    # Also verify the fixture triggers the duplicate-version-input and
    # missing-assertion violations, proving the fixture models the pre-fix
    # workflow shape (not just an empty/missing-file collection error).
    assert "duplicate_version_input:deploy-main" in result.stdout
    assert "missing_version_assertion:deploy-main" in result.stdout


def test_post_fix_current_repo_has_no_violations():
    """Sanity check: the CLI reports zero violations against the current
    (post-fix) repository root."""
    violations = _collect_violations(REPO_ROOT)
    assert violations == [], violations


# ---------------------------------------------------------------------------
# AC1-AC5 fine-grained contract tests (current repo state)
# ---------------------------------------------------------------------------


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
    """AC3: pnpm --version is asserted against package.json#packageManager before
    install, with no error suppression. The expected version is derived at
    runtime from package.json (SSOT) rather than hardcoded in the workflow
    (Issue #1476 P1: only the contract test pins the literal 11.7.0)."""
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
    assert 'require("./package.json").packageManager' in run, (
        f"{job_name}: pnpm version assertion step must derive the expected version "
        f"from package.json#packageManager at runtime (single source of truth), "
        f"got run body without that derivation: {run!r}"
    )
    assert EXPECTED_PNPM_VERSION not in run, (
        f"{job_name}: pnpm version assertion step must NOT hardcode the literal "
        f"pnpm version {EXPECTED_PNPM_VERSION!r} in the workflow; only "
        "package.json#packageManager and this contract test may pin that literal"
    )
    assert '"$actual_pnpm_version" != "$expected_pnpm_version"' in run, (
        f"{job_name}: pnpm version assertion step must compare the runtime-derived "
        f"expected version against the actually-resolved `pnpm --version`, got: {run!r}"
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


# ---------------------------------------------------------------------------
# P1-2: repository-wide pnpm/action-setup consumer coverage
# ---------------------------------------------------------------------------


def _iter_yaml_files(root: Path) -> list[Path]:
    github_dir = root / ".github"
    if not github_dir.is_dir():
        return []
    files = list(github_dir.rglob("*.yml")) + list(github_dir.rglob("*.yaml"))
    return sorted(files)


def _is_composite_action(doc: dict[str, Any]) -> bool:
    runs = doc.get("runs")
    return isinstance(runs, dict) and runs.get("using") == "composite"


def _iter_all_steps(doc: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    result: list[tuple[str | None, dict[str, Any]]] = []
    if _is_composite_action(doc):
        for step in (doc.get("runs") or {}).get("steps") or []:
            result.append((None, step))
    else:
        jobs = doc.get("jobs") or {}
        for job_name, job in jobs.items():
            for step in job.get("steps") or []:
                result.append((job_name, step))
    return result


def test_all_pnpm_action_setup_consumers_are_whitelisted():
    """P1-2: every `pnpm/action-setup` consumer under `.github/**/*.yml` must be a
    reviewed, whitelisted file. An unreviewed new consumer fails this test."""
    found_consumers: set[str] = set()
    for yaml_path in _iter_yaml_files(REPO_ROOT):
        rel = yaml_path.relative_to(REPO_ROOT).as_posix()
        text = yaml_path.read_text(encoding="utf-8")
        if "pnpm/action-setup@" not in text and "pnpm/action-setup@v" not in text:
            continue
        found_consumers.add(rel)
        assert rel in ALLOWED_PNPM_ACTION_SETUP_CONSUMERS, (
            f"unreviewed new pnpm/action-setup consumer detected: {rel}; "
            "review it against the package.json#packageManager single-source-of-truth "
            "contract and add it to ALLOWED_PNPM_ACTION_SETUP_CONSUMERS explicitly"
        )
    # Every whitelisted consumer must actually still be found (no stale entries
    # silently masking a removed/renamed consumer).
    missing = ALLOWED_PNPM_ACTION_SETUP_CONSUMERS - found_consumers
    assert not missing, f"whitelisted pnpm/action-setup consumers no longer found: {missing}"


def test_no_pnpm_action_setup_consumer_declares_version_input():
    """P1-2: no `pnpm/action-setup` step anywhere under `.github/**/*.yml` may
    declare a `version:` input; package.json#packageManager is the sole source."""
    for yaml_path in _iter_yaml_files(REPO_ROOT):
        rel = yaml_path.relative_to(REPO_ROOT).as_posix()
        if rel not in ALLOWED_PNPM_ACTION_SETUP_CONSUMERS:
            continue
        doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        for job_name, step in _iter_all_steps(doc):
            if not _uses_action_setup(step):
                continue
            with_block = step.get("with") or {}
            assert "version" not in with_block, (
                f"{rel} ({job_name or 'composite action'}): pnpm/action-setup must not "
                "declare a version: input"
            )


def test_composite_action_does_not_reintroduce_pnpm_version_input():
    """P1-2: the setup-node-pnpm composite action must not reintroduce a
    `pnpm-version` input."""
    action_path = REPO_ROOT / ".github" / "actions" / "setup-node-pnpm" / "action.yml"
    doc = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    inputs = doc.get("inputs") or {}
    assert "pnpm-version" not in inputs, (
        "setup-node-pnpm/action.yml must not reintroduce a pnpm-version input; "
        "package.json#packageManager is the single source of truth"
    )


def test_pnpm_setup_precedes_pnpm_commands_in_every_whitelisted_job():
    """P1-2: for every whitelisted, non-composite consumer, pnpm/action-setup
    must run before any pnpm install/exec/run/--version step in the same job."""
    for yaml_path in _iter_yaml_files(REPO_ROOT):
        rel = yaml_path.relative_to(REPO_ROOT).as_posix()
        if rel not in ALLOWED_PNPM_ACTION_SETUP_CONSUMERS:
            continue
        doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if _is_composite_action(doc):
            continue
        jobs = doc.get("jobs") or {}
        for job_name, job in jobs.items():
            steps = job.get("steps") or []
            setup_index = next((i for i, s in enumerate(steps) if _uses_action_setup(s)), None)
            if setup_index is None:
                continue
            for i, step in enumerate(steps):
                if i == setup_index:
                    continue
                run = str(step.get("run", ""))
                if _PNPM_COMMAND_RE.search(run):
                    assert i > setup_index, (
                        f"{rel}:{job_name}: pnpm command at step {i} runs before "
                        f"pnpm/action-setup at step {setup_index}"
                    )


# ---------------------------------------------------------------------------
# P1-4: behavioral (subprocess) verification of the version-assertion script
# ---------------------------------------------------------------------------


def _run_version_assert_script(
    run_script: str,
    *,
    fake_pnpm_version: str | None,
    package_json_text: str,
) -> subprocess.CompletedProcess[str]:
    """Execute the extracted version-assertion `run:` block in a bash subprocess
    with a fake `pnpm` on PATH and a fake `package.json` in cwd, so the actual
    fail-closed / fail-open behavior is exercised (not just string presence)."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        if fake_pnpm_version is not None:
            pnpm_path = bin_dir / "pnpm"
            pnpm_path.write_text(f"#!/bin/sh\necho '{fake_pnpm_version}'\n")
            pnpm_path.chmod(0o755)
        (tmp_path / "package.json").write_text(package_json_text, encoding="utf-8")

        script_path = tmp_path / "assert.sh"
        script_path.write_text(run_script, encoding="utf-8")

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"

        return subprocess.run(
            ["bash", str(script_path)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.mark.parametrize("job_name", DEPLOY_JOBS)
def test_version_assertion_script_exits_zero_on_exact_match(job_name: str):
    step = _extract_version_assert_step(job_name)
    run_script = str(step["run"])
    result = _run_version_assert_script(
        run_script,
        fake_pnpm_version=EXPECTED_PNPM_VERSION,
        package_json_text=json.dumps({"packageManager": EXPECTED_PACKAGE_MANAGER}),
    )
    assert result.returncode == 0, (
        f"{job_name}: exact-match pnpm version must exit 0, "
        f"got {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("job_name", DEPLOY_JOBS)
def test_version_assertion_script_exits_nonzero_on_patch_mismatch(job_name: str):
    step = _extract_version_assert_step(job_name)
    run_script = str(step["run"])
    result = _run_version_assert_script(
        run_script,
        fake_pnpm_version="11.7.1",
        package_json_text=json.dumps({"packageManager": EXPECTED_PACKAGE_MANAGER}),
    )
    assert result.returncode != 0, (
        f"{job_name}: mismatched pnpm version (11.7.1 vs 11.7.0) must exit non-zero, "
        f"got 0: stdout={result.stdout!r}"
    )


@pytest.mark.parametrize("job_name", DEPLOY_JOBS)
def test_version_assertion_script_exits_nonzero_on_missing_package_manager(job_name: str):
    step = _extract_version_assert_step(job_name)
    run_script = str(step["run"])
    result = _run_version_assert_script(
        run_script,
        fake_pnpm_version=EXPECTED_PNPM_VERSION,
        package_json_text=json.dumps({"name": "loop-protocol"}),
    )
    assert result.returncode != 0, (
        f"{job_name}: missing package.json#packageManager must exit non-zero, got 0: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("job_name", DEPLOY_JOBS)
def test_version_assertion_script_exits_nonzero_on_range_package_manager(job_name: str):
    step = _extract_version_assert_step(job_name)
    run_script = str(step["run"])
    result = _run_version_assert_script(
        run_script,
        fake_pnpm_version=EXPECTED_PNPM_VERSION,
        package_json_text=json.dumps({"packageManager": "pnpm@^11.7.0"}),
    )
    assert result.returncode != 0, (
        f"{job_name}: a semver-range packageManager (^11.7.0) must exit non-zero, "
        f"got 0: stdout={result.stdout!r} stderr={result.stderr!r}"
    )



# ---------------------------------------------------------------------------
# P1-3: agent-retro-index.yml read-only bootstrap smoke job (Issue #1476)
#
# agent-retro-index.yml's write jobs (build-index / upsert-parent-comment)
# only trigger on workflow_dispatch and push-to-main, so a PR touching this
# file's pnpm bootstrap was never actually exercised by CI. These tests
# assert (a) the write jobs are excluded from pull_request, (b) a dedicated
# read-only bootstrap-smoke job runs on pull_request instead, and (c) its
# version-assertion step is bootstrap-equivalent to deploy-pages.yml's --
# same runtime-derivation-from-packageManager pattern, no error suppression.
# ---------------------------------------------------------------------------

AGENT_RETRO_INDEX_WORKFLOW_RELPATH = Path(".github") / "workflows" / "agent-retro-index.yml"
AGENT_RETRO_INDEX_WRITE_JOBS = ("build-index", "upsert-parent-comment")
AGENT_RETRO_INDEX_SMOKE_JOB = "bootstrap-smoke"


def _load_agent_retro_index_workflow(root: Path = REPO_ROOT) -> dict[str, Any]:
    path = root / AGENT_RETRO_INDEX_WORKFLOW_RELPATH
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_agent_retro_index_has_pull_request_trigger():
    """P1-3: agent-retro-index.yml must declare a pull_request trigger so its
    bootstrap-smoke job is actually exercised by PR-scoped CI."""
    workflow = _load_agent_retro_index_workflow()
    # PyYAML (YAML 1.1) parses the bare `on:` key as the boolean `True`, not
    # the string "on" -- guard against both to avoid a false negative.
    triggers = workflow.get("on")
    if triggers is None:
        triggers = workflow.get(True) or {}
    assert "pull_request" in triggers, (
        "agent-retro-index.yml must declare a pull_request trigger "
        "(Issue #1476 P1-3 bootstrap smoke)"
    )


@pytest.mark.parametrize("job_name", AGENT_RETRO_INDEX_WRITE_JOBS)
def test_agent_retro_index_write_jobs_are_excluded_from_pull_request(job_name: str):
    """P1-3: build-index / upsert-parent-comment must never run on pull_request
    (only the read-only bootstrap-smoke job may run there)."""
    workflow = _load_agent_retro_index_workflow()
    job = workflow["jobs"][job_name]
    condition = str(job.get("if", ""))
    assert "pull_request" in condition, (
        f"{job_name}: must explicitly exclude github.event_name == 'pull_request' "
        f"via an `if:` guard, got if={condition!r}"
    )


def test_agent_retro_index_has_read_only_bootstrap_smoke_job():
    """P1-3: a dedicated bootstrap-smoke job must exist, run only on
    pull_request, and declare no write permissions."""
    workflow = _load_agent_retro_index_workflow()
    jobs = workflow.get("jobs") or {}
    assert AGENT_RETRO_INDEX_SMOKE_JOB in jobs, (
        f"agent-retro-index.yml must declare a {AGENT_RETRO_INDEX_SMOKE_JOB!r} job "
        "(Issue #1476 P1-3)"
    )
    job = jobs[AGENT_RETRO_INDEX_SMOKE_JOB]
    condition = str(job.get("if", ""))
    assert "pull_request" in condition, (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: must be gated to pull_request only, "
        f"got if={condition!r}"
    )
    permissions = job.get("permissions") or {}
    assert permissions.get("issues") != "write", (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: must not declare issues: write "
        "(read-only smoke job)"
    )
    assert permissions.get("pull-requests") != "write", (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: must not declare pull-requests: write "
        "(read-only smoke job)"
    )
    steps = job.get("steps") or []
    assert any(_uses_action_setup(s) for s in steps), (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: must include a pnpm/action-setup step"
    )


def test_agent_retro_index_smoke_job_version_assertion_is_bootstrap_equivalent():
    """P1-3: static bootstrap-equivalence -- the smoke job's version-assertion
    step must derive the expected pnpm version from package.json#packageManager
    at runtime (same pattern as deploy-pages.yml), never hardcode the literal
    version, and must not suppress errors."""
    workflow = _load_agent_retro_index_workflow()
    steps = workflow["jobs"][AGENT_RETRO_INDEX_SMOKE_JOB]["steps"]
    version_assert_step = next(
        (s for s in steps if "pnpm --version" in str(s.get("run", ""))), None
    )
    assert version_assert_step is not None, (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: missing a `pnpm --version` assertion step"
    )
    run = str(version_assert_step.get("run", ""))
    assert 'require("./package.json").packageManager' in run, (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: version assertion must derive the "
        f"expected version from package.json#packageManager at runtime, got: {run!r}"
    )
    assert EXPECTED_PNPM_VERSION not in run, (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: version assertion must NOT hardcode the "
        f"literal pnpm version {EXPECTED_PNPM_VERSION!r}"
    )
    assert '"$actual_pnpm_version" != "$expected_pnpm_version"' in run, (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: version assertion must compare the "
        f"runtime-derived expected version against the actually-resolved "
        f"`pnpm --version`, got: {run!r}"
    )
    assert not _step_has_error_suppression(version_assert_step), (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: version assertion step must not "
        "suppress errors"
    )

    # Bootstrap-equivalence: compare against deploy-pages.yml's assertion body
    # (both derive from the same runtime packageManager parse/compare shape).
    deploy_pages_step = _extract_version_assert_step("deploy-main")
    deploy_pages_run = str(deploy_pages_step.get("run", "")).strip()
    assert run.strip() == deploy_pages_run, (
        f"{AGENT_RETRO_INDEX_SMOKE_JOB}: version-assertion run body must be "
        "identical to deploy-pages.yml's deploy-main assertion body "
        "(bootstrap-equivalence, Issue #1476 P1-3)"
    )


if __name__ == "__main__":
    raise SystemExit(main())

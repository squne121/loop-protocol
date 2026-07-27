#!/usr/bin/env python3
"""Fail-closed invariant verifier for the python-test lane topology (Issue #1760).

Parses the real ``.github/workflows/ci.yml`` YAML AST (never a text/regex scan of the
raw file) and asserts the topology contract introduced by Issue #1760:

  * ``jobs.python-test-core`` exists and its steps never invoke
    ``node`` / ``npm`` / ``npx`` / ``pnpm`` / ``corepack`` / ``codex`` (including through
    common shell indirection: ``bash -c``, ``sh -c``, ``env <cmd>``, and ``$(...)``/
    backtick command substitution) -- AC2.
  * ``jobs.codex-execpolicy`` exists and owns the Node/Codex CLI bootstrap and the
    execpolicy matrix + ``tests/codex/test_local_main_branch_guard.py`` pytest
    invocation -- AC4.
  * ``jobs.python-test`` (the required aggregate) is defined with
    ``needs: [python-test-core, codex-execpolicy]`` and ``if: always()`` -- AC5.
  * ``jobs.codex-execpolicy`` creates a sentinel status artifact step before the
    matrix orchestrator step -- AC6.
  * No step in ``python-test-core`` invokes a pytest target/nodeid string that is
    not part of the plan-driven steps (i.e. no new hard-coded pytest execution was
    smuggled into python-test-core outside the python-test-plan.json SSOT) -- AC3.
  * This verifier itself is wired into ci.yml as an EXACT command (not
    ``continue-on-error: true``, not merely present in a different job) -- AC7
    positive contract test companion (see scripts/ci/tests/test_verify_python_test_lane.py).

This script never mutates ci.yml. Exit 0 = all invariants hold. Exit 2 = one or more
invariants violated (fail-closed); the JSON report on stdout lists every violation.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path
from typing import Any

import yaml

SCHEMA = "python_test_lane_verification_v1"

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

FORBIDDEN_COMMANDS = {"node", "npm", "npx", "pnpm", "corepack", "codex"}

# Steps in python-test-core that are explicitly sanctioned to run pytest -- either
# via the python-test-plan.json SSOT loader, or as a pytest --collect-only guard
# that is itself plan-driven / a fixed hook-discovery invariant unrelated to Issue
# #1760's Node/Codex split.
PYTHON_TEST_CORE_PYTEST_STEP_ALLOWLIST = {
    "pytest python suite (parallel) (timed)",
    "pytest serial lane (parallel-unsafe) (timed)",
    "Verify hook test discovery exclusions",
    "Generate ci_test_selection/v1 artifact",
}


class VerificationError(RuntimeError):
    pass


def load_workflow(ci_yml_path: Path) -> dict[str, Any]:
    if not ci_yml_path.is_file():
        raise VerificationError(f"ci.yml not found: {ci_yml_path}")
    try:
        data = yaml.safe_load(ci_yml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise VerificationError(f"ci.yml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or "jobs" not in data:
        raise VerificationError("ci.yml has no top-level 'jobs' mapping")
    return data


def _step_run_text(step: dict[str, Any]) -> str:
    run = step.get("run")
    return run if isinstance(run, str) else ""


def _tokenize_shell_lines(run_text: str) -> list[list[str]]:
    """Best-effort tokenization of every shell line in a ``run:`` block.

    Each physical line is shlex-tokenized independently (CI ``run:`` blocks are bash
    scripts, not a single shell expression) so a forbidden command hiding as a
    positional argument to ``bash -c`` / ``sh -c`` / ``env`` is still visible as a
    token, and command substitution ``$(...)`` / backticks are unwrapped into their
    own token stream as well.
    """
    lines: list[list[str]] = []
    for raw_line in run_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Unwrap $(...) and `...` command substitution onto the same token stream --
        # a forbidden command can be smuggled inside substitution without appearing
        # as a bare top-level word.
        substituted = re.findall(r"\$\(([^()]*)\)|`([^`]*)`", line)
        expanded = line
        for a, b in substituted:
            expanded += " " + (a or b)
        try:
            tokens = shlex.split(expanded, comments=False)
        except ValueError:
            tokens = expanded.split()
        if tokens:
            lines.append(tokens)
    return lines


def _find_forbidden_commands(run_text: str) -> list[str]:
    """Return the forbidden commands (deduped, sorted) found anywhere in a run block.

    Also inspects the individual whitespace-separated words of any MULTI-WORD token
    (e.g. the quoted command string passed to ``bash -c "npm install"`` / ``sh -c``
    / ``env sh -c ...``) so a forbidden command hidden inside a nested command
    string is still detected, not just bare top-level words.
    """
    hits: set[str] = set()
    for tokens in _tokenize_shell_lines(run_text):
        for tok in tokens:
            candidates = [tok]
            if " " in tok:
                candidates.extend(tok.split())
            for candidate in candidates:
                bare = candidate.split("/")[-1]  # tolerate a path prefix e.g. /usr/bin/node
                if bare in FORBIDDEN_COMMANDS:
                    hits.add(bare)
    return sorted(hits)


def _job_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    return steps if isinstance(steps, list) else []


def check_python_test_core_exists(jobs: dict[str, Any], violations: list[str]) -> dict[str, Any] | None:
    job = jobs.get("python-test-core")
    if not isinstance(job, dict):
        violations.append("AC1: jobs.python-test-core is missing")
        return None
    return job


def check_no_node_codex_in_core(job: dict[str, Any], violations: list[str]) -> None:
    for step in _job_steps(job):
        uses = step.get("uses")
        if isinstance(uses, str) and re.match(r"^actions/setup-node(@|$)", uses):
            violations.append(
                f"AC2: python-test-core step {step.get('name', '<unnamed>')!r} uses actions/setup-node"
            )
        run_text = _step_run_text(step)
        if not run_text:
            continue
        forbidden = _find_forbidden_commands(run_text)
        if forbidden:
            violations.append(
                f"AC2: python-test-core step {step.get('name', '<unnamed>')!r} "
                f"invokes forbidden command(s): {forbidden}"
            )


def check_no_plan_external_pytest(job: dict[str, Any], violations: list[str]) -> None:
    pytest_word = re.compile(r"(?<![\w./-])pytest(?![\w])")
    for step in _job_steps(job):
        name = step.get("name", "<unnamed>")
        run_text = _step_run_text(step)
        if not run_text or not pytest_word.search(run_text):
            continue
        if name not in PYTHON_TEST_CORE_PYTEST_STEP_ALLOWLIST:
            violations.append(
                f"AC3: python-test-core step {name!r} runs pytest but is not in the "
                "plan-driven step allowlist (possible plan-external pytest injection)"
            )


def check_codex_execpolicy_job(jobs: dict[str, Any], violations: list[str]) -> dict[str, Any] | None:
    job = jobs.get("codex-execpolicy")
    if not isinstance(job, dict):
        violations.append("AC4: jobs.codex-execpolicy is missing")
        return None
    steps = _job_steps(job)
    has_setup_node = any(
        isinstance(step.get("uses"), str) and re.match(r"^actions/setup-node(@|$)", step["uses"])
        for step in steps
    )
    if not has_setup_node:
        violations.append("AC4: jobs.codex-execpolicy does not set up Node (actions/setup-node)")
    matrix_steps = [step for step in steps if "codex_execpolicy_matrix.py" in _step_run_text(step)]
    if not matrix_steps:
        violations.append("AC4: jobs.codex-execpolicy does not invoke scripts/ci/codex_execpolicy_matrix.py")
    guard_test_steps = [
        step for step in steps if "tests/codex/test_local_main_branch_guard.py" in _step_run_text(step)
    ]
    if not guard_test_steps:
        violations.append(
            "AC4: jobs.codex-execpolicy does not run tests/codex/test_local_main_branch_guard.py"
        )
    return job


def check_sentinel_before_matrix(job: dict[str, Any], violations: list[str]) -> None:
    steps = _job_steps(job)
    sentinel_idx = None
    matrix_idx = None
    for idx, step in enumerate(steps):
        run_text = _step_run_text(step)
        if sentinel_idx is None and "codex_execpolicy_matrix_status_v1.json" in run_text and (
            '"status": "started"' in run_text or "'status': 'started'" in run_text
        ):
            sentinel_idx = idx
        if matrix_idx is None and "codex_execpolicy_matrix.py" in run_text and "--artifact" in run_text:
            matrix_idx = idx
    if sentinel_idx is None:
        violations.append("AC6: jobs.codex-execpolicy has no sentinel-artifact-creation step")
        return
    if matrix_idx is None:
        violations.append(
            "AC6: jobs.codex-execpolicy has no matrix orchestrator step to compare sentinel ordering against"
        )
        return
    if sentinel_idx >= matrix_idx:
        violations.append("AC6: sentinel artifact step must run BEFORE the matrix orchestrator step")

    # Bootstrap/matrix failure must still leave a status artifact: require the
    # upload step for codex_execpolicy_artifacts/ to run with `if: always()`.
    upload_steps = [
        step
        for step in steps
        if isinstance(step.get("uses"), str)
        and step["uses"].startswith("actions/upload-artifact")
        and "codex_execpolicy_artifacts" in str((step.get("with") or {}).get("path", ""))
    ]
    if not upload_steps:
        violations.append("AC6: jobs.codex-execpolicy has no artifact upload step for codex_execpolicy_artifacts/")
        return
    for step in upload_steps:
        cond = step.get("if")
        if cond not in ("${{ always() }}", "always()"):
            violations.append(
                "AC6: codex_execpolicy_artifacts upload step must run with `if: ${{ always() }}` "
                "so bootstrap/matrix failure artifacts are still uploaded"
            )


def check_aggregate_job(jobs: dict[str, Any], violations: list[str]) -> None:
    job = jobs.get("python-test")
    if not isinstance(job, dict):
        violations.append("AC5: jobs.python-test (required aggregate) is missing")
        return
    needs = job.get("needs")
    if needs != ["python-test-core", "codex-execpolicy"]:
        violations.append(
            f"AC5: jobs.python-test.needs must equal ['python-test-core', 'codex-execpolicy'], got {needs!r}"
        )
    cond = job.get("if")
    if cond not in ("always()", "${{ always() }}"):
        violations.append(f"AC5: jobs.python-test.if must be always(), got {cond!r}")
    # AC10: aggregate must not silently succeed/skip during python_test_bench dispatch --
    # its step content must reference the documented aggregate policy name.
    steps = _job_steps(job)
    has_bench_policy = any("python_test_bench_aggregate_policy" in _step_run_text(step) for step in steps)
    if not has_bench_policy:
        violations.append(
            "AC10: jobs.python-test has no step implementing python_test_bench_aggregate_policy"
        )


def check_verifier_not_disabled(jobs: dict[str, Any], violations: list[str]) -> None:
    """AC7: the verifier itself must be wired into ci.yml as an exact, non-disabled step."""
    found = False
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in _job_steps(job):
            run_text = _step_run_text(step)
            if "verify_python_test_lane.py" in run_text:
                found = True
                if step.get("continue-on-error") is True:
                    violations.append(
                        f"AC7: verify_python_test_lane.py step in job {job_name!r} has "
                        "continue-on-error: true (verifier disabled)"
                    )
    if not found:
        violations.append("AC7: verify_python_test_lane.py is not invoked anywhere in ci.yml")


def verify(ci_yml_path: Path) -> dict[str, Any]:
    violations: list[str] = []
    data = load_workflow(ci_yml_path)
    jobs = data["jobs"]

    core_job = check_python_test_core_exists(jobs, violations)
    if core_job is not None:
        check_no_node_codex_in_core(core_job, violations)
        check_no_plan_external_pytest(core_job, violations)

    codex_job = check_codex_execpolicy_job(jobs, violations)
    if codex_job is not None:
        check_sentinel_before_matrix(codex_job, violations)

    check_aggregate_job(jobs, violations)
    check_verifier_not_disabled(jobs, violations)

    ok = not violations
    return {
        "schema": SCHEMA,
        "ci_yml": str(ci_yml_path),
        "ok": ok,
        "violations": violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ci-yml",
        default=str(DEFAULT_CI_YML),
        help="path to the ci.yml workflow file to verify (default: repo SSOT)",
    )
    args = parser.parse_args(argv)

    try:
        report = verify(Path(args.ci_yml))
    except VerificationError as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "violations": [str(exc)]}, indent=2))
        return 2

    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

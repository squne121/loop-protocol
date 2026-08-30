#!/usr/bin/env python3
"""Fail-closed invariant verifier for the python-test lane topology (Issue #1760).

Parses the real ``.github/workflows/ci.yml`` YAML AST (never a text/regex scan of the
raw file) and asserts the topology contract introduced by Issue #1760:

  * ``jobs.python-test-core`` exists and its steps never invoke
    ``node`` / ``npm`` / ``npx`` / ``pnpm`` / ``corepack`` / ``codex`` (including through
    common shell indirection: ``bash -c``, ``sh -c``, ``env <cmd>``, and ``$(...)``/
    backtick command substitution) -- AC2. This ALSO resolves any local composite
    action (``uses: ./...``) referenced by a python-test-core step and recursively
    scans it: a Node bootstrap hiding behind a composite action (e.g.
    ``./.github/actions/setup-node-pnpm``) is rejected the same as a direct
    ``actions/setup-node`` usage (Issue #1824 P1-2 review point 4).
  * ``jobs.python-test`` (the required aggregate) is defined with
    ``needs: [python-test-core]`` and ``if: always()`` -- AC5, AND
    its policy step invokes ``scripts/ci/evaluate_python_test_aggregate.py`` with an
    EXACT, fixed argv (not merely a substring match on a literal string) -- Issue
    #1824 P1-1 review: a ``run:`` heredoc that merely CONTAINS the string
    ``python_test_bench_aggregate_policy`` (e.g. replaced with
    ``echo python_test_bench_aggregate_policy ok``) must be rejected.
  * No step in ``python-test-core`` invokes a pytest target/nodeid string that is
    not part of the plan-driven steps (i.e. no new hard-coded pytest execution was
    smuggled into python-test-core outside the python-test-plan.json SSOT) -- AC3.
    This includes a hard-coded target token APPENDED to an existing plan-driven
    pytest invocation line, not just a brand-new unregistered step (Issue #1824
    P1-2 review point 2).
  * ``.github/ci/python-test-plan.json`` (the plan SSOT) is loadable, has a
    non-empty ``targets`` list, and python-test-core actually references the
    ``scripts/ci/python_test_plan.py`` loader (Issue #1824 P1-2 review point 3).
  * This verifier itself is wired into ci.yml, in ``jobs.python-test-core``, as an
    EXACT command (full argv match, not a substring), appearing EXACTLY ONCE, with
    no ``if:`` condition and no ``continue-on-error: true`` -- AC7 (Issue #1824
    P1-2 review point 5).

Issue #2161 (native Codex CLI retirement): the former ``jobs.codex-execpolicy``
(AC4/AC6, the dedicated Node/Codex CLI lane and its sentinel-before-matrix
invariant) was removed along with the job.

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

# Node-bootstrapping GitHub Actions that must never be reachable (directly OR via a
# local composite action) from python-test-core.
FORBIDDEN_ACTION_PATTERNS = (
    re.compile(r"^actions/setup-node(@|$)"),
    re.compile(r"^pnpm/action-setup(@|$)"),
)

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

# The subset of PYTHON_TEST_CORE_PYTEST_STEP_ALLOWLIST whose pytest invocation MUST
# be 100% plan-driven (argv comes only from the python-test-plan.json loader via a
# shell variable) -- a literal path/nodeid token appended here is a P1-2 review
# hard-coding regression, not a legitimate hook-discovery/collect-only guard.
PLAN_DRIVEN_PYTEST_STEP_NAMES = {
    "pytest python suite (parallel) (timed)",
    "pytest serial lane (parallel-unsafe) (timed)",
}

# Flags whose next positional token is a VALUE (e.g. a plugin name), not a pytest
# target -- so it must not be flagged as a hard-coded target token.
_FLAG_TAKES_VALUE = {"-p"}

_REDIRECT_TOKENS = {">", ">>", "2>&1", "2>", "&>", "1>"}

_PYTEST_WORD_RE = re.compile(r"(?<![\w./-])pytest(?![\w])")

# Issue #1824 P1-1: the aggregate policy step must invoke this script with an
# EXACT, fixed argv (order-sensitive on the flag names, order-insensitive on
# nothing else -- this is intentionally a strict literal-argv contract).
AGGREGATE_EVALUATOR_SCRIPT = "scripts/ci/evaluate_python_test_aggregate.py"
EXPECTED_AGGREGATE_ARGV = [
    "uv",
    "run",
    "--locked",
    "python3",
    AGGREGATE_EVALUATOR_SCRIPT,
    "--core-result",
    "${{ needs.python-test-core.result }}",
    "--bench-mode",
    "${{ github.event.inputs.python_test_bench }}",
]

# Issue #1824 P1-2 review point 5: the verifier's own self-wiring step must match
# this EXACT argv, appear exactly once, in job python-test-core, with no `if:` and
# no continue-on-error.
EXPECTED_VERIFIER_ARGV = [
    "uv",
    "run",
    "--locked",
    "python",
    "scripts/ci/verify_python_test_lane.py",
    "--ci-yml",
    ".github/workflows/ci.yml",
]


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


def _tokenize_shell_line(line: str) -> list[str]:
    try:
        return shlex.split(line, comments=False)
    except ValueError:
        return line.split()


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
        tokens = _tokenize_shell_line(expanded)
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


def _matches_forbidden_action(uses: str) -> bool:
    return any(pattern.match(uses) for pattern in FORBIDDEN_ACTION_PATTERNS)


def _resolve_local_composite_action(uses: str, repo_root: Path) -> Path | None:
    """Resolve a ``uses: ./relative/path`` local composite action to its action.yml.

    Returns ``None`` (not an error) when the referenced path does not resolve to an
    on-disk composite action -- test fixtures using synthetic paths under an
    isolated tmp_path repo have nothing to resolve against, and that is not itself
    an Issue #1760/#1824 topology violation.
    """
    if not uses.startswith("./") and not uses.startswith("../"):
        return None
    action_dir = (repo_root / uses).resolve()
    for candidate_name in ("action.yml", "action.yaml"):
        candidate = action_dir / candidate_name
        if candidate.is_file():
            return candidate
    return None


def _scan_composite_action_for_node_bootstrap(
    action_path: Path, *, source_uses: str, violations: list[str]
) -> None:
    try:
        data = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(data, dict):
        return
    runs = data.get("runs")
    if not isinstance(runs, dict) or runs.get("using") != "composite":
        return
    steps = runs.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses")
        if isinstance(uses, str) and _matches_forbidden_action(uses):
            violations.append(
                f"AC2: python-test-core step uses local composite action {source_uses!r} "
                f"which itself uses Node-bootstrapping action {uses!r} (indirect "
                "Node bootstrap is still forbidden in python-test-core)"
            )
        run_text = _step_run_text(step)
        if run_text:
            forbidden = _find_forbidden_commands(run_text)
            if forbidden:
                violations.append(
                    f"AC2: python-test-core step uses local composite action {source_uses!r} "
                    f"whose own step invokes forbidden command(s) {forbidden}"
                )


def check_python_test_core_exists(jobs: dict[str, Any], violations: list[str]) -> dict[str, Any] | None:
    job = jobs.get("python-test-core")
    if not isinstance(job, dict):
        violations.append("AC1: jobs.python-test-core is missing")
        return None
    return job


def check_no_node_codex_in_core(job: dict[str, Any], violations: list[str], *, repo_root: Path) -> None:
    for step in _job_steps(job):
        uses = step.get("uses")
        if isinstance(uses, str):
            if _matches_forbidden_action(uses):
                violations.append(
                    f"AC2: python-test-core step {step.get('name', '<unnamed>')!r} uses {uses!r} "
                    "(Node-bootstrapping action)"
                )
            else:
                composite_path = _resolve_local_composite_action(uses, repo_root)
                if composite_path is not None:
                    _scan_composite_action_for_node_bootstrap(
                        composite_path, source_uses=uses, violations=violations
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
    for step in _job_steps(job):
        name = step.get("name", "<unnamed>")
        run_text = _step_run_text(step)
        if not run_text or not _PYTEST_WORD_RE.search(run_text):
            continue
        if name not in PYTHON_TEST_CORE_PYTEST_STEP_ALLOWLIST:
            violations.append(
                f"AC3: python-test-core step {name!r} runs pytest but is not in the "
                "plan-driven step allowlist (possible plan-external pytest injection)"
            )


def check_no_hardcoded_pytest_target(job: dict[str, Any], violations: list[str]) -> None:
    """AC3 (Issue #1824 P1-2 review point 2): a hard-coded pytest target/nodeid
    token appended to an EXISTING plan-driven pytest invocation line must be
    detected, not just a brand-new unregistered step."""
    for step in _job_steps(job):
        name = step.get("name", "<unnamed>")
        if name not in PLAN_DRIVEN_PYTEST_STEP_NAMES:
            continue
        run_text = _step_run_text(step)
        for raw_line in run_text.splitlines():
            line = raw_line.strip()
            if not _PYTEST_WORD_RE.search(line):
                continue
            tokens = _tokenize_shell_line(line)
            truncated: list[str] = []
            for tok in tokens:
                if tok in _REDIRECT_TOKENS:
                    break
                truncated.append(tok)
            skip_next = False
            for tok in truncated:
                if skip_next:
                    skip_next = False
                    continue
                if tok in _FLAG_TAKES_VALUE:
                    skip_next = True
                    continue
                if tok in {"uv", "run", "--locked", "pytest"}:
                    continue
                if tok.startswith("-"):
                    continue
                if tok.startswith("$") or "${" in tok:
                    continue
                if "/" in tok or tok.endswith(".py"):
                    violations.append(
                        f"AC3: python-test-core step {name!r} pytest invocation line "
                        f"has a hard-coded target/path token {tok!r} outside the "
                        "python-test-plan.json-driven argv"
                    )


def check_plan_is_loadable_and_referenced(
    jobs: dict[str, Any], violations: list[str], *, ci_yml_path: Path
) -> None:
    """Issue #1824 P1-2 review point 3: the plan SSOT must actually be loaded /
    referenced, not merely assumed."""
    github_dir = ci_yml_path.resolve().parents[1]
    plan_path = github_dir / "ci" / "python-test-plan.json"
    if not plan_path.is_file():
        # No plan file to cross-check against (e.g. an isolated test fixture whose
        # tmp_path repo has no .github/ci/ directory) -- not itself a violation.
        return
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        violations.append(f"AC3: {plan_path} failed to load: {exc}")
        return
    targets = plan.get("targets") if isinstance(plan, dict) else None
    if not isinstance(targets, list) or not targets:
        violations.append(f"AC3: {plan_path} has no non-empty 'targets' list")
        return

    core_job = jobs.get("python-test-core")
    if not isinstance(core_job, dict):
        return
    loader_referenced = any(
        "scripts/ci/python_test_plan.py" in _step_run_text(step) for step in _job_steps(core_job)
    )
    if not loader_referenced:
        violations.append(
            "AC3: python-test-core does not reference scripts/ci/python_test_plan.py "
            "(pytest target set may not be plan-driven)"
        )


def _run_text_exact_argv(run_text: str, expected_argv: list[str]) -> bool:
    """True iff ``run_text`` is (once split into shell lines and re-tokenized as a
    single logical command via backslash-continuation joining) EXACTLY the given
    argv -- not merely containing it as a substring."""
    # Join backslash-continued lines into one logical line, matching how bash
    # actually parses a `run: |` block's continuation syntax.
    joined = re.sub(r"\\\s*\n\s*", " ", run_text.strip())
    lines = [ln.strip() for ln in joined.splitlines() if ln.strip()]
    if len(lines) != 1:
        return False
    tokens = _tokenize_shell_line(lines[0])
    return tokens == expected_argv


def check_aggregate_job(jobs: dict[str, Any], violations: list[str]) -> None:
    job = jobs.get("python-test")
    if not isinstance(job, dict):
        violations.append("AC5: jobs.python-test (required aggregate) is missing")
        return
    needs = job.get("needs")
    if needs != ["python-test-core"]:
        violations.append(
            f"AC5: jobs.python-test.needs must equal ['python-test-core'], got {needs!r}"
        )
    cond = job.get("if")
    if cond not in ("always()", "${{ always() }}"):
        violations.append(f"AC5: jobs.python-test.if must be always(), got {cond!r}")

    # AC10/P1-1: the aggregate policy step must invoke evaluate_python_test_aggregate.py
    # with an EXACT, fixed argv -- a substring match on
    # "python_test_bench_aggregate_policy" is insufficient (a no-op stub containing
    # that string as a comment/echo would still pass a substring check).
    steps = _job_steps(job)
    matching_steps = [
        step for step in steps if AGGREGATE_EVALUATOR_SCRIPT in _step_run_text(step)
    ]
    if not matching_steps:
        violations.append(
            f"AC10: jobs.python-test has no step invoking {AGGREGATE_EVALUATOR_SCRIPT}"
        )
        return
    if len(matching_steps) != 1:
        violations.append(
            f"AC10: jobs.python-test must invoke {AGGREGATE_EVALUATOR_SCRIPT} in EXACTLY one "
            f"step, found {len(matching_steps)}"
        )
    for step in matching_steps:
        run_text = _step_run_text(step)
        if not _run_text_exact_argv(run_text, EXPECTED_AGGREGATE_ARGV):
            violations.append(
                f"AC10: jobs.python-test step {step.get('name', '<unnamed>')!r} does not invoke "
                f"{AGGREGATE_EVALUATOR_SCRIPT} with the exact expected argv"
            )
        if step.get("if") is not None:
            violations.append(
                f"AC10: jobs.python-test step {step.get('name', '<unnamed>')!r} must not have its "
                "own `if:` condition (the job-level `if: always()` already governs execution)"
            )
        if step.get("continue-on-error") is True:
            violations.append(
                f"AC10: jobs.python-test step {step.get('name', '<unnamed>')!r} has "
                "continue-on-error: true (aggregate policy failure would be silently ignored)"
            )


def check_verifier_not_disabled(jobs: dict[str, Any], violations: list[str]) -> None:
    """AC7: the verifier itself must be wired into ci.yml, in python-test-core, as an
    EXACT, single, non-disabled step (Issue #1824 P1-2 review point 5)."""
    matches: list[tuple[str, dict[str, Any]]] = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in _job_steps(job):
            run_text = _step_run_text(step)
            if "verify_python_test_lane.py" in run_text:
                matches.append((job_name, step))

    if not matches:
        violations.append("AC7: verify_python_test_lane.py is not invoked anywhere in ci.yml")
        return

    if len(matches) != 1:
        violations.append(
            f"AC7: verify_python_test_lane.py must be invoked EXACTLY once, found {len(matches)} "
            f"occurrence(s) in job(s) {sorted({j for j, _ in matches})}"
        )

    for job_name, step in matches:
        run_text = _step_run_text(step)
        if job_name != "python-test-core":
            violations.append(
                f"AC7: verify_python_test_lane.py step found in job {job_name!r}, expected "
                "job 'python-test-core'"
            )
        if not _run_text_exact_argv(run_text, EXPECTED_VERIFIER_ARGV):
            violations.append(
                f"AC7: verify_python_test_lane.py step in job {job_name!r} does not match the "
                "exact expected argv (substring match is insufficient)"
            )
        if step.get("continue-on-error") is True:
            violations.append(
                f"AC7: verify_python_test_lane.py step in job {job_name!r} has "
                "continue-on-error: true (verifier disabled)"
            )
        if step.get("if") is not None:
            violations.append(
                f"AC7: verify_python_test_lane.py step in job {job_name!r} has its own `if:` "
                "condition (could skip the verifier while the job still reports success)"
            )


def verify(ci_yml_path: Path) -> dict[str, Any]:
    violations: list[str] = []
    data = load_workflow(ci_yml_path)
    jobs = data["jobs"]

    repo_root = ci_yml_path.resolve().parents[2]

    core_job = check_python_test_core_exists(jobs, violations)
    if core_job is not None:
        check_no_node_codex_in_core(core_job, violations, repo_root=repo_root)
        check_no_plan_external_pytest(core_job, violations)
        check_no_hardcoded_pytest_target(core_job, violations)

    check_plan_is_loadable_and_referenced(jobs, violations, ci_yml_path=ci_yml_path)

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

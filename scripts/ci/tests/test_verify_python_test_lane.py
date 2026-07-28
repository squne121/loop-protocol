"""Tests for scripts/ci/verify_python_test_lane.py (Issue #1760 / #1824 P1-2).

AC7: negative tests for Node/Codex reintroduction, plan-external pytest injection,
job removal/relocation, and verifier self-disablement, PLUS a positive contract test
that the verifier accepts the exact real ci.yml AND that it is itself wired into
ci.yml as an exact (non-disabled) command.

Issue #1824 P1-2 review adds negative fixtures for each documented bypass:
  1. substring-only detection -> exact argv
  2. hard-coded pytest target appended to an existing plan-driven step
  3. python-test-plan.json never loaded/cross-checked
  4. a local composite action hiding a Node bootstrap
  5. verifier invocation (job/argv/single-occurrence/if/continue-on-error) fixed
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO_ROOT / "scripts" / "ci" / "verify_python_test_lane.py"
_REAL_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_python_test_lane", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


_SENTINEL_STEP = (
    "      - name: Create codex-execpolicy sentinel artifact (AC6)\n"
    "        run: |\n"
    "          python3 - <<'PY'\n"
    "          import pathlib\n"
    "          pathlib.Path(\n"
    '              "codex_execpolicy_artifacts/codex_execpolicy_matrix_status_v1.json"\n'
    "          ).write_text(\n"
    "              '{\"status\": \"started\"}'\n"
    "          )\n"
    "          PY\n"
)
_MATRIX_STEP = (
    "      - name: codex execpolicy + cleanup-route matrix (timed)\n"
    "        run: |\n"
    "          python3 scripts/ci/codex_execpolicy_matrix.py \\\n"
    "            --artifact codex_execpolicy_artifacts/codex_execpolicy_matrix_v1.json\n"
    "          uv run --locked pytest tests/codex/test_local_main_branch_guard.py\n"
)
_VERIFIER_STEP_NAME = "      - name: Verify python-test lane topology invariants (#1760)\n"
_VERIFIER_STEP_RUN = (
    "        run: uv run --locked python scripts/ci/verify_python_test_lane.py "
    "--ci-yml .github/workflows/ci.yml\n"
)
_VERIFIER_STEP = _VERIFIER_STEP_NAME + _VERIFIER_STEP_RUN

_PYTEST_STEP = (
    "      - name: pytest python suite (parallel) (timed)\n"
    "        run: |\n"
    '          uv run --locked pytest "${_pytest_argv[@]}" -n 4\n'
)

_AGGREGATE_STEP = (
    "      - name: Evaluate python_test_bench_aggregate_policy (AC5/AC10)\n"
    "        run: |\n"
    "          uv run --locked python3 scripts/ci/evaluate_python_test_aggregate.py "
    '--core-result "${{ needs.python-test-core.result }}" '
    '--codex-result "${{ needs.codex-execpolicy.result }}" '
    '--bench-mode "${{ github.event.inputs.python_test_bench }}"\n'
)

# A minimal, valid baseline workflow satisfying every invariant. Individual tests
# mutate a copy of this baseline to introduce exactly one violation.
_BASELINE = (
    "name: ci\n"
    "jobs:\n"
    "  python-test-core:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - uses: actions/checkout@v6\n"
    + _PYTEST_STEP
    + _VERIFIER_STEP
    + "  codex-execpolicy:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - uses: actions/checkout@v6\n"
    "      - uses: actions/setup-node@v6\n"
    "        with:\n"
    '          node-version: "22"\n'
    + _SENTINEL_STEP
    + _MATRIX_STEP
    + "      - name: Upload codex execpolicy artifacts\n"
    "        if: ${{ always() }}\n"
    "        uses: actions/upload-artifact@v7\n"
    "        with:\n"
    "          name: codex-execpolicy-${{ github.run_attempt }}\n"
    "          path: codex_execpolicy_artifacts/\n"
    "  python-test:\n"
    "    runs-on: ubuntu-latest\n"
    "    needs: [python-test-core, codex-execpolicy]\n"
    "    if: always()\n"
    "    steps:\n"
    + _AGGREGATE_STEP
)


def _write(tmp_path: Path, text: str) -> Path:
    # Nest under .github/workflows/ci.yml to mirror the real repo layout -- the
    # verifier resolves repo_root and the python-test-plan.json location relative
    # to this exact nesting depth (Issue #1824 P1-2 review).
    ci_yml_dir = tmp_path / ".github" / "workflows"
    ci_yml_dir.mkdir(parents=True, exist_ok=True)
    p = ci_yml_dir / "ci.yml"
    p.write_text(text)
    return p


class TestPositiveBaseline:
    def test_baseline_passes(self, mod, tmp_path):
        path = _write(tmp_path, _BASELINE)
        report = mod.verify(path)
        assert report["ok"] is True, report["violations"]


class TestAC2NodeCodexReintroduction:
    def test_node_command_in_core_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            _PYTEST_STEP,
            _PYTEST_STEP + "      - run: node --version\n",
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("forbidden command" in v for v in report["violations"])

    def test_npm_via_bash_dash_c_indirection_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            _PYTEST_STEP,
            _PYTEST_STEP + '      - run: bash -c "npm install"\n',
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("forbidden command" in v for v in report["violations"])

    def test_codex_via_command_substitution_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            _PYTEST_STEP,
            _PYTEST_STEP + '      - run: echo "$(codex --version)"\n',
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("forbidden command" in v for v in report["violations"])

    def test_setup_node_action_in_core_is_rejected(self, mod, tmp_path):
        old = "      - uses: actions/checkout@v6\n" + _PYTEST_STEP
        new = (
            "      - uses: actions/checkout@v6\n"
            "      - uses: actions/setup-node@v6\n"
            + _PYTEST_STEP
        )
        mutated = _BASELINE.replace(old, new, 1)
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("Node-bootstrapping action" in v for v in report["violations"])

    def test_local_composite_action_hiding_node_bootstrap_is_rejected(self, mod, tmp_path):
        """P1-2 review point 4: a local composite action (e.g. a hypothetical
        ./.github/actions/setup-node-pnpm) must be resolved and scanned, not just
        the literal `uses: actions/setup-node` string."""
        old = "      - uses: actions/checkout@v6\n" + _PYTEST_STEP
        new = (
            "      - uses: actions/checkout@v6\n"
            "      - uses: ./.github/actions/setup-node-pnpm\n"
            + _PYTEST_STEP
        )
        mutated = _BASELINE.replace(old, new, 1)
        path = _write(tmp_path, mutated)
        action_dir = tmp_path / ".github" / "actions" / "setup-node-pnpm"
        action_dir.mkdir(parents=True)
        (action_dir / "action.yml").write_text(
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            "    - uses: actions/setup-node@v6\n"
        )
        report = mod.verify(path)
        assert report["ok"] is False
        assert any("local composite action" in v and "Node-bootstrapping" in v for v in report["violations"])

    def test_local_composite_action_with_clean_steps_is_accepted(self, mod, tmp_path):
        """Negative-of-negative: a local composite action that does NOT bootstrap
        Node must not be flagged (avoid over-blocking)."""
        old = "      - uses: actions/checkout@v6\n" + _PYTEST_STEP
        new = (
            "      - uses: actions/checkout@v6\n"
            "      - uses: ./.github/actions/setup-python-uv\n"
            + _PYTEST_STEP
        )
        mutated = _BASELINE.replace(old, new, 1)
        path = _write(tmp_path, mutated)
        action_dir = tmp_path / ".github" / "actions" / "setup-python-uv"
        action_dir.mkdir(parents=True)
        (action_dir / "action.yml").write_text(
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            "    - uses: astral-sh/setup-uv@v3\n"
        )
        report = mod.verify(path)
        assert report["ok"] is True, report["violations"]


class TestAC3PlanExternalPytestInjection:
    def test_unregistered_pytest_step_in_core_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            _VERIFIER_STEP_NAME,
            "      - name: sneaky extra pytest step\n"
            "        run: uv run --locked pytest tests/some_new_hardcoded_test.py\n"
            + _VERIFIER_STEP_NAME,
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("plan-external pytest injection" in v for v in report["violations"])

    def test_hardcoded_target_appended_to_existing_plan_driven_step_is_rejected(self, mod, tmp_path):
        """P1-2 review point 2: appending a literal path to the EXISTING
        plan-driven pytest invocation line must be caught, not just a whole new
        unregistered step."""
        mutated = _BASELINE.replace(
            _PYTEST_STEP,
            "      - name: pytest python suite (parallel) (timed)\n"
            "        run: |\n"
            '          uv run --locked pytest "${_pytest_argv[@]}" scripts/ci/tests/test_sneaky.py\n',
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("hard-coded target/path token" in v for v in report["violations"])


class TestAC1AC4AC5JobRemovalOrRelocation:
    def test_missing_python_test_core_job_is_rejected(self, mod, tmp_path):
        core_block = (
            "  python-test-core:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v6\n"
            + _PYTEST_STEP
            + _VERIFIER_STEP
        )
        assert core_block in _BASELINE
        mutated = _BASELINE.replace(core_block, "", 1)
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("python-test-core is missing" in v for v in report["violations"])

    def test_missing_codex_execpolicy_job_is_rejected(self, mod, tmp_path):
        lines = _BASELINE.splitlines(keepends=True)
        start = next(i for i, ln in enumerate(lines) if ln.startswith("  codex-execpolicy:"))
        end = next(i for i, ln in enumerate(lines) if i > start and ln.startswith("  python-test:"))
        mutated = "".join(lines[:start] + lines[end:])
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("codex-execpolicy is missing" in v for v in report["violations"])

    def test_aggregate_needs_relocated_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            "needs: [python-test-core, codex-execpolicy]", "needs: [python-test-core]", 1
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("jobs.python-test.needs" in v for v in report["violations"])

    def test_aggregate_if_always_removed_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            "    needs: [python-test-core, codex-execpolicy]\n    if: always()\n",
            "    needs: [python-test-core, codex-execpolicy]\n",
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("jobs.python-test.if" in v for v in report["violations"])


class TestAC6SentinelOrdering:
    def test_sentinel_after_matrix_is_rejected(self, mod, tmp_path):
        assert _SENTINEL_STEP in _BASELINE and _MATRIX_STEP in _BASELINE
        mutated = _BASELINE.replace(_SENTINEL_STEP, "", 1).replace(
            _MATRIX_STEP, _MATRIX_STEP + _SENTINEL_STEP, 1
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("BEFORE the matrix orchestrator" in v for v in report["violations"])

    def test_upload_without_always_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            "        if: ${{ always() }}\n        uses: actions/upload-artifact@v7\n",
            "        if: ${{ success() }}\n        uses: actions/upload-artifact@v7\n",
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("if: ${{ always() }}" in v for v in report["violations"])


class TestAC7VerifierDisablement:
    def test_missing_verifier_wiring_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(_VERIFIER_STEP, "", 1)
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("is not invoked anywhere" in v for v in report["violations"])

    def test_continue_on_error_disablement_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            _VERIFIER_STEP,
            _VERIFIER_STEP_NAME + "        continue-on-error: true\n" + _VERIFIER_STEP_RUN,
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("continue-on-error: true" in v for v in report["violations"])

    def test_verifier_step_with_if_condition_is_rejected(self, mod, tmp_path):
        """P1-2 review point 5: an `if:` condition on the verifier step could skip
        it while the job still reports success."""
        mutated = _BASELINE.replace(
            _VERIFIER_STEP,
            _VERIFIER_STEP_NAME + "        if: ${{ success() }}\n" + _VERIFIER_STEP_RUN,
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("its own `if:` condition" in v for v in report["violations"])

    def test_verifier_step_wrong_job_is_rejected(self, mod, tmp_path):
        """P1-2 review point 5: the verifier must be wired in python-test-core,
        not merely somewhere in the workflow."""
        mutated = _BASELINE.replace(_VERIFIER_STEP, "", 1).replace(
            "      - name: Upload codex execpolicy artifacts\n",
            _VERIFIER_STEP + "      - name: Upload codex execpolicy artifacts\n",
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("expected job 'python-test-core'" in v for v in report["violations"])

    def test_verifier_step_duplicated_is_rejected(self, mod, tmp_path):
        """P1-2 review point 5: exactly one occurrence is required."""
        mutated = _BASELINE.replace(_VERIFIER_STEP, _VERIFIER_STEP + _VERIFIER_STEP, 1)
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("EXACTLY once" in v for v in report["violations"])

    def test_verifier_step_extra_flag_is_rejected(self, mod, tmp_path):
        """P1-2 review point 1/5: substring match is insufficient -- an EXTRA flag
        appended after the expected argv must be rejected (exact argv only)."""
        mutated = _BASELINE.replace(
            _VERIFIER_STEP_RUN,
            "        run: uv run --locked python scripts/ci/verify_python_test_lane.py "
            "--ci-yml .github/workflows/ci.yml --extra-unexpected-flag\n",
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("does not match the exact expected argv" in v for v in report["violations"])


class TestAC10AggregatePolicyInvocation:
    def test_aggregate_step_replaced_with_echo_stub_is_rejected(self, mod, tmp_path):
        """The EXACT PR #1824 review finding: replacing the evaluator invocation
        with `echo python_test_bench_aggregate_policy ok` must be rejected even
        though the OLD substring-search verifier would have accepted it."""
        mutated = _BASELINE.replace(
            _AGGREGATE_STEP,
            "      - name: Evaluate python_test_bench_aggregate_policy (AC5/AC10)\n"
            "        run: echo python_test_bench_aggregate_policy ok\n",
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any(f"has no step invoking {mod.AGGREGATE_EVALUATOR_SCRIPT}" in v for v in report["violations"])

    def test_aggregate_step_missing_flag_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            _AGGREGATE_STEP,
            "      - name: Evaluate python_test_bench_aggregate_policy (AC5/AC10)\n"
            "        run: |\n"
            "          uv run --locked python3 scripts/ci/evaluate_python_test_aggregate.py "
            '--core-result "${{ needs.python-test-core.result }}"\n',
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("does not invoke" in v and "exact expected argv" in v for v in report["violations"])

    def test_aggregate_step_with_continue_on_error_is_rejected(self, mod, tmp_path):
        mutated = _BASELINE.replace(
            _AGGREGATE_STEP,
            "      - name: Evaluate python_test_bench_aggregate_policy (AC5/AC10)\n"
            "        continue-on-error: true\n"
            "        run: |\n"
            "          uv run --locked python3 scripts/ci/evaluate_python_test_aggregate.py "
            '--core-result "${{ needs.python-test-core.result }}" '
            '--codex-result "${{ needs.codex-execpolicy.result }}" '
            '--bench-mode "${{ github.event.inputs.python_test_bench }}"\n',
            1,
        )
        report = mod.verify(_write(tmp_path, mutated))
        assert report["ok"] is False
        assert any("continue-on-error: true" in v for v in report["violations"])


class TestP1_2PlanLoadedAndReferenced:
    def test_missing_plan_reference_is_rejected(self, mod, tmp_path):
        """When a real python-test-plan.json exists on disk (mirroring the repo
        layout) but python-test-core never references the loader script, that
        must be a violation."""
        ci_yml_dir = tmp_path / ".github" / "workflows"
        ci_yml_dir.mkdir(parents=True)
        plan_dir = tmp_path / ".github" / "ci"
        plan_dir.mkdir(parents=True)
        (plan_dir / "python-test-plan.json").write_text('{"targets": ["scripts/ci/tests/"]}')
        path = ci_yml_dir / "ci.yml"
        path.write_text(_BASELINE)
        report = mod.verify(path)
        assert report["ok"] is False
        assert any("does not reference scripts/ci/python_test_plan.py" in v for v in report["violations"])

    def test_empty_plan_targets_is_rejected(self, mod, tmp_path):
        ci_yml_dir = tmp_path / ".github" / "workflows"
        ci_yml_dir.mkdir(parents=True)
        plan_dir = tmp_path / ".github" / "ci"
        plan_dir.mkdir(parents=True)
        (plan_dir / "python-test-plan.json").write_text('{"targets": []}')
        mutated = _BASELINE.replace(
            _PYTEST_STEP,
            "      - name: pytest python suite (parallel) (timed)\n"
            "        run: |\n"
            '          python3 scripts/ci/python_test_plan.py --emit run-argv > /tmp/argv\n'
            '          uv run --locked pytest "${_pytest_argv[@]}" -n 4\n',
            1,
        )
        path = ci_yml_dir / "ci.yml"
        path.write_text(mutated)
        report = mod.verify(path)
        assert report["ok"] is False
        assert any("no non-empty 'targets' list" in v for v in report["violations"])

    def test_plan_referenced_and_non_empty_is_accepted(self, mod, tmp_path):
        ci_yml_dir = tmp_path / ".github" / "workflows"
        ci_yml_dir.mkdir(parents=True)
        plan_dir = tmp_path / ".github" / "ci"
        plan_dir.mkdir(parents=True)
        (plan_dir / "python-test-plan.json").write_text('{"targets": ["scripts/ci/tests/"]}')
        mutated = _BASELINE.replace(
            _PYTEST_STEP,
            "      - name: pytest python suite (parallel) (timed)\n"
            "        run: |\n"
            '          python3 scripts/ci/python_test_plan.py --emit run-argv > /tmp/argv\n'
            '          uv run --locked pytest "${_pytest_argv[@]}" -n 4\n',
            1,
        )
        path = ci_yml_dir / "ci.yml"
        path.write_text(mutated)
        report = mod.verify(path)
        assert report["ok"] is True, report["violations"]


class TestPositiveContractRealCiYml:
    """AC7 positive contract test: the verifier accepts the real, current ci.yml."""

    def test_real_ci_yml_passes(self, mod):
        report = mod.verify(_REAL_CI_YML)
        assert report["ok"] is True, report["violations"]

    def test_real_ci_yml_wires_verifier_as_exact_command(self):
        text = _REAL_CI_YML.read_text(encoding="utf-8")
        assert "scripts/ci/verify_python_test_lane.py --ci-yml .github/workflows/ci.yml" in text

    def test_real_ci_yml_has_expected_job_topology(self, mod):
        jobs = mod.load_workflow(_REAL_CI_YML)["jobs"]
        assert "python-test-core" in jobs
        assert "codex-execpolicy" in jobs
        assert "python-test" in jobs
        assert jobs["python-test"]["needs"] == ["python-test-core", "codex-execpolicy"]

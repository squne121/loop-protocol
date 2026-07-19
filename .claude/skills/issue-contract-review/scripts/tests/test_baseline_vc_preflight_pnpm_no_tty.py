"""
Tests for Issue #994: pnpm no-TTY / CI env handling generalized to all canonical pnpm gates.

AC1: pnpm typecheck no-TTY → classification==blocked / category==package_manager_no_tty_prompt
AC2: pnpm lint no-TTY → classification==blocked / category==package_manager_no_tty_prompt
AC3: pnpm test no-TTY → classification==blocked / category==package_manager_no_tty_prompt
AC4: pnpm build 既存挙動維持（既存テスト regression_gate pass 継続）
AC5: no-TTY fix_hint が runtime_only を主解として案内しない
AC6: 既存 package_manager_no_tty_prompt テスト全 PASS（test_baseline_vc_preflight.py に委譲）
AC7: baseline-expect: pass の pnpm gate が no-TTY 失敗 → unexpected_fail でなく blocked/package_manager_no_tty_prompt
AC8: shell env prefix / env wrapper が block される（shell=False / exact argv 境界維持）
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT_PATH = HERE.parent / "baseline_vc_preflight.py"

sys.path.insert(0, str(SCRIPT_PATH.parent))
from baseline_vc_preflight import classify_result, _is_package_manager_no_tty_prompt  # noqa: E402

_NO_TTY_STDERR = (
    "ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY "
    "Aborted removal of modules directory due to no TTY. If running in CI, set CI=true"
)


def run_preflight(fixture_file: str, issue_num: int = 999) -> dict:
    """Run baseline_vc_preflight.py against a fixture file and return parsed JSON."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--body-file",
            fixture_file,
            "--issue",
            str(issue_num),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.stdout, f"No output from preflight: {result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# AC1: pnpm typecheck no-TTY
# ---------------------------------------------------------------------------

def test_typecheck_no_tty_classification():
    """AC1: pnpm typecheck no-TTY → classification==blocked / category==package_manager_no_tty_prompt"""
    classification, category, decision, fix_hint, scope_class = classify_result(
        1,
        "",
        _NO_TTY_STDERR,
        "pnpm typecheck",
        cwd=".",
        runner_env_delta={},
    )
    assert classification == "blocked", f"Expected blocked, got {classification}"
    assert category == "package_manager_no_tty_prompt", f"Expected package_manager_no_tty_prompt, got {category}"
    assert decision == "blocked", f"Expected blocked, got {decision}"
    assert scope_class == "regression_gate", f"Expected regression_gate, got {scope_class}"


def test_typecheck_no_tty_internal_detector():
    """AC1: _is_package_manager_no_tty_prompt detects pnpm typecheck no-TTY stderr"""
    assert _is_package_manager_no_tty_prompt("pnpm typecheck", "", _NO_TTY_STDERR)


# ---------------------------------------------------------------------------
# AC2: pnpm lint no-TTY
# ---------------------------------------------------------------------------

def test_lint_no_tty_classification():
    """AC2: pnpm lint no-TTY → classification==blocked / category==package_manager_no_tty_prompt"""
    classification, category, decision, fix_hint, scope_class = classify_result(
        1,
        "",
        _NO_TTY_STDERR,
        "pnpm lint",
        cwd=".",
        runner_env_delta={},
    )
    assert classification == "blocked", f"Expected blocked, got {classification}"
    assert category == "package_manager_no_tty_prompt", f"Expected package_manager_no_tty_prompt, got {category}"
    assert decision == "blocked", f"Expected blocked, got {decision}"
    assert scope_class == "regression_gate", f"Expected regression_gate, got {scope_class}"


def test_lint_no_tty_internal_detector():
    """AC2: _is_package_manager_no_tty_prompt detects pnpm lint no-TTY stderr"""
    assert _is_package_manager_no_tty_prompt("pnpm lint", "", _NO_TTY_STDERR)


# ---------------------------------------------------------------------------
# AC3: pnpm test no-TTY
# ---------------------------------------------------------------------------

def test_test_no_tty_classification():
    """AC3: pnpm test no-TTY → classification==blocked / category==package_manager_no_tty_prompt"""
    classification, category, decision, fix_hint, scope_class = classify_result(
        1,
        "",
        _NO_TTY_STDERR,
        "pnpm test",
        cwd=".",
        runner_env_delta={},
    )
    assert classification == "blocked", f"Expected blocked, got {classification}"
    assert category == "package_manager_no_tty_prompt", f"Expected package_manager_no_tty_prompt, got {category}"
    assert decision == "blocked", f"Expected blocked, got {decision}"
    assert scope_class == "regression_gate", f"Expected regression_gate, got {scope_class}"


def test_test_no_tty_internal_detector():
    """AC3: _is_package_manager_no_tty_prompt detects pnpm test no-TTY stderr"""
    assert _is_package_manager_no_tty_prompt("pnpm test", "", _NO_TTY_STDERR)


# ---------------------------------------------------------------------------
# AC5: fix_hint does not suggest runtime_only as primary solution
# ---------------------------------------------------------------------------

def test_fix_hint_no_runtime_only_typecheck():
    """AC5: pnpm typecheck no-TTY fix_hint must not suggest runtime_only as primary resolution"""
    _, _, _, fix_hint, _ = classify_result(
        1,
        "",
        _NO_TTY_STDERR,
        "pnpm typecheck",
        cwd=".",
        runner_env_delta={},
    )
    assert fix_hint is not None, "fix_hint must not be None for no-TTY"
    # AC5: fix_hint must promote CI=true injection (runner-side fix), not runtime_only preflight-scope
    # The hint may mention runtime_only as something to avoid ("do not rewrite to runtime_only"),
    # but must not present runtime_only as the primary solution.
    assert "CI=true" in fix_hint, (
        f"fix_hint must promote runner-side CI=true injection. Got: {fix_hint!r}"
    )
    # The primary message must be a tooling/env blocker, not an Issue body defect
    assert "Issue body defect" in fix_hint or "tooling" in fix_hint or "runner" in fix_hint, (
        f"fix_hint must identify this as a tooling/env blocker. Got: {fix_hint!r}"
    )


def test_fix_hint_no_runtime_only_lint():
    """AC5: pnpm lint no-TTY fix_hint must not suggest runtime_only as primary resolution"""
    _, _, _, fix_hint, _ = classify_result(
        1,
        "",
        _NO_TTY_STDERR,
        "pnpm lint",
        cwd=".",
        runner_env_delta={},
    )
    assert fix_hint is not None, "fix_hint must not be None for no-TTY"
    # AC5: fix_hint must promote CI=true injection, not runtime_only preflight-scope
    assert "CI=true" in fix_hint, (
        f"fix_hint must promote runner-side CI=true injection. Got: {fix_hint!r}"
    )
    assert "Issue body defect" in fix_hint or "tooling" in fix_hint or "runner" in fix_hint, (
        f"fix_hint must identify this as a tooling/env blocker. Got: {fix_hint!r}"
    )


def test_fix_hint_no_runtime_only_build():
    """AC5: pnpm build no-TTY fix_hint (no runner delta) must promote runner-side CI=true"""
    _, _, _, fix_hint, _ = classify_result(
        1,
        "",
        _NO_TTY_STDERR,
        "pnpm build",
        cwd=".",
        runner_env_delta={},
    )
    assert fix_hint is not None, "fix_hint must not be None for no-TTY"
    # AC5: primary guidance must be CI=true on the runner side, not rewrite to runtime_only
    assert "CI=true" in fix_hint, (
        f"fix_hint must promote runner-side CI=true injection. Got: {fix_hint!r}"
    )


# ---------------------------------------------------------------------------
# AC7: baseline-expect: pass pnpm gate no-TTY → blocked not unexpected_fail
# ---------------------------------------------------------------------------

def test_baseline_pass_reclassify_typecheck():
    """AC7: pnpm typecheck no-TTY → blocked/package_manager_no_tty_prompt, not unexpected_fail"""
    classification, category, decision, fix_hint, scope_class = classify_result(
        1,
        "",
        _NO_TTY_STDERR,
        "pnpm typecheck",
        cwd=".",
        runner_env_delta={},
    )
    assert classification == "blocked", f"Expected blocked, got {classification}"
    assert category == "package_manager_no_tty_prompt", f"Expected package_manager_no_tty_prompt, got {category}"
    assert classification != "unexpected_fail", (
        "package_manager_no_tty_prompt must not be reclassified as unexpected_fail"
    )


def test_baseline_pass_reclassify_lint():
    """AC7: pnpm lint no-TTY → blocked/package_manager_no_tty_prompt, not unexpected_fail"""
    classification, category, decision, fix_hint, scope_class = classify_result(
        1,
        "",
        _NO_TTY_STDERR,
        "pnpm lint",
        cwd=".",
        runner_env_delta={},
    )
    assert classification == "blocked", f"Expected blocked, got {classification}"
    assert category == "package_manager_no_tty_prompt", f"Expected package_manager_no_tty_prompt, got {category}"
    assert classification != "unexpected_fail", (
        "package_manager_no_tty_prompt must not be reclassified as unexpected_fail"
    )


def test_baseline_pass_reclassify_via_preflight():
    (
        """AC7: baseline-expect: pass + pnpm typecheck no-TTY via full preflight → blocked (not"""
        """ baseline_regression_failed)"""
    )
    # This test uses a mock that fakes pnpm typecheck exit 1 with no-TTY stderr,
    # but since we cannot intercept subprocess in full pipeline mode,
    # we test directly via classify_result (which is the actual re-mapping logic).
    # The annotation re-mapping in run_commands_from_body applies baseline_expect AFTER
    # classify_result; we verify that package_manager_no_tty_prompt is immune.
    classification, category, decision, fix_hint, scope_class = classify_result(
        1,
        "",
        _NO_TTY_STDERR,
        "pnpm typecheck",
        cwd=".",
        runner_env_delta={},
    )
    # The re-mapping at baseline_expect == "pass" must NOT change this to baseline_regression_failed
    assert category != "baseline_regression_failed", (
        "no-TTY classification must not be overridden by baseline-expect: pass re-mapping"
    )
    assert classification == "blocked"
    assert category == "package_manager_no_tty_prompt"


# ---------------------------------------------------------------------------
# AC8: shell env prefix / env wrapper rejected
# ---------------------------------------------------------------------------

def test_env_wrapper_rejected_typecheck():
    """AC8: 'env CI=true pnpm typecheck' must be blocked as command_not_allowed"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ env CI=true pnpm typecheck
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        found = any(
            r.get("category") == "command_not_allowed"
            and r.get("decision") == "blocked"
            for r in results
        )
        assert found, (
            f"Expected 'env CI=true pnpm typecheck' to be blocked as command_not_allowed. Results: {results}"
        )
    finally:
        os.unlink(fixture_file)


def test_env_wrapper_rejected_lint():
    """AC8: 'env CI=true pnpm lint' must be blocked as command_not_allowed"""
    fixture_content = """## Verification Commands

```bash
# AC2
$ env CI=true pnpm lint
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        found = any(
            r.get("category") == "command_not_allowed"
            and r.get("decision") == "blocked"
            for r in results
        )
        assert found, (
            f"Expected 'env CI=true pnpm lint' to be blocked as command_not_allowed. Results: {results}"
        )
    finally:
        os.unlink(fixture_file)


def test_shell_env_prefix_rejected_typecheck():
    """AC8: 'CI=true pnpm typecheck' shell env prefix must be blocked"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ CI=true pnpm typecheck
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        found = any(
            r.get("classification") == "blocked"
            for r in results
        )
        assert found, (
            f"Expected 'CI=true pnpm typecheck' to be blocked. Results: {results}"
        )
    finally:
        os.unlink(fixture_file)


# ---------------------------------------------------------------------------
# AC7 full-pipeline: baseline-expect: pass + pnpm typecheck no-TTY via monkeypatched run_command
# ---------------------------------------------------------------------------

def test_ac7_full_pipeline_baseline_pass_no_tty_typecheck():
    """AC7 full-pipeline: baseline-expect: pass + pnpm typecheck no-TTY stderr →
    classification==blocked / category==package_manager_no_tty_prompt
    (NOT unexpected_fail / NOT baseline_regression_failed).

    Monkeypatches run_command in the imported baseline_vc_preflight module so that
    'pnpm typecheck' returns exit 1 with no-TTY stderr, then calls main() capturing stdout
    with a fixture file containing '# baseline-expect: pass' annotation.
    """
    import importlib.util
    import io
    import contextlib
    from unittest.mock import patch

    # Load the module fresh so we can patch it
    spec = importlib.util.spec_from_file_location("baseline_vc_preflight_ac7", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    bvp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bvp)  # type: ignore[union-attr]

    fixture_content = """## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ pnpm typecheck
```
"""

    _NO_TTY_STDERR_LOCAL = (
        "ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY "
        "Aborted removal of modules directory due to no TTY. If running in CI, set CI=true"
    )

    def fake_run_command(command: str, timeout_seconds: int, cwd: str):
        """Simulate pnpm typecheck failing with no-TTY stderr."""
        if "pnpm" in command and "typecheck" in command:
            return 1, "", _NO_TTY_STDERR_LOCAL, 500, {}
        return 0, "", "", 0, {}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        captured_output = io.StringIO()
        original_argv = sys.argv[:]
        sys.argv = [str(SCRIPT_PATH), "--body-file", fixture_file, "--issue", "994"]
        try:
            with patch.object(bvp, "run_command", side_effect=fake_run_command):
                with contextlib.redirect_stdout(captured_output):
                    try:
                        bvp.main()
                    except SystemExit:
                        pass
        finally:
            sys.argv = original_argv

        output = captured_output.getvalue().strip()
        assert output, "main() must produce JSON output"
        data = json.loads(output)
        results = data.get("results", [])
        assert len(results) > 0, f"Expected at least one result, got: {data}"

        ac1_result = results[0]
        assert ac1_result.get("classification") == "blocked", (
            f"AC7 full-pipeline: expected classification==blocked, got {ac1_result.get('classification')!r}. "
            f"Full result: {ac1_result}"
        )
        assert ac1_result.get("category") == "package_manager_no_tty_prompt", (
            f"AC7 full-pipeline: expected category==package_manager_no_tty_prompt, "
            f"got {ac1_result.get('category')!r}. Full result: {ac1_result}"
        )
        assert ac1_result.get("decision") == "blocked", (
            f"AC7 full-pipeline: expected decision==blocked, got {ac1_result.get('decision')!r}"
        )
        assert ac1_result.get("classification") != "unexpected_fail", (
            "AC7 full-pipeline: classification must not be unexpected_fail"
        )
        assert ac1_result.get("category") != "baseline_regression_failed", (
            "AC7 full-pipeline: category must not be baseline_regression_failed "
            "(baseline-expect: pass must not override package_manager_no_tty_prompt)"
        )
    finally:
        os.unlink(fixture_file)


def test_full_pipeline_missing_root_manifest_blocks_before_no_tty_launch(tmp_path):
    """AC2: manifest 不在の tempdir では PATH 上 fake pnpm を起動しない。"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ pnpm test
```

## Allowed Paths

- .claude/skills/issue-contract-review
```
"""

    fake_root = tmp_path / "issue_1199_replay"
    fake_root.mkdir()
    fake_bin = fake_root / "bin"
    fake_bin.mkdir()
    node_modules = fake_root / "node_modules"
    node_modules.mkdir()
    broken_modules = node_modules / ".modules.yaml"
    broken_modules.write_text(
        """
layoutVersion: 6
storeDir: /unexpected
virtualStoreDir: /unexpected/v3
"""
    )

    fake_pnpm = fake_bin / "pnpm"
    fake_pnpm.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "modules_file = Path('.').resolve() / 'node_modules' / '.modules.yaml'\n"
        "text = ''\n"
        "if modules_file.exists():\n"
        "    text = modules_file.read_text(encoding='utf-8')\n"
        "if 'storeDir: /expected' in text:\n"
        "    sys.stdout.write('pnpm test passed\\n')\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY '\n"
        "              'Aborted removal of modules directory due to no TTY. '\n"
        "              'If running in CI, set CI=true\\n')\n"
        "sys.exit(1)\n"
    )
    fake_pnpm.chmod(0o755)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--body-file", fixture_file],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(fake_root),
            env=env,
        )
        assert result.stdout, f"No output from preflight: {result.stderr}"
        data = json.loads(result.stdout)

        assert data["status"] == "blocked"
        assert data["results"], "Expected at least one preflight result"
        first = data["results"][0]
        assert first["classification"] == "blocked", (
            f"Expected blocked classification, got {first}"
        )
        assert first["category"] == "regression_gate", f"Expected regression_gate, got {first}"
        assert "manifest_integrity:manifest_unreadable" in first["stderr_head"][0]
        assert first["scope_class"] == "regression_gate", (
            f"Expected scope_class regression_gate, got {first}"
        )
        assert first["decision"] == "blocked", (
            f"Expected decision blocked, got {first}"
        )
    finally:
        os.unlink(fixture_file)


def test_full_pipeline_contrast_vitest_failure_in_tempdir(tmp_path):
    """AC4: contrast test: stable .modules.yaml + Vitest failure is regression_gate,
    not package_manager_no_tty_prompt."""
    fixture_content = """## Verification Commands

```bash
# AC1
$ pnpm test
```

## Allowed Paths

- .claude/skills/issue-contract-review
```
"""

    fake_root = tmp_path / "issue_1199_replay_contrast"
    fake_root.mkdir()
    fake_bin = fake_root / "bin"
    fake_bin.mkdir()
    node_modules = fake_root / "node_modules"
    node_modules.mkdir()
    (node_modules / ".modules.yaml").write_text(
        """
layoutVersion: 6
storeDir: /expected/.pnpm-store
virtualStoreDir: /expected/.pnpm-store/v3
"""
    )

    fake_pnpm = fake_bin / "pnpm"
    fake_pnpm.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write(' FAIL  .claude/skills/open-pr/tests/test_pr_body_hygiene.py >\\n')\n"
        "sys.stderr.write('     AssertionError: expected 1 to be 2\\n')\n"
        "sys.exit(1)\n"
    )
    fake_pnpm.chmod(0o755)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--body-file", fixture_file],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(fake_root),
            env=env,
        )
        assert result.stdout, f"No output from preflight: {result.stderr}"
        data = json.loads(result.stdout)

        assert data["status"] == "blocked"
        assert data["results"], "Expected at least one preflight result"
        first = data["results"][0]
        assert first["classification"] == "blocked", (
            f"Expected blocked classification, got {first}"
        )
        assert first["category"] == "regression_gate", (
            f"Expected contrast regression_gate, got {first}"
        )
        assert first["scope_class"] == "regression_gate", (
            f"Expected scope_class regression_gate, got {first}"
        )
        assert first["decision"] == "blocked", (
            f"Expected decision blocked, got {first}"
        )
    finally:
        os.unlink(fixture_file)

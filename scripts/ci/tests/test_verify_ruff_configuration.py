"""Fixture-driven tests for the Ruff configuration authority verifier."""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

_CHECKER_DIR = Path(__file__).resolve().parents[1]
if str(_CHECKER_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKER_DIR))

import verify_ruff_configuration as checker  # noqa: E402


def _write_fixture(
    root: Path,
    *,
    pyproject: str | None = None,
    ruff_command: str | None = None,
    skill_command: str | None = None,
) -> Path:
    """Create the smallest repository fixture accepted by the verifier."""
    (root / ".github/workflows").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        pyproject
        or """
[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ["E", "F"]
""".lstrip(),
        encoding="utf-8",
    )
    (root / ".github/workflows/ci.yml").write_text(
        """
jobs:
  python-test:
    steps:
      - name: Verify Ruff configuration authority
        run: uv run --locked python3 scripts/ci/verify_ruff_configuration.py --root .
      - name: Ruff check (timed)
        run: |
          run_timed ruff_check %s
""".lstrip()
        % (
            ruff_command
            or "uv run --locked ruff check .claude/scripts scripts schemas .claude/skills"
        ),
        encoding="utf-8",
    )
    skill_path = root / ".claude/skills/ci-test-performance/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        (skill_command or "uv run --locked ruff check .claude/scripts scripts schemas .claude/skills")
        + "\n",
        encoding="utf-8",
    )
    return root


def _failure_keys(root: Path) -> set[str]:
    return {violation.key for violation in checker.collect_violations(root)}


def _run_cli(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_CHECKER_DIR / "verify_ruff_configuration.py"), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_linted_python_file(root: Path, relative_path: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("value = 1\nimport os\n", encoding="utf-8")


def test_given_valid_authorities_when_verified_then_no_failure_keys(tmp_path: Path):
    assert _failure_keys(_write_fixture(tmp_path)) == set()


def test_given_missing_select_when_verified_then_rejects_configuration_authority(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path, pyproject="[tool.ruff]\nline-length = 120\n")

    assert "ruff_select_not_e_f" in _failure_keys(fixture)


def test_rejects_workflow_cli_rule_override(tmp_path: Path):
    fixture = _write_fixture(
        tmp_path,
        ruff_command=(
            "uv run --locked ruff check --select E,F "
            ".claude/scripts scripts schemas .claude/skills"
        ),
    )

    assert "workflow_cli_rule_override" in _failure_keys(fixture)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--select", "E"),
        ("--extend-select", "I"),
        ("--ignore", "E402"),
        ("--extend-ignore", "E402"),
        ("--per-file-ignores", "*.py:E402"),
        ("--extend-per-file-ignores", "*.py:E402"),
        ("--config", "lint.select=['ALL']"),
        ("--isolated", None),
        ("--preview", None),
    ],
)
def test_given_each_rule_override_when_verified_then_rejects_workflow_authority_override(
    tmp_path: Path, option: str, value: str | None
):
    override = option if value is None else f"{option} {value}"
    fixture = _write_fixture(
        tmp_path,
        ruff_command=(
            f"uv run --locked ruff check {override} "
            ".claude/scripts scripts schemas .claude/skills"
        ),
    )

    assert "workflow_cli_rule_override" in _failure_keys(fixture)


def test_given_config_override_when_verified_then_rejects_workflow_authority_override(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        ruff_command=(
            "uv run --locked ruff check --config 'lint.select=[\"ALL\"]' "
            ".claude/scripts scripts schemas .claude/skills"
        ),
    )

    assert "workflow_cli_rule_override" in _failure_keys(fixture)


def test_given_equals_form_config_override_when_verified_then_rejects_workflow_authority_override(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        ruff_command=(
            "uv run --locked ruff check --config=lint.select='[\"ALL\"]' "
            ".claude/scripts scripts schemas .claude/skills"
        ),
    )

    assert "workflow_cli_rule_override" in _failure_keys(fixture)


def test_given_isolated_when_verified_then_rejects_workflow_authority_override(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        ruff_command=(
            "uv run --locked ruff check --isolated "
            ".claude/scripts scripts schemas .claude/skills"
        ),
    )

    assert "workflow_cli_rule_override" in _failure_keys(fixture)


def test_rejects_global_e402_ignore(tmp_path: Path):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff.lint]
select = ["E", "F"]
ignore = ["E402"]
""".lstrip(),
    )

    assert "ruff_global_e402_ignore" in _failure_keys(fixture)


def test_rejects_changed_target_paths(tmp_path: Path):
    fixture = _write_fixture(
        tmp_path,
        ruff_command="uv run --locked ruff check .claude/scripts scripts schemas",
    )

    assert "workflow_ruff_target_paths_changed" in _failure_keys(fixture)


def test_given_reordered_target_paths_when_verified_then_accepts_target_set(tmp_path: Path):
    fixture = _write_fixture(
        tmp_path,
        ruff_command="uv run --locked ruff check scripts schemas .claude/scripts .claude/skills",
    )

    assert _failure_keys(fixture) == set()


def test_given_duplicate_target_path_when_verified_then_rejects_changed_target_set(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        ruff_command=(
            "uv run --locked ruff check .claude/scripts scripts scripts schemas .claude/skills"
        ),
    )

    assert "workflow_ruff_target_paths_changed" in _failure_keys(fixture)


def test_given_reordered_select_when_verified_then_accepts_rule_set(tmp_path: Path):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff.lint]
select = ["F", "E"]
""".lstrip(),
    )

    assert _failure_keys(fixture) == set()


def test_given_wildcard_e402_exception_when_verified_then_rejects_exception_scope(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff.lint]
select = ["E", "F"]
per-file-ignores = { "**/*.py" = ["E402"] }
""".lstrip(),
    )

    assert "ruff_per_file_e402_exception_invalid" in _failure_keys(fixture)


def test_given_non_e402_exception_when_verified_then_rejects_exception_scope(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff.lint]
select = ["E", "F"]
per-file-ignores = { "scripts/example.py" = ["E402", "F401"] }
""".lstrip(),
    )

    assert "ruff_per_file_e402_exception_invalid" in _failure_keys(fixture)


def test_given_missing_e402_exception_file_when_verified_then_rejects_exception_scope(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff.lint]
select = ["E", "F"]
per-file-ignores = { "scripts/missing.py" = ["E402"] }
""".lstrip(),
    )

    assert "ruff_per_file_e402_exception_invalid" in _failure_keys(fixture)


def test_given_non_python_e402_exception_file_when_verified_then_rejects_exception_scope(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff.lint]
select = ["E", "F"]
per-file-ignores = { "scripts/example.md" = ["E402"] }
""".lstrip(),
    )
    (fixture / "scripts").mkdir()
    (fixture / "scripts/example.md").write_text("# fixture\n", encoding="utf-8")

    assert "ruff_per_file_e402_exception_invalid" in _failure_keys(fixture)


def test_given_outside_target_e402_exception_file_when_verified_then_rejects_exception_scope(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff.lint]
select = ["E", "F"]
per-file-ignores = { "tests/example.py" = ["E402"] }
""".lstrip(),
    )
    _write_linted_python_file(fixture, "tests/example.py")

    assert "ruff_per_file_e402_exception_invalid" in _failure_keys(fixture)


@pytest.mark.parametrize("relative_path", ["scripts/example.py", "scripts/example.pyi"])
def test_given_existing_target_python_e402_exception_when_verified_then_accepts_exception(
    tmp_path: Path, relative_path: str, monkeypatch: pytest.MonkeyPatch
):
    fixture = _write_fixture(
        tmp_path,
        pyproject=f"""
[tool.ruff.lint]
select = ["E", "F"]
per-file-ignores = {{ "{relative_path}" = ["E402"] }}
""".lstrip(),
    )
    _write_linted_python_file(fixture, relative_path)
    monkeypatch.setattr(checker, "_probe_e402_diagnostic", lambda _root, _path: (True, "E402 diagnostic observed"))

    assert _failure_keys(fixture) == set()


def test_given_e402_exception_without_isolated_diagnostic_when_verified_then_rejects_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff.lint]
select = ["E", "F"]
per-file-ignores = { "scripts/example.py" = ["E402"] }
""".lstrip(),
    )
    _write_linted_python_file(fixture, "scripts/example.py")
    monkeypatch.setattr(checker, "_probe_e402_diagnostic", lambda _root, _path: (False, "timeout"))

    assert "ruff_per_file_e402_diagnostic_missing" in _failure_keys(fixture)


def test_given_e402_probe_timeout_when_probed_then_reports_no_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def _timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="ruff", timeout=checker.E402_PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(checker.subprocess, "run", _timeout)

    observed, detail = checker._probe_e402_diagnostic(tmp_path, "scripts/example.py")

    assert not observed
    assert "failed" in detail


def test_given_current_e402_exception_when_isolated_probe_runs_then_observes_actual_diagnostic():
    root = Path(__file__).resolve().parents[3]

    observed, detail = checker._probe_e402_diagnostic(
        root,
        ".claude/skills/create-issue/scripts/validate_issue_body.py",
    )

    assert observed, detail


def test_given_additional_workflow_ruff_invocation_when_verified_then_rejects_all_workflow_invocations(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path)
    workflow = fixture / ".github/workflows/ci.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8")
        + """
  extra:
    steps:
      - name: Extra Ruff
        run: uv run --locked ruff check --select E .
""",
        encoding="utf-8",
    )

    assert "workflow_ruff_invocation_count_invalid" in _failure_keys(fixture)


def test_given_missing_ci_root_verifier_when_verified_then_rejects_workflow(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path)
    workflow = fixture / ".github/workflows/ci.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "      - name: Verify Ruff configuration authority\n"
            "        run: uv run --locked python3 scripts/ci/verify_ruff_configuration.py --root .\n",
            "",
        ),
        encoding="utf-8",
    )

    assert "workflow_ruff_verifier_root_step_invalid" in _failure_keys(fixture)


def test_given_verifier_after_canonical_ruff_when_verified_then_rejects_order(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path)
    workflow = fixture / ".github/workflows/ci.yml"
    verifier = (
        "      - name: Verify Ruff configuration authority\n"
        "        run: uv run --locked python3 "
        "scripts/ci/verify_ruff_configuration.py --root .\n"
    )
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(verifier, "") + verifier,
        encoding="utf-8",
    )

    assert "workflow_ruff_verifier_order_invalid" in _failure_keys(fixture)


@pytest.mark.parametrize(
    "ruff_command",
    [
        "bash -c 'uv run --locked ruff check .claude/scripts scripts schemas .claude/skills'",
        "bash -c 'uv run --locked ruff check --select E,F .claude/scripts scripts schemas .claude/skills'",
        "$RUFF_COMMAND",
        "uv run --locked ruff check @ruff-args.txt",
        "uv run --locked ruff check .claude/scripts scripts schemas .claude/skills && true",
    ],
)
def test_given_ruff_wrapper_or_indirection_when_verified_then_rejects_it(
    tmp_path: Path, ruff_command: str
):
    keys = _failure_keys(_write_fixture(tmp_path, ruff_command=ruff_command))

    assert "workflow_ruff_indirection_not_allowed" in keys


def test_given_shell_wrapped_verifier_when_verified_then_rejects_indirection(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path)
    workflow = fixture / ".github/workflows/ci.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "run: uv run --locked python3 scripts/ci/verify_ruff_configuration.py --root .",
            "run: bash -c 'uv run --locked python3 scripts/ci/verify_ruff_configuration.py --root .'",
        ),
        encoding="utf-8",
    )

    keys = _failure_keys(fixture)
    assert "workflow_ruff_verifier_root_step_invalid" in keys
    assert "workflow_ruff_verifier_indirection_not_allowed" in keys


def test_given_extend_select_when_verified_then_rejects_unresolved_rule_configuration(
    tmp_path: Path,
):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff.lint]
select = ["E", "F"]
extend-select = ["I"]
""".lstrip(),
    )

    assert "ruff_rule_configuration_not_resolved" in _failure_keys(fixture)


@pytest.mark.parametrize(
    "relative_path",
    ["ruff.toml", ".ruff.toml", "scripts/ruff.toml", "scripts/.ruff.toml"],
)
def test_given_secondary_ruff_configuration_when_verified_then_rejects_split_authority(
    tmp_path: Path, relative_path: str
):
    fixture = _write_fixture(tmp_path)
    configuration = fixture / relative_path
    configuration.parent.mkdir(parents=True, exist_ok=True)
    configuration.write_text("[lint]\nselect = [\"F\"]\n", encoding="utf-8")

    assert "ruff_configuration_source_not_pyproject" in _failure_keys(fixture)


def test_given_nested_pyproject_when_verified_then_rejects_split_authority(tmp_path: Path):
    fixture = _write_fixture(tmp_path)
    (fixture / "scripts").mkdir()
    (fixture / "scripts/pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")

    assert "ruff_configuration_source_not_pyproject" in _failure_keys(fixture)


def test_given_nested_pyproject_without_ruff_when_verified_then_allows_project_metadata(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path)
    (fixture / "scripts").mkdir()
    (fixture / "scripts/pyproject.toml").write_text(
        "[project]\nname = 'metadata-only'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )

    assert _failure_keys(fixture) == set()


def test_given_invalid_nested_pyproject_when_verified_then_allows_non_discoverable_file(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path)
    (fixture / "scripts").mkdir()
    (fixture / "scripts/pyproject.toml").write_text("[tool.ruff\n", encoding="utf-8")

    assert _failure_keys(fixture) == set()


def test_given_ruff_nested_pyproject_outside_lint_targets_when_verified_then_allows_it(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path)
    (fixture / "tests").mkdir()
    (fixture / "tests/pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")

    assert _failure_keys(fixture) == set()


def test_given_same_directory_ruff_toml_when_verified_then_rejects_priority_override(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path)
    (fixture / "ruff.toml").write_text("[lint]\nselect = ['E', 'F']\n", encoding="utf-8")

    assert "ruff_configuration_source_not_pyproject" in _failure_keys(fixture)


@pytest.mark.parametrize(
    "pyproject",
    [
        "[tool.ruff]\npreview = true\n\n[tool.ruff.lint]\nselect = ['E', 'F']\n",
        "[tool.ruff.lint]\nselect = ['E', 'F']\npreview = true\n",
    ],
)
def test_given_config_side_preview_when_verified_then_rejects_preview_authority(
    tmp_path: Path, pyproject: str
):
    keys = _failure_keys(_write_fixture(tmp_path, pyproject=pyproject))

    assert "ruff_config_preview_not_allowed" in keys


def test_given_extend_when_verified_then_rejects_split_authority(tmp_path: Path):
    fixture = _write_fixture(
        tmp_path,
        pyproject="""
[tool.ruff]
extend = "ruff-base.toml"

[tool.ruff.lint]
select = ["E", "F"]
""".lstrip(),
    )

    assert "ruff_config_extend_not_allowed" in _failure_keys(fixture)


def test_rejects_skill_cli_rule_override(tmp_path: Path):
    fixture = _write_fixture(
        tmp_path,
        skill_command=(
            "uv run --locked ruff check --select E,F "
            ".claude/scripts scripts schemas .claude/skills"
        ),
    )

    assert "ci_test_performance_skill_cli_rule_override" in _failure_keys(fixture)


def test_given_configuration_violation_when_cli_runs_then_emits_failure_key_and_nonzero(
    tmp_path: Path,
):
    fixture = _write_fixture(tmp_path)
    (fixture / ".ruff.toml").write_text("[lint]\nselect = [\"F\"]\n", encoding="utf-8")

    result = _run_cli(fixture)

    assert result.returncode == 1
    assert "FAILURE_KEY: ruff_configuration_source_not_pyproject" in result.stdout


def test_given_valid_fixture_when_cli_runs_then_reports_pass(tmp_path: Path):
    result = _run_cli(_write_fixture(tmp_path))

    assert result.returncode == 0
    assert result.stdout == "Ruff configuration contract: PASS\n"


def test_given_config_override_when_cli_runs_then_emits_failure_key_and_nonzero(
    tmp_path: Path,
):
    result = _run_cli(
        _write_fixture(
            tmp_path,
            ruff_command=(
                "uv run --locked ruff check --config=lint.select='[\"ALL\"]' "
                ".claude/scripts scripts schemas .claude/skills"
            ),
        )
    )

    assert result.returncode == 1
    assert "FAILURE_KEY: workflow_cli_rule_override" in result.stdout

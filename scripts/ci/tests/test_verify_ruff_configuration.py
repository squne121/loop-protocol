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
    path.write_text("value = 1\n", encoding="utf-8")


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
    tmp_path: Path, relative_path: str
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

    assert _failure_keys(fixture) == set()


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

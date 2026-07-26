"""Fixture-driven tests for the Ruff configuration authority verifier."""

from __future__ import annotations

import sys
from pathlib import Path

_CHECKER_DIR = Path(__file__).resolve().parents[1]
if str(_CHECKER_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKER_DIR))

import verify_ruff_configuration as checker  # noqa: E402


def _write_fixture(
    root: Path,
    *,
    pyproject: str | None = None,
    ruff_command: str | None = None,
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
    return root


def _failure_keys(root: Path) -> set[str]:
    return {violation.key for violation in checker.collect_violations(root)}


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

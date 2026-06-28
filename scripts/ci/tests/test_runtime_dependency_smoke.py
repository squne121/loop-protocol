"""tests/ci/tests/test_runtime_dependency_smoke.py

pytest tests for Issue #1192: uv runtime dependency partition と isolated smoke.

AC1  project_dependencies_partition  — pyyaml/jsonschema が [project].dependencies に、dev group にない
AC2  dev_group_tools_remain          — pytest/pytest-xdist/ruff が dev group に残る
AC5  parse_machine_readable_contract — smoke の mrc_contract_parser behavioral check
AC6  command_shape                   — smoke コマンドのフラグ検証
AC7  invalid_partition_fixture       — baseline fail: 不正 partition で partition check が失敗する
     stale_lock_fixture              — baseline fail: lock file なしで uv lock --check が失敗する
AC8  ci_wiring_runtime_only          — ci.yml に uv lock --check と smoke ステップがある
AC9  no_build_system_added           — [build-system] が追加されていない
AC10 ci_test_performance_decision_fields — test-lane-policy.md に CI_TEST_PERFORMANCE_DECISION_V1 がある
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
CI_YML_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TEST_LANE_POLICY_PATH = REPO_ROOT / "docs" / "dev" / "test-lane-policy.md"

# Canonical smoke command (AC4 / AC6)
CANONICAL_SMOKE_CMD = (
    "uv run --isolated --locked --no-default-groups"
    " python scripts/ci/runtime_dependency_smoke.py"
)

# Required flags in canonical command (AC6)
REQUIRED_FLAGS = ["--isolated", "--locked", "--no-default-groups"]

# Forbidden flags / tokens in canonical command (AC6)
FORBIDDEN_FLAGS = ["--group dev", "--with pyyaml", "--with jsonschema"]



def _deps_by_name(deps: list[str]) -> dict[str, str]:
    """Return {name: specifier} mapping for a list of PEP 508 dependency strings."""
    result: dict[str, str] = {}
    for dep in deps:
        normalized = dep.split(";")[0].strip()
        for op in (">=", "==", "~=", "<=", ">", "<"):
            if op in normalized:
                name, spec = normalized.split(op, 1)
                result[name.strip().lower()] = f"{op}{spec.strip()}"
                break
        else:
            result[normalized.strip().lower()] = ""
    return result


# ---------------------------------------------------------------------------
# AC1: project_dependencies_partition
# ---------------------------------------------------------------------------


def test_project_dependencies_partition() -> None:
    """AC1: pyyaml>=6.0 と jsonschema>=4.0 が [project].dependencies にあり、
    [dependency-groups].dev にはない。version specifier まで検証する。"""
    data = tomllib.loads(PYPROJECT_PATH.read_text())

    project_deps: list[str] = data.get("project", {}).get("dependencies", [])
    dev_deps: list[str] = data.get("dependency-groups", {}).get("dev", [])

    project_specs = _deps_by_name(project_deps)
    dev_specs = _deps_by_name(dev_deps)

    assert project_specs.get("pyyaml") == ">=6.0", (
        f"pyyaml must be in [project].dependencies with specifier >=6.0; found: {project_deps}"
    )
    assert project_specs.get("jsonschema") == ">=4.0", (
        f"jsonschema must be in [project].dependencies with specifier >=4.0; found: {project_deps}"
    )
    assert "pyyaml" not in dev_specs, (
        f"pyyaml must NOT be in [dependency-groups].dev; found: {dev_deps}"
    )
    assert "jsonschema" not in dev_specs, (
        f"jsonschema must NOT be in [dependency-groups].dev; found: {dev_deps}"
    )


# ---------------------------------------------------------------------------
# AC2: dev_group_tools_remain
# ---------------------------------------------------------------------------


def test_dev_group_tools_remain() -> None:
    """AC2: pytest / pytest-xdist / ruff が引き続き [dependency-groups].dev にある。"""
    data = tomllib.loads(PYPROJECT_PATH.read_text())
    dev_deps: list[str] = data.get("dependency-groups", {}).get("dev", [])
    dev_dep_names = {d.split(">=")[0].split("==")[0].split(",")[0].split("[")[0].lower() for d in dev_deps}

    assert "pytest" in dev_dep_names, f"pytest must remain in dev group; found: {dev_deps}"
    assert "pytest-xdist" in dev_dep_names, (
        f"pytest-xdist must remain in dev group; found: {dev_deps}"
    )
    assert "ruff" in dev_dep_names, f"ruff must remain in dev group; found: {dev_deps}"


# ---------------------------------------------------------------------------
# AC5: parse_machine_readable_contract
# ---------------------------------------------------------------------------


def _load_mrc_parser():  # type: ignore[return]
    """Dynamically load mrc_contract_parser module."""
    parser_path = (
        REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts" / "mrc_contract_parser.py"
    )
    assert parser_path.exists(), f"mrc_contract_parser.py not found at: {parser_path}"
    spec = importlib.util.spec_from_file_location("mrc_contract_parser", parser_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so dataclass __module__ lookup resolves
    sys.modules["mrc_contract_parser"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_parse_machine_readable_contract() -> None:
    """AC5: smoke の parse_machine_readable_contract behavioral check."""
    module = _load_mrc_parser()

    valid_fixture = (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        "issue_kind: implementation\n"
        "```\n"
    )
    result = module.parse_machine_readable_contract(valid_fixture)
    assert result.ok, f"parse_machine_readable_contract should succeed on valid fixture: {result}"

    # Negative: invalid fixture (no MRC section) should fail
    invalid_fixture = "## Some Other Section\n\nNo contract here.\n"
    result_bad = module.parse_machine_readable_contract(invalid_fixture)
    assert not result_bad.ok, (
        f"parse_machine_readable_contract should fail on missing MRC section: {result_bad}"
    )


# ---------------------------------------------------------------------------
# AC6: command_shape
# ---------------------------------------------------------------------------


def test_command_shape() -> None:
    """AC6: canonical smoke コマンドに --isolated / --locked / --no-default-groups があり、
    --group dev / --with pyyaml / --with jsonschema がない。"""
    for flag in REQUIRED_FLAGS:
        assert flag in CANONICAL_SMOKE_CMD, (
            f"Required flag '{flag}' missing from canonical smoke command: {CANONICAL_SMOKE_CMD!r}"
        )

    for flag in FORBIDDEN_FLAGS:
        assert flag not in CANONICAL_SMOKE_CMD, (
            f"Forbidden flag '{flag}' found in canonical smoke command: {CANONICAL_SMOKE_CMD!r}"
        )

    # Verify CANONICAL_SMOKE_CMD matches the actual CI run: value
    import yaml as _yaml

    ci = _yaml.safe_load(CI_YML_PATH.read_text())
    steps = ci["jobs"]["python-test"]["steps"]
    smoke_step = next(
        (s for s in steps if s.get("name") == "Runtime dependency smoke (isolated, #1192)"),
        None,
    )
    assert smoke_step is not None, "Smoke step not found in python-test job"
    assert smoke_step["run"] == CANONICAL_SMOKE_CMD, (
        f"CI run: {smoke_step['run']!r} != CANONICAL_SMOKE_CMD: {CANONICAL_SMOKE_CMD!r}"
    )


# ---------------------------------------------------------------------------
# AC7: invalid_partition_fixture / stale_lock_fixture (baseline fail)
# ---------------------------------------------------------------------------


def test_invalid_partition_fixture(tmp_path: Path) -> None:
    """AC7: baseline fail — pyyaml/jsonschema が dev group にある不正 partition は
    partition check スクリプトを non-zero で終了させる。"""
    # Write an intentionally invalid pyproject.toml (pyyaml/jsonschema in dev)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "test"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = []\n\n"
        "[dependency-groups]\n"
        'dev = ["pyyaml>=6.0", "jsonschema>=4.0", "pytest>=8.0"]\n'
    )

    # Inline checker script that mirrors the AC1 logic
    checker = tmp_path / "check_partition.py"
    checker.write_text(
        "import sys, tomllib\n"
        "from pathlib import Path\n\n"
        "data = tomllib.loads(Path('pyproject.toml').read_text())\n"
        "proj = data.get('project', {}).get('dependencies', [])\n"
        "names = {d.split('>=')[0].lower() for d in proj}\n"
        "if 'pyyaml' not in names or 'jsonschema' not in names:\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )

    result = subprocess.run(
        [sys.executable, "check_partition.py"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode != 0, (
        "Invalid partition fixture must cause partition check to exit non-zero"
    )


def test_stale_lock_fixture_missing(tmp_path: Path) -> None:
    """AC7 (missing): lock file が存在しない場合 uv lock --check が non-zero。"""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "test"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12"\n'
        'dependencies = ["pyyaml>=6.0"]\n'
    )
    result = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode != 0, (
        "Absent lock must cause uv lock --check to exit non-zero"
    )


def test_stale_lock_fixture_stale(tmp_path: Path) -> None:
    """AC7 (stale): repo の uv.lock を minimal pyproject (deps=[]) と組み合わせると
    lockfile が stale と判定され uv lock --check が non-zero になる。"""
    # Use a minimal pyproject (no dependencies) with the repo's actual uv.lock.
    # The repo lock was generated with pyyaml/jsonschema as project.dependencies,
    # so it will not match a pyproject with empty dependencies.
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "stale-test"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = []\n"
    )
    shutil.copy2(REPO_ROOT / "uv.lock", tmp_path / "uv.lock")

    result = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode != 0, (
        "Stale lock (repo lock + minimal pyproject) must cause uv lock --check to exit non-zero"
    )


# ---------------------------------------------------------------------------
# AC8: ci_wiring_runtime_only
# ---------------------------------------------------------------------------


def test_ci_wiring_runtime_only() -> None:
    """AC8: python-test job 内で uv lock --check と smoke が uv sync より前にあり、
    run: が正確な値と一致する。YAML parse + step order で検証。"""
    import yaml as _yaml

    ci = _yaml.safe_load(CI_YML_PATH.read_text())
    steps = ci["jobs"]["python-test"]["steps"]
    names = [step.get("name", "") for step in steps]

    lock_name = "uv lock --check (drift guard)"
    smoke_name = "Runtime dependency smoke (isolated, #1192)"
    sync_name = "uv sync (timed)"

    assert lock_name in names, f"Step {lock_name!r} not found in python-test job steps: {names}"
    assert smoke_name in names, f"Step {smoke_name!r} not found in python-test job steps: {names}"
    assert sync_name in names, f"Step {sync_name!r} not found in python-test job steps: {names}"

    lock_i = names.index(lock_name)
    smoke_i = names.index(smoke_name)
    sync_i = names.index(sync_name)

    assert lock_i < sync_i, (
        f"uv lock --check (index {lock_i}) must be before uv sync (index {sync_i})"
    )
    assert smoke_i < sync_i, (
        f"smoke (index {smoke_i}) must be before uv sync (index {sync_i})"
    )

    assert steps[lock_i]["run"] == "uv lock --check", (
        f"lock step run: {steps[lock_i]['run']!r} != 'uv lock --check'"
    )
    assert steps[smoke_i]["run"] == CANONICAL_SMOKE_CMD, (
        f"smoke step run: {steps[smoke_i]['run']!r} != CANONICAL_SMOKE_CMD: {CANONICAL_SMOKE_CMD!r}"
    )


# ---------------------------------------------------------------------------
# AC9: no_build_system_added
# ---------------------------------------------------------------------------


def test_no_build_system_added() -> None:
    """AC9: [build-system] が pyproject.toml に追加されていない。"""
    data = tomllib.loads(PYPROJECT_PATH.read_text())
    assert "build-system" not in data, (
        f"[build-system] must NOT be added to pyproject.toml; found: {data.get('build-system')}"
    )


# ---------------------------------------------------------------------------
# AC10: ci_test_performance_decision_fields
# ---------------------------------------------------------------------------


def test_ci_test_performance_decision_fields() -> None:
    """AC10: test-lane-policy.md に CI_TEST_PERFORMANCE_DECISION_V1 と必須フィールドがある。"""
    policy_text = TEST_LANE_POLICY_PATH.read_text()

    assert "CI_TEST_PERFORMANCE_DECISION_V1" in policy_text, (
        "test-lane-policy.md must contain CI_TEST_PERFORMANCE_DECISION_V1"
    )

    required_fields = [
        "affected_lanes",
        "added_steps",
        "expected_cost",
        "required_evidence",
        "target_ssot_changed",
        "python-test",
        "contract_artifact",
    ]
    for field in required_fields:
        assert field in policy_text, (
            f"test-lane-policy.md CI_TEST_PERFORMANCE_DECISION_V1 must include field: {field!r}"
        )

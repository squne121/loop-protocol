"""Tests for the required-CI VRT snapshot write policy (Issue #1388)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-vrt-snapshot-policy.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_vrt_snapshot_policy", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_repo(
    root: Path,
    *,
    ci_run: str = "pnpm test:e2e:ci",
    package_scripts: dict[str, str] | None = None,
    action_run: str | None = None,
    action_with: str | None = None,
    reusable_workflow: bool = False,
    config: str = "export default { updateSnapshots: 'all' }\n",
    malformed_ci: bool = False,
    wiring_run: str | None = "uv run --locked python scripts/check-vrt-snapshot-policy.py",
    wiring_if: str | None = None,
) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    scripts = package_scripts or {"test:e2e:ci": "playwright test"}
    (root / "package.json").write_text(json.dumps({"scripts": scripts}, indent=2) + "\n", encoding="utf-8")
    (root / "playwright.config.ts").write_text(config, encoding="utf-8")
    if malformed_ci:
        (workflows / "ci.yml").write_text("jobs: [broken\n", encoding="utf-8")
        return

    wiring = ""
    if wiring_run is not None:
        condition = f"\n        if: {wiring_if}" if wiring_if is not None else ""
        wiring = f"      - run: {wiring_run}{condition}\n"
    action = ""
    if action_run is not None:
        action_dir = root / ".github" / "actions" / "vrt"
        action_dir.mkdir(parents=True)
        (action_dir / "action.yml").write_text(
            "name: vrt\nruns:\n  using: composite\n  steps:\n    - run: |\n        "
            + action_run.replace("\n", "\n        ")
            + "\n",
            encoding="utf-8",
        )
        with_block = ""
        if action_with is not None:
            with_block = f"\n        with:\n          script: {action_with}"
        action = f"      - uses: ./.github/actions/vrt{with_block}\n"
    reusable = ""
    if reusable_workflow:
        reusable = "  delegated:\n    uses: ./.github/workflows/reusable.yml\n"
    (workflows / "ci.yml").write_text(
        "jobs:\n  python-test-core:\n    steps:\n"
        + wiring
        + action
        + "      - run: |\n          "
        + ci_run.replace("\n", "\n          ")
        + "\n"
        + reusable,
        encoding="utf-8",
    )


def _errors(root: Path) -> list[str]:
    return _load_module().check_policy(root).errors


def test_rejects_write_capable_playwright_and_vitest_modes(tmp_path: Path):
    for command in [
        "playwright test -u",
        "playwright test --update-snapshots",
        "playwright test --update-snapshots=all",
        "vitest -u",
        "vitest --update",
        "vitest --update=all",
        "vitest --update=new",
        "vitest run --update",
        "vitest watch --update=all",
    ]:
        _write_repo(tmp_path, ci_run=command)
        assert any("write-capable" in error for error in _errors(tmp_path)), command


def test_rejects_indirect_package_script_and_composite_action_paths(tmp_path: Path):
    _write_repo(
        tmp_path,
        package_scripts={
            "test:e2e:ci": 'pnpm run "vrt:delegate"',
            "vrt:delegate": "pnpm run " + "\\\n" + "  test:vrt:update:e2e",
            "test:vrt:update:e2e": "playwright test --update-snapshots=all",
        },
    )
    assert any("test:vrt:update:e2e" in error for error in _errors(tmp_path))

    _write_repo(
        tmp_path,
        ci_run="echo safe",
        action_run="pnpm run " + "$" + "{{ inputs.script }}",
        action_with="test:vrt:update:e2e",
    )
    assert any("unresolved interpolation" in error for error in _errors(tmp_path))


def test_allows_safe_modes_python_u_and_explanatory_text(tmp_path: Path):
    _write_repo(
        tmp_path,
        ci_run="""python -u scripts/check.py
echo \"playwright test --update-snapshots=all\"
printf '%s\\n' 'vitest --update=all'
playwright test --update-snapshots=none
vitest --update=none
vitest run --update=false""",
    )
    errors = _errors(tmp_path)
    assert errors == [], errors


def test_fails_closed_for_dynamic_paths_malformed_yaml_and_structural_wiring(tmp_path: Path):
    _write_repo(tmp_path, ci_run="pnpm run ${{ matrix.script }}")
    assert any("unresolved interpolation" in error for error in _errors(tmp_path))

    _write_repo(tmp_path, ci_run="echo safe", action_run="echo safe", action_with="${{ matrix.script }}")
    assert any("local composite action input" in error for error in _errors(tmp_path))

    _write_repo(tmp_path, reusable_workflow=True)
    assert any("local reusable workflow" in error for error in _errors(tmp_path))

    _write_repo(tmp_path, malformed_ci=True)
    assert any("YAML parse failure" in error for error in _errors(tmp_path))

    _write_repo(tmp_path, wiring_run="echo uv run --locked python scripts/check-vrt-snapshot-policy.py")
    assert any("wiring" in error for error in _errors(tmp_path))

    _write_repo(tmp_path, wiring_if="false")
    assert any("wiring" in error for error in _errors(tmp_path))


def test_current_repository_policy_passes():
    assert _errors(REPO_ROOT) == []

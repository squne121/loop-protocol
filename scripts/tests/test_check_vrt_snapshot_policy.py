"""Tests for the required-CI VRT snapshot write policy (Issue #1388)."""

from __future__ import annotations

import importlib.util
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
    config: str = "export default { updateSnapshots: process.env.CI ? 'none' : 'missing' }\n",
    malformed_ci: bool = False,
    include_wiring: bool = True,
) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    scripts = package_scripts or {"test:e2e:ci": "playwright test"}
    package_entries = ",\n".join(f'    "{name}": "{value}"' for name, value in scripts.items())
    (root / "package.json").write_text('{\n  "scripts": {\n' + package_entries + '\n  }\n}\n', encoding="utf-8")
    (root / "playwright.config.ts").write_text(config, encoding="utf-8")
    if malformed_ci:
        (workflows / "ci.yml").write_text("jobs: [broken\n", encoding="utf-8")
        return

    wiring = "      - run: uv run --locked python scripts/check-vrt-snapshot-policy.py\n" if include_wiring else ""
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
        action = "      - uses: ./.github/actions/vrt\n"
    (workflows / "ci.yml").write_text(
        "jobs:\n  python-test-core:\n    steps:\n"
        + wiring
        + action
        + "      - run: |\n          "
        + ci_run.replace("\n", "\n          ")
        + "\n",
        encoding="utf-8",
    )


def _errors(root: Path) -> list[str]:
    return _load_module().check_policy(root).errors


def test_rejects_write_capable_playwright_and_vitest_modes(tmp_path: Path):
    _write_repo(tmp_path, ci_run="playwright test --update-snapshots=all")
    assert any("Playwright" in error for error in _errors(tmp_path))

    _write_repo(tmp_path, ci_run="vitest run --update")
    assert any("Vitest" in error for error in _errors(tmp_path))


def test_rejects_indirect_package_script_and_composite_action_paths(tmp_path: Path):
    _write_repo(
        tmp_path,
        package_scripts={
            "test:e2e:ci": "pnpm run vrt:delegate",
            "vrt:delegate": "pnpm test:vrt:update:e2e",
            "test:vrt:update:e2e": "playwright test --update-snapshots=all",
        },
    )
    assert any("test:vrt:update:e2e" in error for error in _errors(tmp_path))

    _write_repo(tmp_path, ci_run="echo safe", action_run="pnpm test:vrt:update:e2e")
    assert any("test:vrt:update:e2e" in error for error in _errors(tmp_path))


def test_allows_safe_modes_python_u_and_comment_text(tmp_path: Path):
    _write_repo(
        tmp_path,
        ci_run="""python -u scripts/check.py
# playwright test --update-snapshots=all
playwright test --update-snapshots=none
vitest run --update=none""",
    )
    errors = _errors(tmp_path)
    assert errors == [], errors


def test_fails_closed_for_interpolation_malformed_yaml_and_missing_wiring(tmp_path: Path):
    _write_repo(tmp_path, ci_run="playwright test ${{ matrix.snapshot_mode }}")
    assert any("unresolved interpolation" in error for error in _errors(tmp_path))

    _write_repo(tmp_path, malformed_ci=True)
    assert any("YAML parse failure" in error for error in _errors(tmp_path))

    _write_repo(tmp_path, include_wiring=False)
    assert any("validator CI wiring" in error for error in _errors(tmp_path))

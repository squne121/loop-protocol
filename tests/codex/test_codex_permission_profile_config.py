"""Codex permission-profile root default regression tests (Issue #1859).

The #1849 P0 quarantine removed root-scope `default_permissions` entirely,
which broke `codex status` / `skills/list` (Codex 0.146.0 loader rejects
non-empty `[permissions]` profiles without a root default). This module
verifies the restored contract: root default pinned to the built-in
`:workspace` profile, static validators reject drift without requiring the
Codex CLI, per-agent profile declarations are untouched, and an isolated-home
loader smoke runs when the Codex CLI is available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_TOML = REPO_ROOT / ".codex" / "config.toml"
VALIDATOR_MJS = REPO_ROOT / "scripts" / "check-codex-agents.mjs"
VALIDATOR_PY = REPO_ROOT / "scripts" / "check_codex_agent_config.py"
EXPECTATION_PATH = REPO_ROOT / "tests" / "fixtures" / "codex-agent-config" / "expected-runtime-contract.json"
AGENTS_DIR = REPO_ROOT / ".codex" / "agents"
ARTIFACT_DIR = REPO_ROOT / "artifacts" / "codex-permission-profile-smoke"

READONLY_PROFILE = "loop-protocol-readonly"
WRITE_PROFILE = "loop-protocol-rtk"
ROOT_DEFAULT_PROFILE = ':workspace'

# A synthetic env that hides the real Codex CLI (AC8: static validation must
# not require it) while still converting the validator's execpolicy check to
# a no-op warning instead of a hard failure.
_NO_CODEX_ENV_OVERRIDES = {
    "CODEX_BIN": "/nonexistent-codex-binary-for-static-validation-test",
    "CODEX_ALLOW_NO_CODEX": "1",
}


def _fixture_repo(tmp_path: Path) -> Path:
    """Copy the minimal repo surface the validators need into tmp_path.

    `.git`, `node_modules`, and other heavy/irrelevant trees are excluded.
    `.claude/worktrees` is excluded specifically (it can recursively contain
    other checkouts of this same repo) while the rest of `.claude` (skill
    canonical bodies referenced by `.agents/skills/*`) is kept.
    """
    dest = tmp_path / "repo"
    exclude_relpaths = {".git", "node_modules", "dist", "coverage", "artifacts", "public", ".claude/worktrees"}

    def ignore(dirpath: str, names: list[str]) -> list[str]:
        rel = os.path.relpath(dirpath, REPO_ROOT)
        ignored = []
        for name in names:
            relpath = name if rel == "." else f"{rel}/{name}"
            if relpath in exclude_relpaths or name in {".git", "node_modules"}:
                ignored.append(name)
        return ignored

    shutil.copytree(REPO_ROOT, dest, ignore=ignore)
    return dest


def _run_mjs_validator(repo_root: Path, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(_NO_CODEX_ENV_OVERRIDES)
    env["REPO_ROOT_OVERRIDE"] = str(repo_root)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["node", str(VALIDATOR_MJS)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )


def _run_py_validator(repo_root: Path) -> subprocess.CompletedProcess[str]:
    # scripts/check_codex_agent_config.py resolves REPO_ROOT from its own
    # file location (no override env), so it must be invoked from a copy of
    # the script that lives inside the fixture repo.
    script = repo_root / "scripts" / "check_codex_agent_config.py"
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--assert-required-fields",
            "--assert-runtime-contract",
            "--assert-local-main-branch-guard",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )


def _remove_root_default(config_text: str) -> str:
    lines = [
        line
        for line in config_text.splitlines(keepends=True)
        if not line.strip().startswith("default_permissions =")
    ]
    return "".join(lines)


# ---------------------------------------------------------------------------
# AC1/AC3: root default value and scope
# ---------------------------------------------------------------------------


def test_root_default_permissions_is_workspace_before_features() -> None:
    text = CONFIG_TOML.read_text(encoding="utf-8")
    assert 'default_permissions = ":workspace"' in text
    assert text.index('default_permissions = ":workspace"') < text.index("[features]")

    with CONFIG_TOML.open("rb") as handle:
        parsed = tomllib.load(handle)
    assert parsed["default_permissions"] == ROOT_DEFAULT_PROFILE


def test_root_default_does_not_widen_to_rtk_or_danger_full_access() -> None:
    with CONFIG_TOML.open("rb") as handle:
        parsed = tomllib.load(handle)
    assert parsed["default_permissions"] != WRITE_PROFILE
    assert parsed["default_permissions"] != ":danger-full-access"
    # The rtk network allowlist (GitHub / upload endpoints) must stay scoped
    # to the explicit loop-protocol-rtk profile, not the root default.
    text = CONFIG_TOML.read_text(encoding="utf-8")
    root_scope = text.split("[permissions.")[0]
    assert "uploads.github.com" not in root_scope


# ---------------------------------------------------------------------------
# AC4: per-agent explicit profile declarations remain valid
# ---------------------------------------------------------------------------


def test_agent_profile_declarations_remain_valid() -> None:
    expectations = json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))
    required_agents = expectations["required_agents"]
    assert required_agents, "fixture must declare at least one required agent"
    for agent_name, expected in required_agents.items():
        agent_path = REPO_ROOT / expected["path"]
        with agent_path.open("rb") as handle:
            agent = tomllib.load(handle)
        actual = agent.get("default_permissions")
        expected_profile = expected["default_permissions"]
        assert expected_profile in {READONLY_PROFILE, WRITE_PROFILE}, (
            f"{agent_name}: fixture default_permissions must be a repo-defined profile, got {expected_profile!r}"
        )
        assert actual == expected_profile, (
            f"{agent_name}: default_permissions expected {expected_profile!r}, got {actual!r}"
        )
        # None of the per-agent declarations may silently widen to the root
        # :workspace default or a built-in profile.
        assert actual not in {ROOT_DEFAULT_PROFILE, ":read-only", ":danger-full-access"}


# ---------------------------------------------------------------------------
# AC5/AC6: static validators reject malformed root-default configurations
# ---------------------------------------------------------------------------


def test_removing_root_default_is_rejected(tmp_path: Path) -> None:
    """AC7 (mutation test): deleting the root default must fail both validators."""
    repo = _fixture_repo(tmp_path)
    config_path = repo / ".codex" / "config.toml"
    mutated = _remove_root_default(config_path.read_text(encoding="utf-8"))
    assert 'default_permissions = ":workspace"' not in mutated
    config_path.write_text(mutated, encoding="utf-8")

    mjs_result = _run_mjs_validator(repo)
    assert mjs_result.returncode != 0, mjs_result.stdout + mjs_result.stderr
    assert "default_permissions" in (mjs_result.stdout + mjs_result.stderr)

    py_result = _run_py_validator(repo)
    assert py_result.returncode != 0, py_result.stdout + py_result.stderr
    assert "default_permissions is missing" in (py_result.stdout + py_result.stderr)


def test_baseline_repo_passes_both_validators_without_mutation(tmp_path: Path) -> None:
    """Control case for the mutation test above: the unmutated fixture passes."""
    repo = _fixture_repo(tmp_path)
    mjs_result = _run_mjs_validator(repo)
    assert mjs_result.returncode == 0, mjs_result.stdout + mjs_result.stderr

    py_result = _run_py_validator(repo)
    assert py_result.returncode == 0, py_result.stdout + py_result.stderr


def test_undefined_custom_root_default_profile_is_rejected(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    config_path = repo / ".codex" / "config.toml"
    mutated = config_path.read_text(encoding="utf-8").replace(
        'default_permissions = ":workspace"',
        'default_permissions = "loop-protocol-does-not-exist"',
    )
    config_path.write_text(mutated, encoding="utf-8")

    py_result = _run_py_validator(repo)
    assert py_result.returncode != 0, py_result.stdout + py_result.stderr
    assert "undefined custom profile" in (py_result.stdout + py_result.stderr)


def test_root_default_misplaced_in_features_is_rejected(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    config_path = repo / ".codex" / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    mutated = text.replace(
        'default_permissions = ":workspace"\n',
        "",
    ).replace(
        "[features]\nhooks = true",
        '[features]\nhooks = true\ndefault_permissions = ":workspace"',
    )
    assert 'default_permissions = ":workspace"' in mutated
    config_path.write_text(mutated, encoding="utf-8")

    py_result = _run_py_validator(repo)
    assert py_result.returncode != 0, py_result.stdout + py_result.stderr
    assert "must not be placed inside [features]" in (py_result.stdout + py_result.stderr)

    mjs_result = _run_mjs_validator(repo)
    assert mjs_result.returncode != 0, mjs_result.stdout + mjs_result.stderr


def test_legacy_sandbox_mode_mixed_with_permissions_is_rejected(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    config_path = repo / ".codex" / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    # Insert before [features] so the key lands in the root TOML scope
    # (a trailing append would nest under the last preceding table header).
    mutated = text.replace("[features]", 'sandbox_mode = "workspace-write"\n\n[features]', 1)
    assert mutated != text
    config_path.write_text(mutated, encoding="utf-8")

    mjs_result = _run_mjs_validator(repo)
    assert mjs_result.returncode != 0, mjs_result.stdout + mjs_result.stderr
    assert "sandbox_mode" in (mjs_result.stdout + mjs_result.stderr)

    py_result = _run_py_validator(repo)
    assert py_result.returncode != 0, py_result.stdout + py_result.stderr
    assert "sandbox_mode" in (py_result.stdout + py_result.stderr)


# ---------------------------------------------------------------------------
# AC8: static validation does not require the Codex CLI
# ---------------------------------------------------------------------------


def test_static_validation_does_not_require_codex_cli() -> None:
    env = dict(os.environ)
    env.update(_NO_CODEX_ENV_OVERRIDES)
    result = subprocess.run(
        ["node", str(VALIDATOR_MJS)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "codex binary not found" in result.stderr

    py_result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PY),
            "--assert-required-fields",
            "--assert-runtime-contract",
            "--assert-local-main-branch-guard",
        ],
        cwd=REPO_ROOT,
        env={k: v for k, v in env.items() if k != "PATH"} | {"PATH": env["PATH"]},
        capture_output=True,
        text=True,
    )
    assert py_result.returncode == 0, py_result.stdout + py_result.stderr


# ---------------------------------------------------------------------------
# AC9/AC2: Codex CLI loader / App Server / TUI runtime smoke (isolated-home)
# ---------------------------------------------------------------------------


def _codex_available() -> tuple[bool, str]:
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        return False, "codex CLI not found on PATH"
    try:
        result = subprocess.run([codex_bin, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"codex --version failed: {exc}"
    if result.returncode != 0:
        return False, f"codex --version exited {result.returncode}: {result.stderr}"
    return True, result.stdout.strip()


def test_loader_smoke_uses_isolated_home(tmp_path: Path) -> None:
    """AC2/AC9: `codex status` must succeed via an isolated $HOME/$CODEX_HOME.

    Runtime Verification Applicability: immediate (see Issue #1859). When the
    Codex CLI is unavailable this SKIPs with exit 77 semantics (pytest.skip)
    rather than reporting a false PASS, and records the missing prerequisite
    to the artifact directory.
    """
    available, detail = _codex_available()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = ARTIFACT_DIR / "loader-smoke-isolated-home.json"

    if not available:
        artifact_path.write_text(
            json.dumps({"skipped": True, "reason": detail}, indent=2) + "\n",
            encoding="utf-8",
        )
        pytest.skip(f"Codex CLI unavailable for isolated-home loader smoke: {detail}")

    codex_bin = shutil.which("codex")
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    codex_home = isolated_home / ".codex"
    codex_home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(isolated_home)
    env["CODEX_HOME"] = str(codex_home)
    # `codex doctor --json` is non-interactive (unlike `codex status`, which
    # requires a TTY) and reports a `config.load` check that fails when the
    # loader rejects the project config (the #1849 regression this Issue
    # fixes: non-empty [permissions] without a root default_permissions).
    result = subprocess.run(
        [codex_bin, "doctor", "--json"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # `codex doctor` exits non-zero on any overall-fail check (e.g. missing
    # auth credentials in a fresh isolated home) even though the JSON report
    # is still emitted on stdout. Only `checks["config.load"]` is in scope
    # for this loader smoke; auth/network checks are irrelevant here.
    config_check = None
    parse_error = None
    try:
        report = json.loads(result.stdout)
        config_check = report.get("checks", {}).get("config.load")
    except json.JSONDecodeError as exc:
        parse_error = str(exc)

    artifact_path.write_text(
        json.dumps(
            {
                "skipped": False,
                "codex_version": detail,
                "repo_head_available": True,
                "isolated_home": str(isolated_home),
                "exit_code": result.returncode,
                "config_load_check": config_check,
                "parse_error": parse_error,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    assert parse_error is None, parse_error
    assert config_check is not None, "codex doctor --json did not report a config.load check"
    assert config_check["status"] == "ok", config_check
    assert "does not set" not in config_check["summary"], config_check

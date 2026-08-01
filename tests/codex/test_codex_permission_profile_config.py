"""Codex permission-profile root default regression tests (Issue #1859).

The #1849 P0 quarantine removed root-scope `default_permissions` entirely,
which broke `codex status` / `skills/list` (Codex 0.146.0 loader rejects
non-empty `[permissions]` profiles without a root default). PR #1864 (this
Issue's first implementation) restored the root default as the built-in
`:workspace` profile literal, but an adversarial owner review found 4
blockers: a mis-described `:workspace` filesystem read boundary, an
isolated-home loader smoke that never registered the target repository as a
trusted project (so it could pass even if project-local config were
excluded), AC2/AC4 runtime verification deferred to a follow-up Issue while
still closing this one, and a JS validator that used raw-text regex (missing
misplacement into any table other than `[features]`).

This module verifies the re-revised contract: root default pinned to the
repository-defined `loop-protocol-personal-dev` custom profile (which
`extends = ":workspace"` and layers on an explicit, bounded development
network allowlist), static validators reject drift *structurally* (parsed
TOML / tomllib, not regex) without requiring the Codex CLI, per-agent
profile declarations are untouched, and isolated-home loader smoke runs
positive (trusted project registration) and negative (root default removed)
controls when the Codex CLI is available.
"""

from __future__ import annotations

import json
import os
import re
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
PR_ARTIFACT_DIR = ARTIFACT_DIR / "pr-1864"

READONLY_PROFILE = "loop-protocol-readonly"
WRITE_PROFILE = "loop-protocol-rtk"
# Issue #1915: web-researcher-only profile -- filesystem read-only like
# READONLY_PROFILE, but with a widened outbound web domain allowlist.
WEB_RESEARCH_PROFILE = "loop-protocol-web-research"
ROOT_DEFAULT_PROFILE = "loop-protocol-personal-dev"
ROOT_DEFAULT_EXTENDS = ":workspace"

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


_EXTENDS_LINE_RE = re.compile(r'^extends = ":workspace"$', re.MULTILINE)


def _replace_extends_line(text: str, replacement: str) -> str:
    """Replace the *code* `extends = ":workspace"` line (start-of-line
    anchored), never the identical phrase quoted inside a prose `#` comment
    describing it above the real key."""
    mutated, count = _EXTENDS_LINE_RE.subn(replacement, text, count=1)
    assert count == 1, "expected exactly one code-scope extends line to mutate"
    return mutated


def _mutate_config(repo: Path, transform) -> Path:
    config_path = repo / ".codex" / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    mutated = transform(text)
    assert mutated != text, "mutation must actually change the config text"
    config_path.write_text(mutated, encoding="utf-8")
    return config_path


def _assert_both_reject(repo: Path, *, mjs_substring: str | None = None, py_substring: str | None = None) -> None:
    mjs_result = _run_mjs_validator(repo)
    assert mjs_result.returncode != 0, mjs_result.stdout + mjs_result.stderr
    if mjs_substring is not None:
        assert mjs_substring in (mjs_result.stdout + mjs_result.stderr)

    py_result = _run_py_validator(repo)
    assert py_result.returncode != 0, py_result.stdout + py_result.stderr
    if py_substring is not None:
        assert py_substring in (py_result.stdout + py_result.stderr)


# ---------------------------------------------------------------------------
# AC1/AC3: root default value, scope, and extends
# ---------------------------------------------------------------------------


def test_root_default_permissions_is_personal_dev_before_features() -> None:
    text = CONFIG_TOML.read_text(encoding="utf-8")
    assert 'default_permissions = "loop-protocol-personal-dev"' in text
    assert text.index('default_permissions = "loop-protocol-personal-dev"') < text.index("[features]")

    with CONFIG_TOML.open("rb") as handle:
        parsed = tomllib.load(handle)
    assert parsed["default_permissions"] == ROOT_DEFAULT_PROFILE
    assert parsed["permissions"][ROOT_DEFAULT_PROFILE]["extends"] == ROOT_DEFAULT_EXTENDS


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
# AC3/AC11: root default network semantics
# ---------------------------------------------------------------------------


def test_root_default_network_is_bounded_broad_allowlist() -> None:
    with CONFIG_TOML.open("rb") as handle:
        parsed = tomllib.load(handle)
    network = parsed["permissions"][ROOT_DEFAULT_PROFILE]["network"]
    assert network["enabled"] is True
    assert network["mode"] == "full"
    assert network["allow_local_binding"] is False
    domains = network["domains"]
    assert domains, "root default network allowlist must be non-empty"
    assert "*" not in domains


def test_root_default_forbids_out_of_scope_semantics() -> None:
    text = CONFIG_TOML.read_text(encoding="utf-8")
    assert ":danger-full-access" not in text
    assert 'approval_policy = "never"' not in text
    assert "sandbox_mode" not in text
    with CONFIG_TOML.open("rb") as handle:
        parsed = tomllib.load(handle)
    # Agent-local profiles are untouched by this Issue.
    assert parsed["permissions"][READONLY_PROFILE]["filesystem"][":workspace_roots"]["."] == "read"
    assert parsed["permissions"][WRITE_PROFILE]["filesystem"][":workspace_roots"]["."] == "write"


# ---------------------------------------------------------------------------
# AC4/AC10: per-agent explicit profile declarations remain valid
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
        assert expected_profile in {READONLY_PROFILE, WRITE_PROFILE, WEB_RESEARCH_PROFILE}, (
            f"{agent_name}: fixture default_permissions must be a repo-defined profile, got {expected_profile!r}"
        )
        assert actual == expected_profile, (
            f"{agent_name}: default_permissions expected {expected_profile!r}, got {actual!r}"
        )
        # None of the per-agent declarations may silently widen to the root
        # personal-dev default or a built-in profile.
        assert actual not in {ROOT_DEFAULT_PROFILE, ":workspace", ":read-only", ":danger-full-access"}


# ---------------------------------------------------------------------------
# AC5/AC6/AC7/AC8: static validators reject malformed root-default
# configurations, structurally (parsed TOML), not via raw-text regex.
# ---------------------------------------------------------------------------


def test_removing_root_default_is_rejected(tmp_path: Path) -> None:
    """AC7 (mutation test): deleting the root default must fail both validators."""
    repo = _fixture_repo(tmp_path)
    config_path = _mutate_config(repo, _remove_root_default)
    assert 'default_permissions = "loop-protocol-personal-dev"' not in config_path.read_text(encoding="utf-8")

    _assert_both_reject(
        repo,
        mjs_substring="default_permissions",
        py_substring="default_permissions is missing",
    )


def test_baseline_repo_passes_both_validators_without_mutation(tmp_path: Path) -> None:
    """Control case for the mutation tests below: the unmutated fixture passes."""
    repo = _fixture_repo(tmp_path)
    mjs_result = _run_mjs_validator(repo)
    assert mjs_result.returncode == 0, mjs_result.stdout + mjs_result.stderr

    py_result = _run_py_validator(repo)
    assert py_result.returncode == 0, py_result.stdout + py_result.stderr


def test_root_default_reverted_to_literal_workspace_is_rejected(tmp_path: Path) -> None:
    """AC7 (mutation test): reverting the root default back to the built-in
    `:workspace` literal (instead of the required custom profile) must fail
    both validators."""
    repo = _fixture_repo(tmp_path)
    _mutate_config(
        repo,
        lambda text: text.replace(
            'default_permissions = "loop-protocol-personal-dev"',
            'default_permissions = ":workspace"',
        ),
    )

    _assert_both_reject(
        repo,
        mjs_substring="loop-protocol-personal-dev",
        py_substring="loop-protocol-personal-dev",
    )


def test_undefined_custom_root_default_profile_is_rejected(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    _mutate_config(
        repo,
        lambda text: text.replace(
            'default_permissions = "loop-protocol-personal-dev"',
            'default_permissions = "loop-protocol-does-not-exist"',
        ),
    )

    py_result = _run_py_validator(repo)
    assert py_result.returncode != 0, py_result.stdout + py_result.stderr
    assert "undefined custom profile" in (py_result.stdout + py_result.stderr)


def test_root_default_missing_extends_is_rejected(tmp_path: Path) -> None:
    """AC1/AC7: `loop-protocol-personal-dev` must declare `extends = ":workspace"`."""
    repo = _fixture_repo(tmp_path)
    _mutate_config(repo, lambda text: _replace_extends_line(text, ""))

    _assert_both_reject(repo, mjs_substring="extends", py_substring="extends")


def test_root_default_unsupported_parent_is_rejected(tmp_path: Path) -> None:
    """AC1/AC7: extending anything other than the built-in `:workspace` profile is rejected."""
    repo = _fixture_repo(tmp_path)
    _mutate_config(
        repo,
        lambda text: _replace_extends_line(text, 'extends = ":danger-full-access"'),
    )

    _assert_both_reject(repo, mjs_substring="extends", py_substring="extends")


def test_root_default_network_disabled_is_rejected(tmp_path: Path) -> None:
    """AC3/AC7: network must stay enabled on the root default."""
    repo = _fixture_repo(tmp_path)
    _mutate_config(
        repo,
        lambda text: text.replace(
            "[permissions.loop-protocol-personal-dev.network]\nenabled = true",
            "[permissions.loop-protocol-personal-dev.network]\nenabled = false",
            1,
        ),
    )

    _assert_both_reject(repo, mjs_substring="enabled", py_substring="enabled")


def test_root_default_network_mode_limited_is_rejected(tmp_path: Path) -> None:
    """AC3/AC7: `mode` must be `"full"`, not `"limited"` or other values."""
    repo = _fixture_repo(tmp_path)
    _mutate_config(
        repo,
        lambda text: text.replace(
            'mode = "full"',
            'mode = "limited"',
            1,
        ),
    )

    _assert_both_reject(repo, mjs_substring="mode", py_substring="mode")


def test_root_default_empty_allowlist_is_rejected(tmp_path: Path) -> None:
    """AC3/AC7: the domain allowlist must not be empty."""
    repo = _fixture_repo(tmp_path)

    def strip_domains(text: str) -> str:
        header = '[permissions.loop-protocol-personal-dev.network.domains]\n'
        start = text.index(header) + len(header)
        end = text.index("\n\n", start)
        return text[:start] + text[end:]

    _mutate_config(repo, strip_domains)

    _assert_both_reject(repo, mjs_substring="domains", py_substring="domains")


def test_root_default_global_wildcard_allowlist_is_rejected(tmp_path: Path) -> None:
    """AC3/AC11: a global `"*"` network allowlist entry must be rejected."""
    repo = _fixture_repo(tmp_path)
    _mutate_config(
        repo,
        lambda text: text.replace(
            '[permissions.loop-protocol-personal-dev.network.domains]\n"**.github.com" = "allow"',
            '[permissions.loop-protocol-personal-dev.network.domains]\n"*" = "allow"\n"**.github.com" = "allow"',
            1,
        ),
    )

    _assert_both_reject(repo, mjs_substring="global", py_substring="global")


def test_root_default_local_binding_enabled_is_rejected(tmp_path: Path) -> None:
    """AC3/AC11: `allow_local_binding = true` must be rejected."""
    repo = _fixture_repo(tmp_path)
    _mutate_config(
        repo,
        lambda text: text.replace(
            "allow_local_binding = false",
            "allow_local_binding = true",
            1,
        ),
    )

    _assert_both_reject(repo, mjs_substring="allow_local_binding", py_substring="allow_local_binding")


def test_root_default_misplaced_in_features_is_rejected(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    config_path = repo / ".codex" / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    mutated = text.replace(
        'default_permissions = "loop-protocol-personal-dev"\n',
        "",
    ).replace(
        "[features]\nhooks = true",
        '[features]\nhooks = true\ndefault_permissions = "loop-protocol-personal-dev"',
    )
    assert 'default_permissions = "loop-protocol-personal-dev"' in mutated
    config_path.write_text(mutated, encoding="utf-8")

    py_result = _run_py_validator(repo)
    assert py_result.returncode != 0, py_result.stdout + py_result.stderr
    assert "must not be placed inside [features]" in (py_result.stdout + py_result.stderr)

    mjs_result = _run_mjs_validator(repo)
    assert mjs_result.returncode != 0, mjs_result.stdout + mjs_result.stderr
    assert "[features]" in (mjs_result.stdout + mjs_result.stderr)


def test_root_default_misplaced_in_agents_table_is_rejected(tmp_path: Path) -> None:
    """AC6: misplacement into `[agents]` (not just `[features]`) is detected."""
    repo = _fixture_repo(tmp_path)
    config_path = repo / ".codex" / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    mutated = text.replace(
        'default_permissions = "loop-protocol-personal-dev"\n',
        "",
    ).replace(
        "[permissions.loop-protocol-rtk.filesystem]",
        '[agents]\ndefault_permissions = "loop-protocol-personal-dev"\n\n'
        "[permissions.loop-protocol-rtk.filesystem]",
    )
    assert 'default_permissions = "loop-protocol-personal-dev"' in mutated
    config_path.write_text(mutated, encoding="utf-8")

    py_result = _run_py_validator(repo)
    assert py_result.returncode != 0, py_result.stdout + py_result.stderr
    assert "[agents]" in (py_result.stdout + py_result.stderr)

    mjs_result = _run_mjs_validator(repo)
    assert mjs_result.returncode != 0, mjs_result.stdout + mjs_result.stderr
    assert "[agents]" in (mjs_result.stdout + mjs_result.stderr)


def test_root_default_misplaced_in_other_permissions_table_is_rejected(tmp_path: Path) -> None:
    """AC6: misplacement into an unrelated `[permissions.*]` table is detected."""
    repo = _fixture_repo(tmp_path)
    config_path = repo / ".codex" / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    mutated = text.replace(
        'default_permissions = "loop-protocol-personal-dev"\n',
        "",
    ).replace(
        "[permissions.loop-protocol-readonly.filesystem]",
        '[permissions.loop-protocol-readonly]\ndefault_permissions = "loop-protocol-personal-dev"\n\n'
        "[permissions.loop-protocol-readonly.filesystem]",
    )
    assert 'default_permissions = "loop-protocol-personal-dev"' in mutated
    config_path.write_text(mutated, encoding="utf-8")

    py_result = _run_py_validator(repo)
    assert py_result.returncode != 0, py_result.stdout + py_result.stderr
    assert "[permissions.loop-protocol-readonly]" in (py_result.stdout + py_result.stderr)

    mjs_result = _run_mjs_validator(repo)
    assert mjs_result.returncode != 0, mjs_result.stdout + mjs_result.stderr
    assert "[permissions.loop-protocol-readonly]" in (mjs_result.stdout + mjs_result.stderr)


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


def _run_doctor_json(
    codex_bin: str, *, cwd: Path, home: Path, codex_home: Path
) -> tuple[dict | None, str | None, subprocess.CompletedProcess[str]]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(codex_home)
    result = subprocess.run(
        [codex_bin, "doctor", "--json"],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, str(exc), result
    return report, None, result


def test_loader_smoke_uses_isolated_home(tmp_path: Path) -> None:
    """AC2/AC9: `codex doctor --json` config.load must succeed via an isolated
    $HOME/$CODEX_HOME that registers this repository as a trusted project.

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
    # Issue #1859 blocker 2: register the repository under test as a
    # *trusted* project in the isolated $CODEX_HOME so this positive control
    # cannot pass merely because project-local config was excluded from the
    # effective config (the gap the prior PR #1864 smoke had).
    (codex_home / "config.toml").write_text(
        f'[projects."{REPO_ROOT}"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )

    report, parse_error, result = _run_doctor_json(codex_bin, cwd=REPO_ROOT, home=isolated_home, codex_home=codex_home)
    config_check = report.get("checks", {}).get("config.load") if report else None

    artifact_path.write_text(
        json.dumps(
            {
                "skipped": False,
                "codex_version": detail,
                "repo_head_available": True,
                "isolated_home": str(isolated_home),
                "trusted_project": str(REPO_ROOT),
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


def test_loader_smoke_isolated_home_negative_control_root_default_removed(tmp_path: Path) -> None:
    """AC9: negative control -- with the same trusted-project isolated-home
    conditions as the positive control, removing the project's root
    `default_permissions` must make `config.load` fail (not silently pass).
    This proves the positive control is actually exercising project-local
    config, not merely succeeding regardless of its contents."""
    available, detail = _codex_available()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = ARTIFACT_DIR / "loader-smoke-isolated-home-negative-control.json"

    if not available:
        artifact_path.write_text(
            json.dumps({"skipped": True, "reason": detail}, indent=2) + "\n",
            encoding="utf-8",
        )
        pytest.skip(f"Codex CLI unavailable for isolated-home negative control: {detail}")

    codex_bin = shutil.which("codex")

    # Copy the repo's .codex directory (and package.json/AGENTS.md context is
    # not required by the loader) into a fixture repo whose absolute path we
    # register as trusted, then remove the root default there -- never
    # mutate the real REPO_ROOT config.toml for this control.
    fixture_repo = tmp_path / "repo"
    fixture_repo.mkdir()
    shutil.copytree(REPO_ROOT / ".codex", fixture_repo / ".codex")
    config_path = fixture_repo / ".codex" / "config.toml"
    mutated = _remove_root_default(config_path.read_text(encoding="utf-8"))
    assert 'default_permissions = "loop-protocol-personal-dev"' not in mutated
    config_path.write_text(mutated, encoding="utf-8")

    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    codex_home = isolated_home / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        f'[projects."{fixture_repo}"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )

    report, parse_error, result = _run_doctor_json(
        codex_bin, cwd=fixture_repo, home=isolated_home, codex_home=codex_home
    )
    config_check = report.get("checks", {}).get("config.load") if report else None

    artifact_path.write_text(
        json.dumps(
            {
                "skipped": False,
                "codex_version": detail,
                "isolated_home": str(isolated_home),
                "trusted_project": str(fixture_repo),
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
    assert config_check["status"] == "fail", (
        "negative control expected config.load to fail with the root default removed, "
        f"got: {config_check}"
    )

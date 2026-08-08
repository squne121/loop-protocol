from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "check_codex_agent_config.py"
spec = importlib.util.spec_from_file_location("check_codex_agent_config", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


ROOT_SKILL_DIRECTORY_TARGET = "../.claude/skills"

FIXTURE_PATHS = [
    ".agents/skills",
    ".claude/agents",
    ".claude/skills",
    ".codex",
    "scripts/agent-guards/git_mutation_command_policy.py",
    "scripts/agent-guards/hook_repair_hints.py",
    "scripts/check-codex-agents.mjs",
    "scripts/check_codex_agent_config.py",
    "scripts/check_claude_codex_agent_parity.py",
    "tests/fixtures/codex-agent-config",
]


def _replace_root_skill_link(repo: Path, target: str) -> None:
    surface = repo / ".agents/skills"
    surface.unlink()
    os.symlink(target, surface, target_is_directory=True)


def test_root_skill_directory_symlink_contract_passes(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)

    surface = repo / ".agents/skills"
    assert surface.is_symlink()
    assert os.readlink(surface) == ROOT_SKILL_DIRECTORY_TARGET
    assert surface.resolve() == (repo / ".claude/skills").resolve()
    assert module.validate_root_skill_directory_symlink(repo) == []


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("regular_directory", "must be a root skill-directory symlink"),
        ("../.claude/skills/issue-refinement-loop/SKILL.md", "target must be '../.claude/skills'"),
        ("/tmp/agent-skills", "target must be '../.claude/skills'"),
        ("../../outside", "target must be '../.claude/skills'"),
        ("../.claude/missing-skills", "target must be '../.claude/skills'"),
        ("../.claude/agents", "target must be '../.claude/skills'"),
    ],
)
def test_root_skill_directory_symlink_rejects_invalid_topologies(
    tmp_path: Path, target: str, expected: str
):
    repo = _copy_fixture_repo(tmp_path)
    surface = repo / ".agents/skills"
    surface.unlink()
    if target == "regular_directory":
        surface.mkdir()
    else:
        os.symlink(target, surface, target_is_directory=True)

    failures = module.validate_root_skill_directory_symlink(repo)

    assert any(expected in failure for failure in failures)


def test_negative_guard_text_present():
    text = (REPO_ROOT / "scripts" / "check-codex-agents.mjs").read_text(encoding="utf-8")
    assert ".codex/skills: must not exist as a repo-shared skill surface" in text


def test_codex_scope_rollup_runner_dispatch_contract():
    """#1869 fix_delta P1-1 (inverted, was: must contain automatic spawn note):
    scope-rollup-runner is a manual advisory diagnostic, not an automatic
    Step dispatch. The validator must NOT require an automatic spawn note
    for it, and preparation.md must NOT contain the automatic-spawn
    imperative phrase (it may still reference the agent for manual
    invocation instructions)."""
    validator = (REPO_ROOT / "scripts" / "check_impl_review_loop_codex_dispatch.py").read_text(encoding="utf-8")
    preparation = (REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "steps" / "preparation.md").read_text(
        encoding="utf-8"
    )

    assert '".claude/skills/impl-review-loop/steps/preparation.md": "scope-rollup-runner"' not in validator
    assert "assert_no_scope_rollup_runner_auto_spawn_note" in validator
    assert (
        "Codex CLI: spawn the custom agent named scope-rollup-runner for this step; the root thread must not"
        not in preparation
    )
    assert ".codex/agents/scope-rollup-runner.toml" in preparation
    assert "手動" in preparation and "scope-rollup-runner" in preparation


def test_check_impl_review_loop_codex_dispatch_passes_no_auto_spawn_assertion():
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_impl_review_loop_codex_dispatch.py"),
            "--assert-no-scope-rollup-runner-auto-spawn-note",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_scope_rollup_runner_parity_excludes_permission_profile_but_checks_contracts():
    result = subprocess.run(
        [sys.executable, "scripts/check_claude_codex_agent_parity.py", "--strict"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "agent: scope-rollup-runner" in result.stdout
    assert "claude.permissionMode=auto" in result.stdout
    assert "MUTATION_BOUNDARY:" in result.stdout


def test_parity_treats_codex_delegation_prose_as_advisory(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    agent_path = repo / ".codex/agents/issue-creator.toml"
    agent_path.write_text(
        agent_path.read_text(encoding="utf-8").replace(
            "Known limitation",
            "spawn subagents\n\nKnown limitation",
            1,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "scripts/check_claude_codex_agent_parity.py", "--strict"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "authority: advisory" in result.stdout
    assert "codex_intent_hint: allowed" in result.stdout


def _copy_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel_path in FIXTURE_PATHS:
        src = REPO_ROOT / rel_path
        dst = repo / rel_path
        if src.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(src), dst, target_is_directory=True)
        elif src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    web_researcher = repo / ".claude/agents/web-researcher.md"
    web_researcher.write_text(
        web_researcher.read_text(encoding="utf-8")
        + "\n<!-- fixture parity token: agy_grounded_research_only -->\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".agents/skills"], cwd=repo, check=True)
    return repo


def _run_python_validator(
    repo: Path, *, runtime_contract: bool = False
) -> subprocess.CompletedProcess[str]:
    assertion_flags = (
        ["--assert-runtime-contract"]
        if runtime_contract
        else ["--assert-required-fields", "--assert-local-main-branch-guard"]
    )
    return subprocess.run(
        [
            sys.executable,
            "scripts/check_codex_agent_config.py",
            *assertion_flags,
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_js_validator(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "scripts/check-codex-agents.mjs"],
        cwd=repo,
        env={**os.environ, "CODEX_ALLOW_NO_CODEX": "1"},
        text=True,
        capture_output=True,
        check=False,
    )


def test_python_cli_passes_on_fixture_repo(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    result = _run_python_validator(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: Codex agent contract validation passed" in result.stdout


def test_python_cli_detects_wrong_root_skill_target_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    _replace_root_skill_link(repo, "../.claude/agents")

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert "root skill-directory symlink target must be '../.claude/skills'" in result.stdout


def test_python_cli_detects_route_surface_mismatch_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    agent_toml = repo / ".codex/agents/issue-creator.toml"
    agent_toml.write_text(
        agent_toml.read_text(encoding="utf-8").replace(
            "runtime_followup_route: create-issue",
            "runtime_followup_route: edit-issue",
        ),
        encoding="utf-8",
    )

    result = _run_python_validator(repo, runtime_contract=True)

    assert result.returncode == 1
    assert "runtime_followup_route expected 'create-issue' got 'edit-issue'" in result.stdout


@pytest.mark.parametrize(
    ("config_text", "diagnostic"),
    [
        ('features = "not-a-table"\n', "[features] must be a table"),
        ("[features.multi_agent_v2\nenabled = true\n", "malformed TOML"),
    ],
)
def test_python_runtime_contract_reports_invalid_config_without_traceback(
    tmp_path: Path,
    config_text: str,
    diagnostic: str,
):
    repo = _copy_fixture_repo(tmp_path)
    (repo / ".codex/config.toml").write_text(config_text, encoding="utf-8")

    result = _run_python_validator(repo, runtime_contract=True)

    assert result.returncode == 1
    assert diagnostic in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_python_runtime_contract_reports_missing_config_without_traceback(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    (repo / ".codex/config.toml").unlink()

    result = _run_python_validator(repo, runtime_contract=True)

    assert result.returncode == 1
    assert "TOML file not found" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_python_cli_detects_missing_passive_session_hook_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    hooks_path = repo / ".codex/hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"].pop("SessionEnd")
    hooks_path.write_text(json.dumps(hooks, indent=2), encoding="utf-8")

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert "active hooks must be the passive SessionEnd/SubagentStop allowlist" in result.stdout


def test_python_cli_detects_extra_hooks_root_metadata_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    hooks_path = repo / ".codex/hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"]["PreToolUse"] = []
    hooks_path.write_text(json.dumps(hooks, indent=2), encoding="utf-8")

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert "active hooks must be the passive SessionEnd/SubagentStop allowlist" in result.stdout


def test_python_cli_detects_active_pretool_hook_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    hooks_path = repo / ".codex/hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"]["PreToolUse"] = []
    hooks_path.write_text(json.dumps(hooks, indent=2), encoding="utf-8")

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert "active hooks must be the passive SessionEnd/SubagentStop allowlist" in result.stdout
    assert "command enforcement must use standard sandbox/approval, not repo hooks" in result.stdout


def test_python_cli_detects_parity_failure_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    (repo / ".claude/agents/issue-creator.md").unlink()

    result = subprocess.run(
        [sys.executable, "scripts/check_claude_codex_agent_parity.py", "--strict"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "missing claude agent file" in result.stdout


def test_js_cli_passes_on_fixture_repo(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    result = _run_js_validator(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok 15 agents validated" in result.stdout


def test_js_cli_detects_regular_directory_skill_surface_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    surface = repo / ".agents/skills"
    surface.unlink()
    surface.mkdir()

    result = _run_js_validator(repo)

    assert result.returncode == 1
    assert "must be a root skill-directory symlink" in result.stdout + result.stderr


def test_js_cli_detects_broken_root_skill_target_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    _replace_root_skill_link(repo, "../.claude/missing-skills")

    result = _run_js_validator(repo)

    assert result.returncode == 1
    assert "root skill-directory symlink target is broken" in result.stdout + result.stderr

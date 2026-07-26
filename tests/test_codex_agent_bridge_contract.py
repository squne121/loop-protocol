from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "check_codex_agent_config.py"
spec = importlib.util.spec_from_file_location("check_codex_agent_config", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


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


def _bridge_text(target: str, extra: str = "") -> str:
    return f"""---
name: sample
description: sample
---

# Sample

This file is a derived/non-canonical thin wrapper for the Codex repo-local discovery surface.
Before executing this skill, read the canonical body at `{target}`.
Do not treat this wrapper as the workflow procedure body.
{extra}"""


def test_missing_marker_detected(tmp_path: Path):
    surface = tmp_path / ".agents/skills/create-issue/SKILL.md"
    canonical = tmp_path / ".claude/skills/create-issue/SKILL.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("ok", encoding="utf-8")
    surface.parent.mkdir(parents=True)
    surface.write_text(_bridge_text("../../../.claude/skills/create-issue/SKILL.md").replace("derived/non-canonical ", ""), encoding="utf-8")
    assert any("derived/non-canonical marker required" in failure for failure in module.validate_bridge_surface(surface))


def test_missing_imperative_detected(tmp_path: Path):
    surface = tmp_path / ".agents/skills/create-issue/SKILL.md"
    canonical = tmp_path / ".claude/skills/create-issue/SKILL.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("ok", encoding="utf-8")
    surface.parent.mkdir(parents=True)
    text = _bridge_text("../../../.claude/skills/create-issue/SKILL.md").replace("Before executing this skill, read the canonical body at", "Read")
    surface.write_text(text, encoding="utf-8")
    assert any("exact imperative required" in failure for failure in module.validate_bridge_surface(surface))


def test_wrong_target_detected(tmp_path: Path):
    surface = tmp_path / ".agents/skills/create-issue/SKILL.md"
    surface.parent.mkdir(parents=True)
    surface.write_text(_bridge_text("../../../.claude/skills/edit-issue/SKILL.md"), encoding="utf-8")
    assert any("wrong skill target" in failure for failure in module.validate_bridge_surface(surface))


def test_target_missing_detected(tmp_path: Path):
    surface = tmp_path / ".agents/skills/create-issue/SKILL.md"
    surface.parent.mkdir(parents=True)
    surface.write_text(_bridge_text("../../../.claude/skills/create-issue/SKILL.md"), encoding="utf-8")
    assert any("canonical skill body target missing" in failure for failure in module.validate_bridge_surface(surface))


def test_stale_procedure_body_detected(tmp_path: Path):
    surface = tmp_path / ".agents/skills/create-issue/SKILL.md"
    canonical = tmp_path / ".claude/skills/create-issue/SKILL.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("ok", encoding="utf-8")
    surface.parent.mkdir(parents=True)
    surface.write_text(_bridge_text("../../../.claude/skills/create-issue/SKILL.md", extra="\n## Procedure\n- step\n"), encoding="utf-8")
    assert any("stale procedure body detected" in failure for failure in module.validate_bridge_surface(surface))


def test_body_bloat_detected(tmp_path: Path):
    surface = tmp_path / ".agents/skills/create-issue/SKILL.md"
    canonical = tmp_path / ".claude/skills/create-issue/SKILL.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("ok", encoding="utf-8")
    surface.parent.mkdir(parents=True)
    surface.write_text(_bridge_text("../../../.claude/skills/create-issue/SKILL.md", extra="\nExtra line.\n"), encoding="utf-8")
    assert any("body bloat detected" in failure for failure in module.validate_bridge_surface(surface))


def test_duplicate_target_detected(tmp_path: Path):
    canonical = tmp_path / ".claude/skills/create-issue/SKILL.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("ok", encoding="utf-8")
    first = tmp_path / ".agents/skills/create-issue/SKILL.md"
    second = tmp_path / ".agents/skills/edit-issue/SKILL.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    target = "../../../.claude/skills/create-issue/SKILL.md"
    first.write_text(_bridge_text(target), encoding="utf-8")
    second.write_text(_bridge_text(target), encoding="utf-8")
    assert any("duplicate canonical target" in failure for failure in module.find_duplicate_canonical_targets([first, second]))


def test_negative_guard_text_present():
    text = (REPO_ROOT / "scripts" / "check-codex-agents.mjs").read_text(encoding="utf-8")
    assert ".codex/skills: must not exist as a repo-shared skill surface" in text


def test_codex_scope_rollup_runner_dispatch_contract():
    validator = (REPO_ROOT / "scripts" / "check_impl_review_loop_codex_dispatch.py").read_text(encoding="utf-8")
    preparation = (REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "steps" / "preparation.md").read_text(
        encoding="utf-8"
    )

    assert '".claude/skills/impl-review-loop/steps/preparation.md": "scope-rollup-runner"' in validator
    assert "Codex CLI: spawn the custom agent named scope-rollup-runner for this step; the root thread must not" in preparation
    assert "generic/default/worker fallback" in preparation
    assert ".codex/agents/scope-rollup-runner.toml" in preparation


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


def _copy_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel_path in FIXTURE_PATHS:
        src = REPO_ROOT / rel_path
        dst = repo / rel_path
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    web_researcher = repo / ".claude/agents/web-researcher.md"
    web_researcher.write_text(
        web_researcher.read_text(encoding="utf-8")
        + "\n<!-- fixture parity token: grounded_research_or_direct_web -->\n",
        encoding="utf-8",
    )
    issue_author = repo / ".claude/agents/issue-author.md"
    issue_author.write_text(
        issue_author.read_text(encoding="utf-8")
        + "\n## 出力契約（ISSUE_AUTHOR_RESULT_COMPACT_V1）\n\nfixture parity marker.\n",
        encoding="utf-8",
    )
    return repo


def _run_python_validator(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "scripts/check_codex_agent_config.py",
            "--assert-required-fields",
            "--assert-runtime-contract",
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


def test_python_cli_detects_missing_marker_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    bridge = repo / ".agents/skills/create-issue/SKILL.md"
    bridge.write_text(bridge.read_text(encoding="utf-8").replace("derived/non-canonical ", ""), encoding="utf-8")

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert "derived/non-canonical marker required" in result.stdout


def test_python_cli_detects_route_surface_mismatch_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    agent_toml = repo / ".codex/agents/issue-author.toml"
    agent_toml.write_text(
        agent_toml.read_text(encoding="utf-8").replace(
            "runtime_followup_route: create-issue|edit-issue",
            "runtime_followup_route: create-issue",
        ),
        encoding="utf-8",
    )

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert "runtime_followup_route expected 'create-issue|edit-issue' got 'create-issue'" in result.stdout


def test_python_cli_detects_missing_subagent_hook_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    hooks_path = repo / ".codex/hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"].pop("SubagentStart")
    hooks_path.write_text(json.dumps(hooks, indent=2), encoding="utf-8")

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert ".codex/hooks.json: missing hooks for SubagentStart" in result.stdout


def test_python_cli_detects_extra_hooks_root_metadata_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    hooks_path = repo / ".codex/hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["fastpathContract"] = {}
    hooks_path.write_text(json.dumps(hooks, indent=2), encoding="utf-8")

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert ".codex/hooks.json: root keys must be exactly" in result.stdout


def test_python_cli_detects_extra_pretool_handler_field_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    hooks_path = repo / ".codex/hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"]["PreToolUse"][0]["hooks"][0]["async"] = True
    hooks_path.write_text(json.dumps(hooks, indent=2), encoding="utf-8")

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert "must exactly match expected PreToolUse handler matrix" in result.stdout
    assert "keys must be exactly" in result.stdout


def test_python_cli_detects_parity_failure_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    (repo / ".claude/agents/issue-author.md").unlink()

    result = _run_python_validator(repo)

    assert result.returncode == 1
    assert "scripts/check_claude_codex_agent_parity.py: parity validation failed" in result.stdout


def test_js_cli_passes_on_fixture_repo(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    result = _run_js_validator(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok 14 agents validated" in result.stdout


def test_js_cli_detects_missing_marker_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    bridge = repo / ".agents/skills/create-issue/SKILL.md"
    bridge.write_text(bridge.read_text(encoding="utf-8").replace("derived/non-canonical ", ""), encoding="utf-8")

    result = _run_js_validator(repo)

    assert result.returncode == 1
    assert "derived/non-canonical marker required" in result.stdout + result.stderr


def test_js_cli_detects_duplicate_canonical_target_via_subprocess(tmp_path: Path):
    repo = _copy_fixture_repo(tmp_path)
    second_bridge = repo / ".agents/skills/edit-issue/SKILL.md"
    second_bridge.write_text(
        second_bridge.read_text(encoding="utf-8").replace(
            "../../../.claude/skills/edit-issue/SKILL.md",
            "../../../.claude/skills/create-issue/SKILL.md",
        ),
        encoding="utf-8",
    )

    result = _run_js_validator(repo)

    assert result.returncode == 1
    assert "duplicate canonical target:" in result.stdout + result.stderr

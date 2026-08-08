"""Issue #1952: Codex issue-creator / issue-editor cutover contract."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / ".codex" / "agents"
ROOT_SKILL_SURFACE = REPO_ROOT / ".agents" / "skills"
CANONICAL_SKILLS = REPO_ROOT / ".claude" / "skills"
ACTIVE_CALLER_PATHS = (
    ".claude/skills/create-issue/references/body-authoring.md",
    ".claude/skills/issue-refinement-loop/references/scope-signal-guard.md",
    ".claude/skills/issue-refinement-loop/references/termination-policy.md",
    ".claude/skills/issue-refinement-loop/schemas/contract_patch_plan_v1.schema.json",
)


def _load_agent(name: str) -> dict:
    return tomllib.loads((AGENTS_DIR / f"{name}.toml").read_text(encoding="utf-8"))


def _instructions(name: str) -> str:
    return str(_load_agent(name)["developer_instructions"])


def test_root_skill_symlink_identity_and_hash_contract():
    """AC3: creator/editor routes share the one canonical Skill surface."""
    assert ROOT_SKILL_SURFACE.is_symlink()
    assert ROOT_SKILL_SURFACE.readlink().as_posix() == "../.claude/skills"
    assert ROOT_SKILL_SURFACE.resolve() == CANONICAL_SKILLS.resolve()
    assert not (REPO_ROOT / ".codex" / "skills").exists()

    mode = subprocess.run(
        ["git", "ls-files", "-s", "--", ".agents/skills"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[0]
    assert mode == "120000"

    for role, skill in (("issue-creator", "create-issue"), ("issue-editor", "edit-issue")):
        routed = ROOT_SKILL_SURFACE / skill / "SKILL.md"
        canonical = CANONICAL_SKILLS / skill / "SKILL.md"
        assert routed.samefile(canonical)
        assert hashlib.sha256(routed.read_bytes()).hexdigest() == hashlib.sha256(canonical.read_bytes()).hexdigest()
        assert f".agents/skills/{skill}/SKILL.md" in _instructions(role)


def test_creator_editor_controlled_executor_chains():
    """AC1/AC2/AC4: each intent has one role, one Skill, and one executor."""
    expectations = {
        "issue-creator": ("create-issue", "create_issue_txn.py", ".claude/agents/issue-creator.md"),
        "issue-editor": ("edit-issue", "edit_issue_txn.py", ".claude/agents/issue-editor.md"),
    }
    fixture = json.loads(
        (REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json").read_text(encoding="utf-8")
    )["required_agents"]

    assert not (AGENTS_DIR / "issue-author.toml").exists()
    assert not (REPO_ROOT / ".claude/agents/issue-author.md").exists()
    assert "issue-author" not in fixture

    for role, (route, executor, claude_path) in expectations.items():
        agent = _load_agent(role)
        instructions = _instructions(role)
        expected = fixture[role]
        assert agent["name"] == role
        assert agent["default_permissions"] == "loop-protocol-rtk"
        assert expected["runtime_followup_route"] == route
        assert expected["repo_local_skill_surfaces"] == [f".agents/skills/{route}/SKILL.md"]
        assert f"runtime_followup_route: {route}" in instructions
        assert executor in instructions
        assert (REPO_ROOT / ".claude/skills" / route / "scripts" / executor).is_file()
        assert "postcondition readback" in instructions
        assert "direct gh bypass" in instructions
        assert "fail-closed local guardrail" in instructions
        assert "security boundary" in instructions
        assert (REPO_ROOT / claude_path).is_file()

    assert "existing Issue の更新 intent は mutation 前に拒否する" in _instructions("issue-creator")
    assert "new Issue creation intent は mutation 前に拒否する" in _instructions("issue-editor")


def test_active_caller_inventory_has_no_legacy_issue_author_reference():
    """AC2: active caller/owner surfaces select creator or editor explicitly."""
    legacy_hits = {
        relative: (REPO_ROOT / relative).read_text(encoding="utf-8")
        for relative in ACTIVE_CALLER_PATHS
        if "issue-author" in (REPO_ROOT / relative).read_text(encoding="utf-8")
    }
    assert legacy_hits == {}

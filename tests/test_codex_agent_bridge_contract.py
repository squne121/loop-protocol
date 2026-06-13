from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "check_codex_agent_config.py"
spec = importlib.util.spec_from_file_location("check_codex_agent_config", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


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

"""Codex project-local passive hook surface regression tests."""

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_JSON = REPO_ROOT / ".codex" / "hooks.json"
CONFIG_TOML = REPO_ROOT / ".codex" / "config.toml"
VALIDATOR = REPO_ROOT / "scripts" / "session-recording" / "validate-codex-hooks.mjs"


def test_hooks_json_is_passive_allowlist() -> None:
    hooks = json.loads(HOOKS_JSON.read_text())["hooks"]
    assert sorted(hooks) == ["SessionEnd", "SubagentStop"]
    assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] <= 3
    assert hooks["SubagentStop"][0]["hooks"][0]["timeout"] <= 3


def test_project_config_keeps_hooks_and_standard_approval() -> None:
    text = CONFIG_TOML.read_text()
    assert 'approval_policy = "on-request"' in text
    assert "hooks = true" in text
    assert "default_permissions =" not in text


def test_passive_hook_validator_passes() -> None:
    result = subprocess.run(
        ["node", str(VALIDATOR), str(HOOKS_JSON)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_config_does_not_define_inline_hooks() -> None:
    active_lines = [
        line.strip() for line in CONFIG_TOML.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "[hooks]" not in active_lines

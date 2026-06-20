"""
Structural validation tests for Codex hook surface guard (#1020).
Verifies .codex/hooks.json is the single source for hooks and
.codex/config.toml does not contain inline hooks.
"""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_JSON = REPO_ROOT / ".codex" / "hooks.json"
CONFIG_TOML = REPO_ROOT / ".codex" / "config.toml"


def load_hooks():
    return json.loads(HOOKS_JSON.read_text())


# ---------------------------------------------------------------------------
# AC1: config.toml must not have a [hooks] section
# ---------------------------------------------------------------------------

def test_config_toml_no_hooks_section():
    """AC1: .codex/config.toml must not define a [hooks] section."""
    text = CONFIG_TOML.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert stripped != "[hooks]", (
            "config.toml must not define a [hooks] section; hooks belong in .codex/hooks.json"
        )


def test_config_toml_mentions_hooks_json():
    """AC1 corollary: config.toml should document that hooks live in hooks.json."""
    text = CONFIG_TOML.read_text()
    assert ".codex/hooks.json" in text, (
        "config.toml must mention .codex/hooks.json as the hook surface"
    )


# ---------------------------------------------------------------------------
# AC2: .codex/hooks.json must have PreToolUse with nested hooks[] arrays
# ---------------------------------------------------------------------------

def test_hooks_json_pretooluse_nesting():
    """AC2: hooks.json must have hooks.PreToolUse[] -> hooks[] nesting."""
    data = load_hooks()
    entries = data.get("hooks", {}).get("PreToolUse", [])
    assert isinstance(entries, list) and len(entries) >= 1, (
        "hooks.json: hooks.PreToolUse must be a non-empty array"
    )
    for entry in entries:
        nested = entry.get("hooks", [])
        assert isinstance(nested, list) and len(nested) >= 1, (
            f"hooks.json: PreToolUse entry with matcher '{entry.get('matcher')}' must have nested hooks[]"
        )


# ---------------------------------------------------------------------------
# AC3: SubagentStart / PreToolUse / PermissionRequest / Stop / SubagentStop
#       must all be present and non-empty
# ---------------------------------------------------------------------------

REQUIRED_EVENTS = ["SubagentStart", "PreToolUse", "PermissionRequest", "Stop", "SubagentStop"]


@pytest.mark.parametrize("event", REQUIRED_EVENTS)
def test_hooks_json_required_events_present(event):
    """AC3: All required hook events must be present as non-empty arrays."""
    data = load_hooks()
    entries = data.get("hooks", {}).get(event, [])
    assert isinstance(entries, list), f"hooks.json: hooks.{event} must be an array"
    assert len(entries) >= 1, f"hooks.json: hooks.{event} must have at least one entry"


# ---------------------------------------------------------------------------
# AC4: local_main_branch_guard must only appear in hooks.json, not config.toml
# ---------------------------------------------------------------------------

def test_local_main_branch_guard_not_in_config_toml():
    """AC4: local_main_branch_guard must not be defined in config.toml."""
    text = CONFIG_TOML.read_text()
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        assert "local_main_branch_guard" not in line, (
            "local_main_branch_guard must be defined in .codex/hooks.json only, not config.toml"
        )


# ---------------------------------------------------------------------------
# AC5: hooks.json root structure
# ---------------------------------------------------------------------------

def test_hooks_json_root_structure():
    """AC5 corollary: hooks.json root must be {hooks: {...}} structure."""
    data = load_hooks()
    assert isinstance(data, dict), "hooks.json root must be a JSON object"
    assert "hooks" in data, "hooks.json must have a 'hooks' key at root"
    assert isinstance(data["hooks"], dict), "hooks.json 'hooks' value must be an object"

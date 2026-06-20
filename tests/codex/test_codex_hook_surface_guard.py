"""
Structural validation tests for Codex hook surface guard (#1020).
Verifies .codex/hooks.json is the single source for hooks and
.codex/config.toml does not contain inline hooks.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_JSON = REPO_ROOT / ".codex" / "hooks.json"
CONFIG_TOML = REPO_ROOT / ".codex" / "config.toml"
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check-codex-agents.mjs"


def _run_validator(env_override: dict | None = None) -> subprocess.CompletedProcess:
    """Run check-codex-agents.mjs and return CompletedProcess."""
    env = os.environ.copy()
    env["CODEX_ALLOW_NO_CODEX"] = "1"
    if env_override:
        env.update(env_override)
    return subprocess.run(
        ["node", str(CHECK_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _make_fixture_repo(tmp_path: Path) -> Path:
    """
    Create a minimal repo fixture under tmp_path that mirrors the real repo structure.
    Directories that the validator reads but are not under test are symlinked to the
    real repo so the fixture stays valid for those parts.  The caller is responsible
    for writing the specific file(s) that should be incorrect.
    """
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)

    # Symlink sub-directories we are NOT testing so the validator can read them.
    for sub in ("agents", "rules", "hooks"):
        src = REPO_ROOT / ".codex" / sub
        if src.exists():
            (codex_dir / sub).symlink_to(src)

    # Symlink .agents/skills (canonical skill surface validator checks)
    agents_skills_src = REPO_ROOT / ".agents" / "skills"
    if agents_skills_src.exists():
        agents_dir = tmp_path / ".agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "skills").symlink_to(agents_skills_src)

    # Symlink .claude (canonical skill body target for relative symlinks in .agents/skills)
    claude_src = REPO_ROOT / ".claude"
    if claude_src.exists():
        (tmp_path / ".claude").symlink_to(claude_src)

    # Symlink artifacts/ (ledger path)
    artifacts_src = REPO_ROOT / "artifacts"
    if artifacts_src.exists():
        (tmp_path / "artifacts").symlink_to(artifacts_src)

    # Copy the real hooks.json and config.toml as defaults; callers may overwrite.
    (codex_dir / "config.toml").write_text(CONFIG_TOML.read_text())
    (codex_dir / "hooks.json").write_text(HOOKS_JSON.read_text())

    return tmp_path


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


@pytest.mark.parametrize("header", [
    "[hooks]",
    "[hooks.PreToolUse]",
    "[[hooks.PreToolUse]]",
    "[[hooks.PreToolUse.hooks]]",
    "[[hooks.PermissionRequest]]",
    "[[hooks.Stop]]",
    "[[hooks.SubagentStop]]",
])
def test_config_toml_hooks_array_of_tables_rejected(header, tmp_path):
    """AC1 negative: config.toml with any [[hooks.*]] header must cause validator to fail."""
    fixture_root = _make_fixture_repo(tmp_path)

    # Overwrite config.toml with the offending header appended.
    base_text = CONFIG_TOML.read_text()
    patched = base_text + f"\n{header}\ncommand = \"echo bad\"\n"
    (fixture_root / ".codex" / "config.toml").write_text(patched)

    result = _run_validator(env_override={"REPO_ROOT_OVERRIDE": str(fixture_root)})
    assert result.returncode != 0, (
        f"Validator must exit non-zero when config.toml contains '{header}' hooks header.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
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
# AC3 negative: handlers with empty objects (no "command") must fail validation
# ---------------------------------------------------------------------------

def _make_hooks_json_with_empty_handler(event_key: str) -> dict:
    """Build a hooks.json where the given event has a handler with no 'command'."""
    base = json.loads(HOOKS_JSON.read_text())
    # Replace the event entry with an array-of-tables entry having an empty object handler
    base["hooks"][event_key] = [{"matcher": ".*", "hooks": [{}]}]
    return base


@pytest.mark.parametrize("event_key", ["PermissionRequest", "Stop", "SubagentStop"])
def test_hooks_json_empty_handler_command_rejected(event_key, tmp_path):
    """AC3 negative: handler with no 'command' field (e.g. [{}]) must be detected as invalid."""
    fixture_root = _make_fixture_repo(tmp_path)

    # Overwrite hooks.json with the patched version that has an empty handler.
    patched = _make_hooks_json_with_empty_handler(event_key)
    (fixture_root / ".codex" / "hooks.json").write_text(json.dumps(patched))

    result = _run_validator(env_override={"REPO_ROOT_OVERRIDE": str(fixture_root)})
    assert result.returncode != 0, (
        f"Validator must exit non-zero when hooks.json has empty handler command for {event_key}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert event_key in combined, (
        f"Validator error output must mention '{event_key}'.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


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

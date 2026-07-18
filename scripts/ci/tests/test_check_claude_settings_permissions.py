"""Tests for the .claude/settings.json permissions validator (Issue #1551).

Covers:
  - AC1: repository .claude/settings.json has no scoped Write(...) rule
  - AC4: bare Write accepted, hooks matcher not inspected
  - AC5: reject/accept/malformed fixture matrix
  - AC6: full test suite entry point
  - AC7: production CLI invocation against the real repository settings.json
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_VALIDATOR_PATH = _REPO_ROOT / "scripts" / "ci" / "check_claude_settings_permissions.py"
_REAL_SETTINGS_PATH = _REPO_ROOT / ".claude" / "settings.json"

# Issue #1551 AC2: these canonical Edit(...) deny entries must always be
# present in the repository's .claude/settings.json permissions.deny.
REQUIRED_CANONICAL_EDIT_DENIES = {
    "Edit(assets/**)",
    "Edit(LICENSES/**)",
    "Edit(.env)",
    "Edit(.env.*)",
    "Edit(secrets/**)",
    "Edit(**/.ssh/**)",
    "Edit(**/.config/gh/**)",
}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_claude_settings_permissions_under_test", _VALIDATOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _write_settings(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _run_cli(*args: str):
    return subprocess.run(
        [sys.executable, str(_VALIDATOR_PATH), *args],
        capture_output=True,
        text=True,
    )


# --- AC1: real repository settings.json has no scoped Write(...) rule ---


def test_repository_settings_have_no_scoped_write_rule():
    exit_code, diagnostics = mod.run_validation(_REAL_SETTINGS_PATH)
    assert exit_code == 0, f"unexpected violations: {diagnostics}"
    assert diagnostics == []


# --- AC2 regression guard: required canonical Edit(...) denies present ---


def test_repository_settings_retain_required_canonical_edit_denies():
    settings = json.loads(_REAL_SETTINGS_PATH.read_text(encoding="utf-8"))
    deny = set(settings.get("permissions", {}).get("deny", []))
    assert REQUIRED_CANONICAL_EDIT_DENIES <= deny, (
        f"missing: {REQUIRED_CANONICAL_EDIT_DENIES - deny}"
    )


@pytest.mark.parametrize("removed_entry", sorted(REQUIRED_CANONICAL_EDIT_DENIES))
def test_missing_canonical_edit_deny_is_detected(removed_entry):
    settings = json.loads(_REAL_SETTINGS_PATH.read_text(encoding="utf-8"))
    data = copy.deepcopy(settings)
    deny_list = data["permissions"]["deny"]
    assert removed_entry in deny_list
    data["permissions"]["deny"] = [e for e in deny_list if e != removed_entry]

    missing = mod.find_missing_canonical_edit_denies(data)
    assert removed_entry in missing


def test_find_missing_canonical_edit_denies_empty_when_all_present():
    settings = json.loads(_REAL_SETTINGS_PATH.read_text(encoding="utf-8"))
    missing = mod.find_missing_canonical_edit_denies(settings)
    assert missing == []


# --- AC4: bare Write accepted, hooks matcher ignored ---


def test_accepts_bare_write_and_ignores_hook_matcher(tmp_path):
    settings = {
        "permissions": {
            "allow": ["Write", "Bash(pnpm test)"],
            "ask": [],
            "deny": ["Edit(assets/**)"],
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [{"type": "command", "command": "some-hook.sh"}],
                }
            ]
        },
    }
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 0
    assert diagnostics == []


def test_ignores_write_like_strings_outside_permission_arrays(tmp_path):
    settings = {
        "permissions": {"allow": ["Write"], "ask": [], "deny": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo Write(unrelated) is not a permission rule",
                        }
                    ],
                }
            ]
        },
    }
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 0
    assert diagnostics == []


# --- AC5: fixture matrix (accept / reject / malformed) ---


@pytest.mark.parametrize("key", ["allow", "ask", "deny"])
def test_scoped_write_rejected_in_each_array(tmp_path, key):
    settings = {"permissions": {"allow": [], "ask": [], "deny": []}}
    settings["permissions"][key] = ["Write(.env)"]
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 1
    assert diagnostics == [f"permissions.{key}[0]"]


@pytest.mark.parametrize(
    "entry",
    [
        "Write(assets/**)",
        "Write(.env)",
        "Write(.env.*)",
        "Write(secrets/**)",
        "Write(**/.ssh/**)",
        "Write(**/.config/gh/**)",
    ],
)
def test_scoped_write_variants_rejected(tmp_path, entry):
    settings = {"permissions": {"allow": [], "ask": [], "deny": [entry]}}
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 1
    assert diagnostics == ["permissions.deny[0]"]


def test_fixture_matrix_accept_and_reject(tmp_path):
    accept_settings = {
        "permissions": {
            "allow": ["Write", "Edit(/assets/**)", "Bash(pnpm test)"],
            "ask": [],
            "deny": ["Edit(LICENSES/**)"],
        },
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "noop.sh"}]}
            ]
        },
    }
    accept_path = _write_settings(tmp_path, accept_settings)
    exit_code, diagnostics = mod.run_validation(accept_path)
    assert exit_code == 0
    assert diagnostics == []


def test_malformed_json_raises_shape_error(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not valid json", encoding="utf-8")
    exit_code, diagnostics = mod.run_validation(p)
    assert exit_code == 2
    assert diagnostics


def test_non_string_entry_raises_shape_error(tmp_path):
    settings = {"permissions": {"allow": [123], "ask": [], "deny": []}}
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 2
    assert diagnostics


def test_non_object_root_raises_shape_error(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    exit_code, diagnostics = mod.run_validation(p)
    assert exit_code == 2
    assert diagnostics


def test_non_array_permission_key_raises_shape_error(tmp_path):
    settings = {"permissions": {"allow": "not-an-array", "ask": [], "deny": []}}
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 2
    assert diagnostics


def test_missing_permissions_block_is_valid(tmp_path):
    settings = {"hooks": {}}
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 0
    assert diagnostics == []


# --- AC3: explicit null / non-dict permissions is a shape error, distinct
# --- from the key being absent entirely ---


def test_null_permissions_is_shape_error(tmp_path):
    settings = {"permissions": None}
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 2
    assert diagnostics


def test_list_permissions_is_shape_error(tmp_path):
    settings = {"permissions": []}
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 2
    assert diagnostics


def test_string_permissions_is_shape_error(tmp_path):
    settings = {"permissions": "not-an-object"}
    path = _write_settings(tmp_path, settings)
    exit_code, diagnostics = mod.run_validation(path)
    assert exit_code == 2
    assert diagnostics


def test_missing_settings_file_raises_shape_error(tmp_path):
    exit_code, diagnostics = mod.run_validation(tmp_path / "nope.json")
    assert exit_code == 2
    assert diagnostics


# --- CLI invocation ---


def test_cli_invocation_against_repository_settings():
    proc = _run_cli("--settings", str(_REAL_SETTINGS_PATH))
    assert proc.returncode == 0, proc.stderr


def test_cli_exit_code_1_on_violation(tmp_path):
    settings = {"permissions": {"allow": [], "ask": [], "deny": ["Write(.env)"]}}
    path = _write_settings(tmp_path, settings)
    proc = _run_cli("--settings", str(path))
    assert proc.returncode == 1
    assert "permissions.deny[0]" in proc.stderr


def test_cli_exit_code_2_on_malformed_json(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not valid json", encoding="utf-8")
    proc = _run_cli("--settings", str(p))
    assert proc.returncode == 2


def test_cli_requires_settings_flag():
    proc = _run_cli()
    assert proc.returncode == 2

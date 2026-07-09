"""Tests for .claude/hooks/root_temporary_residue_advisory.sh."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / ".claude" / "hooks" / "root_temporary_residue_advisory.sh"

TRIGGER_PATHS = [
    ".tmp/result.json",
    ".temp/cache/index.txt",
    ".tmp-agent/session/output.md",
]
NON_TRIGGER_PATHS = [
    "tmp/session/output.md",
    ".claude/tmp/session/output.md",
    "docs/dev/repository-folder-policy.md",
]


def _make_input_write(file_path: str = "") -> str:
    return json.dumps({"tool_name": "Write", "tool_input": {"file_path": file_path}})


def _make_input_edit(file_path: str = "") -> str:
    return json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": file_path, "old_string": "a", "new_string": "b"},
        }
    )


def _make_input_bash(command: str = "") -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _run_hook(stdin_data: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=stdin_data,
        capture_output=True,
        text=True,
    )


def _parse_inner(result: subprocess.CompletedProcess) -> dict:
    outer = json.loads(result.stdout)
    hso = outer["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    ctx = hso["additionalContext"]
    assert ctx.startswith("REPO_TEMP_FOLDER_ADVICE_V1 ")
    return json.loads(ctx[len("REPO_TEMP_FOLDER_ADVICE_V1 "):])


@pytest.mark.parametrize("trigger_path", TRIGGER_PATHS)
def test_write_tmp_alias_triggers_advisory(trigger_path: str):
    result = _run_hook(_make_input_write(trigger_path))
    assert result.returncode == 0
    assert result.stdout.strip()
    inner = _parse_inner(result)
    assert inner["schema"] == "REPO_TEMP_FOLDER_ADVICE_V1"
    assert inner["block"] is False
    assert inner["approved_replacement"] == "tmp/"
    assert inner["cleanup_required"] is True


def test_edit_tmp_alias_triggers_advisory():
    result = _run_hook(_make_input_edit(".tmp/session/notes.md"))
    assert result.returncode == 0
    inner = _parse_inner(result)
    assert inner["observed_path"] == ".tmp/"


def test_bash_tmp_alias_triggers_advisory():
    result = _run_hook(_make_input_bash("mkdir -p .temp/cache"))
    assert result.returncode == 0
    inner = _parse_inner(result)
    assert inner["observed_path"] == ".temp/"


@pytest.mark.parametrize("non_trigger_path", NON_TRIGGER_PATHS)
def test_non_trigger_paths_produce_no_output(non_trigger_path: str):
    result = _run_hook(_make_input_write(non_trigger_path))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_invalid_json_fails_open():
    result = _run_hook("{")
    assert result.returncode == 0
    assert result.stdout.strip() == ""

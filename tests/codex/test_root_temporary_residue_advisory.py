"""Tests for .codex/hooks/root_temporary_residue_advisory.sh."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".codex" / "hooks" / "root_temporary_residue_advisory.sh"


def _make_input_write(file_path: str = "") -> str:
    return json.dumps(
        {"cwd": str(REPO_ROOT), "tool_name": "Write", "tool_input": {"file_path": file_path}}
    )


def _make_input_apply_patch(command: str = "") -> str:
    return json.dumps(
        {"cwd": str(REPO_ROOT), "tool_name": "apply_patch", "tool_input": {"command": command}}
    )


def _make_input_bash(command: str = "") -> str:
    return json.dumps({"cwd": str(REPO_ROOT), "tool_name": "Bash", "tool_input": {"command": command}})


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


@pytest.mark.parametrize(
    "file_path, expected",
    [
        (str(REPO_ROOT / ".tmp" / "out.json"), ".tmp/"),
        (str(REPO_ROOT / ".tmp-agent" / "out.json"), ".tmp-*/"),
        (str(REPO_ROOT / ".temp" / "out.json"), ".temp/"),
    ],
)
def test_write_tmp_alias_triggers_advisory(file_path: str, expected: str):
    result = _run_hook(_make_input_write(file_path))
    assert result.returncode == 0
    assert result.stdout.strip()
    inner = _parse_inner(result)
    assert inner["schema"] == "REPO_TEMP_FOLDER_ADVICE_V1"
    assert inner["block"] is False
    assert inner["observed_path"] == expected


def test_apply_patch_tmp_alias_triggers_advisory():
    cmd = "*** Begin Patch\n*** Add File: .tmp/notes.md\n+hello\n*** End Patch\n"
    result = _run_hook(_make_input_apply_patch(cmd))
    assert result.returncode == 0
    inner = _parse_inner(result)
    assert inner["observed_path"] == ".tmp/"


def test_bash_tmp_alias_triggers_advisory():
    result = _run_hook(_make_input_bash("python tool.py --out=.tmp-run/result.json"))
    assert result.returncode == 0
    inner = _parse_inner(result)
    assert inner["observed_path"] == ".tmp-*/"


def test_bash_read_only_and_cleanup_commands_are_silent():
    for command in ("cat .tmp/report.json", "ls .tmp", "rm -rf .tmp"):
        result = _run_hook(_make_input_bash(command))
        assert result.returncode == 0
        assert result.stdout.strip() == ""


def test_bash_subdirectory_local_tmp_is_silent():
    stdin_data = json.dumps(
        {
            "cwd": str(REPO_ROOT / "src"),
            "tool_name": "Bash",
            "tool_input": {"command": "mkdir -p .tmp/cache"},
        }
    )
    result = _run_hook(stdin_data)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_bash_subdirectory_root_escape_triggers_advisory():
    stdin_data = json.dumps(
        {
            "cwd": str(REPO_ROOT / "src"),
            "tool_name": "Bash",
            "tool_input": {"command": "mkdir -p ../.tmp/cache"},
        }
    )
    result = _run_hook(stdin_data)
    assert result.returncode == 0
    inner = _parse_inner(result)
    assert inner["observed_path"] == ".tmp/"


@pytest.mark.parametrize(
    "safe_path",
    [
        str(REPO_ROOT / "tmp" / "out.json"),
        str(REPO_ROOT / ".claude" / "tmp" / "out.json"),
        str(REPO_ROOT / "schemas" / "catalog.yaml"),
    ],
)
def test_non_trigger_paths_produce_no_output(safe_path: str):
    result = _run_hook(_make_input_write(safe_path))
    assert result.returncode == 0
    assert result.stdout.strip() == ""

"""Tests for .claude/hooks/root_temporary_residue_advisory.sh.

The live hook (per Issue #2007) invokes the producer with
``--schema-version v2``, so all advisories emitted by the hook use the
``REPO_TEMP_FOLDER_ADVICE_V2`` schema (write-root / legacy-root separated
payload). V1 producer-level compatibility is covered separately in
``scripts/agent-guards/tests/test_root_temporary_residue_policy.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / ".claude" / "hooks" / "root_temporary_residue_advisory.sh"

TRIGGER_PATHS = [
    str(REPO_ROOT / ".tmp" / "result.json"),
    str(REPO_ROOT / ".temp" / "cache" / "index.txt"),
    str(REPO_ROOT / ".tmp-agent" / "session" / "output.md"),
]
NON_TRIGGER_PATHS = [
    str(REPO_ROOT / "tmp" / "session" / "output.md"),
    str(REPO_ROOT / "docs" / "dev" / "repository-folder-policy.md"),
]
LEGACY_ROOT_WRITE_PATH = str(REPO_ROOT / ".claude" / "tmp" / "session" / "output.md")


def _make_input_write(file_path: str = "", *, cwd: Path = REPO_ROOT) -> str:
    return json.dumps(
        {"cwd": str(cwd), "tool_name": "Write", "tool_input": {"file_path": file_path}}
    )


def _make_input_edit(file_path: str = "", *, cwd: Path = REPO_ROOT) -> str:
    return json.dumps(
        {
            "cwd": str(cwd),
            "tool_name": "Edit",
            "tool_input": {"file_path": file_path, "old_string": "a", "new_string": "b"},
        }
    )


def _make_input_read(file_path: str = "", *, cwd: Path = REPO_ROOT) -> str:
    return json.dumps(
        {"cwd": str(cwd), "tool_name": "Read", "tool_input": {"file_path": file_path}}
    )


def _make_input_bash(command: str = "", *, cwd: Path = REPO_ROOT) -> str:
    return json.dumps({"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command}})


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
    assert "permissionDecision" not in hso
    ctx = hso["additionalContext"]
    assert ctx.startswith("REPO_TEMP_FOLDER_ADVICE_V2 ")
    inner = json.loads(ctx[len("REPO_TEMP_FOLDER_ADVICE_V2 "):])
    assert ctx.startswith(f"{inner['schema']} ")
    return inner


@pytest.mark.parametrize("trigger_path", TRIGGER_PATHS)
def test_write_tmp_alias_triggers_advisory(trigger_path: str):
    result = _run_hook(_make_input_write(trigger_path))
    assert result.returncode == 0
    assert result.stdout.strip()
    inner = _parse_inner(result)
    assert inner["schema"] == "REPO_TEMP_FOLDER_ADVICE_V2"
    assert inner["block"] is False
    assert inner["reason_code"] == "root_temporary_alias"
    assert inner["approved_replacement"] == "tmp/"
    assert inner["approved_write_roots"] == ["tmp/"]
    assert inner["deprecated_legacy_roots"] == [".claude/tmp/"]
    assert inner["cleanup_required"] is True


def test_edit_tmp_alias_triggers_advisory():
    result = _run_hook(_make_input_edit(str(REPO_ROOT / ".tmp" / "session" / "notes.md")))
    assert result.returncode == 0
    inner = _parse_inner(result)
    assert inner["observed_path"] == ".tmp/"


def test_bash_root_escape_from_subdirectory_triggers_advisory():
    result = _run_hook(_make_input_bash("mkdir -p ../.temp/cache", cwd=REPO_ROOT / "src"))
    assert result.returncode == 0
    inner = _parse_inner(result)
    assert inner["observed_path"] == ".temp/"


def test_bash_local_tmp_in_subdirectory_is_silent():
    result = _run_hook(_make_input_bash("mkdir -p .tmp/cache", cwd=REPO_ROOT / "src"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("non_trigger_path", NON_TRIGGER_PATHS)
def test_non_trigger_paths_produce_no_output(non_trigger_path: str):
    result = _run_hook(_make_input_write(non_trigger_path))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_invalid_json_fails_open():
    result = _run_hook("{")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# --------------------------------------------------------------------------- #
# V2: .claude/tmp/** write advisory (deprecated_legacy_root_write)
# --------------------------------------------------------------------------- #
def test_write_to_legacy_root_triggers_deprecated_advisory():
    result = _run_hook(_make_input_write(LEGACY_ROOT_WRITE_PATH))
    assert result.returncode == 0
    assert result.stdout.strip()
    inner = _parse_inner(result)
    assert inner["schema"] == "REPO_TEMP_FOLDER_ADVICE_V2"
    assert inner["block"] is False
    assert inner["reason_code"] == "deprecated_legacy_root_write"
    assert inner["observed_path"] == ".claude/tmp/"
    assert inner["approved_write_roots"] == ["tmp/"]
    assert inner["deprecated_legacy_roots"] == [".claude/tmp/"]


def test_edit_to_legacy_root_triggers_deprecated_advisory():
    result = _run_hook(_make_input_edit(LEGACY_ROOT_WRITE_PATH))
    assert result.returncode == 0
    inner = _parse_inner(result)
    assert inner["reason_code"] == "deprecated_legacy_root_write"


def test_bash_write_to_legacy_root_triggers_deprecated_advisory():
    result = _run_hook(_make_input_bash("mkdir -p .claude/tmp/session-x"))
    assert result.returncode == 0
    inner = _parse_inner(result)
    assert inner["reason_code"] == "deprecated_legacy_root_write"
    assert inner["observed_path"] == ".claude/tmp/"


# --------------------------------------------------------------------------- #
# V2: .claude/tmp/** read / scan / delete must NOT trigger advisory
# --------------------------------------------------------------------------- #
def test_read_legacy_root_produces_no_output():
    result = _run_hook(_make_input_read(LEGACY_ROOT_WRITE_PATH))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.parametrize(
    "command",
    [
        "cat .claude/tmp/session/output.md",
        "ls .claude/tmp",
        "find .claude/tmp -name '*.json'",
        "grep -r foo .claude/tmp",
    ],
)
def test_bash_scan_legacy_root_produces_no_output(command: str):
    result = _run_hook(_make_input_bash(command))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_bash_delete_legacy_root_produces_no_output():
    result = _run_hook(_make_input_bash("rm -rf .claude/tmp/session-x"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# --------------------------------------------------------------------------- #
# Live process contract: returncode 0, no permissionDecision, prefix/schema match
# --------------------------------------------------------------------------- #
def test_live_hook_process_never_emits_permission_decision_and_prefix_matches_schema():
    for stdin_data in (
        _make_input_write(LEGACY_ROOT_WRITE_PATH),
        _make_input_bash("ls .claude/tmp"),
        _make_input_write(str(REPO_ROOT / ".tmp" / "x.json")),
    ):
        result = _run_hook(stdin_data)
        assert result.returncode == 0
        if result.stdout.strip():
            outer = json.loads(result.stdout)
            hso = outer["hookSpecificOutput"]
            assert "permissionDecision" not in hso
            inner = json.loads(hso["additionalContext"].split(" ", 1)[1])
            assert hso["additionalContext"].startswith(f"{inner['schema']} ")

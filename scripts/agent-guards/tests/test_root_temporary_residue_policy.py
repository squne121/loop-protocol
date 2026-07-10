from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from root_temporary_residue_policy import build_temp_folder_advice

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / "repo_temp_folder_advice_v1.schema.json"


def _validate_against_schema(advice: dict) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(advice)


def test_absolute_file_path_tmp_alias_builds_advice():
    advice = build_temp_folder_advice(
        {
            "cwd": str(REPO_ROOT),
            "tool_input": {"file_path": str(REPO_ROOT / ".tmp" / "session" / "output.json")},
        },
        repo_root=REPO_ROOT,
    )
    assert advice is not None
    assert advice["schema"] == "REPO_TEMP_FOLDER_ADVICE_V1"
    assert advice["block"] is False
    assert advice["observed_path"] == ".tmp/"
    assert advice["approved_replacement"] == "tmp/"
    assert advice["approved_temporary_roots"] == ["tmp/", ".claude/tmp/"]
    assert advice["cleanup_required"] is True
    _validate_against_schema(advice)


def test_bash_temp_alias_builds_advice():
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"command": "mkdir -p .temp/cache"}},
        repo_root=REPO_ROOT,
    )
    assert advice is not None
    assert advice["observed_path"] == ".temp/"
    _validate_against_schema(advice)


def test_equals_style_argument_builds_advice():
    advice = build_temp_folder_advice(
        {
            "cwd": str(REPO_ROOT),
            "tool_input": {"command": "python tool.py --out=.tmp-agent/result.json"},
        },
        repo_root=REPO_ROOT,
    )
    assert advice is not None
    assert advice["observed_path"] == ".tmp-*/"
    _validate_against_schema(advice)


def test_subdirectory_cwd_only_flags_root_relative_parent_escape():
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT / "src"), "tool_input": {"command": "mkdir ../.tmp/cache"}},
        repo_root=REPO_ROOT,
    )
    assert advice is not None
    assert advice["observed_path"] == ".tmp/"


def test_subdirectory_cwd_does_not_flag_local_tmp():
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT / "src"), "tool_input": {"command": "mkdir .tmp/cache"}},
        repo_root=REPO_ROOT,
    )
    assert advice is None


def test_pwd_expansion_prefix_is_best_effort_supported():
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"command": 'mkdir "$PWD/.tmp/cache"'}},
        repo_root=REPO_ROOT,
    )
    assert advice is not None
    assert advice["observed_path"] == ".tmp/"


def test_repo_root_command_substitution_prefix_is_best_effort_supported():
    advice = build_temp_folder_advice(
        {
            "cwd": str(REPO_ROOT / "src"),
            "tool_input": {"command": 'mkdir "$(git rev-parse --show-toplevel)/.tmp/cache"'},
        },
        repo_root=REPO_ROOT,
    )
    assert advice is not None
    assert advice["observed_path"] == ".tmp/"


def test_write_redirection_is_detected():
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"command": "printf x >./.tmp/out"}},
        repo_root=REPO_ROOT,
    )
    assert advice is not None
    assert advice["observed_path"] == ".tmp/"


def test_repo_approved_workspace_is_not_flagged():
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"file_path": ".claude/tmp/session-123/result.json"}},
        repo_root=REPO_ROOT,
    )
    assert advice is None


def test_nested_non_root_tmp_path_is_not_flagged():
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"command": "mkdir -p src/.tmp/cache"}},
        repo_root=REPO_ROOT,
    )
    assert advice is None


def test_read_only_and_cleanup_commands_do_not_emit_replacement_advice():
    for command in ("cat .tmp/report.json", "ls .tmp", "rm -rf .tmp"):
        advice = build_temp_folder_advice(
            {"cwd": str(REPO_ROOT), "tool_input": {"command": command}},
            repo_root=REPO_ROOT,
        )
        assert advice is None


def test_gitignore_root_anchor_matches_expected_paths():
    def is_ignored(path: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    assert is_ignored("tmp/file")
    assert not is_ignored("src/tmp/file")
    assert is_ignored(".claude/tmp/file")
    assert not is_ignored(".tmp/file")

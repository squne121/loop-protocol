from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from root_temporary_residue_policy import build_temp_folder_advice

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / "repo_temp_folder_advice_v1.schema.json"
SCHEMA_PATH_V2 = REPO_ROOT / "schemas" / "repo_temp_folder_advice_v2.schema.json"


def _validate_against_schema(advice: dict) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(advice)


def _validate_against_schema_v2(advice: dict) -> None:
    schema = json.loads(SCHEMA_PATH_V2.read_text(encoding="utf-8"))
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


# --------------------------------------------------------------------------- #
# AC2 regression: no --schema-version flag and explicit --schema-version v1
# must produce byte-identical output to the pre-V2 producer (#2007).
# --------------------------------------------------------------------------- #
_V1_REGRESSION_PAYLOADS = [
    {
        "cwd": str(REPO_ROOT),
        "tool_input": {"file_path": str(REPO_ROOT / ".tmp" / "session" / "output.json")},
    },
    {"cwd": str(REPO_ROOT), "tool_input": {"command": "mkdir -p .temp/cache"}},
    {
        "cwd": str(REPO_ROOT),
        "tool_input": {"file_path": ".claude/tmp/session-123/result.json"},
    },
]


@pytest.mark.parametrize("payload", _V1_REGRESSION_PAYLOADS)
def test_default_schema_version_matches_v1_output(payload: dict):
    default_advice = build_temp_folder_advice(payload, repo_root=REPO_ROOT)
    explicit_v1_advice = build_temp_folder_advice(payload, repo_root=REPO_ROOT, schema_version="v1")
    assert default_advice == explicit_v1_advice


def test_explicit_schema_version_v1_matches_pre_v2_payload_shape():
    advice = build_temp_folder_advice(
        {
            "cwd": str(REPO_ROOT),
            "tool_input": {"file_path": str(REPO_ROOT / ".tmp" / "session" / "output.json")},
        },
        repo_root=REPO_ROOT,
        schema_version="v1",
    )
    assert advice is not None
    assert advice == {
        "schema": "REPO_TEMP_FOLDER_ADVICE_V1",
        "block": False,
        "reason_code": "root_temporary_alias",
        "observed_path": ".tmp/",
        "approved_replacement": "tmp/",
        "approved_temporary_roots": ["tmp/", ".claude/tmp/"],
        "cleanup_required": True,
        "policy_doc": "docs/dev/repository-folder-policy.md",
        "message_ja": (
            "repo root の一時 alias は残置ノイズになります。tmp/ または .claude/tmp/ を使い、"
            "終了時に削除または報告してください。"
        ),
    }
    _validate_against_schema(advice)


# --------------------------------------------------------------------------- #
# fix_delta MEDIUM 2: real CLI subprocess byte-level golden regression, for
# both no-flag and explicit --schema-version v1. Pins field order / JSON
# serialization style / trailing-newline behavior of `print()`, not just
# Python dict equality (which the tests above already cover).
# --------------------------------------------------------------------------- #
_V1_CLI_GOLDEN_STDOUT = (
    json.dumps(
        {
            "schema": "REPO_TEMP_FOLDER_ADVICE_V1",
            "block": False,
            "reason_code": "root_temporary_alias",
            "observed_path": ".tmp/",
            "approved_replacement": "tmp/",
            "approved_temporary_roots": ["tmp/", ".claude/tmp/"],
            "cleanup_required": True,
            "policy_doc": "docs/dev/repository-folder-policy.md",
            "message_ja": (
                "repo root の一時 alias は残置ノイズになります。tmp/ または .claude/tmp/ を使い、"
                "終了時に削除または報告してください。"
            ),
        },
        ensure_ascii=False,
    )
    + "\n"
)


@pytest.mark.parametrize("extra_args", [[], ["--schema-version", "v1"]])
def test_v1_cli_subprocess_stdout_bytes_match_golden(extra_args: list[str]):
    script_path = REPO_ROOT / "scripts" / "agent-guards" / "root_temporary_residue_policy.py"
    stdin_payload = json.dumps({"tool_input": {"command": "mkdir -p .tmp/cache"}})
    result = subprocess.run(
        [sys.executable, str(script_path), *extra_args],
        input=stdin_payload,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert result.stdout == _V1_CLI_GOLDEN_STDOUT


def test_v1_legacy_root_write_still_silent_regardless_of_schema_version():
    """.claude/tmp/** write must remain silent under v1 (only v2 flags it)."""
    payload = {
        "cwd": str(REPO_ROOT),
        "tool_name": "Write",
        "tool_input": {"file_path": ".claude/tmp/session-123/result.json"},
    }
    assert build_temp_folder_advice(payload, repo_root=REPO_ROOT) is None
    assert build_temp_folder_advice(payload, repo_root=REPO_ROOT, schema_version="v1") is None


# --------------------------------------------------------------------------- #
# AC5: --schema-version v2 V2 schema compliance
# --------------------------------------------------------------------------- #
def test_v2_root_alias_write_builds_v2_advice():
    advice = build_temp_folder_advice(
        {
            "cwd": str(REPO_ROOT),
            "tool_name": "Write",
            "tool_input": {"file_path": str(REPO_ROOT / ".tmp" / "session" / "output.json")},
        },
        repo_root=REPO_ROOT,
        schema_version="v2",
    )
    assert advice is not None
    assert advice["schema"] == "REPO_TEMP_FOLDER_ADVICE_V2"
    assert advice["reason_code"] == "root_temporary_alias"
    assert advice["observed_path"] == ".tmp/"
    assert advice["approved_write_roots"] == ["tmp/"]
    assert advice["deprecated_legacy_roots"] == [".claude/tmp/"]
    _validate_against_schema_v2(advice)


def test_v2_legacy_root_write_via_file_path_builds_deprecated_advice():
    advice = build_temp_folder_advice(
        {
            "cwd": str(REPO_ROOT),
            "tool_name": "Write",
            "tool_input": {"file_path": str(REPO_ROOT / ".claude" / "tmp" / "session-1" / "out.json")},
        },
        repo_root=REPO_ROOT,
        schema_version="v2",
    )
    assert advice is not None
    assert advice["schema"] == "REPO_TEMP_FOLDER_ADVICE_V2"
    assert advice["reason_code"] == "deprecated_legacy_root_write"
    assert advice["observed_path"] == ".claude/tmp/"
    _validate_against_schema_v2(advice)


def test_v2_legacy_root_write_via_edit_builds_deprecated_advice():
    advice = build_temp_folder_advice(
        {
            "cwd": str(REPO_ROOT),
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(REPO_ROOT / ".claude" / "tmp" / "session-1" / "out.json"),
                "old_string": "a",
                "new_string": "b",
            },
        },
        repo_root=REPO_ROOT,
        schema_version="v2",
    )
    assert advice is not None
    assert advice["reason_code"] == "deprecated_legacy_root_write"


def test_v2_legacy_root_write_via_bash_mkdir_builds_deprecated_advice():
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"command": "mkdir -p .claude/tmp/session-x"}},
        repo_root=REPO_ROOT,
        schema_version="v2",
    )
    assert advice is not None
    assert advice["reason_code"] == "deprecated_legacy_root_write"
    assert advice["observed_path"] == ".claude/tmp/"
    _validate_against_schema_v2(advice)


def test_v2_legacy_root_read_via_file_path_is_silent():
    advice = build_temp_folder_advice(
        {
            "cwd": str(REPO_ROOT),
            "tool_name": "Read",
            "tool_input": {"file_path": str(REPO_ROOT / ".claude" / "tmp" / "session-1" / "out.json")},
        },
        repo_root=REPO_ROOT,
        schema_version="v2",
    )
    assert advice is None


@pytest.mark.parametrize(
    "command",
    [
        "cat .claude/tmp/session/output.md",
        "ls .claude/tmp",
        "find .claude/tmp -name '*.json'",
    ],
)
def test_v2_legacy_root_scan_via_bash_is_silent(command: str):
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"command": command}},
        repo_root=REPO_ROOT,
        schema_version="v2",
    )
    assert advice is None


def test_v2_legacy_root_delete_via_bash_is_silent():
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"command": "rm -rf .claude/tmp/session-x"}},
        repo_root=REPO_ROOT,
        schema_version="v2",
    )
    assert advice is None


# --------------------------------------------------------------------------- #
# fix_delta P0: destination-aware `.claude/tmp/**` Bash-command classifier
# (_detect_bash_legacy_root_write). Narrow, bounded fixture matrix -- exactly
# the cases called out by the human PR review comment on #2581 plus the
# compound-segment-splitting pin.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "command",
    [
        "cat input > .claude/tmp/output",
        "sed -i 's/a/b/' .claude/tmp/file",
        "ls .claude/tmp && mkdir -p .claude/tmp/new",
        "cp tmp/input .claude/tmp/output",
        "tee .claude/tmp/output",
    ],
)
def test_v2_legacy_root_write_bash_classifier_true_positives(command: str):
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"command": command}},
        repo_root=REPO_ROOT,
        schema_version="v2",
    )
    assert advice is not None, f"expected deprecated_legacy_root_write advisory for: {command}"
    assert advice["reason_code"] == "deprecated_legacy_root_write"
    assert advice["observed_path"] == ".claude/tmp/"
    _validate_against_schema_v2(advice)


@pytest.mark.parametrize(
    "command",
    [
        "cp .claude/tmp/result.json tmp/result.json",
        "echo .claude/tmp/result.json",
        "printf '%s' .claude/tmp/result.json",
        "git status -- .claude/tmp/result.json",
        "sed 's/a/b/' .claude/tmp/file",
    ],
)
def test_v2_legacy_root_write_bash_classifier_true_negatives(command: str):
    advice = build_temp_folder_advice(
        {"cwd": str(REPO_ROOT), "tool_input": {"command": command}},
        repo_root=REPO_ROOT,
        schema_version="v2",
    )
    assert advice is None, f"expected no advisory for: {command}"


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

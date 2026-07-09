from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from root_temporary_residue_policy import build_temp_folder_advice


def test_file_path_tmp_alias_builds_advice():
    advice = build_temp_folder_advice(
        {"tool_input": {"file_path": ".tmp/session/output.json"}}
    )
    assert advice is not None
    assert advice["schema"] == "REPO_TEMP_FOLDER_ADVICE_V1"
    assert advice["block"] is False
    assert advice["observed_path"] == ".tmp/"
    assert advice["approved_replacement"] == "tmp/"
    assert advice["approved_temporary_roots"] == ["tmp/", ".claude/tmp/"]
    assert advice["cleanup_required"] is True


def test_bash_temp_alias_builds_advice():
    advice = build_temp_folder_advice(
        {"tool_input": {"command": "mkdir -p .temp/cache"}}
    )
    assert advice is not None
    assert advice["observed_path"] == ".temp/"


def test_equals_style_argument_builds_advice():
    advice = build_temp_folder_advice(
        {"tool_input": {"command": "python tool.py --out=.tmp-agent/result.json"}}
    )
    assert advice is not None
    assert advice["observed_path"] == ".tmp-agent/"


def test_repo_approved_workspace_is_not_flagged():
    advice = build_temp_folder_advice(
        {"tool_input": {"file_path": ".claude/tmp/session-123/result.json"}}
    )
    assert advice is None


def test_nested_non_root_tmp_path_is_not_flagged():
    advice = build_temp_folder_advice(
        {"tool_input": {"command": "mkdir -p src/.tmp/cache"}}
    )
    assert advice is None

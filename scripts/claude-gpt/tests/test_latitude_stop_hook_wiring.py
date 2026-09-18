"""scripts/claude-gpt/tests/test_latitude_stop_hook_wiring.py

Issue #2426 AC1: launcher-owned Latitude Stop hook が Claude-GPT generated
settings に additive に存在し、既存 hook groups（UserPromptSubmit/PreToolUse/
SubagentStart/SubagentStop の authorization gate）は維持されることを、
`launch.sh --check-only` が実際に生成する `settings.local.json` を読んで検証する
（hermetic: fake proxy binary、claude 本体は起動しない）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2426_wiring", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_helper)

LAUNCH_SH = _helper.LAUNCH_SH
run_check_only = _helper.run_check_only


def test_stop_hook_group_is_additive_and_present(tmp_path):
    """GIVEN 通常の launch.sh --check-only 実行
    WHEN 生成された settings.local.json を読む
    THEN "Stop" に launcher-owned Latitude hook group が存在する（AC1）
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings["hooks"]
    assert "Stop" in hooks
    stop_groups = hooks["Stop"]
    assert len(stop_groups) == 1
    stop_commands = [h["command"] for group in stop_groups for h in group["hooks"]]
    assert any("CLAUDE_GPT_LATITUDE_HOOK" in cmd for cmd in stop_commands)


def test_spark_authorization_gate_hook_groups_are_retired(tmp_path):
    """GIVEN 通常の launch.sh --check-only 実行
    WHEN 生成された settings.local.json を読む
    THEN 旧 Spark 認可 gate（UserPromptSubmit/PreToolUse(Agent)/
         SubagentStart/SubagentStop への SPARK_GATE_WRITER command 注入）は
         一切存在しない（Issue #2651 AC1: gate 自体が撤去され、
         Latitude Stop hook group（別テストで確認済み）のような他の既存
         hook 登録の共存は引き続き妨げられない）
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings["hooks"]

    ups_commands = [h["command"] for group in hooks["UserPromptSubmit"] for h in group["hooks"]]
    assert not any("SPARK_GATE_WRITER" in cmd for cmd in ups_commands)

    ptu_groups = hooks["PreToolUse"]
    assert not any(group.get("matcher") == "Agent" for group in ptu_groups)
    ptu_commands = [h["command"] for group in ptu_groups for h in group["hooks"]]
    assert not any("SPARK_GATE_WRITER" in cmd for cmd in ptu_commands)

    sas_commands = [h["command"] for group in hooks["SubagentStart"] for h in group["hooks"]]
    assert not any("SPARK_GATE_WRITER" in cmd for cmd in sas_commands)

    sap_commands = [h["command"] for group in hooks["SubagentStop"] for h in group["hooks"]]
    assert not any("SPARK_GATE_WRITER" in cmd for cmd in sap_commands)

    # "env" no longer carries SPARK_GATE_WRITER either (Issue #2651).
    assert "SPARK_GATE_WRITER" not in settings.get("env", {})


def test_latitude_hook_command_references_repo_owned_script_path(tmp_path):
    """GIVEN 通常の launch.sh --check-only 実行
    WHEN 生成された settings.local.json の env フラグメントを読む
    THEN CLAUDE_GPT_LATITUDE_HOOK が repo 内 scripts/claude-gpt/latitude_hook.py
         の絶対パスを指す（AC1: 動的生成スクリプトではなく tracked ファイル）
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hook_path = settings["env"]["CLAUDE_GPT_LATITUDE_HOOK"]
    assert hook_path.endswith("scripts/claude-gpt/latitude_hook.py")


def test_latitude_hook_group_registered_as_async():
    """GIVEN launch.sh の LATITUDE_HOOK_GROUP 定義
    WHEN grep する
    THEN async: true が指定されている（Stop イベント全体をブロックしない。Design 4節）
    """
    content = LAUNCH_SH.read_text(encoding="utf-8")
    assert "LATITUDE_HOOK_GROUP=" in content
    idx = content.index("LATITUDE_HOOK_GROUP=")
    line = content[idx : content.index("\n", idx)]
    assert '"async": true' in line

"""scripts/claude-gpt/tests/test_latitude_same_project_binding.py

Issue #2426 AC4: Native と Claude-GPT は同一 `LATITUDE_PROJECT` を使用でき、
既存 #2375（PR #2392）collector の project/session binding を変更しない。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parent.parent / "latitude_hook.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_hook_module_2426_same_project", _HOOK_PATH
)
latitude_hook = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(latitude_hook)

_COLLECT_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / ".claude"
    / "skills"
    / "agent-retrospective"
    / "scripts"
    / "collect_snapshot.py"
)


def test_native_project_value_is_forwarded_unmodified_to_child_env(tmp_path):
    """GIVEN Native settings が LATITUDE_PROJECT="shared-project" を持つ
    WHEN read_native_latitude_allowlist() -> build_child_env() の経路を通す
    THEN telemetry subprocess へ渡す child env の LATITUDE_PROJECT は Native の
         値のまま変更されない（AC4: launcher が project を書き換えない）
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"env": {"LATITUDE_API_KEY": "k", "LATITUDE_PROJECT": "shared-project"}}',
        encoding="utf-8",
    )
    allowlist = latitude_hook.read_native_latitude_allowlist(str(settings_path))
    child_env = latitude_hook.build_child_env("/tmp/session-home", allowlist, path_value="/bin")
    assert child_env["LATITUDE_PROJECT"] == "shared-project"


def test_collector_does_not_read_launcher_owned_env_vars():
    """GIVEN #2375（PR #2392）collect_snapshot.py の Latitude collector
    WHEN ソースを検索する
    THEN CLAUDE_GPT_LATITUDE_* / CLAUDE_GPT_NATIVE_SETTINGS_PATH など、本 Issue
         が新設した launcher-owned env var 名を一切参照しない（AC4/AC9:
         collector 自身への変更が本 Issue で不要であることの回帰確認）
    """
    assert _COLLECT_SNAPSHOT_PATH.exists()
    content = _COLLECT_SNAPSHOT_PATH.read_text(encoding="utf-8")
    for token in (
        "CLAUDE_GPT_LATITUDE_HOOK",
        "CLAUDE_GPT_LATITUDE_PACKAGE_SPEC",
        "CLAUDE_GPT_NATIVE_SETTINGS_PATH",
        "CLAUDE_GPT_HOME_ROOT",
    ):
        assert token not in content


def test_collector_still_resolves_project_slug_from_latitude_project_env(monkeypatch):
    """GIVEN #2375 collector の _default_latitude_project_slug()
    WHEN LATITUDE_PROJECT を設定する
    THEN collector はその値をそのまま project slug として解決できる（AC4:
         Native/Claude-GPT が同一 LATITUDE_PROJECT を共有しても collector 側の
         解決ロジックはそのまま機能する）
    """
    import sys

    cs_spec = importlib.util.spec_from_file_location(
        "collect_snapshot_module_2426_same_project", _COLLECT_SNAPSHOT_PATH
    )
    collect_snapshot = importlib.util.module_from_spec(cs_spec)
    assert cs_spec.loader is not None
    sys.modules[cs_spec.name] = collect_snapshot
    cs_spec.loader.exec_module(collect_snapshot)

    monkeypatch.setenv("LATITUDE_PROJECT", "shared-project")
    assert collect_snapshot._default_latitude_project_slug() == "shared-project"

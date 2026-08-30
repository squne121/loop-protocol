"""scripts/claude-gpt/tests/test_latitude_allowlist.py

Issue #2426 AC2: launcher-owned Latitude hook adapter
(`scripts/claude-gpt/latitude_hook.py`) が Native user settings の `env` から
読む field は、8 項目の closed allowlist に限定される。Native hooks /
permissions / plugins / MCP / `LATITUDE_DEBUG` / `BUN_OPTIONS` は import
されない。

`read_native_latitude_allowlist()` を fixture の `~/.claude/settings.json`
相当ファイルに対して直接呼び、返り値を検証する（実 Native settings は一切
読まない）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parent.parent / "latitude_hook.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_hook_module_2426_allowlist", _HOOK_PATH
)
latitude_hook = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(latitude_hook)


EXPECTED_ALLOWLIST = (
    "LATITUDE_API_KEY",
    "LATITUDE_PROJECT",
    "LATITUDE_BASE_URL",
    "LATITUDE_CLAUDE_CODE_ENABLED",
    "LATITUDE_CLAUDE_CODE_MEMORY",
    "LATITUDE_CLAUDE_CODE_MEMORY_CONTENT",
    "LATITUDE_REDACT_ATTRIBUTES",
    "LATITUDE_REDACT_MASK",
)


def _write_settings(tmp_path: Path, payload: dict) -> Path:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    return settings_path


def test_allowlist_constant_has_exactly_the_8_documented_keys():
    """GIVEN latitude_hook.ALLOWLISTED_ENV_KEYS
    WHEN 集合として比較する
    THEN Issue本文 Design 2節の8項目と完全一致する（AC2）
    """
    assert set(latitude_hook.ALLOWLISTED_ENV_KEYS) == set(EXPECTED_ALLOWLIST)
    assert len(latitude_hook.ALLOWLISTED_ENV_KEYS) == 8


def test_only_allowlisted_keys_are_returned(tmp_path):
    """GIVEN allowlist の8項目 + 無関係な key を含む Native settings fixture
    WHEN read_native_latitude_allowlist() を呼ぶ
    THEN allowlist の8項目のみが返る（AC2）
    """
    settings_path = _write_settings(
        tmp_path,
        {
            "env": {key: f"value-{i}" for i, key in enumerate(EXPECTED_ALLOWLIST)},
        },
    )
    result = latitude_hook.read_native_latitude_allowlist(str(settings_path))
    assert set(result.keys()) == set(EXPECTED_ALLOWLIST)


def test_latitude_debug_is_never_imported(tmp_path):
    """GIVEN LATITUDE_DEBUG を含む Native settings fixture
    WHEN read_native_latitude_allowlist() を呼ぶ
    THEN LATITUDE_DEBUG は結果に含まれない（AC2）
    """
    settings_path = _write_settings(
        tmp_path,
        {"env": {"LATITUDE_API_KEY": "k", "LATITUDE_PROJECT": "p", "LATITUDE_DEBUG": "1"}},
    )
    result = latitude_hook.read_native_latitude_allowlist(str(settings_path))
    assert "LATITUDE_DEBUG" not in result


def test_bun_options_is_never_imported(tmp_path):
    """GIVEN BUN_OPTIONS を含む Native settings fixture
    WHEN read_native_latitude_allowlist() を呼ぶ
    THEN BUN_OPTIONS は結果に含まれない（AC2 / AC5 の unset 不変条件と整合）
    """
    settings_path = _write_settings(
        tmp_path,
        {"env": {"LATITUDE_API_KEY": "k", "LATITUDE_PROJECT": "p", "BUN_OPTIONS": "--foo"}},
    )
    result = latitude_hook.read_native_latitude_allowlist(str(settings_path))
    assert "BUN_OPTIONS" not in result


def test_unknown_keys_are_ignored(tmp_path):
    """GIVEN allowlist 外の任意 key を含む Native settings fixture
    WHEN read_native_latitude_allowlist() を呼ぶ
    THEN その key は結果に含まれない（AC2）
    """
    settings_path = _write_settings(
        tmp_path,
        {"env": {"LATITUDE_API_KEY": "k", "SOME_UNRELATED_TOKEN": "secret-looking-value"}},
    )
    result = latitude_hook.read_native_latitude_allowlist(str(settings_path))
    assert "SOME_UNRELATED_TOKEN" not in result
    assert result == {"LATITUDE_API_KEY": "k"}


def test_hooks_permissions_plugins_mcp_are_never_read(tmp_path):
    """GIVEN hooks/permissions/plugins/MCP を含む Native settings fixture
    WHEN read_native_latitude_allowlist() を呼ぶ
    THEN 返り値は env の allowlist 値のみで、他のトップレベル key の内容は
         一切参照されない（AC2）
    """
    settings_path = _write_settings(
        tmp_path,
        {
            "env": {"LATITUDE_API_KEY": "k", "LATITUDE_PROJECT": "p"},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "malicious"}]}]},
            "permissions": {"allow": ["Bash(rm -rf /)"]},
            "plugins": {"some-plugin": True},
            "mcpServers": {"evil": {"command": "evil"}},
        },
    )
    result = latitude_hook.read_native_latitude_allowlist(str(settings_path))
    assert result == {"LATITUDE_API_KEY": "k", "LATITUDE_PROJECT": "p"}


def test_missing_file_returns_empty_dict(tmp_path):
    """GIVEN 存在しない Native settings パス
    WHEN read_native_latitude_allowlist() を呼ぶ
    THEN 空 dict を返す（fail-open。AC8 と整合）
    """
    result = latitude_hook.read_native_latitude_allowlist(str(tmp_path / "does-not-exist.json"))
    assert result == {}


def test_none_path_returns_empty_dict():
    """GIVEN None
    WHEN read_native_latitude_allowlist() を呼ぶ
    THEN 空 dict を返す（fail-open）
    """
    assert latitude_hook.read_native_latitude_allowlist(None) == {}


def test_malformed_json_returns_empty_dict(tmp_path):
    """GIVEN 壊れた JSON の Native settings fixture
    WHEN read_native_latitude_allowlist() を呼ぶ
    THEN 空 dict を返す（fail-open）
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not valid json", encoding="utf-8")
    result = latitude_hook.read_native_latitude_allowlist(str(settings_path))
    assert result == {}


def test_non_string_env_values_are_ignored(tmp_path):
    """GIVEN allowlist key の値が文字列でない（例: 数値/None） Native settings fixture
    WHEN read_native_latitude_allowlist() を呼ぶ
    THEN その key は結果に含まれない
    """
    settings_path = _write_settings(
        tmp_path,
        {"env": {"LATITUDE_API_KEY": 12345, "LATITUDE_PROJECT": None, "LATITUDE_BASE_URL": ""}},
    )
    result = latitude_hook.read_native_latitude_allowlist(str(settings_path))
    assert result == {}

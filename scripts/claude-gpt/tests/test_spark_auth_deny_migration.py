"""scripts/claude-gpt/tests/test_spark_auth_deny_migration.py

Issue #2440 AC1/AC2/AC3/AC6: `launch.sh` の `settings.local.json` 生成 heredoc
テンプレートから、spark-auth 向け legacy `Write(<CLAUDE_GPT_HOME>/spark-auth/**)`
deny 行が完全に削除され、canonical `Edit(<CLAUDE_GPT_HOME>/spark-auth/**)` のみが
出力されることを、`launch.sh --check-only` が実際に生成する `settings.local.json`
を test-owned isolated `CLAUDE_GPT_HOME` で読んで検証する（hermetic: fake proxy
binary、claude 本体は起動しない）。

`_latitude_check_only_helper.py` の共有ハーネス（Issue #2426 由来、
`test_latitude_stop_hook_wiring.py` 等で既に使用）を再利用し、hermetic
`launch.sh --check-only` 起動ロジックを重複実装しない（DRY）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2440_spark_auth_migration", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_helper)

run_check_only = _helper.run_check_only


def _spark_auth_write_or_edit_entries(settings: dict) -> list[str]:
    deny = settings["permissions"]["deny"]
    return [
        rule
        for rule in deny
        if "spark-auth" in rule and (rule.startswith("Write(") or rule.startswith("Edit("))
    ]


def test_canonical_edit_deny_present_and_legacy_write_deny_absent(tmp_path):
    """GIVEN 通常の launch.sh --check-only 実行（test-owned isolated CLAUDE_GPT_HOME）
    WHEN 生成された settings.local.json の permissions.deny を読む
    THEN spark-auth 向け deny は canonical Edit(<CLAUDE_GPT_HOME>/spark-auth/**) の
    1件のみであり、legacy Write(<CLAUDE_GPT_HOME>/spark-auth/**) は一切含まれない
    (AC1, AC2)
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    entries = _spark_auth_write_or_edit_entries(settings)
    assert len(entries) == 1, entries
    assert entries[0].startswith("Edit("), entries
    assert not any(entry.startswith("Write(") for entry in entries)


def test_unrelated_deny_entries_are_preserved(tmp_path):
    """GIVEN 通常の launch.sh --check-only 実行
    WHEN 生成された settings.local.json の permissions.deny を読む
    THEN spark-auth 向け Read(...) deny を含め、既存の unrelated Read(...) deny
    エントリ群（proxy config/state/home dir）は削除・変更されず保持される (AC2)
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    deny = settings["permissions"]["deny"]

    read_entries = [rule for rule in deny if rule.startswith("Read(")]
    # proxy config dir / proxy state dir / proxy home dir / spark-auth dir
    assert len(read_entries) >= 4, read_entries
    assert any("spark-auth" in rule for rule in read_entries), read_entries


def test_repeated_launch_does_not_duplicate_or_reintroduce_legacy_write_deny(tmp_path):
    """GIVEN 同一 CLAUDE_GPT_HOME に対して launch.sh --check-only を連続2回実行する
    （既存の settings.local.json が存在する状態を含む）
    WHEN 2回目実行後の settings.local.json を読む
    THEN spark-auth deny エントリは常に canonical Edit(...) 1件のみであり、
    legacy Write(...) との重複・再出現は発生しない (AC3: 毎回の完全再生成による
    回帰確認)
    """
    result1, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result1.stderr
    assert settings_path.stat().st_size > 0

    result2, settings_path_second_run = run_check_only(tmp_path)
    assert settings_path_second_run == settings_path
    assert settings_path.exists(), result2.stderr

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = _spark_auth_write_or_edit_entries(settings)
    assert len(entries) == 1, entries
    assert entries[0].startswith("Edit("), entries


def test_spark_auth_deny_path_is_test_owned_isolated_home_not_ambient_home(tmp_path):
    """GIVEN test-owned isolated CLAUDE_GPT_HOME（run_check_only が tmp_path 配下に
    生成する）での launch.sh --check-only 実行
    WHEN canonical Edit(...) deny のパスを読む
    THEN パスは tmp_path 配下（test-owned）であり、実運用の ambient HOME や
    production spark-auth content を参照しない (AC6)
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    entries = _spark_auth_write_or_edit_entries(settings)
    assert len(entries) == 1, entries
    # entry の形は "Edit(/<abs path>/**)"（launch.sh が絶対パス target を
    # 文字列展開した結果、"//" 始まりになる）。tmp_path が含まれることだけを
    # 確認し、production/real-user のような固定パスに一致しないことを保証する。
    deny_path = entries[0].split("(", 1)[1].rstrip(")")
    assert str(tmp_path) in deny_path, deny_path
    assert "/home/" not in deny_path.replace(str(tmp_path), ""), deny_path

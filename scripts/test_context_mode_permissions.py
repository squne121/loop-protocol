#!/usr/bin/env python3
"""
context-mode permissions 検証スクリプト (#825)

このスクリプトは .claude/settings.json の context-mode deny rule を検証し、
secret token 漏洩防止ポリシーが正しく設定されていることを確認する。

Usage:
    uv run python3 scripts/test_context_mode_permissions.py

Exit codes:
    0: 全検証 PASS
    1: 検証 FAIL
    77: SKIP（環境条件を満たさない場合 — SKIP != PASS）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# リポジトリルート（このスクリプトの親の親）
_REPO_ROOT = Path(__file__).parent.parent
_SETTINGS_PATH = _REPO_ROOT / ".claude" / "settings.json"
_ARTIFACT_DIR = _REPO_ROOT / ".claude" / "artifacts" / "context-mode"


def load_settings() -> dict:
    """settings.json を JSON parse して返す。"""
    if not _SETTINGS_PATH.exists():
        print(f"ERROR: settings.json が存在しません: {_SETTINGS_PATH}", file=sys.stderr)
        sys.exit(1)
    return json.loads(_SETTINGS_PATH.read_text())


def check_mcp_deny_entries(settings: dict) -> list[str]:
    """context-mode MCP deny entries の検証。"""
    errors = []
    deny_list = settings.get("permissions", {}).get("deny", [])

    required_deny = [
        "mcp__context-mode__ctx_execute",
        "mcp__context-mode__ctx_fetch_and_index",
    ]
    for entry in required_deny:
        if entry not in deny_list:
            errors.append(f"FAIL: '{entry}' が permissions.deny にありません")

    return errors


def check_token_dump_command_deny(settings: dict) -> list[str]:
    """token dump コマンドの deny 確認。"""
    errors = []
    deny_list = settings.get("permissions", {}).get("deny", [])

    checks = [
        ("gh secret deny", any("gh secret" in e for e in deny_list)),
        ("printenv deny", any("printenv" in e for e in deny_list)),
        ("env deny", any(e in ("Bash(env)", "Bash(env *)") for e in deny_list)),
    ]
    for name, passed in checks:
        if not passed:
            errors.append(f"FAIL: {name} が設定されていません")

    return errors


def check_env_file_read_deny(settings: dict) -> list[str]:
    """env ファイルの Read deny 確認。"""
    errors = []
    deny_list = settings.get("permissions", {}).get("deny", [])

    env_read_denied = any(
        "Read(.env" in e or "Read(./.env" in e for e in deny_list
    )
    if not env_read_denied:
        errors.append("FAIL: .env ファイルの Read deny が設定されていません")

    return errors


def check_ctx_index_not_allowed(settings: dict) -> list[str]:
    """ctx_index / ctx_search が allow に追加されていないことを確認。"""
    errors = []
    allow_list = settings.get("permissions", {}).get("allow", [])

    for entry in allow_list:
        if "ctx_index" in entry:
            errors.append(
                f"FAIL: ctx_index が allow に追加されています: {entry}"
                " (#825 完了前は allow 禁止)"
            )
        if "ctx_search" in entry:
            errors.append(
                f"FAIL: ctx_search が allow に追加されています: {entry}"
                " (#825 完了前は allow 禁止)"
            )

    return errors


def main() -> int:
    """メイン検証処理。"""
    print("context-mode permissions 検証 (#825)")
    print("=" * 60)

    settings = load_settings()
    all_errors: list[str] = []

    # MCP deny entries
    print("\n[1] MCP deny entries の確認...")
    errors = check_mcp_deny_entries(settings)
    if errors:
        all_errors.extend(errors)
        for e in errors:
            print(f"  {e}")
    else:
        print("  PASS: MCP deny entries が正しく設定されています")

    # token dump コマンド deny
    print("\n[2] token dump コマンド deny の確認...")
    errors = check_token_dump_command_deny(settings)
    if errors:
        all_errors.extend(errors)
        for e in errors:
            print(f"  {e}")
    else:
        print("  PASS: token dump コマンドが deny されています")

    # env ファイル Read deny
    print("\n[3] .env ファイル Read deny の確認...")
    errors = check_env_file_read_deny(settings)
    if errors:
        all_errors.extend(errors)
        for e in errors:
            print(f"  {e}")
    else:
        print("  PASS: .env ファイルの Read deny が設定されています")

    # ctx_index / ctx_search not in allow
    print("\n[4] ctx_index / ctx_search が allow に追加されていないことの確認...")
    errors = check_ctx_index_not_allowed(settings)
    if errors:
        all_errors.extend(errors)
        for e in errors:
            print(f"  {e}")
    else:
        print("  PASS: ctx_index / ctx_search は allow にありません")

    print("\n" + "=" * 60)
    if all_errors:
        print(f"FAIL: {len(all_errors)} 件のエラー")
        return 1
    else:
        print("PASS: 全 4 チェックが通過しました")
        return 0


if __name__ == "__main__":
    sys.exit(main())

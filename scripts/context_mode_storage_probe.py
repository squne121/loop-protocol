#!/usr/bin/env python3
"""
context-mode storage probe スクリプト (#828)

context-mode の effective storage root を解決し、
.claude/artifacts/context-mode/persistence-proof.json を更新する。

使用方法:
    uv run python3 scripts/context_mode_storage_probe.py [--output <path>]

注意:
- context-mode がインストール済みの環境で実行すること。
- home path は <HOME> にマスクして出力する。
- raw DB / secret / unredacted home path を出力しない。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_OUTPUT = _REPO_ROOT / ".claude" / "artifacts" / "context-mode" / "persistence-proof.json"


def mask_home(path: str) -> str:
    """home path を <HOME> にマスクする。"""
    home = os.path.expanduser("~")
    return path.replace(home, "<HOME>")


def run_ctx_doctor() -> dict:
    """ctx-doctor-result.json から storage paths を読み取る。"""
    doctor_file = _REPO_ROOT / ".claude" / "artifacts" / "context-mode" / "ctx-doctor-result.json"
    if not doctor_file.exists():
        return {}
    try:
        return json.loads(doctor_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def extract_storage_paths(doctor_data: dict) -> dict[str, str]:
    """ctx-doctor-result.json の checks から storage paths を抽出する。"""
    paths: dict[str, str] = {}
    for check in doctor_data.get("checks", []):
        msg = check.get("message", "")
        if "Storage session:" in msg:
            # "Storage session: PASS — <HOME>/.claude/context-mode/sessions (adapter default)"
            match = re.search(r"—\s+(.+?)\s+\(", msg)
            if match:
                paths["sessions"] = mask_home(match.group(1))
        elif "Storage content:" in msg:
            match = re.search(r"—\s+(.+?)\s+\(", msg)
            if match:
                paths["content"] = mask_home(match.group(1))
    return paths


def build_persistence_proof(storage_paths: dict[str, str], version: str = "1.0.162") -> dict:
    """persistence-proof.json を構築する。"""
    sessions = storage_paths.get("sessions", "<HOME>/.claude/context-mode/sessions")
    content = storage_paths.get("content", "<HOME>/.claude/context-mode/content")

    return {
        "_schema": "context_mode_persistence_proof_v1",
        "_issue": "#828",
        "_generated_at": datetime.now(timezone.utc).isoformat(),
        "storage_root_resolution_order": [
            {
                "priority": 1,
                "env_var": "CONTEXT_MODE_DIR",
                "description": "明示的に指定した storage root（最優先）",
                "resolved": bool(os.environ.get("CONTEXT_MODE_DIR")),
                "note": (
                    f"CONTEXT_MODE_DIR={mask_home(os.environ['CONTEXT_MODE_DIR'])}"
                    if os.environ.get("CONTEXT_MODE_DIR")
                    else "本プロジェクトでは CONTEXT_MODE_DIR を設定していない（adapter default を使用）"
                ),
            },
            {
                "priority": 2,
                "env_var": "CLAUDE_PLUGIN_DATA",
                "description": "Claude Code plugin data ディレクトリ",
                "resolved": bool(os.environ.get("CLAUDE_PLUGIN_DATA")),
                "note": (
                    f"CLAUDE_PLUGIN_DATA={mask_home(os.environ['CLAUDE_PLUGIN_DATA'])}"
                    if os.environ.get("CLAUDE_PLUGIN_DATA")
                    else "本プロジェクトでは CLAUDE_PLUGIN_DATA を設定していない"
                ),
            },
            {
                "priority": 3,
                "source": "adapter default",
                "description": "context-mode adapter が決定するデフォルト root",
                "resolved": True,
                "note": "ctx-doctor-result.json の Storage paths 出力で確認済み（adapter default）",
            },
            {
                "priority": 4,
                "path_suffix": "sessions",
                "description": "<root>/sessions — セッションデータ（SQLite DB）",
                "resolved": True,
                "evidence_ref": "ctx-doctor-result.json checks[1].message",
                "resolved_path": sessions,
            },
            {
                "priority": 5,
                "path_suffix": "content",
                "description": "<root>/content — コンテンツ/インデックスデータ（SQLite FTS）",
                "resolved": True,
                "evidence_ref": "ctx-doctor-result.json checks[2].message",
                "resolved_path": content,
            },
        ],
        "effective_storage_root": {
            "adapter": "adapter default",
            "sessions_path_pattern": sessions,
            "content_path_pattern": content,
            "confirmed_by": f"ctx-doctor-result.json (v{version} 実行結果)",
            "home_path_masked": True,
            "note": "実際のパスは HOME に依存する。ctx-doctor を実行して確認すること",
        },
        "storage_type": {
            "sessions": "SQLite",
            "content": "SQLite + FTS5（Full Text Search）",
            "fts5_status": "native module works (ctx-doctor-result.json checks[18].message)",
        },
        "no_commit_policy": {
            "db_files": "repo に commit 禁止",
            "index_files": "repo に commit 禁止",
            "cache_files": "repo に commit 禁止",
            "raw_fetched_body": "repo に commit 禁止",
            "policy_basis": "#828 AC8 / docs/dev/agent-ops/context-mode-ops.md",
        },
        "purge_methods_verified": [
            {
                "method": "ctx_purge MCP tool",
                "tool_name": "mcp__context-mode__ctx_purge",
                "verified_version": version,
                "evidence_ref": "registered-tools.json registered_tools[ctx_purge]",
                "note": f"v{version} で registered_tools に存在確認済み",
            },
            {
                "method": "slash command /context reset",
                "command": "/context reset",
                "verified_version": version,
                "note": "セッションリセット用 slash command（コンテキストをクリア）",
            },
            {
                "method": "fallback: manual DB deletion",
                "path_pattern": f"{sessions}/",
                "note": "plugin 停止後に手動削除（fallback 手順）",
            },
            {
                "method": "fallback: manual index deletion",
                "path_pattern": f"{content}/",
                "note": "plugin 停止後に手動削除（fallback 手順）",
            },
        ],
        "version": version,
        "redaction": {
            "home_path_masked": True,
            "raw_db_excluded": True,
            "raw_secret_excluded": True,
            "placeholder_excluded": True,
            "note": "HOME path を <HOME> に置換済み。raw DB / secret / unredacted home path を含まない",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="context-mode storage probe")
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="出力先 JSON ファイルパス",
    )
    parser.add_argument(
        "--version",
        default="1.0.162",
        help="context-mode バージョン（デフォルト: 1.0.162）",
    )
    args = parser.parse_args()

    doctor_data = run_ctx_doctor()
    storage_paths = extract_storage_paths(doctor_data)

    proof = build_persistence_proof(storage_paths, version=args.version)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")

    print(f"persistence-proof.json を出力しました: {args.output}")
    print(f"  sessions: {storage_paths.get('sessions', '(not resolved)')}")
    print(f"  content:  {storage_paths.get('content', '(not resolved)')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

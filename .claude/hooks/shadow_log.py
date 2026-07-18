#!/usr/bin/env python3
"""shadow_log.py

JSONL 記録ヘルパー: guard-japanese-prose.sh の shadow mode で使用する。

責務:
  - 指定フィールドを JSONL ファイルに追記する
  - raw body / full command / token / Authorization header の記録禁止 (AC8)
  - instrumentation 失敗は stderr に記録し、silent allow しない (AC9)

Usage:
  uv run python3 shadow_log.py --log-file <path> --fields-json '<JSON>'

Exit codes:
  0  記録成功
  2  instrumentation 失敗（AC9）
"""

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict


# JSONL スキーマバージョン
SCHEMA_VERSION = "1"

# 禁止フィールド: raw body / full command / token / Authorization header を含む可能性があるキー
FORBIDDEN_FIELD_KEYS = frozenset({
    "raw_body",
    "full_command",
    "token",
    "authorization_header",
    "body_content",
    "command_line",
})


def sha256_of(text: str) -> str:
    """テキストの SHA256 ハッシュを返す（hex 文字列）。"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sanitize_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    禁止フィールドを除外し、必須フィールドを確認する。
    AC8 準拠: raw body / full command / token / Authorization header を除外。
    """
    sanitized = {}
    for key, value in fields.items():
        if key in FORBIDDEN_FIELD_KEYS:
            # 禁止フィールドは記録しない
            continue
        sanitized[key] = value
    return sanitized


def build_log_entry(fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    JSONL エントリを組み立てる。
    schema_version と timestamp を自動補完する。
    """
    entry: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    clean = sanitize_fields(fields)
    entry.update(clean)

    return entry


def append_jsonl(log_file: str, entry: Dict[str, Any]) -> None:
    """JSONL ファイルにエントリを追記する。"""
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    # Issue #1572 REQUEST_CHANGES Blocker 4: allow_nan=False so this producer
    # can never emit NaN/Infinity/-Infinity tokens, which are accepted by
    # Python's json module by default but are not valid JSON values -- the
    # consumer-side executor's well-formed-JSONL check
    # (scripts/agent-guards/skill_runtime_exec.py::_parse_shadow_log_jsonl)
    # explicitly rejects them, so this producer must never generate them.
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="shadow_log.py: JSONL 記録ヘルパー")
    parser.add_argument("--log-file", required=True, help="書き込み先 JSONL ファイルパス")
    parser.add_argument(
        "--fields-json",
        required=True,
        help="記録フィールドの JSON オブジェクト文字列",
    )
    args = parser.parse_args()

    # fields-json を解析
    try:
        fields = json.loads(args.fields_json)
    except json.JSONDecodeError as e:
        print(
            f"shadow_log: instrumentation_error: fields-json parse failed: {e}",
            file=sys.stderr,
        )
        return 2

    if not isinstance(fields, dict):
        print(
            "shadow_log: instrumentation_error: fields-json must be a JSON object",
            file=sys.stderr,
        )
        return 2

    # エントリを組み立てて書き込む
    try:
        entry = build_log_entry(fields)
        append_jsonl(args.log_file, entry)
    except Exception as e:  # noqa: BLE001
        print(
            f"shadow_log: instrumentation_error: failed to write JSONL: {e}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())

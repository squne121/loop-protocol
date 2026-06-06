"""test_shadow_log.py

shadow_log.py の単体テスト。

AC2, AC8, AC9 対応。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SHADOW_LOG_PY = Path(__file__).parent.parent / "shadow_log.py"


def run_shadow_log(
    log_file: str,
    fields_json: str,
) -> subprocess.CompletedProcess:
    """shadow_log.py を実行するヘルパー。"""
    return subprocess.run(
        [
            sys.executable,
            str(SHADOW_LOG_PY),
            "--log-file", log_file,
            "--fields-json", fields_json,
        ],
        capture_output=True,
        text=True,
    )


def read_jsonl(path: str) -> list[dict]:
    """JSONL ファイルを読み込んでリストとして返す。"""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


class TestShadowLogBasic:
    """shadow_log.py の基本動作テスト。"""

    def test_writes_jsonl_entry(self, tmp_path):
        """基本フィールドで JSONL を書き込む。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fields = {
            "hook_event": "PreToolUse",
            "mode_configured": "shadow",
            "mode_effective": "shadow",
            "tool_name": "Bash",
            "route_id": "gh_inline_body",
            "body_source": "gh_body_inline",
            "public_mutation": True,
            "decision_would_be": "deny",
            "reason_code": "japanese_prose_insufficient",
            "failed_block_count": 2,
            "duration_ms": 42,
            "body_sha256": "abc123",
            "body_bytes": 100,
        }
        result = run_shadow_log(log_file, json.dumps(fields))
        assert result.returncode == 0, f"shadow_log failed: {result.stderr}"

        entries = read_jsonl(log_file)
        assert len(entries) == 1
        entry = entries[0]

        # schema_version と timestamp は自動補完される
        assert entry["schema_version"] == "1"
        assert "timestamp" in entry

        # フィールドが正しく記録される
        assert entry["hook_event"] == "PreToolUse"
        assert entry["route_id"] == "gh_inline_body"
        assert entry["decision_would_be"] == "deny"

    def test_appends_multiple_entries(self, tmp_path):
        """複数エントリを追記できる。"""
        log_file = str(tmp_path / "shadow.jsonl")
        for i in range(3):
            fields = {"route_id": f"route_{i}", "failed_block_count": i}
            result = run_shadow_log(log_file, json.dumps(fields))
            assert result.returncode == 0

        entries = read_jsonl(log_file)
        assert len(entries) == 3
        for i, entry in enumerate(entries):
            assert entry["route_id"] == f"route_{i}"

    def test_creates_parent_directory(self, tmp_path):
        """親ディレクトリが存在しなくても作成する。"""
        log_file = str(tmp_path / "nested" / "deep" / "shadow.jsonl")
        result = run_shadow_log(log_file, json.dumps({"route_id": "test"}))
        assert result.returncode == 0
        assert Path(log_file).exists()


class TestShadowLogAC8NoBannedFields:
    """AC8: 禁止フィールドを記録しない。"""

    def test_raw_body_is_excluded(self, tmp_path):
        """raw_body フィールドは記録されない（AC8）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fields = {
            "raw_body": "secret content here",
            "route_id": "test",
        }
        result = run_shadow_log(log_file, json.dumps(fields))
        assert result.returncode == 0

        entries = read_jsonl(log_file)
        assert len(entries) == 1
        assert "raw_body" not in entries[0], "raw_body が記録されている"

    def test_full_command_is_excluded(self, tmp_path):
        """full_command フィールドは記録されない（AC8）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fields = {
            "full_command": "gh issue create --token abc123",
            "route_id": "test",
        }
        result = run_shadow_log(log_file, json.dumps(fields))
        assert result.returncode == 0

        entries = read_jsonl(log_file)
        assert "full_command" not in entries[0], "full_command が記録されている"

    def test_token_is_excluded(self, tmp_path):
        """token フィールドは記録されない（AC8）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fields = {
            "token": "ghp_secret_token_12345",
            "route_id": "test",
        }
        result = run_shadow_log(log_file, json.dumps(fields))
        assert result.returncode == 0

        entries = read_jsonl(log_file)
        assert "token" not in entries[0], "token が記録されている"

    def test_authorization_header_is_excluded(self, tmp_path):
        """authorization_header フィールドは記録されない（AC8）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fields = {
            "authorization_header": "Bearer ghp_secret",
            "route_id": "test",
        }
        result = run_shadow_log(log_file, json.dumps(fields))
        assert result.returncode == 0

        entries = read_jsonl(log_file)
        assert "authorization_header" not in entries[0], (
            "authorization_header が記録されている"
        )

    def test_allowed_fields_are_recorded(self, tmp_path):
        """hash・byte length・route_id・body_source・reason_code は記録可能（AC8）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fields = {
            "body_sha256": "abc123def456",
            "body_bytes": 200,
            "route_id": "gh_inline_body",
            "body_source": "gh_body_inline",
            "reason_code": "japanese_prose_insufficient",
        }
        result = run_shadow_log(log_file, json.dumps(fields))
        assert result.returncode == 0

        entries = read_jsonl(log_file)
        entry = entries[0]
        assert entry["body_sha256"] == "abc123def456"
        assert entry["body_bytes"] == 200
        assert entry["route_id"] == "gh_inline_body"
        assert entry["body_source"] == "gh_body_inline"
        assert entry["reason_code"] == "japanese_prose_insufficient"


class TestShadowLogAC9InstrumentationError:
    """AC9: instrumentation 失敗は exit 2 で返す（silent allow しない）。"""

    def test_invalid_json_returns_exit2(self, tmp_path):
        """fields-json が不正な JSON の場合は exit 2。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_shadow_log(log_file, "not-valid-json{{{")
        assert result.returncode == 2, f"invalid JSON must return exit 2: {result.stderr}"
        assert "instrumentation_error" in result.stderr

    def test_non_object_json_returns_exit2(self, tmp_path):
        """fields-json がオブジェクトでない場合は exit 2。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_shadow_log(log_file, "[1, 2, 3]")
        assert result.returncode == 2, f"non-object JSON must return exit 2: {result.stderr}"
        assert "instrumentation_error" in result.stderr

    def test_write_failure_returns_exit2(self, tmp_path):
        """書き込み不可パスへの書き込みは exit 2。"""
        # /nonexistent_dir/ は通常書き込み不可
        log_file = "/nonexistent_dir_xyz/shadow.jsonl"
        result = run_shadow_log(log_file, json.dumps({"route_id": "test"}))
        assert result.returncode == 2, (
            f"write failure must return exit 2: returncode={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert "instrumentation_error" in result.stderr


class TestShadowLogSchemaVersion:
    """schema_version の自動付与テスト。"""

    def test_schema_version_is_auto_added(self, tmp_path):
        """schema_version は fields に含まれなくても自動付与される。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fields = {"route_id": "test_route"}
        result = run_shadow_log(log_file, json.dumps(fields))
        assert result.returncode == 0

        entries = read_jsonl(log_file)
        assert entries[0]["schema_version"] == "1"

    def test_timestamp_is_auto_added(self, tmp_path):
        """timestamp は fields に含まれなくても自動付与される。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fields = {"route_id": "test_route"}
        result = run_shadow_log(log_file, json.dumps(fields))
        assert result.returncode == 0

        entries = read_jsonl(log_file)
        assert "timestamp" in entries[0]
        # ISO 8601 形式の確認（簡易）
        assert "T" in entries[0]["timestamp"]
        assert "Z" in entries[0]["timestamp"]

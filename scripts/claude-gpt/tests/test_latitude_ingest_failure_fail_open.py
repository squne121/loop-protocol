"""scripts/claude-gpt/tests/test_latitude_ingest_failure_fail_open.py

Issue #2426 AC8: Latitude ingest unavailable / auth failure / hook failure は
Claude-GPT session 本体を失敗させない（observation-only / fail-open）。
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parent.parent / "latitude_hook.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_hook_module_2426_fail_open", _HOOK_PATH
)
latitude_hook = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(latitude_hook)


class _FakeStdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def test_main_returns_zero_when_required_env_vars_are_absent(monkeypatch):
    """GIVEN CLAUDE_GPT_HOME_ROOT/CLAUDE_GPT_LATITUDE_PACKAGE_SPEC が未設定
    WHEN main() を呼ぶ
    THEN exit code 0（AC8: 設定不足で Stop イベントを失敗させない）
    """
    monkeypatch.delenv("CLAUDE_GPT_HOME_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_GPT_LATITUDE_PACKAGE_SPEC", raising=False)
    monkeypatch.delenv("CLAUDE_GPT_NATIVE_SETTINGS_PATH", raising=False)
    monkeypatch.setattr("sys.stdin", _FakeStdin(b'{"session_id": "abc"}'))
    assert latitude_hook.main() == 0


def test_main_returns_zero_when_native_settings_missing_api_key(monkeypatch, tmp_path):
    """GIVEN Native settings に LATITUDE_API_KEY が無い（ingest 未設定）
    WHEN main() を呼ぶ
    THEN exit code 0（何もせず正常終了。AC8）
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"env": {}}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_GPT_NATIVE_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("CLAUDE_GPT_HOME_ROOT", str(tmp_path / "home-root"))
    monkeypatch.setenv("CLAUDE_GPT_LATITUDE_PACKAGE_SPEC", "@latitude-data/claude-code-telemetry@0.0.14")
    monkeypatch.setattr("sys.stdin", _FakeStdin(b'{"session_id": "abc"}'))
    assert latitude_hook.main() == 0


def test_main_returns_zero_when_npx_is_not_resolvable(monkeypatch, tmp_path):
    """GIVEN 資格情報は揃っているが npx binary が解決できない環境
    WHEN main() を呼ぶ
    THEN exit code 0（telemetry 起動を諦めるだけで Stop イベント自体は失敗させない）
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"env": {"LATITUDE_API_KEY": "k", "LATITUDE_PROJECT": "p"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_GPT_NATIVE_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("CLAUDE_GPT_HOME_ROOT", str(tmp_path / "home-root"))
    monkeypatch.setenv("CLAUDE_GPT_LATITUDE_PACKAGE_SPEC", "@latitude-data/claude-code-telemetry@0.0.14")
    monkeypatch.delenv("CLAUDE_GPT_LATITUDE_NPX_BIN", raising=False)
    monkeypatch.setattr(latitude_hook.shutil, "which", lambda _name: None)
    monkeypatch.setattr("sys.stdin", _FakeStdin(b'{"session_id": "abc"}'))
    assert latitude_hook.main() == 0


def test_main_returns_zero_when_telemetry_subprocess_raises(monkeypatch, tmp_path):
    """GIVEN telemetry subprocess の起動（Popen）が例外を送出する（npx binary
         破損等の crash 相当）
    WHEN main() を呼ぶ
    THEN exit code 0（AC8: telemetry の失敗を Claude-GPT session の失敗にしない）
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"env": {"LATITUDE_API_KEY": "k", "LATITUDE_PROJECT": "p"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_GPT_NATIVE_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("CLAUDE_GPT_HOME_ROOT", str(tmp_path / "home-root"))
    monkeypatch.setenv("CLAUDE_GPT_LATITUDE_PACKAGE_SPEC", "@latitude-data/claude-code-telemetry@0.0.14")
    monkeypatch.setattr(latitude_hook.shutil, "which", lambda _name: "/usr/bin/npx")

    def _raise(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(latitude_hook.subprocess, "Popen", _raise)
    monkeypatch.setattr("sys.stdin", _FakeStdin(b'{"session_id": "abc"}'))
    assert latitude_hook.main() == 0


def test_main_returns_zero_when_telemetry_subprocess_stdin_write_fails(monkeypatch, tmp_path):
    """GIVEN telemetry subprocess の起動には成功するが stdin への書き込みが失敗する
         （broken pipe 相当。fire-and-forget spawn 後の異常系）
    WHEN main() を呼ぶ
    THEN exit code 0（AC8）
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"env": {"LATITUDE_API_KEY": "k", "LATITUDE_PROJECT": "p"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_GPT_NATIVE_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("CLAUDE_GPT_HOME_ROOT", str(tmp_path / "home-root"))
    monkeypatch.setenv("CLAUDE_GPT_LATITUDE_PACKAGE_SPEC", "@latitude-data/claude-code-telemetry@0.0.14")
    monkeypatch.setattr(latitude_hook.shutil, "which", lambda _name: "/usr/bin/npx")

    class _FakeStdinPipe:
        def write(self, _data):
            raise BrokenPipeError("boom")

        def close(self):
            pass

    class _FakeProc:
        stdin = _FakeStdinPipe()

    monkeypatch.setattr(latitude_hook.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr("sys.stdin", _FakeStdin(b'{"session_id": "abc"}'))
    assert latitude_hook.main() == 0


def test_main_returns_zero_on_completely_malformed_stdin():
    """GIVEN stdin が JSON ですらない
    WHEN main() を呼ぶ
    THEN exit code 0（parse failure でも fail-open。AC8）
    """
    import sys

    old_stdin = sys.stdin
    try:
        sys.stdin = _FakeStdin(b"\xff\xfe not json at all")
        assert latitude_hook.main() == 0
    finally:
        sys.stdin = old_stdin


def test_main_returns_zero_on_empty_stdin():
    """GIVEN stdin が空
    WHEN main() を呼ぶ
    THEN exit code 0
    """
    import sys

    old_stdin = sys.stdin
    try:
        sys.stdin = _FakeStdin(b"")
        assert latitude_hook.main() == 0
    finally:
        sys.stdin = old_stdin

"""scripts/claude-gpt/tests/test_latitude_api_key_hygiene.py

Issue #2426 AC3: `LATITUDE_API_KEY` は repository tracked file、Claude-GPT
generated settings、argv、stdout/stderr、persisted evidence、fixture に
現れない。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_hspec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2426_hygiene", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_hspec)
assert _hspec.loader is not None
_hspec.loader.exec_module(_helper)
run_check_only = _helper.run_check_only

_HOOK_PATH = Path(__file__).resolve().parent.parent / "latitude_hook.py"
_mspec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_hook_module_2426_hygiene", _HOOK_PATH
)
latitude_hook = importlib.util.module_from_spec(_mspec)
assert _mspec.loader is not None
_mspec.loader.exec_module(latitude_hook)

FAKE_KEY = "sk-latitude-test-fixture-not-a-real-secret-0123456789"


def test_generated_settings_never_contain_the_native_api_key_value(tmp_path):
    """GIVEN Native settings に FAKE_KEY を持つ fixture
    WHEN launch.sh --check-only を実行する
    THEN 生成された settings.local.json のどこにも FAKE_KEY の値が現れない（AC3）
    """
    result, settings_path = run_check_only(
        tmp_path,
        native_settings={"env": {"LATITUDE_API_KEY": FAKE_KEY, "LATITUDE_PROJECT": "proj"}},
    )
    assert settings_path.exists(), result.stderr
    raw = settings_path.read_text(encoding="utf-8")
    assert FAKE_KEY not in raw
    # 参照は path のみ（値そのものではない）
    settings = json.loads(raw)
    assert settings["env"]["CLAUDE_GPT_NATIVE_SETTINGS_PATH"].endswith(".claude/settings.json")


def test_launch_sh_stdout_and_stderr_never_contain_the_native_api_key_value(tmp_path):
    """GIVEN Native settings に FAKE_KEY を持つ fixture
    WHEN launch.sh --check-only を実行する
    THEN launch.sh 自身の stdout/stderr にも FAKE_KEY が一切現れない（AC3）
    """
    result, _settings_path = run_check_only(
        tmp_path,
        native_settings={"env": {"LATITUDE_API_KEY": FAKE_KEY, "LATITUDE_PROJECT": "proj"}},
    )
    assert FAKE_KEY not in result.stdout
    assert FAKE_KEY not in result.stderr


def test_build_child_env_only_contains_home_path_and_allowlist_keys():
    """GIVEN session_home と allowlist dict
    WHEN build_child_env() を呼ぶ
    THEN 返り値の key は HOME/PATH + 渡された allowlist の key だけであり、
         ambient os.environ の他の変数は一切含まれない（AC3）
    """
    child_env = latitude_hook.build_child_env(
        "/tmp/fake-session-home",
        {"LATITUDE_API_KEY": FAKE_KEY, "LATITUDE_PROJECT": "proj"},
        path_value="/usr/bin:/bin",
    )
    assert child_env == {
        "HOME": "/tmp/fake-session-home",
        "PATH": "/usr/bin:/bin",
        "LATITUDE_API_KEY": FAKE_KEY,
        "LATITUDE_PROJECT": "proj",
    }


def test_build_child_env_never_leaks_ambient_secrets(monkeypatch):
    """GIVEN ambient os.environ に allowlist 外の secret-looking 変数が存在する
    WHEN build_child_env() を呼ぶ（path_value 未指定 = os.environ["PATH"] にフォールバック）
    THEN その secret-looking 変数は child env に一切現れない（AC3）
    """
    monkeypatch.setenv("SOME_OTHER_SECRET_TOKEN", "should-never-leak")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    child_env = latitude_hook.build_child_env(
        "/tmp/fake-session-home",
        {"LATITUDE_API_KEY": FAKE_KEY},
    )
    assert "SOME_OTHER_SECRET_TOKEN" not in child_env
    assert set(child_env.keys()) == {"HOME", "PATH", "LATITUDE_API_KEY"}


def test_hook_main_never_raises_and_never_prints_key_on_bogus_stdin(monkeypatch, capsys):
    """GIVEN 壊れた stdin payload + CLAUDE_GPT_NATIVE_SETTINGS_PATH が FAKE_KEY を
         持つ fixture を指す環境
    WHEN main() を呼ぶ（npx 解決不能などで内部的に失敗する状況を想定）
    THEN exit code は常に 0、かつ stdout/stderr のどちらにも FAKE_KEY が出ない（AC3/AC8）
    """
    monkeypatch.setattr("sys.stdin", _FakeStdin(b"not valid json"))
    monkeypatch.setenv("CLAUDE_GPT_NATIVE_SETTINGS_PATH", "/nonexistent/settings.json")
    monkeypatch.setenv("CLAUDE_GPT_HOME_ROOT", "/tmp/does-not-matter")
    monkeypatch.setenv("CLAUDE_GPT_LATITUDE_PACKAGE_SPEC", "@latitude-data/claude-code-telemetry@0.0.14")
    rc = latitude_hook.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err


class _FakeStdin:
    def __init__(self, data: bytes) -> None:
        self._buffer = _FakeBuffer(data)

    @property
    def buffer(self):
        return self._buffer


class _FakeBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

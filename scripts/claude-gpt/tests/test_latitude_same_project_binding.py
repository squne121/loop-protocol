"""scripts/claude-gpt/tests/test_latitude_same_project_binding.py

Issue #2426 AC4: Native と Claude-GPT は同一 `LATITUDE_PROJECT` を使用でき、
既存 #2375（PR #2392）collector の project/session binding を変更しない。

PR #2439 P1 fix-delta (OWNER REQUEST_CHANGES): 以前のバージョンは (1) Native
settings の project 値が telemetry subprocess の child env に届くこと、(2) #2375
collector が `monkeypatch.setenv()` で LATITUDE_PROJECT を読めることを別々に
検証するだけで、launcher（`launch.sh`）自身が実際にその2つを橋渡ししている
ことを一度も検証していなかった（test 側の monkeypatch が launcher の代役に
なってしまっていた）。本ファイルは `launch.sh --check-only` が実際に生成する
Claude-GPT settings artifact を対象に、launcher が生成する
`env.LATITUDE_PROJECT` の値が Native の値と exact に一致すること、
`LATITUDE_API_KEY` がその artifact に一切現れないこと、そして #2375 collector
の `_default_latitude_project_slug()` がその real runtime env（monkeypatch では
なく実際に `env` へ export した値）から project を解決できることを end-to-end
で検証する。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parent.parent / "latitude_hook.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_hook_module_2426_same_project", _HOOK_PATH
)
latitude_hook = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(latitude_hook)

_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_hspec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2426_same_project", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_hspec)
assert _hspec.loader is not None
_hspec.loader.exec_module(_helper)
run_check_only = _helper.run_check_only

_COLLECT_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / ".claude"
    / "skills"
    / "agent-retrospective"
    / "scripts"
    / "collect_snapshot.py"
)

FAKE_PROJECT = "shared-project"
FAKE_KEY = "sk-latitude-same-project-binding-fixture-not-a-real-secret"


def test_native_project_value_is_forwarded_unmodified_to_child_env(tmp_path):
    """GIVEN Native settings が LATITUDE_PROJECT="shared-project" を持つ
    WHEN read_native_latitude_allowlist() -> build_child_env() の経路を通す
    THEN telemetry subprocess へ渡す child env の LATITUDE_PROJECT は Native の
         値のまま変更されない（AC4: launcher が project を書き換えない）
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        f'{{"env": {{"LATITUDE_API_KEY": "k", "LATITUDE_PROJECT": "{FAKE_PROJECT}"}}}}',
        encoding="utf-8",
    )
    allowlist = latitude_hook.read_native_latitude_allowlist(str(settings_path))
    child_env = latitude_hook.build_child_env("/tmp/session-home", allowlist, path_value="/bin")
    assert child_env["LATITUDE_PROJECT"] == FAKE_PROJECT


def test_launcher_generated_settings_carry_the_exact_native_latitude_project_value(tmp_path):
    """GIVEN Native settings が LATITUDE_PROJECT="shared-project" を持つ
    WHEN launch.sh --check-only を実機実行する（monkeypatch ではなく real launcher）
    THEN 生成された Claude-GPT settings.local.json の `env.LATITUDE_PROJECT` が
         Native の値と exact に一致し（PR #2439 P0 fix-delta の回帰確認）、
         `env.LATITUDE_API_KEY` はそこに一切現れない（AC3 と整合。secret は
         引き続き latitude_hook.py の child-only telemetry subprocess env に
         のみ渡る）
    """
    result, settings_path = run_check_only(
        tmp_path,
        native_settings={"env": {"LATITUDE_API_KEY": FAKE_KEY, "LATITUDE_PROJECT": FAKE_PROJECT}},
    )
    assert settings_path.exists(), result.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    env = settings["env"]
    assert env["LATITUDE_PROJECT"] == FAKE_PROJECT
    assert "LATITUDE_API_KEY" not in env
    raw = settings_path.read_text(encoding="utf-8")
    assert FAKE_KEY not in raw


def test_collector_resolves_project_from_real_launcher_generated_runtime_env(tmp_path):
    """GIVEN launch.sh --check-only が実際に生成した settings.local.json の
         `env.LATITUDE_PROJECT`
    WHEN その real 値を（monkeypatch.setenv ではなく）子プロセス起動 argv 経由で
         #2375 collector の `_default_latitude_project_slug()` へ渡す
    THEN launcher が生成した runtime 値そのものから project slug を解決できる
         （AC4/AC9: launcher と collector の間の end-to-end binding を、
         テスト側の代役 monkeypatch なしに確認する）
    """
    result, settings_path = run_check_only(
        tmp_path,
        native_settings={"env": {"LATITUDE_API_KEY": FAKE_KEY, "LATITUDE_PROJECT": FAKE_PROJECT}},
    )
    assert settings_path.exists(), result.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    launcher_generated_project = settings["env"]["LATITUDE_PROJECT"]
    assert launcher_generated_project == FAKE_PROJECT

    child_env = dict(os.environ)
    child_env["LATITUDE_PROJECT"] = launcher_generated_project
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, sys; "
                "spec = importlib.util.spec_from_file_location('cs', sys.argv[1]); "
                "m = importlib.util.module_from_spec(spec); "
                "sys.modules['cs'] = m; "
                "spec.loader.exec_module(m); "
                "sys.stdout.write(m._default_latitude_project_slug() or '')"
            ),
            str(_COLLECT_SNAPSHOT_PATH),
        ],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == FAKE_PROJECT


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

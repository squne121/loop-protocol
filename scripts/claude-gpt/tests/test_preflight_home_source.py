"""scripts/claude-gpt/tests/test_preflight_home_source.py

Issue #2803: `preflight.sh` の既存通常実行結果（`CLAUDE_GPT_PREFLIGHT_RESULT_V1`）に
追加された `home_source: default | env_override` フィールドの regression test。

検証対象:
  - AC3: `home_source` フィールドが `preflight.sh --env-only` の JSON 出力に
    追加されており、`CLAUDE_GPT_HOME` を明示指定しない場合は `default`、
    明示指定した場合は `env_override` になる。
  - AC4: `home_source` フィールドの値が credential の中身（token / auth.json
    content / 実際のパス文字列）を一切含まず、`default` / `env_override` の
    2値のいずれかのみを取ることを検証する。

`preflight.sh` は単体で subprocess として起動可能なため、`launch.sh` を経由
せずに直接検証する（Runtime Verification Applicability: この AC3/AC4/AC5 は
static/hermetic 検証で完結する。external process 起動を伴う
`launch.sh --check-only` 経由の伝播検証は AC6 として
`test_launch_check_only_home_source_smoke.py` に分離している）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PREFLIGHT_SH = SCRIPT_DIR / "preflight.sh"


def _run_preflight_env_only(tmp_path: Path, *, claude_gpt_home: str | None) -> dict:
    """`preflight.sh --env-only` を isolated HOME で hermetic に実行する。

    実 `~/.claude-gpt` を絶対に汚染しないため、`HOME` も isolated tmp 配下へ
    固定する（`claude_gpt_home` が None の場合、canonical default
    `${HOME}/.claude-gpt` は isolated HOME 配下に解決される）。
    """
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["HOME"] = str(isolated_home)
    if claude_gpt_home is None:
        env.pop("CLAUDE_GPT_HOME", None)
    else:
        env["CLAUDE_GPT_HOME"] = claude_gpt_home

    result = subprocess.run(
        [str(PREFLIGHT_SH), "--env-only"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip(), f"preflight.sh produced no stdout: {result.stderr}"
    return json.loads(result.stdout)


def test_home_source_is_default_when_claude_gpt_home_unset(tmp_path):
    """GIVEN CLAUDE_GPT_HOME が未設定 (isolated HOME 配下)
    WHEN preflight.sh --env-only を実行する
    THEN home_source は "default" になる (canonical ${HOME}/.claude-gpt 相当)
    """
    payload = _run_preflight_env_only(tmp_path, claude_gpt_home=None)

    assert payload["schema"] == "CLAUDE_GPT_PREFLIGHT_RESULT_V1"
    assert payload["home_source"] == "default"


def test_home_source_is_env_override_when_claude_gpt_home_explicitly_set(tmp_path):
    """GIVEN CLAUDE_GPT_HOME が明示的な isolated temp path に設定されている
    WHEN preflight.sh --env-only を実行する
    THEN home_source は "env_override" になる
    """
    override_home = tmp_path / "explicit-claude-gpt-home"
    payload = _run_preflight_env_only(tmp_path, claude_gpt_home=str(override_home))

    assert payload["schema"] == "CLAUDE_GPT_PREFLIGHT_RESULT_V1"
    assert payload["home_source"] == "env_override"


def test_home_source_value_is_sanitized_enum_only(tmp_path):
    """GIVEN CLAUDE_GPT_HOME が明示的な isolated temp path に設定されている
    WHEN preflight.sh --env-only を実行する
    THEN home_source の値は "default" / "env_override" の固定文字列のみであり、
         実際のパス文字列・credential の中身 (token / auth.json content) を
         一切含まない (AC4)
    """
    override_home = tmp_path / "explicit-claude-gpt-home-with-secretlike-name"
    payload = _run_preflight_env_only(tmp_path, claude_gpt_home=str(override_home))

    home_source = payload["home_source"]

    assert home_source in ("default", "env_override")
    # sanitized: パス文字列そのもの・区切り文字・home directory 名を含まない
    assert str(override_home) not in home_source
    assert "/" not in home_source
    assert str(tmp_path) not in home_source
    # sanitized: よくある credential/token フィールド名を値として含まない
    assert "token" not in home_source.lower()
    assert "auth" not in home_source.lower()


def test_home_source_does_not_leak_into_other_preflight_runs_negative_control(tmp_path):
    """GIVEN 2回連続で異なる CLAUDE_GPT_HOME 設定 (override -> unset) で実行する
    WHEN 各実行の home_source を比較する
    THEN 前の実行の override 状態が後続の default 判定に混入しない
         (snapshot が呼び出し毎に独立していることの negative control)
    """
    override_home = tmp_path / "first-run-override-home"
    first = _run_preflight_env_only(tmp_path, claude_gpt_home=str(override_home))
    assert first["home_source"] == "env_override"

    second = _run_preflight_env_only(tmp_path, claude_gpt_home=None)
    assert second["home_source"] == "default"

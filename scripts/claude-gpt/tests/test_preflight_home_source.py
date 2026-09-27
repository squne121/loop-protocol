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

PR #2804 レビュー P3-3 対応: `_run_preflight_env_only()` は PATH 上の実
`claude-code-proxy` に依存せず、既存の shared hermetic fixture
（`_latitude_check_only_helper.py` の `FAKE_PROXY_SOURCE` / `write_executable()`）
で `claude-code-proxy` を fake 化し、`preflight.sh --env-only` の exit code
契約（0=全 PASS、3=proxy バイナリなし、4=認証利用不能）を明示的に assert する。
これにより `home_source` フィールドの一致だけで binary/auth check の失敗を
見逃したまま PASS 扱いにしない。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PREFLIGHT_SH = SCRIPT_DIR / "preflight.sh"

_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2803_preflight_home_source", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_helper)

write_executable = _helper.write_executable
FAKE_PROXY_SOURCE = _helper.FAKE_PROXY_SOURCE


def _run_preflight_env_only(
    tmp_path: Path,
    *,
    claude_gpt_home: str | None,
    expected_returncode: int = 0,
) -> dict:
    """`preflight.sh --env-only` を isolated HOME で hermetic に実行する。

    実 `~/.claude-gpt` を絶対に汚染しないため、`HOME` も isolated tmp 配下へ
    固定する（`claude_gpt_home` が None の場合、canonical default
    `${HOME}/.claude-gpt` は isolated HOME 配下に解決される）。

    PATH 上の実 `claude-code-proxy` には依存せず、既存の shared hermetic
    fixture（fake proxy）を `CLAUDE_GPT_PROXY_BIN` で注入する
    （`lib.sh` の `claude_gpt_resolve_proxy_bin()` がこの env var を優先解決
    に使う）。`--env-only` は外部 `claude` バイナリを呼ばないため
    `CLAUDE_GPT_CLAUDE_BIN` の fake 化は不要（pop のみ行い、ambient な export
    があっても影響しないことを保証する）。`expected_returncode` で
    `preflight.sh --env-only` の exit code 契約を明示的に検証する。
    """
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir(parents=True, exist_ok=True)

    fake_proxy = write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)

    env = dict(os.environ)
    env["HOME"] = str(isolated_home)
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env.pop("CLAUDE_GPT_CLAUDE_BIN", None)
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
    assert result.returncode == expected_returncode, (
        f"preflight.sh --env-only exited {result.returncode} "
        f"(expected {expected_returncode}); stderr={result.stderr}"
    )
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


def test_home_source_is_default_when_explicit_value_lexically_equals_canonical_default(
    tmp_path,
):
    """GIVEN caller が CLAUDE_GPT_HOME を明示的に、canonical default 式
         (`${HOME}/.claude-gpt`, isolated HOME 配下) と文字列として完全一致する
         値に設定する
    WHEN preflight.sh --env-only を実行する
    THEN home_source は "default" になる

    P2 (PR #2804 レビュー comment 5856378465): `home_source` は「caller が env
    var を明示指定したかどうか」の provenance ではなく、effective 値が
    canonical default 式と lexical に一致するかどうかだけで判定される
    diagnostic field であることを固定する negative control。ここでは caller
    が明示的に env var を指定しているにもかかわらず、値が canonical default
    式と lexical に一致するため `default` になる。
    """
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    canonical_default_value = str(isolated_home / ".claude-gpt")

    payload = _run_preflight_env_only(tmp_path, claude_gpt_home=canonical_default_value)

    assert payload["schema"] == "CLAUDE_GPT_PREFLIGHT_RESULT_V1"
    assert payload["home_source"] == "default"


def test_home_source_is_env_override_for_same_directory_different_lexical_form(
    tmp_path,
):
    """GIVEN caller が canonical default と同じ実ディレクトリを意図しているが、
         末尾スラッシュ等 lexical representation が異なる値を明示指定する
    WHEN preflight.sh --env-only を実行する
    THEN home_source は "env_override" になる

    P2 (PR #2804 レビュー comment 5856378465) negative control: `home_source`
    の判定は文字列としての lexical 一致のみで行い、realpath 等による
    filesystem canonicalization（同一ディレクトリを指す表記揺れの正規化）は
    一切行わないことを固定する。
    """
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    canonical_default_value = str(isolated_home / ".claude-gpt")
    lexically_different_same_target = canonical_default_value + "/"

    payload = _run_preflight_env_only(
        tmp_path, claude_gpt_home=lexically_different_same_target
    )

    assert payload["schema"] == "CLAUDE_GPT_PREFLIGHT_RESULT_V1"
    assert payload["home_source"] == "env_override"

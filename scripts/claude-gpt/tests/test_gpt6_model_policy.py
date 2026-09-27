"""scripts/claude-gpt/tests/test_gpt6_model_policy.py

Issue #2772: GPT-6 Sol/Luna を既定 allocation policy とし、GPT-6 Astra を
default opus alias にせず明示的な on-demand escalation としてのみ到達可能に
する。Auto mode classifier routing は Luna へ復帰する。

対応する Verification Command（Issue #2772 本文 `## Verification Commands`）:
  uv run --locked pytest scripts/claude-gpt/tests/test_gpt6_model_policy.py -q
applicable AC: AC1, AC2, AC3, AC4, AC6, AC7, AC8

Runtime Verification Applicability: Issue #2772 は `decision: immediate`
（applicable_acs: AC7, AC8, AC10）。本ファイルは AC7/AC8 の静的・hermetic
subprocess 部分のみを扱う。AC10（実 ChatGPT subscription request による
one-shot adoption smoke）は本ファイルでは扱わず、`runtime_smoke_test.sh` の
手動/CI 実行で確認する（実環境が利用不能な場合 SKIP=exit 77 とし、PASS へ
昇格しない）。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

# --- shared fixtures --------------------------------------------------------

FAKE_PROXY_SOURCE_TEMPLATE = r"""#!/usr/bin/env python3
import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = __MODELS_JSON__


def _capture_env():
    capture_dir = os.environ.get("CCP_CONFIG_DIR")
    if not capture_dir:
        return
    try:
        os.makedirs(capture_dir, exist_ok=True)
        target = os.path.join(capture_dir, "captured-env.json")
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(dict(os.environ), fh)
    except OSError:
        pass


def _serve(port: int) -> int:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):  # noqa: A002
            return

    httpd = HTTPServer(("127.0.0.1", port), Handler)

    def _on_term(signum, frame):
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    httpd.serve_forever()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 1
    if args[0] == "--version":
        print("fake-claude-code-proxy 0.0.0-test")
        return 0
    if args[0] == "codex" and len(args) >= 3 and args[1] == "auth" and args[2] == "status":
        print("Account: fake-test-account")
        return 0
    if args[0] == "serve":
        port = None
        i = 1
        while i < len(args):
            if args[i] == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
            else:
                i += 1
        if port is None:
            return 1
        _capture_env()
        return _serve(port)
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""

# fake claude: 既存 test_auto_mode_policy.py の FAKE_CLAUDE_SOURCE と同じ
# readback 応答（--version / auto-mode defaults / auto-mode config）を実装し、
# preflight.sh --auto-mode-check を PASS させる。加えて、実際の claude 本体
# 起動 invocation（--version/auto-mode 以外）が受け取った os.environ 全体を
# CAPTURED_CLAUDE_ENV_FILE へ書き出す（AC4: claude client 側の
# CLAUDE_CODE_AUTO_MODE_SERVER export が実際に子プロセスへ届くことを確認する
# ため。proxy 側 env -i capture とは別の独立した capture 経路）。
FAKE_CLAUDE_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]

if argv and argv[0] == "--version":
    print(os.environ.get("FAKE_CLAUDE_VERSION") or "2.1.211 (Claude Code)")
    sys.exit(0)

if "auto-mode" in argv:
    auto_mode_idx = argv.index("auto-mode")
    subcommand = argv[auto_mode_idx + 1] if auto_mode_idx + 1 < len(argv) else ""
    baseline = {
        "environment": ["defaults-env-baseline"],
        "allow": ["defaults-allow-baseline"],
        "hard_deny": ["defaults-hard-deny-baseline"],
        "soft_deny": ["defaults-soft-deny-baseline"],
    }
    if subcommand == "defaults":
        print(json.dumps(baseline))
        sys.exit(0)
    if subcommand == "config":
        config = dict(baseline)
        settings_path = None
        for i, tok in enumerate(argv):
            if tok == "--settings" and i + 1 < len(argv):
                settings_path = argv[i + 1]
        if settings_path and os.path.exists(settings_path):
            with open(settings_path, encoding="utf-8") as fh:
                settings = json.load(fh)
            auto_mode = settings.get("autoMode", {})

            def _merge(key):
                entries = auto_mode.get(key)
                if entries is None:
                    return
                merged = []
                for entry in entries:
                    if entry == "$defaults":
                        merged.extend(baseline[key])
                    else:
                        merged.append(entry)
                config[key] = merged

            _merge("environment")
            _merge("allow")
            _merge("hard_deny")
        print(json.dumps(config))
        sys.exit(0)

# 実際の claude 本体起動 invocation（readback 呼び出し以外）。この呼び出しが
# 受け取った env 全体を capture する。
captured_env_path = os.environ.get("CAPTURED_CLAUDE_ENV_FILE")
if captured_env_path:
    with open(captured_env_path, "w", encoding="utf-8") as fh:
        json.dump(dict(os.environ), fh)

argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w", encoding="utf-8") as fh:
        json.dump(argv, fh)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT_CODE", "0")))
"""


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_proxy_source(models: list[str]) -> str:
    return FAKE_PROXY_SOURCE_TEMPLATE.replace("__MODELS_JSON__", json.dumps(models))


def _proxy_config_dir(claude_gpt_home: Path) -> Path:
    return claude_gpt_home / "proxy-config"


def _run_check_only(
    tmp_path: Path,
    *,
    models: list[str],
    extra_claude_argv: list[str] | None = None,
    timeout: float = 40.0,
) -> subprocess.CompletedProcess[str]:
    claude_gpt_home = tmp_path / "claude-gpt-home"
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)

    fake_proxy = _write_executable(tmp_path / "fake-claude-code-proxy", _fake_proxy_source(models))
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env.pop("CLAUDE_GPT_CLAUDE_BIN", None)

    argv = ["--check-only"]
    if extra_claude_argv:
        argv += ["--", *extra_claude_argv]

    return subprocess.run(
        [str(LAUNCH_SH), *argv],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_full_launch(
    tmp_path: Path,
    *,
    models: list[str],
    claude_argv: list[str],
    timeout: float = 40.0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """launch.sh を（--check-only を付けず）通常起動モードで実行し、fake claude が
    capture した env ファイルの path を返す。"""
    claude_gpt_home = tmp_path / "claude-gpt-home"
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)

    fake_proxy = _write_executable(tmp_path / "fake-claude-code-proxy", _fake_proxy_source(models))
    fake_claude = _write_executable(tmp_path / "fake-claude", FAKE_CLAUDE_SOURCE)
    captured_env_path = tmp_path / "captured-claude-env.json"
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env["CLAUDE_GPT_CLAUDE_BIN"] = str(fake_claude)
    env["CAPTURED_CLAUDE_ENV_FILE"] = str(captured_env_path)

    result = subprocess.run(
        [str(LAUNCH_SH), "--", *claude_argv],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result, captured_env_path


def _read_captured_proxy_env(tmp_path: Path) -> dict[str, str]:
    claude_gpt_home = tmp_path / "claude-gpt-home"
    captured_path = _proxy_config_dir(claude_gpt_home) / "captured-env.json"
    assert captured_path.exists(), "fake proxy did not capture child env (file missing)"
    return json.loads(captured_path.read_text(encoding="utf-8"))


def _run_sh_function(function_name: str, *args: str) -> subprocess.CompletedProcess[str]:
    """lib.sh を source し、指定した関数を呼び出して stdout を返す（POSIX sh subshell）。"""
    quoted_args = " ".join(f'"{arg}"' for arg in args)
    script = f'. "{LIB_SH}"; {function_name} {quoted_args}'
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=20)


DEFAULT_MODELS = ["gpt-6-sol", "gpt-6-luna"]
DEFAULT_MODELS_WITH_ASTRA = ["gpt-6-sol", "gpt-6-luna", "gpt-6-astra"]


# --- AC1: 通常既定 allocation policy ----------------------------------------


def test_default_allocation_constants_are_gpt6_sol_and_luna():
    """GIVEN lib.sh
    WHEN main/sonnet/opus/haiku の定数定義行を読む
    THEN main/sonnet/opus は gpt-6-sol、haiku は gpt-6-luna（AC1）。
    base ID と `[1m]` client hint は分離されたまま保持される。
    """
    content = LIB_SH.read_text(encoding="utf-8")
    assert 'CLAUDE_GPT_MODEL_MAIN="gpt-6-sol[1m]"' in content
    assert 'CLAUDE_GPT_MODEL_OPUS="gpt-6-sol[1m]"' in content
    assert 'CLAUDE_GPT_MODEL_SONNET="gpt-6-sol[1m]"' in content
    assert 'CLAUDE_GPT_MODEL_HAIKU="gpt-6-luna[1m]"' in content
    # 旧 gpt-5.6 系の実行時定数は残らない（historical comment 以外に literal
    # 定数定義として残っていないことを確認する）。
    assert 'CLAUDE_GPT_MODEL_MAIN="gpt-5.6-terra[1m]"' not in content
    assert 'CLAUDE_GPT_MODEL_OPUS="gpt-5.6-sol[1m]"' not in content
    assert 'CLAUDE_GPT_MODEL_SONNET="gpt-5.6-terra[1m]"' not in content
    assert 'CLAUDE_GPT_MODEL_HAIKU="gpt-5.6-luna[1m]"' not in content


def test_default_check_only_launch_resolves_gpt6_aliases(tmp_path):
    """GIVEN fake proxy の /v1/models registry が gpt-6-sol/gpt-6-luna を含む
    WHEN launch.sh --check-only を実行する
    THEN model_alias_ok=true で status=ok になる（AC1 の実行時確認）。
    """
    result = _run_check_only(tmp_path, models=DEFAULT_MODELS)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["model_alias_ok"] is True


# --- AC2: Astra は default opus alias ではない -------------------------------


def test_opus_default_is_not_astra():
    """GIVEN lib.sh の CLAUDE_GPT_MODEL_OPUS
    WHEN 値を読む
    THEN gpt-6-astra ではない（AC2）。
    """
    content = LIB_SH.read_text(encoding="utf-8")
    start = content.index('CLAUDE_GPT_MODEL_OPUS="')
    line = content[start : content.index("\n", start)]
    assert "astra" not in line


def test_startup_preflight_loop_excludes_astra():
    """GIVEN launch.sh の model alias resolution preflight ブロック
    WHEN `claude_gpt_required_model_set()` からの derivation ループ本体を読む
    THEN `CLAUDE_GPT_MODEL_ASTRA` を含まない（AC2: 起動可否が Astra
    entitlement に依存しない）。

    Issue #2801 AC8 が、この preflight ループを固定 MAIN/OPUS/HAIKU 列挙から
    `claude_gpt_required_model_set()` 由来の derivation ループへ置き換えた
    （旧 hardcoded 列挙が存在しないことは
    `test_launch_sh_model_check_loop_uses_derived_required_set_not_hardcoded_subset`
    が明示的に固定している）。この recurrence test はその新しい構造を対象に
    更新し、意図（Astra が起動時 preflight から除外されていること）を維持
    する。単に「ASTRA という文字列がファイルのどこにも無い」という broad
    file-wide 否定にはしない -- Astra は on-demand escalation 用に別の場所
    （明示 --model 要求時の availability check）で正当に参照されているため、
    broad な否定は偽陰性・偽陽性双方のリスクがある。
    """
    content = LAUNCH_SH.read_text(encoding="utf-8")
    start = content.index("REQUIRED_MODELS_NL=$(claude_gpt_required_model_set)")
    loop_start = content.index("for m_base in $REQUIRED_MODELS_NL", start)
    loop_end = content.index("\ndone", loop_start)
    block = content[start : loop_end + len("\ndone")]

    # derivation が実際にこのブロックで呼ばれていること自体を確認する
    # （「ASTRA という文字列が無い」だけの緩い assertion にしない）。
    assert "claude_gpt_required_model_set" in block
    assert "for m_base in $REQUIRED_MODELS_NL" in block
    assert "CLAUDE_GPT_MODEL_ASTRA" not in block


def test_default_check_only_launch_succeeds_without_astra_in_registry(tmp_path):
    """GIVEN fake proxy の registry に gpt-6-astra が存在しない
    WHEN launch.sh --check-only を通常既定（明示 --model なし）で実行する
    THEN status=ok になる（AC2: 既定 opus=Sol の場合、通常起動は Astra
    availability に依存しない）。
    """
    result = _run_check_only(tmp_path, models=DEFAULT_MODELS)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


# --- AC3: Astra の明示的 on-demand escalation ---------------------------------


def test_explicit_astra_escalation_fails_closed_when_unavailable(tmp_path):
    """GIVEN fake proxy の /v1/models registry から gpt-6-astra が欠落している
    WHEN launch.sh --check-only -- --model gpt-6-astra[1m] を実行する
    THEN non-zero exit で unsupported/unavailable の reason を返し、他モデルへの
    silent fallback（exit 0 かつ別 model 応答）は発生しない（AC3）。
    """
    result = _run_check_only(
        tmp_path,
        models=DEFAULT_MODELS,  # astra を含まない
        extra_claude_argv=["--model", "gpt-6-astra[1m]"],
    )
    assert result.returncode != 0, result.stdout
    assert result.returncode == 11
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason"] == "explicit_model_escalation_unavailable"
    assert payload["requested_model"] == "gpt-6-astra"
    message = payload["message"].lower()
    assert "unsupported" in message or "unavailable" in message
    assert "fallback" not in message or "no fallback" in message


def test_explicit_astra_escalation_succeeds_when_available(tmp_path):
    """GIVEN fake proxy の /v1/models registry に gpt-6-astra が含まれる
    WHEN launch.sh --check-only -- --model gpt-6-astra[1m] を実行する
    THEN status=ok になる（explicit escalation check が false positive で
    always-fail していないことの回帰確認）。
    """
    result = _run_check_only(
        tmp_path,
        models=DEFAULT_MODELS_WITH_ASTRA,
        extra_claude_argv=["--model", "gpt-6-astra[1m]"],
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_explicit_astra_escalation_equals_form_also_checked(tmp_path):
    """GIVEN caller が `--model=gpt-6-astra[1m]`（単一トークン形式）で要求する
    WHEN registry に astra が存在しない
    THEN 同じく fail-closed になる（`--model <value>` 形式と同じ検証経路を通る）。
    """
    result = _run_check_only(
        tmp_path,
        models=DEFAULT_MODELS,
        extra_claude_argv=["--model=gpt-6-astra[1m]"],
    )
    assert result.returncode == 11, result.stdout
    payload = json.loads(result.stdout)
    assert payload["reason"] == "explicit_model_escalation_unavailable"


def test_unrelated_explicit_model_request_does_not_trigger_astra_path(tmp_path):
    """GIVEN caller が既定 registry に存在する model を明示要求する
    WHEN launch.sh --check-only -- --model gpt-6-sol[1m] を実行する
    THEN status=ok（explicit escalation check は「registry に存在しないモデル」
    だけを fail させ、既定既存モデルの再指定は妨げない）。
    """
    result = _run_check_only(
        tmp_path,
        models=DEFAULT_MODELS,
        extra_claude_argv=["--model", "gpt-6-sol[1m]"],
    )
    assert result.returncode == 0, result.stderr


# --- AC4: Auto mode classifier route（2つの独立した env 注入ポイント） -------


def test_ccp_auto_review_model_reaches_proxy_child_env_as_luna(tmp_path):
    """GIVEN 通常起動
    WHEN --check-only で fake proxy が capture した child env を読む
    THEN CCP_AUTO_REVIEW_MODEL は "gpt-6-luna"（proxy 子プロセス側の注入
    ポイント。AC4）。
    """
    result = _run_check_only(tmp_path, models=DEFAULT_MODELS)
    assert result.returncode == 0, result.stderr
    captured = _read_captured_proxy_env(tmp_path)
    assert captured.get("CCP_AUTO_REVIEW_MODEL") == "gpt-6-luna"


def test_claude_code_auto_mode_server_disabled_reaches_claude_client_env(tmp_path):
    """GIVEN 通常起動（--check-only を付けない、fake claude 使用）
    WHEN fake claude が capture した env を読む
    THEN CLAUDE_CODE_AUTO_MODE_SERVER=0 が届く（claude client 側の注入
    ポイント。proxy 側 env -i capture とは独立して確認する。AC4）。
    """
    result, captured_env_path = _run_full_launch(
        tmp_path, models=DEFAULT_MODELS, claude_argv=["-p", "hello"]
    )
    assert result.returncode == 0, result.stderr
    assert captured_env_path.exists(), "fake claude did not capture env"
    captured = json.loads(captured_env_path.read_text(encoding="utf-8"))
    assert captured.get("CLAUDE_CODE_AUTO_MODE_SERVER") == "0"


def test_auto_mode_server_env_injection_point_is_claude_client_only():
    """GIVEN launch.sh の実 proxy `env -i` invocation（`claude-code-proxy serve`
    へ渡す env の allowlist）
    WHEN その invocation block のテキストを読む
    THEN CLAUDE_CODE_AUTO_MODE_SERVER を含まない（AC4: proxy 子プロセス側の
    env -i に置くと upstream proxy に無視され claude client にも渡らないため、
    claude 起動直前の export ブロックにのみ置く設計を静的に確認する）。
    """
    content = LAUNCH_SH.read_text(encoding="utf-8")
    start = content.index('"$PROXY_BIN_TARGET" serve --port')
    block_start = content.rindex("env -i \\", 0, start)
    block_end = content.index("&\n", start)
    env_i_block = content[block_start:block_end]
    assert "CLAUDE_CODE_AUTO_MODE_SERVER" not in env_i_block
    assert "CCP_AUTO_REVIEW_MODEL=$CLAUDE_GPT_AUTO_REVIEW_MODEL_POLICY" in env_i_block

    # claude 起動直前の export ブロック（CLAUDE_CODE_ALWAYS_ENABLE_EFFORT と
    # 同じ箇所）には存在する。
    client_start = content.index("export CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1")
    client_end = content.index("CLAUDE_EXIT=0", client_start)
    client_block = content[client_start:client_end]
    assert "export CLAUDE_CODE_AUTO_MODE_SERVER=0" in client_block


# --- AC6: behavioral consumer（fake proxy/model catalog等）の GPT-6 誤分類防止 ---


def _load_latitude_helpers():
    import importlib.util

    helpers_path = SCRIPT_DIR / "tests" / "latitude_live_helpers.py"
    spec = importlib.util.spec_from_file_location(
        "claude_gpt_latitude_live_helpers_2772_model_policy", helpers_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_gpt6_models_not_misclassified_as_unknown_or_native():
    """GIVEN launcher-owned GPT-6 model set（sol/luna/astra）
    WHEN runtime classifier（latitude_live_helpers.classify_runtime）へ渡す
    THEN claude_gpt と判定される（unknown/native への誤分類禁止。AC6）。
    過去の gpt-5.6-* evidence も引き続き claude_gpt と判定できることを合わせて
    確認する（読み取り互換性。AC6）。
    """
    helpers = _load_latitude_helpers()
    assert helpers.classify_runtime(["gpt-6-sol"]) == "claude_gpt"
    assert helpers.classify_runtime(["gpt-6-luna"]) == "claude_gpt"
    assert helpers.classify_runtime(["gpt-6-astra"]) == "claude_gpt"
    assert helpers.classify_runtime(["gpt-5.6-sol"]) == "claude_gpt"
    assert helpers.classify_runtime(["gpt-5.6-terra"]) == "claude_gpt"
    assert helpers.classify_runtime(["gpt-5.6-luna"]) == "claude_gpt"
    assert helpers.classify_runtime(["claude-sonnet-5"]) == "claude_code_native"
    assert helpers.classify_runtime(["gpt-4-turbo"]) == "unknown"


# --- AC7: 選択 binary/version と account acceptance の分離 --------------------


def test_check_only_result_records_binary_identity_without_claiming_account_acceptance(
    tmp_path,
):
    """GIVEN --check-only の正常応答
    WHEN JSON payload を読む
    THEN 選択 proxy の absolute_path/version は記録されるが、account による
    request acceptance を主張するフィールド（例: "account_verified"）は
    存在しない（AC7: registry 表示 != account 利用可能）。
    """
    result = _run_check_only(tmp_path, models=DEFAULT_MODELS)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["proxy"]["absolute_path"]
    assert "0.0.0-test" in payload["proxy"]["version"]
    assert "account_verified" not in payload
    assert "account_accepted" not in payload


def test_unsupported_model_failure_message_is_explicit_not_silent():
    """GIVEN AC3 の失敗経路
    WHEN launch.sh のソースを読む
    THEN 失敗理由の message が "unsupported or unavailable" を明示し、別
    モデルや Native Claude への fallback を行わない旨をコード上に明記して
    いる（AC7: unsupported model は正確に失敗を示す）。
    """
    content = LAUNCH_SH.read_text(encoding="utf-8")
    assert "unsupported or unavailable" in content
    assert "no fallback to another model or Native Claude is performed" in content


# --- AC8: `[1m]` context hint と base model ID の分離 -------------------------


def test_strip_context_hint_separates_base_id_from_client_hint():
    """GIVEN claude_gpt_strip_context_hint()（lib.sh）
    WHEN "gpt-6-astra[1m]" を渡す
    THEN "gpt-6-astra"（suffix なし base ID）を返す（AC8）。
    """
    result = _run_sh_function("claude_gpt_strip_context_hint", "gpt-6-astra[1m]")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "gpt-6-astra"


def test_strip_context_hint_is_noop_for_bare_model_id():
    result = _run_sh_function("claude_gpt_strip_context_hint", "gpt-6-sol")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "gpt-6-sol"


def test_explicit_escalation_check_uses_strip_context_hint():
    """GIVEN launch.sh の明示的モデル escalation 検証ブロック
    WHEN ソースを読む
    THEN claude_gpt_strip_context_hint を呼び出して stripped base ID を
    registry 照合に使っている（AC8: assertion は stripped base ID に対して
    行う）。
    """
    content = LAUNCH_SH.read_text(encoding="utf-8")
    start = content.index("EXPLICIT_MODEL_REQUEST=")
    end = content.index("exit 11", start)
    block = content[start:end]
    assert "claude_gpt_strip_context_hint" in block


# --- PR #2800 OWNER REQUEST_CHANGES fix_delta（P2/P3, Issue #2189/#2191 と同系統の
#     argv semantics 回帰）------------------------------------------------------


def test_explicit_model_opusplan_is_not_rejected_as_catalog_miss(tmp_path):
    """GIVEN fake proxy の registry に文字列 "opusplan" が存在しない（Claude Code
    公式仕様上 opusplan は具体的な model ID ではなく特殊モードのため、catalog に
    含まれることはそもそも想定されない）
    WHEN launch.sh --check-only -- --model opusplan を実行する
    THEN explicit_model_escalation_unavailable として誤って fail-closed に
    ならず status=ok になる（PR #2800 P2 fix_delta）。
    """
    result = _run_check_only(
        tmp_path, models=DEFAULT_MODELS, extra_claude_argv=["--model", "opusplan"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_explicit_model_default_is_not_rejected_as_catalog_miss(tmp_path):
    """GIVEN fake proxy の registry に文字列 "default" が存在しない（`default` は
    model override の解除を意味する特殊値であり、具体的な model ID ではない）
    WHEN launch.sh --check-only -- --model default を実行する
    THEN explicit_model_escalation_unavailable として誤って fail-closed に
    ならず status=ok になる（PR #2800 P2 fix_delta）。
    """
    result = _run_check_only(
        tmp_path, models=DEFAULT_MODELS, extra_claude_argv=["--model", "default"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_explicit_model_opus_alias_still_resolves_against_real_catalog(tmp_path):
    """GIVEN fake proxy の registry に具体的な model ID "opus" が存在する
    WHEN launch.sh --check-only -- --model opus を実行する
    THEN opusplan/default とは異なり、通常どおり registry 照合を経て status=ok
    になる（特殊値 exemption が「あらゆる catalog miss を無条件で許可する」
    退行になっていないことの回帰確認。PR #2800 OWNER コメント「`--model opus`
    も上流で拒否される、という指摘はしない」の裏付け）。
    """
    result = _run_check_only(
        tmp_path, models=[*DEFAULT_MODELS, "opus"], extra_claude_argv=["--model", "opus"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_explicit_model_unknown_literal_still_fails_closed_after_special_value_exemption(
    tmp_path,
):
    """GIVEN registry に存在しない通常の model literal（opusplan/default の
    special-case exemption 対象ではない）
    WHEN launch.sh --check-only -- --model not-a-real-model を実行する
    THEN 引き続き explicit_model_escalation_unavailable で fail-closed になる
    （P2 fix_delta が catalog gate 自体を弱めていないことの回帰確認）。
    """
    result = _run_check_only(
        tmp_path, models=DEFAULT_MODELS, extra_claude_argv=["--model", "not-a-real-model"]
    )
    assert result.returncode == 11, result.stdout
    payload = json.loads(result.stdout)
    assert payload["reason"] == "explicit_model_escalation_unavailable"
    assert payload["requested_model"] == "not-a-real-model"


def test_downstream_double_dash_literal_is_not_misread_as_model_request(tmp_path):
    """GIVEN Claude 自身の downstream `--` より後に現れる `--model=...` 形の
    positional literal（`-p -- --model=not-a-model` という、PR #2800 OWNER
    コメントの P2 再現そのもの）
    WHEN launch.sh --check-only を実行する
    THEN launcher-level `--` の後にさらに現れる downstream `--` 以降は一切
    --model 判定の対象にせず、status=ok になる（誤って exit 11 にならない。
    Issue #2189 と同系統の argv semantics 回帰の修正確認）。
    """
    result = _run_check_only(
        tmp_path,
        models=DEFAULT_MODELS,
        extra_claude_argv=["-p", "--", "--model=not-a-model"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_value_position_after_append_system_prompt_is_not_misread_as_model_request(
    tmp_path,
):
    """GIVEN `--append-system-prompt` の値としてたまたま `--model=...` という
    文字列が渡される（PR #2800 OWNER コメントのもう一つの P2 再現）
    WHEN launch.sh --check-only を実行する
    THEN その値は `--append-system-prompt` の value position として消費され、
    独立した `--model` 要求として誤認しない（status=ok）。
    """
    result = _run_check_only(
        tmp_path,
        models=DEFAULT_MODELS,
        extra_claude_argv=["--append-system-prompt", "--model=not-a-model", "-p", "hello"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_append_system_prompt_equals_variant_still_passed_through_unchanged(tmp_path):
    """GIVEN `--append-system-prompt=--model=not-a-model`（単一トークン variant）
    WHEN launch.sh --check-only を実行する
    THEN 元から `--model=*` prefix match の対象ではなく、この変更後も引き続き
    誤検知しない（regression なしの確認）。
    """
    result = _run_check_only(
        tmp_path,
        models=DEFAULT_MODELS,
        extra_claude_argv=["--append-system-prompt=--model=not-a-model", "-p", "hello"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_explicit_model_two_token_form_still_captured_after_value_position_fix(tmp_path):
    """GIVEN 通常の `--model <value>`（二トークン形式）
    WHEN registry に存在しない値を指定する
    THEN value-position スキップ機構を追加した後も、`--model` 自身の値は
    引き続き正しく捕捉され fail-closed になる（regression なしの確認）。
    """
    result = _run_check_only(
        tmp_path, models=DEFAULT_MODELS, extra_claude_argv=["--model", "not-a-real-model"]
    )
    assert result.returncode == 11, result.stdout
    payload = json.loads(result.stdout)
    assert payload["reason"] == "explicit_model_escalation_unavailable"
    assert payload["requested_model"] == "not-a-real-model"


def test_explicit_model_failure_json_is_valid_with_quote_containing_value(tmp_path):
    """GIVEN requested model 文字列に二重引用符が含まれる
    （`scripts/claude-gpt/launch.sh -- --model 'bad"model'`。PR #2800 OWNER
    コメント P3 の再現）
    WHEN launch.sh --check-only を実行する
    THEN 失敗 JSON は `json.loads()` で正しくパースでき、`requested_model` に
    元の値がそのまま（エスケープ後に復元されて）含まれる（P3 fix_delta）。
    """
    result = _run_check_only(
        tmp_path, models=DEFAULT_MODELS, extra_claude_argv=["--model", 'bad"model']
    )
    assert result.returncode == 11, result.stdout
    payload = json.loads(result.stdout)  # json.loads 自体が壊れていないことの確認
    assert payload["reason"] == "explicit_model_escalation_unavailable"
    assert payload["requested_model"] == 'bad"model'


def test_explicit_model_failure_json_is_valid_with_backslash_and_newline_value(tmp_path):
    """GIVEN requested model 文字列にバックスラッシュ・改行が含まれる
    WHEN launch.sh --check-only を実行する
    THEN 失敗 JSON は引き続き `json.loads()` で正しくパースできる（P3
    fix_delta の境界ケース）。
    """
    result = _run_check_only(
        tmp_path,
        models=DEFAULT_MODELS,
        extra_claude_argv=["--model", 'bad\\model\nwith-newline'],
    )
    assert result.returncode == 11, result.stdout
    payload = json.loads(result.stdout)
    assert payload["reason"] == "explicit_model_escalation_unavailable"
    assert payload["requested_model"] == 'bad\\model\nwith-newline'

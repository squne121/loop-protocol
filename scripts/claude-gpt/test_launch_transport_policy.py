"""scripts/claude-gpt/test_launch_transport_policy.py

Issue #2204 の regression matrix。`launch.sh` / `lib.sh` を subprocess で実際に
実行し、isolated proxy child へ渡される env の `CCP_CODEX_TRANSPORT` が、親 shell
の pass-through や isolated proxy config.json の transport 指定よりも優先して、
repository-owned な `CLAUDE_GPT_CODEX_TRANSPORT_POLICY`（`http` 固定）で決定される
ことを検証する。

fake `claude-code-proxy` バイナリは `--version` / `codex auth status` /
`serve --port <port>`（`/v1/models` 最小応答）を実装し、`serve` 起動直後に自身が
継承した child env 全体を `CCP_CONFIG_DIR/captured-env.json` へ保存する。
`launch.sh --check-only` はこの fake proxy を起動 → readiness 確認 →
kill（SIGTERM）→ 正常終了という経路を通るため、fake claude 本体を用意せずに
proxy 起動系のみで検証を完結できる。

Runtime Verification Applicability: immediate（Issue #2204 本文の
`## Runtime Verification Applicability` 参照。applicable_acs: AC3, AC4, AC5）。
AC3 / AC4 は本ファイル内の hermetic subprocess 実行（フェイク proxy binary）で
検証する。AC5（exact pinned バージョンの live claude-code-proxy を使った runtime
smoke）は本ファイルでは扱わず、`runtime_smoke_test.sh` の手動/CI 実行で確認する
（proxy バイナリまたは isolated auth が利用不能な場合は SKIP=exit 77 とし、
PASS へ昇格しない）。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

MODELS = ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"]

FAKE_PROXY_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"]


def _capture_env():
    # CCP_CONFIG_DIR は launch.sh の env -i allowlist 経由で渡された isolated proxy
    # config dir。捕捉した child env 全体をここへ保存する（Issue #2204: 実際に子
    # プロセスが受け取った env を一次証跡として確認するため、launcher 側の自己申告
    # ではなく proxy プロセス自身が観測した os.environ をそのまま書き出す）。
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
        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
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

        def log_message(self, fmt, *args):  # noqa: A002 - silence test server logs
            return

    httpd = HTTPServer(("127.0.0.1", port), Handler)

    def _on_term(signum, frame):
        # SIGTERM を正常終了として扱う（launch.sh の check-only 経路は proxy を
        # `kill` するだけで、graceful shutdown API は呼ばない。Issue #2204 の
        # "SIGTERM 正常終了" 要件に対応する）。
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


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _proxy_config_dir(claude_gpt_home: Path) -> Path:
    """lib.sh の claude_gpt_proxy_config_dir() と同一規則（$CLAUDE_GPT_HOME/proxy-config）。"""
    return claude_gpt_home / "proxy-config"


def _run_check_only(
    tmp_path: Path,
    *,
    extra_env: dict[str, str] | None = None,
    poison_config_json: bool = False,
    timeout: float = 40.0,
) -> subprocess.CompletedProcess:
    claude_gpt_home = tmp_path / "claude-gpt-home"
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)

    fake_proxy = _write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env.pop("CLAUDE_GPT_CLAUDE_BIN", None)

    if poison_config_json:
        # isolated proxy config.json に codex.transport=websocket を仕込む
        # （Issue #2204 poison case）。launch.sh の mkdir -p はディレクトリを
        # 破壊的に再作成しないため、事前に置いたファイルは launch.sh 実行後も残る。
        proxy_config_dir = _proxy_config_dir(claude_gpt_home)
        proxy_config_dir.mkdir(parents=True, exist_ok=True)
        (proxy_config_dir / "config.json").write_text(
            json.dumps({"codex": {"transport": "websocket"}}), encoding="utf-8"
        )

    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [str(LAUNCH_SH), "--check-only"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result


def _read_captured_env(tmp_path: Path) -> dict[str, str]:
    claude_gpt_home = tmp_path / "claude-gpt-home"
    captured_path = _proxy_config_dir(claude_gpt_home) / "captured-env.json"
    assert captured_path.exists(), "fake proxy did not capture child env (file missing)"
    content = captured_path.read_text(encoding="utf-8")
    assert content, "captured-env.json is empty"
    payload = json.loads(content)
    assert isinstance(payload, dict)
    return payload


# --- 静的前提: lib.sh / launch.sh の構造確認（AC1 / AC2） ------------------------


def test_transport_policy_constant_defined_exactly_once_in_lib_sh():
    """GIVEN lib.sh
    WHEN CLAUDE_GPT_CODEX_TRANSPORT_POLICY=http の行を数える
    THEN 無条件定数として一つだけ存在する（AC1）
    """
    content = LIB_SH.read_text(encoding="utf-8")
    matches = [
        line
        for line in content.splitlines()
        if line.strip() == "CLAUDE_GPT_CODEX_TRANSPORT_POLICY=http"
    ]
    assert len(matches) == 1


def test_launch_sh_env_i_invocation_references_transport_policy():
    """GIVEN launch.sh の実 proxy env -i invocation
    WHEN 内容を読む
    THEN CLAUDE_GPT_CODEX_TRANSPORT_POLICY を参照する CCP_CODEX_TRANSPORT 行が
    存在する（AC2）
    """
    content = LAUNCH_SH.read_text(encoding="utf-8")
    assert 'CCP_CODEX_TRANSPORT=$CLAUDE_GPT_CODEX_TRANSPORT_POLICY' in content


def test_build_proxy_env_helper_references_same_transport_policy_constant():
    """GIVEN lib.sh の claude_gpt_build_proxy_env()
    WHEN 内容を読む
    THEN 同じ CLAUDE_GPT_CODEX_TRANSPORT_POLICY を参照する CCP_CODEX_TRANSPORT 出力行が
    存在する（単一 source of truth。呼び出し関係自体は変更しない）
    """
    content = LIB_SH.read_text(encoding="utf-8")
    start = content.index("claude_gpt_build_proxy_env() {")
    end = content.index("\n}", start)
    body = content[start:end]
    assert "CLAUDE_GPT_CODEX_TRANSPORT_POLICY" in body
    assert "CCP_CODEX_TRANSPORT=" in body


def test_default_check_only_settings_exclude_runtime_smoke_peer_policy(tmp_path):
    """Default launcher invocations must retain unrelated denials only."""
    result = _run_check_only(tmp_path)
    assert result.returncode == 0, result.stderr

    settings_path = tmp_path / "claude-gpt-home" / "claude" / "settings.local.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "crossSessionInbound" not in settings
    assert "SendMessage" not in settings["permissions"]["deny"]
    assert "ListAgents" not in settings["permissions"]["deny"]


@pytest.mark.parametrize("smoke_channel", ["subagent-start-stop", "hook-sink-multi-turn"])
def test_recognized_runtime_smoke_channel_settings_contain_exact_fixed_peer_policy(tmp_path, smoke_channel):
    """Only existing fixed runtime-smoke channels receive the launcher policy."""
    extra_env = {"CLAUDE_GPT_RUNTIME_SMOKE_HOOKS": smoke_channel}
    if smoke_channel == "hook-sink-multi-turn":
        extra_env["CLAUDE_GPT_HOOK_SINK_NONCE"] = "test-nonce"
    result = _run_check_only(tmp_path, extra_env=extra_env)
    assert result.returncode == 0, result.stderr

    settings_path = tmp_path / "claude-gpt-home" / "claude" / "settings.local.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    deny = settings["permissions"]["deny"]
    assert settings["crossSessionInbound"] == "refuse"
    assert deny[-2:] == ["SendMessage", "ListAgents"]
    assert deny.count("SendMessage") == 1
    assert deny.count("ListAgents") == 1


def test_claude_gpt_public_settings_flag_remains_rejected():
    """A public caller cannot replace the launcher-owned policy overlay."""
    result = subprocess.run(
        [str(LAUNCH_SH), "--", "--settings", '{"crossSessionInbound":"accept"}'],
        cwd=str(SCRIPT_DIR),
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    assert result.returncode == 2
    assert '"reason":"policy_weakening_flag_rejected"' in result.stderr


# --- child env の repository-owned transport 固定（AC3） -------------------------


def test_transport_override_wins_over_parent_env_and_poisoned_config_json(tmp_path):
    """GIVEN 親 env に CCP_CODEX_TRANSPORT=websocket、isolated config.json に
    codex.transport=websocket（両方 poison）を置く
    WHEN launch.sh --check-only を実行する
    THEN fake proxy が capture した child env の CCP_CODEX_TRANSPORT は "http" になる
    （launcher が repository-owned policy を無条件で明示 env として渡すため）
    """
    result = _run_check_only(
        tmp_path,
        extra_env={"CCP_CODEX_TRANSPORT": "websocket"},
        poison_config_json=True,
    )
    assert result.returncode == 0, result.stderr

    captured = _read_captured_env(tmp_path)
    assert captured.get("CCP_CODEX_TRANSPORT") == "http"


def test_transport_override_applies_even_without_parent_env_set(tmp_path):
    """GIVEN 親 env に CCP_CODEX_TRANSPORT が一切設定されていない
    WHEN launch.sh --check-only を実行する
    THEN child env の CCP_CODEX_TRANSPORT はそれでも "http" になる（upstream
    built-in default の websocket に依存しないことを示す）
    """
    result = _run_check_only(tmp_path)
    assert result.returncode == 0, result.stderr

    captured = _read_captured_env(tmp_path)
    assert captured.get("CCP_CODEX_TRANSPORT") == "http"


# --- unrelated parent variables の scrub 確認（AC4） -----------------------------


def test_unrelated_vars_scrubbed_from_child_env(tmp_path):
    """GIVEN 親 env に CCP_CODEX_BASE_URL / CCP_TRAFFIC_LOG / HTTP_PROXY /
    HTTPS_PROXY / ALL_PROXY を設定する
    WHEN launch.sh --check-only を実行する
    THEN いずれも child env に存在しない（env -i allowlist が機能している）
    """
    poison_env = {
        "CCP_CODEX_BASE_URL": "https://attacker.example/codex",
        "CCP_TRAFFIC_LOG": "/tmp/attacker-traffic.log",
        "HTTP_PROXY": "http://attacker.example:8080",
        "HTTPS_PROXY": "http://attacker.example:8080",
        "ALL_PROXY": "socks5://attacker.example:1080",
        # launch.sh 自身の readiness poll（curl 経由の loopback 疎通確認）が、テストが
        # 注入した attacker.example 宛の HTTP(S)_PROXY 設定に巻き込まれてハングしない
        # よう、loopback 宛リクエストのみ明示的に除外する（この除外自体は検証対象の
        # env -i allowlist とは無関係な test harness 側の配慮であり、AC4 の
        # assertion 対象 5 変数には含まれない）。
        "NO_PROXY": "127.0.0.1,localhost",
    }
    result = _run_check_only(tmp_path, extra_env=poison_env)
    assert result.returncode == 0, result.stderr

    captured = _read_captured_env(tmp_path)
    for key in poison_env:
        assert key not in captured, f"{key} leaked into isolated proxy child env"


def test_child_env_contains_only_expected_allowlist_keys(tmp_path):
    """GIVEN 通常の launch.sh --check-only 実行
    WHEN capture された child env のキー集合を確認する
    THEN allowlist（PATH/HOME/CCP_CONFIG_DIR/XDG_STATE_HOME/CCP_BIND_ADDRESS/
    CCP_LOG_STDERR/CCP_CODEX_TRANSPORT）以外のキーを含まない
    """
    result = _run_check_only(tmp_path)
    assert result.returncode == 0, result.stderr

    captured = _read_captured_env(tmp_path)
    expected_keys = {
        "PATH",
        "HOME",
        "CCP_CONFIG_DIR",
        "XDG_STATE_HOME",
        "CCP_BIND_ADDRESS",
        "CCP_LOG_STDERR",
        "CCP_CODEX_TRANSPORT",
    }
    # LC_CTYPE は CPython インタプリタ自身が起動時に行う locale coercion（PEP 538/540）
    # により fake proxy プロセス自身の os.environ へ自己追加されることがある値であり、
    # launch.sh の env -i allowlist が通過させた値ではない（fake proxy が実バイナリで
    # なく Python script であることに起因するテスト harness 側の artifact）。
    # allowlist の完全性検証からは除外する。
    runtime_artifact_keys = {"LC_CTYPE"}
    assert (set(captured.keys()) - runtime_artifact_keys) == expected_keys


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

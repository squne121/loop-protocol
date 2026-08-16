"""scripts/claude-gpt/test_launch_strict_mcp_config_normalization.py

Issue #2189 の regression matrix。`launch.sh` を subprocess で実際に実行し、
Herdr が常時付与する exact `--strict-mcp-config` トークンが安全に normalize/drop
され、他の forbidden flag の拒否ロジックには一切影響しないことを確認する。

`launch.sh` の pre-filter/forbidden-flag チェックは proxy/claude 起動前に完結する
ため、拒否系ケースはフェイクバイナリなしで高速に検証できる。受理系ケース
（最終 invocation の確認を含む）はフェイクの `claude-code-proxy` /
`claude` バイナリを使って launcher を最後まで走らせ、実際に claude へ渡された
argv を確認する。

Runtime Verification Applicability: not_applicable（Issue #2189 本文参照）。
本テストは静的な subprocess 実行による検証であり、実 ChatGPT subscription や
実 claude-code-proxy への依存を持たない（フェイクバイナリで完結する）。
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

FAKE_PROXY_SOURCE = r"""#!/usr/bin/env python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"]


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
        return _serve(port)
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""

FAKE_CLAUDE_SOURCE = r"""#!/usr/bin/env python3
import os
import sys

argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sys.argv[1:]))
sys.exit(0)
"""


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_launch(
    tmp_path: Path,
    claude_argv: list[str],
    *,
    use_fakes: bool = False,
    extra_env: dict[str, str] | None = None,
    timeout: float = 40.0,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(tmp_path / "claude-gpt-home")

    if use_fakes:
        fake_proxy = _write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
        fake_claude = _write_executable(tmp_path / "fake-claude", FAKE_CLAUDE_SOURCE)
        env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
        env["CLAUDE_GPT_CLAUDE_BIN"] = str(fake_claude)
        argv_file = tmp_path / "claude-argv.txt"
        env["FAKE_CLAUDE_ARGV_FILE"] = str(argv_file)
    else:
        # 拒否系ケースは forbidden-flag チェックの時点で exit するため、
        # 未解決の proxy/claude バイナリに依存させない（fail-fast にする）。
        env.pop("CLAUDE_GPT_PROXY_BIN", None)
        env.pop("CLAUDE_GPT_CLAUDE_BIN", None)

    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [str(LAUNCH_SH), "--", *claude_argv],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result


def _read_claude_argv(tmp_path: Path) -> list[str]:
    argv_file = tmp_path / "claude-argv.txt"
    assert argv_file.exists(), "fake claude was not invoked (argv file missing)"
    content = argv_file.read_text(encoding="utf-8")
    return content.split("\n") if content else []


def _blocked_reason(result: subprocess.CompletedProcess) -> str:
    for line in (result.stdout + result.stderr).splitlines():
        line = line.strip()
        if line.startswith("{") and '"status":"blocked"' in line:
            payload = json.loads(line)
            return payload.get("reason", "")
    return ""


def _blocked_flag(result: subprocess.CompletedProcess) -> str:
    for line in (result.stdout + result.stderr).splitlines():
        line = line.strip()
        if line.startswith("{") and '"status":"blocked"' in line:
            payload = json.loads(line)
            return payload.get("flag", "")
    return ""


# --- 静的前提: lib.sh / launch.sh の構造確認 ---------------------------------


def test_strict_mcp_config_removed_from_forbidden_extra_flags_list():
    """GIVEN lib.sh の CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS
    WHEN 内容を読む
    THEN --strict-mcp-config は含まれず、他の既存 forbidden flag は残っている
    """
    content = LIB_SH.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS="):
            assert "--strict-mcp-config " not in line
            assert not line.rstrip().endswith("--strict-mcp-config\"")
            for still_forbidden in (
                "--settings",
                "--mcp-config",
                "--dangerously-skip-permissions",
                "--allow-dangerously-skip-permissions",
            ):
                assert still_forbidden in line
            return
    pytest.fail("CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS not found in lib.sh")


# --- 単独 / 重複 exact 一致（AC1） -------------------------------------------


def test_exact_single_strict_mcp_config_token_is_stripped_and_launch_succeeds(tmp_path):
    """GIVEN 呼び出し元が exact --strict-mcp-config を1個渡す
    WHEN launch.sh を実行する
    THEN forbidden-flag 拒否は発生せず、最終 claude invocation には launcher
    自身の canonical --strict-mcp-config --mcp-config <path> --settings <path>
    が1個ずつ含まれる
    """
    result = _run_launch(tmp_path, ["--strict-mcp-config"], use_fakes=True)
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    assert argv.count("--strict-mcp-config") == 1
    assert "--mcp-config" in argv
    assert "--settings" in argv


def test_exact_duplicate_strict_mcp_config_tokens_are_all_stripped(tmp_path):
    """GIVEN 呼び出し元が exact --strict-mcp-config を2個（重複）渡す
    WHEN launch.sh を実行する
    THEN 両方とも pre-filter で除去され、最終 invocation には launcher 自身の
    canonical 分のみ1個残る
    """
    result = _run_launch(
        tmp_path, ["--strict-mcp-config", "--strict-mcp-config"], use_fakes=True
    )
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    assert argv.count("--strict-mcp-config") == 1


def test_launch_without_caller_strict_mcp_config_still_has_canonical_flags(tmp_path):
    """GIVEN 呼び出し元が --strict-mcp-config を渡さない
    WHEN launch.sh を実行する
    THEN launcher 自身の canonical --strict-mcp-config --mcp-config --settings は
    引き続き最終 invocation に含まれる（AC6 末尾ケース）
    """
    result = _run_launch(tmp_path, ["-p", "hello"], use_fakes=True)
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    assert argv.count("--strict-mcp-config") == 1
    assert "--mcp-config" in argv
    assert "--settings" in argv


# --- forbidden flag 同時指定（AC2 / AC4） ------------------------------------


def test_mcp_config_cooccurrence_is_rejected(tmp_path):
    """GIVEN --strict-mcp-config と --mcp-config が同時に渡される
    WHEN launch.sh を実行する
    THEN --mcp-config 側の forbidden 判定が優先され exit 2 で拒否される
    """
    result = _run_launch(
        tmp_path,
        ["--strict-mcp-config", "--mcp-config", "/tmp/attacker.json"],
        use_fakes=False,
    )
    assert result.returncode == 2
    assert _blocked_reason(result) == "policy_weakening_flag_rejected"
    assert _blocked_flag(result) == "--mcp-config"


def test_settings_cooccurrence_is_rejected(tmp_path):
    """GIVEN --strict-mcp-config と --settings が同時に渡される
    WHEN launch.sh を実行する
    THEN --settings 側の forbidden 判定が優先され exit 2 で拒否される
    """
    result = _run_launch(
        tmp_path,
        ["--strict-mcp-config", "--settings", "/tmp/attacker.json"],
        use_fakes=False,
    )
    assert result.returncode == 2
    assert _blocked_reason(result) == "policy_weakening_flag_rejected"
    assert _blocked_flag(result) == "--settings"


def test_mcp_config_equals_variant_is_rejected(tmp_path):
    """GIVEN --mcp-config=... の単一トークン variant
    WHEN launch.sh を実行する
    THEN forbidden 判定で exit 2
    """
    result = _run_launch(tmp_path, ["--mcp-config=/tmp/attacker.json"], use_fakes=False)
    assert result.returncode == 2
    assert _blocked_reason(result) == "policy_weakening_flag_rejected"


def test_settings_equals_variant_is_rejected(tmp_path):
    """GIVEN --settings=... の単一トークン variant
    WHEN launch.sh を実行する
    THEN forbidden 判定で exit 2
    """
    result = _run_launch(tmp_path, ["--settings=/tmp/attacker.json"], use_fakes=False)
    assert result.returncode == 2
    assert _blocked_reason(result) == "policy_weakening_flag_rejected"


@pytest.mark.parametrize(
    "argv",
    [
        ["--dangerously-skip-permissions"],
        ["--allow-dangerously-skip-permissions"],
        ["--permission-mode", "bypassPermissions"],
        ["--permission-mode=bypassPermissions"],
    ],
)
def test_bypass_permission_variants_are_rejected(tmp_path, argv):
    """GIVEN 各種 bypass-permission 系 forbidden flag
    WHEN launch.sh を実行する
    THEN 本変更後も exit 2 で確実に拒否される（regression なし）
    """
    result = _run_launch(tmp_path, argv, use_fakes=False)
    assert result.returncode == 2
    assert _blocked_reason(result) == "policy_weakening_flag_rejected"


# --- --strict-mcp-config=... パラメータ付き variant（AC3） -------------------


def test_strict_mcp_config_equals_variant_is_rejected(tmp_path):
    """GIVEN --strict-mcp-config=... のパラメータ付き variant
    WHEN launch.sh を実行する
    THEN 値を取らない boolean flag の不正な用法として exit 2 で拒否される
    """
    result = _run_launch(tmp_path, ["--strict-mcp-config=evil"], use_fakes=False)
    assert result.returncode == 2
    assert _blocked_reason(result) == "policy_weakening_flag_rejected"
    assert _blocked_flag(result) == "--strict-mcp-config=..."


# --- 大文字小文字・部分文字列の誤認防止（AC5） --------------------------------


def test_case_sensitivity_variant_is_not_treated_as_exact_match(tmp_path):
    """GIVEN 大文字小文字違いの --Strict-Mcp-Config
    WHEN launch.sh を実行する
    THEN exact-match の削除対象と誤認せず、forbidden flag としても扱わず
    そのまま claude 側へ渡す（unknown flag 判定は claude 側の責務）
    """
    result = _run_launch(tmp_path, ["--Strict-Mcp-Config"], use_fakes=True)
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    assert "--Strict-Mcp-Config" in argv
    assert argv.count("--strict-mcp-config") == 1  # launcher 自身の canonical 分のみ


def test_substring_variant_is_not_treated_as_exact_match(tmp_path):
    """GIVEN --strict-mcp-config を含む別トークン --strict-mcp-config-evil
    WHEN launch.sh を実行する
    THEN exact-match の削除対象と誤認せず、そのまま claude 側へ渡す
    """
    result = _run_launch(tmp_path, ["--strict-mcp-config-evil"], use_fakes=True)
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    assert "--strict-mcp-config-evil" in argv
    assert argv.count("--strict-mcp-config") == 1  # launcher 自身の canonical 分のみ


# --- 無関係な安全な既存引数の順序保持 -----------------------------------------


def test_unrelated_safe_args_order_is_preserved(tmp_path):
    """GIVEN --strict-mcp-config と無関係な安全な引数が混在する
    WHEN launch.sh を実行する
    THEN 安全な引数群の相対順序は変更されない
    """
    result = _run_launch(
        tmp_path,
        ["--strict-mcp-config", "-p", "hello world", "--output-format", "json"],
        use_fakes=True,
    )
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    safe_indices = [argv.index(tok) for tok in ("-p", "hello world", "--output-format", "json")]
    assert safe_indices == sorted(safe_indices)


# --- Herdr が実際に生成する argv 形状を模した fixture ------------------------


def test_herdr_style_argv_fixture_passes_through_and_launches(tmp_path):
    """GIVEN Herdr が claude-kind agent 起動時に付与する典型的な argv 形状
    （非対話 -p 実行 + 常時付与される exact --strict-mcp-config）
    WHEN launch.sh を実行する
    THEN forbidden-flag チェックを通過し、launcher の canonical strict-mcp
    flag が最終 invocation に含まれる
    """
    result = _run_launch(
        tmp_path,
        [
            "-p",
            "run the assigned task",
            "--output-format",
            "stream-json",
            "--strict-mcp-config",
        ],
        use_fakes=True,
    )
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    assert argv.count("--strict-mcp-config") == 1
    assert "--mcp-config" in argv
    assert "--settings" in argv
    assert "run the assigned task" in argv


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

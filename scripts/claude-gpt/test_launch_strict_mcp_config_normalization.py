"""scripts/claude-gpt/test_launch_strict_mcp_config_normalization.py

Issue #2189 の regression matrix。`launch.sh` を subprocess で実際に実行し、
caller argv 中の launcher-level `--` 直後から始まる連続した（leading run の）
exact `--strict-mcp-config` トークンのみが安全に normalize/drop され、
value position（`-p` の値等）・downstream `--` より後の positional literal・
任意文字列オプションの値としての同一トークンは削除されないこと、また他の
forbidden flag の拒否ロジックに一切影響しないことを確認する。

`launch.sh` の pre-filter/forbidden-flag チェックは proxy/claude 起動前に完結する
ため、拒否系ケースはフェイクバイナリなしで高速に検証できる。受理系ケース
（最終 invocation の確認を含む）はフェイクの `claude-code-proxy` /
`claude` バイナリを使って launcher を最後まで走らせ、実際に claude へ渡された
argv を確認する。

Runtime Verification Applicability: immediate（Issue #2189 Provenance Correction
反映。applicable_acs: AC8, AC12, AC13, AC14）。AC8 / AC12 は本ファイル内の
subprocess 実行（フェイク binary を含む）で検証する。AC13 は実 Claude Code CLI
に対する bounded parser probe（`TestRealCliParserProbe`）で検証し、利用不能な
場合は pytest skip とする（PASS へ昇格しない）。AC14（#2176 current head との
stacked integration 実行）は本ファイルの pytest では扱わず、Issue #2189
Verification Commands 記載の手動コマンド実行で確認する（SKIP 時は SKIP のまま
記録する）。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

REAL_CLAUDE_BIN = shutil.which("claude")

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

# --- 素直に受理する fake claude（JSON array argv recorder。Issue #2189 AC9） ---
# 未知フラグ検査は行わず、受理系ケースの argv 確認専用。exit code / SIGTERM 伝播の
# 確認用に FAKE_CLAUDE_EXIT_CODE / FAKE_CLAUDE_TRAP_TERM_EXIT_CODE を受け付ける。
FAKE_CLAUDE_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import signal
import sys
import time

argv = sys.argv[1:]

# --- Issue #2203 (P0-3, PR #2214 OWNER adversarial review fix-delta): 単に
#     「最後の argv を記録する」recorder だと、launch.sh が通常起動へ配線した
#     `preflight.sh --auto-mode-check`（`--version` / `auto-mode defaults` /
#     `auto-mode config`）の複数回 invocation と、実際の claude 本体 invocation
#     を区別できない。全 invocation を JSONL へ追記し、呼び出し元テストが
#     preflight invocation と本 invocation を別々に assert できるようにする。 ---
argv_log = os.environ.get("FAKE_CLAUDE_ARGV_LOG")
if argv_log:
    with open(argv_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")

# --- readback subcommand（`--version` / `auto-mode defaults` / `auto-mode
#     config`）に応答する。既定では launcher の narrow autoMode 契約
#     （hard_deny/soft_deny の $defaults 保持 + narrow 追加 + classifyAllShell）を
#     満たす effective config を返し、readback が PASS するようにする
#     （FAKE_CLAUDE_AUTO_MODE_READBACK_FAIL=1 で意図的に readback を失敗させる
#     ことも可能。fail-closed 経路のテスト用）。 ---
if argv and argv[0] == "--version":
    print(os.environ.get("FAKE_CLAUDE_VERSION") or "2.1.211 (Claude Code)")
    sys.exit(0)

if "auto-mode" in argv:
    auto_mode_idx = argv.index("auto-mode")
    subcommand = argv[auto_mode_idx + 1] if auto_mode_idx + 1 < len(argv) else ""
    force_fail = os.environ.get("FAKE_CLAUDE_AUTO_MODE_READBACK_FAIL") == "1"
    baseline = {
        "environment": ["defaults-env-baseline"],
        "allow": ["defaults-allow-baseline"],
        "hard_deny": ["defaults-hard-deny-baseline"],
        "soft_deny": ["defaults-soft-deny-baseline"],
        "classifyAllShell": False,
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
        if settings_path and os.path.exists(settings_path) and not force_fail:
            with open(settings_path, encoding="utf-8") as fh:
                settings = json.load(fh)
            auto_mode = settings.get("autoMode", {})

            def _merge(key: str) -> None:
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
            if auto_mode.get("classifyAllShell"):
                config["classifyAllShell"] = True
        print(json.dumps(config))
        sys.exit(0)

argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w", encoding="utf-8") as fh:
        json.dump(argv, fh)

trap_term_exit_code = os.environ.get("FAKE_CLAUDE_TRAP_TERM_EXIT_CODE")
if trap_term_exit_code:
    def _on_term(signum, frame):
        sys.exit(int(trap_term_exit_code))

    signal.signal(signal.SIGTERM, _on_term)
    # launcher からの SIGTERM forwarding を待つ（bounded）。
    for _ in range(300):
        time.sleep(0.1)
    sys.exit(0)

sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT_CODE", "0")))
"""

# --- 未知フラグを非ゼロ終了する strict fake claude（Issue #2189 AC5 / AC12）。
#     `--`-prefixed トークンが既知の許可集合に含まれない場合、値を取らない
#     boolean/known flag と誤認せず即座に非ゼロ終了する。
FAKE_CLAUDE_STRICT_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import sys

KNOWN_FLAGS = {
    "--strict-mcp-config",
    "--mcp-config",
    "--settings",
    "-p",
    "--output-format",
    "--append-system-prompt",
    "--permission-mode",
}
VALUE_FLAGS = {
    "--mcp-config",
    "--settings",
    "-p",
    "--output-format",
    "--append-system-prompt",
    "--permission-mode",
}

argv = sys.argv[1:]

argv_log = os.environ.get("FAKE_CLAUDE_ARGV_LOG")
if argv_log:
    with open(argv_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")

# --- readback subcommand（Issue #2203 P0-3）は unknown-flag 判定より前に応答する
#     （launch.sh が本 invocation の前に必ず --auto-mode-check を実行するため、
#     strict fake もここで readback PASS を返さないと本 invocation へ到達できず、
#     unknown-flag rejection の検証ができなくなる）。 ---
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
        "classifyAllShell": False,
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

            def _merge(key: str) -> None:
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
            if auto_mode.get("classifyAllShell"):
                config["classifyAllShell"] = True
        print(json.dumps(config))
        sys.exit(0)

argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w", encoding="utf-8") as fh:
        json.dump(argv, fh)

unknown_exit_code = int(os.environ.get("FAKE_CLAUDE_STRICT_UNKNOWN_EXIT_CODE", "1"))

i = 0
while i < len(argv):
    tok = argv[i]
    if tok.startswith("-"):
        if tok not in KNOWN_FLAGS:
            sys.exit(unknown_exit_code)
        if tok in VALUE_FLAGS and i + 1 < len(argv):
            i += 1
    i += 1

sys.exit(0)
"""


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _canonical_prefix(tmp_path: Path) -> list[str]:
    """launcher が最終 invocation の先頭に必ず付与する canonical flag 7 トークン分
    （`--strict-mcp-config --mcp-config <path> --settings <path> --permission-mode auto`）
    を、テストが使う CLAUDE_GPT_HOME から独立に計算する（lib.sh の
    claude_gpt_mcp_config_path / claude_gpt_session_settings_path と同一規則）。

    Issue #2203（2026-08-16 OWNER adversarial review 反映, Scope Delta）で
    launcher が exactly one の `--permission-mode auto` を注入する契約が加わり、
    canonical prefix は 5 トークンから 7 トークンへ拡張された。
    """
    claude_config_dir = tmp_path / "claude-gpt-home" / "claude"
    mcp_config_path = claude_config_dir / "mcp-empty.json"
    settings_path = claude_config_dir / "settings.local.json"
    return [
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config_path),
        "--settings",
        str(settings_path),
        "--permission-mode",
        "auto",
    ]


def _extract_agents_flag(argv: list[str]) -> tuple[dict, list[str]]:
    """canonical prefix 直後の argv から launcher-owned `--agents <json>` flag を
    検出し、`(parsed_json, remaining_argv)` を返す。

    Issue #2186（PR #2244 P0/P1 adversarial review fix-delta）で launcher が
    session-local `spark-codex` custom SubAgent を登録するため、canonical prefix
    へ新規 launcher-owned flag `--agents <json>` が追加された。本ヘルパーは
    「launcher-owned flag は exactly once・正しい flag/value pairing で存在する」
    という構造的 invariant だけを検証し、それ以降の caller argv（`remaining`）は
    一切加工せず返す。これにより「launcher-owned flag の構造的検証」と
    「caller argv が byte-for-byte で一切改変されていないことの検証」を分離する。

    `--agents` が存在しない場合は `({}, argv)` を返す（Spark 機能が無効な旧経路
    との後方互換）。`--agents` が複数回存在する場合は `AssertionError`。
    """
    count = argv.count("--agents")
    assert count <= 1, f"--agents must appear at most once, found {count} times: {argv!r}"
    if count == 0:
        return {}, argv
    idx = argv.index("--agents")
    assert idx + 1 < len(argv), f"--agents flag missing its value: {argv!r}"
    parsed = json.loads(argv[idx + 1])
    remaining = argv[:idx] + argv[idx + 2 :]
    return parsed, remaining


def _assert_agents_flag_wired(argv: list[str]) -> list[str]:
    """`argv`（canonical prefix 除去済み）から `--agents` flag を抽出し、
    spark-codex の `model` が `gpt-5.3-codex-spark` と一致することを検証した上で、
    残り（caller argv 相当）を返す。"""
    agents_flag, remaining = _extract_agents_flag(argv)
    assert agents_flag.get("spark-codex", {}).get("model") == "gpt-5.3-codex-spark", (
        f"--agents flag missing or malformed spark-codex model id: {agents_flag!r}"
    )
    return remaining


def _build_env(
    tmp_path: Path,
    *,
    use_fakes: bool,
    strict_fake: bool = False,
    extra_env: dict[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(tmp_path / "claude-gpt-home")
    argv_file = tmp_path / "claude-argv.json"

    if use_fakes:
        fake_proxy = _write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
        fake_claude_source = FAKE_CLAUDE_STRICT_SOURCE if strict_fake else FAKE_CLAUDE_SOURCE
        fake_claude = _write_executable(tmp_path / "fake-claude", fake_claude_source)
        env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
        env["CLAUDE_GPT_CLAUDE_BIN"] = str(fake_claude)
        env["FAKE_CLAUDE_ARGV_FILE"] = str(argv_file)
        env["FAKE_CLAUDE_ARGV_LOG"] = str(tmp_path / "claude-argv-log.jsonl")
    else:
        # 拒否系ケースは forbidden-flag / unknown-launcher-option チェックの時点で
        # exit するため、未解決の proxy/claude バイナリに依存させない（fail-fast）。
        env.pop("CLAUDE_GPT_PROXY_BIN", None)
        env.pop("CLAUDE_GPT_CLAUDE_BIN", None)

    if extra_env:
        env.update(extra_env)

    return env, argv_file


def _run_launch(
    tmp_path: Path,
    claude_argv: list[str],
    *,
    use_fakes: bool = False,
    strict_fake: bool = False,
    extra_env: dict[str, str] | None = None,
    timeout: float = 40.0,
) -> subprocess.CompletedProcess:
    env, _argv_file = _build_env(
        tmp_path, use_fakes=use_fakes, strict_fake=strict_fake, extra_env=extra_env
    )
    result = subprocess.run(
        [str(LAUNCH_SH), "--", *claude_argv],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result


def _run_launch_raw(
    tmp_path: Path,
    raw_argv: list[str],
    *,
    use_fakes: bool = False,
    extra_env: dict[str, str] | None = None,
    timeout: float = 40.0,
) -> subprocess.CompletedProcess:
    """launcher-level `--` を自動挿入せず、raw_argv をそのまま launch.sh に渡す。
    launcher-level `--` が存在しない実 invocation の検証用（AC11）。
    """
    env, _argv_file = _build_env(tmp_path, use_fakes=use_fakes, extra_env=extra_env)
    result = subprocess.run(
        [str(LAUNCH_SH), *raw_argv],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result


def _read_claude_argv_log(tmp_path: Path) -> list[list[str]]:
    """FAKE_CLAUDE_ARGV_LOG（JSONL, 全 invocation 追記）を読み、invocation ごとの
    argv リストを順番どおり返す（Issue #2203 P0-3, PR #2214 fix-delta）。"""
    log_path = tmp_path / "claude-argv-log.jsonl"
    assert log_path.exists(), "fake claude argv log was not written (readback not wired?)"
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _read_claude_argv(tmp_path: Path) -> list[str]:
    argv_file = tmp_path / "claude-argv.json"
    assert argv_file.exists(), "fake claude was not invoked (argv file missing)"
    content = argv_file.read_text(encoding="utf-8")
    assert content, "argv file is empty"
    payload = json.loads(content)
    assert isinstance(payload, list)
    return payload


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


def _blocked_option(result: subprocess.CompletedProcess) -> str:
    for line in (result.stdout + result.stderr).splitlines():
        line = line.strip()
        if line.startswith("{") and '"status":"blocked"' in line:
            payload = json.loads(line)
            return payload.get("option", "")
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
    """GIVEN 呼び出し元が exact --strict-mcp-config を1個渡す（leading run）
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
    """GIVEN 呼び出し元が exact --strict-mcp-config を2個（重複、leading run）渡す
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


def test_exact_triplicate_strict_mcp_config_tokens_are_all_stripped(tmp_path):
    """GIVEN 呼び出し元が exact --strict-mcp-config を3個（leading run）渡す
    WHEN launch.sh を実行する
    THEN すべて pre-filter で除去され、canonical 分のみ1個残る
    """
    result = _run_launch(
        tmp_path,
        ["--strict-mcp-config", "--strict-mcp-config", "--strict-mcp-config"],
        use_fakes=True,
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


# --- forbidden flag 同時指定（AC2 / AC4 / AC11） ------------------------------


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


@pytest.mark.parametrize(
    "argv",
    [
        # exact strict と forbidden flag の前後両順序（AC11）
        ["--strict-mcp-config", "--mcp-config", "/tmp/x.json"],
        ["--mcp-config", "/tmp/x.json", "--strict-mcp-config"],
        ["--strict-mcp-config", "--settings", "/tmp/x.json"],
        ["--settings", "/tmp/x.json", "--strict-mcp-config"],
        # duplicate strict が forbidden flag を挟むケース（AC11）
        ["--strict-mcp-config", "--mcp-config", "/tmp/x.json", "--strict-mcp-config"],
        ["--strict-mcp-config", "--strict-mcp-config", "--settings", "/tmp/x.json"],
        # --permission-mode bypassPermissions / =bypassPermissions と exact strict の共起（AC11）
        ["--strict-mcp-config", "--permission-mode", "bypassPermissions"],
        ["--permission-mode", "bypassPermissions", "--strict-mcp-config"],
        ["--strict-mcp-config", "--permission-mode=bypassPermissions"],
    ],
)
def test_expanded_regression_matrix_forbidden_flag_cooccurrence_rejected(tmp_path, argv):
    """GIVEN exact strict と forbidden flag が両順序・重複を含めて混在する
    WHEN launch.sh を実行する
    THEN forbidden 判定は exact strict の leading-run 削除の有無に関わらず
    一貫して exit 2 で拒否される（Issue #2189 AC11 拡充 regression matrix）
    """
    result = _run_launch(tmp_path, argv, use_fakes=False)
    assert result.returncode == 2
    assert _blocked_reason(result) == "policy_weakening_flag_rejected"


def test_launcher_level_double_dash_missing_rejects_as_unknown_launcher_option(tmp_path):
    """GIVEN launcher-level `--` を付けずに --strict-mcp-config を直接渡す
    （Issue #2189 Live Reproduction 1 の再現。`--` の前は launch.sh 自身の
    オプションパーサが処理するため、pre-filter に到達する前に拒否される）
    WHEN launch.sh を実行する
    THEN unknown_launcher_option で exit 2 拒否される（AC11）
    """
    result = _run_launch_raw(tmp_path, ["--strict-mcp-config"], use_fakes=False)
    assert result.returncode == 2
    assert _blocked_reason(result) == "unknown_launcher_option"
    assert _blocked_option(result) == "--strict-mcp-config"


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


def test_case_variant_reaches_strict_fake_and_is_rejected_as_unknown_flag(tmp_path):
    """GIVEN 大文字小文字違いの --Strict-Mcp-Config が claude 側まで到達する
    WHEN 未知フラグを非ゼロ終了する strict fake claude binary を使って launch.sh を実行する
    THEN fake binary が未知フラグとして非ゼロ終了し、launcher がその status を
    そのまま最終 exit code として伝播する（fake の常時 exit 0 では検出できない。AC5）
    """
    result = _run_launch(tmp_path, ["--Strict-Mcp-Config"], use_fakes=True, strict_fake=True)
    assert result.returncode == 1, result.stderr


def test_substring_variant_reaches_strict_fake_and_is_rejected_as_unknown_flag(tmp_path):
    """GIVEN --strict-mcp-config-evil が claude 側まで到達する
    WHEN 未知フラグを非ゼロ終了する strict fake claude binary を使って launch.sh を実行する
    THEN fake binary が未知フラグとして非ゼロ終了し、launcher がその status を
    そのまま最終 exit code として伝播する（AC5 / AC12）
    """
    result = _run_launch(
        tmp_path, ["--strict-mcp-config-evil"], use_fakes=True, strict_fake=True
    )
    assert result.returncode == 1, result.stderr


# --- child exit code / signal 伝播（AC12） -----------------------------------


def test_child_nonzero_exit_code_is_propagated_as_launcher_final_exit_code(tmp_path):
    """GIVEN claude 子プロセスが nonzero exit code（例: 42）で終了する
    WHEN launch.sh を実行する
    THEN launcher の最終 exit code もそのまま 42 になる
    """
    result = _run_launch(
        tmp_path,
        ["-p", "hello"],
        use_fakes=True,
        extra_env={"FAKE_CLAUDE_EXIT_CODE": "42"},
    )
    assert result.returncode == 42


def test_child_sigterm_is_forwarded_and_final_exit_code_reflects_child_handler(tmp_path):
    """GIVEN claude 子プロセスが SIGTERM を捕捉し、特定の exit code で終了するよう
    trap する
    WHEN launcher プロセス自身に SIGTERM を送る
    THEN launcher の signal forwarding（claude_gpt_forward_signal）が子プロセスへ
    SIGTERM を転送し、子プロセスの trap 後 exit code が launcher の最終 exit code
    としてそのまま伝播する
    """
    env, argv_file = _build_env(
        tmp_path,
        use_fakes=True,
        extra_env={"FAKE_CLAUDE_TRAP_TERM_EXIT_CODE": "77"},
    )
    proc = subprocess.Popen(
        [str(LAUNCH_SH), "--", "-p", "hello"],
        cwd=str(SCRIPT_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 20.0
        while time.time() < deadline and not argv_file.exists():
            time.sleep(0.1)
        assert argv_file.exists(), "fake claude did not start in time"
        # SIGTERM ハンドラ登録の猶予（bounded）。
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    assert proc.returncode == 77, f"stdout={stdout!r} stderr={stderr!r}"


# --- leading run の外側（value position）は削除しない（AC8） ------------------


def test_value_position_p_flag_value_is_not_stripped(tmp_path):
    """GIVEN `-p` の値として渡された --strict-mcp-config（leading run の外側）
    WHEN launch.sh を実行する
    THEN pre-filter は leading run の先頭で非一致トークン(-p)に遭遇した時点で
    走査を止め、以降の --strict-mcp-config は一切削除しない
    （Live Reproduction 3 の自動テスト化）
    """
    result = _run_launch(tmp_path, ["-p", "--strict-mcp-config"], use_fakes=True)
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    canonical = _canonical_prefix(tmp_path)
    assert argv[: len(canonical)] == canonical
    remaining = _assert_agents_flag_wired(argv[len(canonical) :])
    assert remaining == ["-p", "--strict-mcp-config"]
    assert argv.count("--strict-mcp-config") == 2


def test_value_position_downstream_double_dash_literal_is_not_stripped(tmp_path):
    """GIVEN downstream `--` より後の positional literal としての --strict-mcp-config
    WHEN launch.sh を実行する
    THEN leading run は先頭の literal `--` で即座に止まり、以降は一切削除しない
    """
    result = _run_launch(tmp_path, ["--", "--strict-mcp-config"], use_fakes=True)
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    canonical = _canonical_prefix(tmp_path)
    assert argv[: len(canonical)] == canonical
    remaining = _assert_agents_flag_wired(argv[len(canonical) :])
    assert remaining == ["--", "--strict-mcp-config"]
    assert argv.count("--strict-mcp-config") == 2


def test_value_position_arbitrary_string_option_value_is_not_stripped(tmp_path):
    """GIVEN --append-system-prompt の値としての --strict-mcp-config
    WHEN launch.sh を実行する
    THEN leading run は先頭の --append-system-prompt で即座に止まり、
    以降は一切削除しない
    """
    result = _run_launch(
        tmp_path, ["--append-system-prompt", "--strict-mcp-config"], use_fakes=True
    )
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    canonical = _canonical_prefix(tmp_path)
    assert argv[: len(canonical)] == canonical
    remaining = _assert_agents_flag_wired(argv[len(canonical) :])
    assert remaining == ["--append-system-prompt", "--strict-mcp-config"]
    assert argv.count("--strict-mcp-config") == 2


def test_leading_run_stops_at_first_mismatch_but_removes_prior_run(tmp_path):
    """GIVEN leading run 内に一致トークンが複数あり、その後に非一致トークンと
    さらに別の --strict-mcp-config が続く
    WHEN launch.sh を実行する
    THEN leading run 内の一致分のみ削除され、非一致トークン以降（後続の
    --strict-mcp-config を含む）は一切触れない
    """
    result = _run_launch(
        tmp_path,
        [
            "--strict-mcp-config",
            "--strict-mcp-config",
            "-p",
            "--strict-mcp-config",
        ],
        use_fakes=True,
    )
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    canonical = _canonical_prefix(tmp_path)
    assert argv[: len(canonical)] == canonical
    remaining = _assert_agents_flag_wired(argv[len(canonical) :])
    assert remaining == ["-p", "--strict-mcp-config"]
    assert argv.count("--strict-mcp-config") == 2


# --- argv recorder が JSON array であること（AC9） ----------------------------


def test_argv_recorder_restores_empty_string_and_embedded_newline_and_trailing_empty(
    tmp_path,
):
    """GIVEN 空文字列引数・埋め込み改行を含む引数・末尾の空文字列引数
    WHEN launch.sh を実行する
    THEN JSON array recorder がこれらを一意に復元できる
    （改行結合方式では復元不能な入力。Issue #2189 AC9）
    """
    claude_argv = ["-p", "", "--append-system-prompt", "line1\nline2\nline3", ""]
    result = _run_launch(tmp_path, claude_argv, use_fakes=True)
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    canonical = _canonical_prefix(tmp_path)
    assert argv[: len(canonical)] == canonical
    remaining = _assert_agents_flag_wired(argv[len(canonical) :])
    assert remaining == claude_argv


def test_argv_recorder_restores_whitespace_and_glob_metacharacters(tmp_path):
    """GIVEN 空白・glob metacharacter を含む引数
    WHEN launch.sh を実行する
    THEN glob 展開・単語分割されずそのまま復元される
    """
    claude_argv = ["-p", "hello world", "--output-format", "a*b?c[d]e"]
    result = _run_launch(tmp_path, claude_argv, use_fakes=True)
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    canonical = _canonical_prefix(tmp_path)
    remaining = _assert_agents_flag_wired(argv[len(canonical) :])
    assert remaining == claude_argv


# --- --mcp-config / --settings の exactly-once + canonical path 一致（AC10） --


def test_mcp_config_and_settings_appear_exactly_once_with_canonical_paths(tmp_path):
    """GIVEN 通常の launch.sh 実行（呼び出し元は --mcp-config / --settings を渡さない）
    WHEN 最終 claude invocation の argv を構造化比較する
    THEN --mcp-config と --settings はそれぞれ exactly once ずつ存在し、直後の値が
    launcher 生成の canonical path と完全一致する
    """
    result = _run_launch(tmp_path, ["-p", "hi"], use_fakes=True)
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    canonical = _canonical_prefix(tmp_path)

    assert argv.count("--mcp-config") == 1
    assert argv.count("--settings") == 1
    assert argv[argv.index("--mcp-config") + 1] == canonical[2]
    assert argv[argv.index("--settings") + 1] == canonical[4]


# --- 無関係な安全な既存引数の順序保持 -----------------------------------------


def test_unrelated_safe_args_order_is_preserved(tmp_path):
    """GIVEN --strict-mcp-config（leading run）と無関係な安全な引数が混在する
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


# --- Herdr が渡す典型的な argv 形状を模した fixture（provenance-independent）--


def test_leading_run_strict_mcp_config_fixture_passes_through_and_launches(tmp_path):
    """GIVEN 呼び出し元が leading run として exact --strict-mcp-config を渡した後に
    非対話 -p 実行の引数が続く（Herdr が仮にこの形で付与した場合を模した fixture。
    Provenance Correction 反映: 現行 Herdr はこの flag を付与しないことを確認済みだが、
    leading-run 削除の受理経路自体は provenance と独立に検証する）
    WHEN launch.sh を実行する
    THEN forbidden-flag チェックを通過し、launcher の canonical strict-mcp flag が
    最終 invocation に1個だけ含まれる
    """
    result = _run_launch(
        tmp_path,
        [
            "--strict-mcp-config",
            "-p",
            "run the assigned task",
            "--output-format",
            "stream-json",
        ],
        use_fakes=True,
    )
    assert result.returncode == 0, result.stderr
    argv = _read_claude_argv(tmp_path)
    assert argv.count("--strict-mcp-config") == 1
    assert "--mcp-config" in argv
    assert "--settings" in argv
    assert "run the assigned task" in argv


def test_trailing_strict_mcp_config_after_downstream_args_is_preserved_not_stripped(
    tmp_path,
):
    """GIVEN --strict-mcp-config が引数列の末尾（leading run の外側）に現れる
    （旧実装が誤って削除していた形。Live Reproduction 節相当の一般化）
    WHEN launch.sh を実行する
    THEN leading run の外側にあるため削除されず、canonical 分と合わせて2個残る
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
    assert argv.count("--strict-mcp-config") == 2
    assert "run the assigned task" in argv


# --- bounded real Claude Code CLI parser probe（AC13） ------------------------


def _real_claude_version(env: dict[str, str]) -> str:
    result = subprocess.run(
        [REAL_CLAUDE_BIN, "--version"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() or result.stderr.strip()


def _real_claude_probe_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    home = tmp_path / "real-claude-home"
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    # isolated config root（実 credential / 実 config を読ませない）。
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env.pop("ANTHROPIC_API_KEY", None)
    return env


@pytest.mark.skipif(
    REAL_CLAUDE_BIN is None,
    reason=(
        "real Claude Code CLI binary not found on PATH; skip bounded parser probe "
        "(Issue #2189 AC13 skip_conditions). SKIP はそのまま記録し PASS へ昇格しない。"
    ),
)
class TestRealCliParserProbe:
    """Issue #2189 AC13: bounded timeout・isolated HOME/config での実 Claude Code
    CLI parser probe。認証・会話開始を必要としない範囲（option parsing のみ）に
    限定する。probe_cwd は tmp_path を使い、リポジトリの .claude/settings.json を
    読ませない。
    """

    @pytest.mark.parametrize(
        "flag",
        [
            "--Strict-Mcp-Config",
            "--strict-mcp-config-evil",
            "--strict-mcp-config=evil",
            "--no-strict-mcp-config",
        ],
    )
    def test_real_cli_rejects_unknown_strict_mcp_config_variants(self, tmp_path, flag):
        env = _real_claude_probe_env(tmp_path)
        version = _real_claude_version(env)
        assert version, "expected `claude --version` to report a non-empty version string"

        probe_cwd = tmp_path / "probe-cwd"
        probe_cwd.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [REAL_CLAUDE_BIN, flag],
            cwd=str(probe_cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        combined = (result.stdout + result.stderr).lower()
        assert result.returncode != 0, (
            f"claude version={version!r} flag={flag!r} unexpectedly exited 0: {combined!r}"
        )
        assert "unknown option" in combined, (
            f"claude version={version!r} flag={flag!r} did not report unknown option: "
            f"{combined!r}"
        )

    def test_real_cli_downstream_double_dash_literal_is_not_rejected_as_unknown_option(
        self, tmp_path
    ):
        """GIVEN downstream `--` より後の positional literal としての
        --strict-mcp-config
        WHEN 実 Claude Code CLI を bounded timeout で実行する
        THEN option parser レベルでの unknown-option 拒否は発生しない
        （認証未設定のため別の理由で非ゼロ終了しうるが、"unknown option" とは
        異なる失敗理由であることを確認する）
        """
        env = _real_claude_probe_env(tmp_path)
        version = _real_claude_version(env)
        assert version

        probe_cwd = tmp_path / "probe-cwd"
        probe_cwd.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [REAL_CLAUDE_BIN, "-p", "probe", "--", "--strict-mcp-config"],
            cwd=str(probe_cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        combined = (result.stdout + result.stderr).lower()
        assert "unknown option" not in combined, (
            f"claude version={version!r} unexpectedly rejected downstream literal "
            f"as unknown option: {combined!r}"
        )


# --- Issue #2203 P0-3 (PR #2214 OWNER adversarial review fix-delta): 通常起動へ
#     fail-closed auto-mode readback を配線する ---------------------------------


def test_normal_launch_wires_auto_mode_readback_before_main_invocation(tmp_path):
    """GIVEN 通常起動（--check-only/--dry-run なし）
    WHEN launch.sh を実行する
    THEN claude 本体起動前に `--version` / `auto-mode defaults` / `auto-mode
    config` の readback invocation が記録され、最後に本 invocation（canonical
    prefix + caller argv）が記録される（readback が unwired のまま放置される
    regression を防ぐ）
    """
    result = _run_launch(tmp_path, ["-p", "hello"], use_fakes=True)
    assert result.returncode == 0, result.stderr
    invocations = _read_claude_argv_log(tmp_path)
    assert invocations[0] == ["--version"]
    auto_mode_calls = [argv for argv in invocations if "auto-mode" in argv]
    assert ["auto-mode", "defaults"] in auto_mode_calls
    assert any(argv[-2:] == ["auto-mode", "config"] for argv in auto_mode_calls)
    main_invocation = invocations[-1]
    canonical = _canonical_prefix(tmp_path)
    assert main_invocation[: len(canonical)] == canonical
    remaining = _assert_agents_flag_wired(main_invocation[len(canonical) :])
    assert remaining == ["-p", "hello"]
    assert "CLAUDE_GPT_AUTO_MODE_CHECK_PATH=" in result.stderr


def test_normal_launch_writes_auto_mode_check_evidence_artifact(tmp_path):
    """GIVEN 通常起動が成功する
    WHEN launch.sh を実行する
    THEN readback 結果 JSON（digests / checks を含む）が
    `<claude_config_dir>/auto-mode-check.json` に書き出される（P1-3: evidence
    schema が実 digest を再計算できるようにする入力）
    """
    result = _run_launch(tmp_path, ["-p", "hello"], use_fakes=True)
    assert result.returncode == 0, result.stderr
    check_path = tmp_path / "claude-gpt-home" / "claude" / "auto-mode-check.json"
    assert check_path.exists()
    payload = json.loads(check_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["digests"]["auto_mode_defaults_digest"] != "unknown"
    assert payload["digests"]["effective_config_digest"] != "unknown"


def test_normal_launch_blocks_when_auto_mode_readback_fails(tmp_path):
    """GIVEN readback（`auto-mode config`）が narrow label を反映しない effective
    config を返す実行環境
    WHEN launch.sh を実行する
    THEN claude 本体の main invocation は一度も行われず、exit 8 で
    auto_mode_readback_failed として fail-closed 拒否される
    """
    result = _run_launch(
        tmp_path,
        ["-p", "hello"],
        use_fakes=True,
        extra_env={"FAKE_CLAUDE_AUTO_MODE_READBACK_FAIL": "1"},
    )
    assert result.returncode == 8, result.stdout + result.stderr
    assert "auto_mode_readback_failed" in (result.stdout + result.stderr)
    invocations = _read_claude_argv_log(tmp_path)
    assert not any("-p" in argv for argv in invocations), (
        "claude 本体は readback 失敗後に一切起動されてはならない"
    )


# --- Issue #2186 P0 fix-delta (PR #2244 adversarial review): runtime-smoke
#     observation hooks must never displace the spark-codex authorization
#     gate's PreToolUse(matcher: Agent) entry ----------------------------


@pytest.mark.parametrize(
    ("smoke_hooks_value", "extra_env"),
    [
        ("subagent-start-stop", {}),
        ("hook-sink-multi-turn", {"CLAUDE_GPT_HOOK_SINK_NONCE": "nonce-fixture-hooksink"}),
    ],
)
def test_runtime_smoke_hooks_never_drop_spark_authorization_gate(
    tmp_path, smoke_hooks_value, extra_env
):
    """GIVEN CLAUDE_GPT_RUNTIME_SMOKE_HOOKS is set to either
    ``subagent-start-stop`` or ``hook-sink-multi-turn``
    WHEN launch.sh generates the launcher-owned ``--settings`` JSON
    THEN ``hooks.PreToolUse`` still contains a ``matcher: "Agent"`` entry
    whose command references ``SPARK_GATE_WRITER`` (Issue #2186 P0
    fix-delta: prior to this fix, requesting runtime-smoke observation hooks
    silently REPLACED the entire hooks fragment, dropping the spark-codex
    explicit-only authorization gate's PreToolUse entry entirely -- which
    left Claude Code's default-allow behavior in effect for any Agent tool
    call while runtime-smoke observation was active).
    """
    result = _run_launch(
        tmp_path,
        ["-p", "hello"],
        use_fakes=True,
        extra_env={"CLAUDE_GPT_RUNTIME_SMOKE_HOOKS": smoke_hooks_value, **extra_env},
    )
    assert result.returncode == 0, result.stderr
    settings_path = tmp_path / "claude-gpt-home" / "claude" / "settings.local.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings["hooks"]

    pre_tool_use_groups = hooks["PreToolUse"]
    agent_groups = [g for g in pre_tool_use_groups if g.get("matcher") == "Agent"]
    assert agent_groups, f"no matcher=Agent PreToolUse group found: {pre_tool_use_groups!r}"
    assert any(
        "SPARK_GATE_WRITER" in h.get("command", "")
        for g in agent_groups
        for h in g.get("hooks", [])
    ), f"matcher=Agent PreToolUse group missing SPARK_GATE_WRITER command: {agent_groups!r}"

    # The gate's UserPromptSubmit/SubagentStart/SubagentStop entries must
    # also survive alongside any observation-only sink commands.
    for event in ("UserPromptSubmit", "SubagentStart", "SubagentStop"):
        groups = hooks[event]
        assert any(
            "SPARK_GATE_WRITER" in h.get("command", "")
            for g in groups
            for h in g.get("hooks", [])
        ), f"{event} missing SPARK_GATE_WRITER gate command: {groups!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

#!/usr/bin/env python3
"""scripts/claude-gpt/latitude_hook.py

Issue #2426: launcher-owned Latitude Stop hook adapter.

Claude-GPT の生成済み settings（`launch.sh`）は、既存 hook groups を維持したまま
この adapter を `Stop` イベントへ additive に追加する。この adapter は:

1. stdin の Stop payload（`session_id` を含む）を読み、そのまま telemetry
   subprocess の stdin へ再転送する（実際の telemetry package 自身が payload の
   `session_id` / `transcript_path` を読んで span を構築するため）。
2. Native Claude Code user settings（`~/.claude/settings.json` の `env`）から、
   closed allowlist（`ALLOWLISTED_ENV_KEYS`、8 項目）に含まれる key だけを読む。
   `LATITUDE_DEBUG` / `BUN_OPTIONS` / hooks / permissions / plugins / MCP は
   一切読まない。
3. Stop payload の `session_id` から session-local telemetry HOME
   （`$CLAUDE_GPT_HOME_ROOT/latitude-sessions/<sha256(session_id)>/`）を作り、
   telemetry subprocess の `HOME` をそこへ差し替える（upstream telemetry の
   state locking を session ごとに分離するため）。
4. 一箇所の SSOT（`lib.sh` の `claude_gpt_latitude_package_spec`）で pin された
   exact version の telemetry package を、上記の最小 allowlist 環境変数だけを
   持つ child process として起動する。

observation-only / fail-open: 例外は握りつぶし、常に exit code 0 を返す
（Latitude ingest failure が Claude-GPT session 本体を失敗させない。AC8）。

`LATITUDE_API_KEY` の値は、stdout / stderr / argv / persisted evidence の
いずれにも一切出力しない（AC3）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys

# --- Design 2節: closed allowlist（8項目固定）。unknown key は無視する。
#     `LATITUDE_DEBUG` と `BUN_OPTIONS` はこの allowlist に含めない。 ---
ALLOWLISTED_ENV_KEYS: tuple[str, ...] = (
    "LATITUDE_API_KEY",
    "LATITUDE_PROJECT",
    "LATITUDE_BASE_URL",
    "LATITUDE_CLAUDE_CODE_ENABLED",
    "LATITUDE_CLAUDE_CODE_MEMORY",
    "LATITUDE_CLAUDE_CODE_MEMORY_CONTENT",
    "LATITUDE_REDACT_ATTRIBUTES",
    "LATITUDE_REDACT_MASK",
)

def read_native_latitude_allowlist(native_settings_path: str | None) -> dict[str, str]:
    """Native user settings の `env` から closed allowlist の key だけを返す。

    - ファイル不在 / JSON parse 失敗 / 構造不正はすべて空 dict を返す（fail-open）。
    - `env` 以外のトップレベル key（`hooks`/`permissions`/`plugins`/MCP 設定等）は
      一切読まない。
    - allowlist に無い key（`LATITUDE_DEBUG`/`BUN_OPTIONS` を含む）は無視する。
    - 値が非空文字列の場合のみ結果に含める。
    """
    if not native_settings_path:
        return {}
    try:
        with open(native_settings_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    result: dict[str, str] = {}
    for key in ALLOWLISTED_ENV_KEYS:
        value = env.get(key)
        if isinstance(value, str) and value != "":
            result[key] = value
    return result


def session_local_telemetry_home(home_root: str, session_id: str | None) -> str:
    """Design 6節: `$CLAUDE_GPT_HOME_ROOT/latitude-sessions/<sha256(session_id)>/`。

    `session_id` の raw value 自体は返り値・公開 evidence に出さない（opaque な
    sha256 digest のみを path 要素として使う）。
    """
    digest = hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()
    return os.path.join(home_root, "latitude-sessions", digest)


def shared_npm_cache_dir(home_root: str) -> str:
    """`npx` が起動のたびに telemetry package を再ダウンロードしないよう、
    session-local HOME とは別に、`CLAUDE_GPT_HOME_ROOT` 配下の永続 npm cache
    ディレクトリを1つだけ共有する（Design 6節の「upstream telemetry の state
    locking を session ごとに分離する」対象は telemetry package 自身の
    runtime state であって、npm package cache ではない -- cache を共有しても
    session 間の state 競合は再発しない）。
    """
    return os.path.join(home_root, "latitude-npm-cache")


def build_child_env(
    session_home: str,
    allowlist_env: dict[str, str],
    *,
    path_value: str | None = None,
    npm_cache_dir: str | None = None,
) -> dict[str, str]:
    """telemetry subprocess へ渡す最小 child env を組み立てる。

    ambient `os.environ` をそのまま継承しない（PATH と `HOME` 以外は allowlist の
    8 項目 + 任意の `npm_cache_dir` のみ）。呼び出し元の `os.environ` に他の
    secret が存在しても、この child env には現れない。
    """
    child_env: dict[str, str] = {
        "HOME": session_home,
        "PATH": path_value if path_value is not None else os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if npm_cache_dir:
        child_env["NPM_CONFIG_CACHE"] = npm_cache_dir
    for key in ALLOWLISTED_ENV_KEYS:
        value = allowlist_env.get(key)
        if value:
            child_env[key] = value
    return child_env


def _resolve_npx_bin() -> str | None:
    override = os.environ.get("CLAUDE_GPT_LATITUDE_NPX_BIN")
    if override:
        return override
    return shutil.which("npx")


def run_latitude_telemetry(raw_stdin: bytes, payload: dict) -> None:
    """Stop payload を pin 済み telemetry package へ additive に中継する。

    どの段階で失敗しても例外を外へ伝播させない（呼び出し元 `main()` の
    fail-open 契約はここでは前提にせず、この関数自身も内部で fail-open する）。
    """
    native_settings_path = os.environ.get("CLAUDE_GPT_NATIVE_SETTINGS_PATH")
    home_root = os.environ.get("CLAUDE_GPT_HOME_ROOT")
    package_spec = os.environ.get("CLAUDE_GPT_LATITUDE_PACKAGE_SPEC")
    if not home_root or not package_spec:
        return

    allowlist_env = read_native_latitude_allowlist(native_settings_path)
    # AC8: Latitude ingest 用の資格情報が無ければ何もしない（Claude-GPT 本体の
    # 失敗にしない。telemetry を諦めるだけで static-fail にはしない）。
    if not allowlist_env.get("LATITUDE_API_KEY") or not allowlist_env.get("LATITUDE_PROJECT"):
        return

    npx_bin = _resolve_npx_bin()
    if not npx_bin:
        return

    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    session_home = session_local_telemetry_home(home_root, session_id)
    npm_cache_dir = shared_npm_cache_dir(home_root)
    try:
        os.makedirs(session_home, mode=0o700, exist_ok=True)
        os.chmod(session_home, 0o700)
        os.makedirs(npm_cache_dir, mode=0o700, exist_ok=True)
    except OSError:
        return

    child_env = build_child_env(session_home, allowlist_env, npm_cache_dir=npm_cache_dir)

    # 実機検証（Issue #2426 AC5/AC6 live probe, 2026-08-30）: この adapter 自身は
    # Claude Code から `"async": true` Stop hook として起動されるが、それだけでは
    # telemetry subprocess は adapter プロセスと同じ process group に留まる。
    # one-shot `-p` 呼び出しでは launch.sh の EXIT trap（`claude_gpt_cleanup`）が
    # `$CLAUDE_PID` を kill した直後に呼び出し元プロセス階層全体が終了し、
    # 同一 process group に留まったままの telemetry subprocess も一緒に断たれる
    # ことを実機で確認した（`subprocess.run()` で待機する版では trace が
    # Latitude に到達しなかった）。`start_new_session=True`（setsid 相当）で
    # 新しい session/process group を持たせ、`Popen` の完了を待たずに fire-and-
    # forget することで、adapter プロセス自身の終了とは独立に生存させ、init へ
    # reparent された後も telemetry package が完走できるようにする。
    try:
        proc = subprocess.Popen(
            [npx_bin, "-y", package_spec],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.write(raw_stdin)
            proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    # 意図的に proc.wait()/communicate() を呼ばない（fire-and-forget）。


def main() -> int:
    raw_stdin = b""
    try:
        raw_stdin = sys.stdin.buffer.read()
    except (OSError, ValueError):
        raw_stdin = b""
    payload: dict = {}
    try:
        parsed = json.loads(raw_stdin.decode("utf-8")) if raw_stdin else {}
        if isinstance(parsed, dict):
            payload = parsed
    except (UnicodeDecodeError, ValueError):
        payload = {}
    try:
        run_latitude_telemetry(raw_stdin, payload)
    except Exception:
        # observation-only / fail-open（AC8）: どのような内部例外でも
        # Claude-GPT session 本体（Stop イベント）を失敗させない。例外の
        # 内容（secret を含みうる）は一切出力しない。
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

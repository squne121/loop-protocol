#!/bin/sh
# scripts/claude-gpt/launch.sh
#
# repository-owned claude-gpt launcher。
#
# herdr session B（ChatGPT Pro Codex subscription 経由 GPT-5.6 Sol/Terra/Luna）を
# `raine/claude-code-proxy` 経由で起動する。Native Claude（herdr session A）とは
# config root / credential / working tree を完全分離する（Issue #2158 / Parent #2154
# アーキテクチャ決定 A〜E 準拠）。PR #2162 OWNER REQUEST_CHANGES（P0-1〜P0-4, P1-1〜P1-3）反映。
#
# Usage:
#   scripts/claude-gpt/launch.sh [--check-only] [--dry-run] [--claude-bin <path>] [-- <claude 追加引数...>]
#
#   --check-only   proxy を起動し preflight / bind / model 解決だけ確認して終了する
#                  （`claude` 本体は起動しない。runtime_smoke_test.sh から使う）。
#   --dry-run      ディレクトリ作成・設定ファイル書き込み・proxy 起動を一切行わず、
#                  実行予定の内容を JSON で表示するのみ（scripts/CLAUDE.md 破壊的処理不変条件）。
#   --claude-bin   claude 実行ファイルの絶対パスを明示する（CLAUDE_GPT_CLAUDE_BIN と同義）。
#   --             以降は claude 本体へそのまま渡す追加引数（`"$@"` のまま保持し、文字列化
#                  して再分割しない。P1-1）。policy-weakening flag（--settings 等）は拒否する。
#
# Exit code:
#   0   起動成功（--check-only 時 / 通常起動時は claude 子プロセスの exit code をそのまま返す）
#   2   launcher 自身の引数エラー（未知オプション・policy-weakening flag 検出）
#   3   claude-code-proxy バイナリまたは claude バイナリが見つからない
#   4   ChatGPT subscription 認証が利用不能
#   5   canonical path 違反（repo/worktree 配下への書き込みを拒否）
#   6   read 制限 settings が未生成または不正
#   7   proxy 起動失敗（loopback bind / readiness / model alias を確認できない）
#   8   sandbox 初期化に失敗した
#   9   CLAUDE_GPT_ISOLATED_PROXY_USER が設定されているが provisioning が未完了
#       （isolated_proxy_user_not_provisioned。preflight.sh 参照）

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

CHECK_ONLY=false
DRY_RUN=false

while [ $# -gt 0 ]; do
  case "$1" in
    --check-only)
      CHECK_ONLY=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --claude-bin)
      if [ $# -lt 2 ]; then
        printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"missing_value","option":"--claude-bin"}\n' >&2
        exit 2
      fi
      CLAUDE_GPT_CLAUDE_BIN="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -*)
      printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"unknown_launcher_option","option":"%s"}\n' "$1" >&2
      exit 2
      ;;
    *)
      printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"unexpected_positional_argument_before_double_dash","value":"%s"}\n' "$1" >&2
      exit 2
      ;;
  esac
done

# --- 以降 "$@" は `--` の後ろに来た claude 追加引数そのもの。文字列へ結合して再分割しない。 ---

# --- policy-weakening flag 拒否（P1-1）。呼び出し元が --settings / --mcp-config /
#     --strict-mcp-config / --dangerously-skip-permissions で launcher の安全設定を
#     上書きすることを拒否する。--permission-mode bypassPermissions も拒否する。 ---
prev=""
for arg in "$@"; do
  for forbidden in $CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS; do
    case "$arg" in
      "$forbidden"|"$forbidden"=*)
        printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"policy_weakening_flag_rejected","flag":"%s"}\n' "$forbidden" >&2
        exit 2
        ;;
    esac
  done
  if [ "$prev" = "--permission-mode" ] && [ "$arg" = "bypassPermissions" ]; then
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"policy_weakening_flag_rejected","flag":"--permission-mode bypassPermissions"}\n' >&2
    exit 2
  fi
  prev="$arg"
done

CLAUDE_CONFIG_DIR_TARGET=$(claude_gpt_claude_config_dir)
PROXY_CONFIG_DIR_TARGET=$(claude_gpt_proxy_config_dir)
PROXY_STATE_DIR_TARGET=$(claude_gpt_proxy_state_dir)
PROXY_HOME_TARGET=$(claude_gpt_proxy_home_dir)
MCP_CONFIG_PATH=$(claude_gpt_mcp_config_path)
SETTINGS_PATH=$(claude_gpt_session_settings_path)

# --- canonical path safety（ディレクトリ作成前に必ず検証する） ---
for d in "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET" "$PROXY_HOME_TARGET"; do
  if ! claude_gpt_reject_if_under_repo "$d" "$SELF_PATH"; then
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"canonical_path_under_repo_or_worktree","path":"%s"}\n' "$d"
    exit 5
  fi
done

if [ "$DRY_RUN" = "true" ]; then
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"dry_run","claude_config_dir":"%s","proxy_config_dir":"%s","proxy_state_dir":"%s","proxy_home_dir":"%s","mcp_config_path":"%s","settings_path":"%s"}\n' \
    "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET" "$PROXY_HOME_TARGET" "$MCP_CONFIG_PATH" "$SETTINGS_PATH"
  exit 0
fi

if [ "$CHECK_ONLY" = "false" ] && [ "$DRY_RUN" = "false" ]; then
  CLAUDE_BIN=$(claude_gpt_resolve_claude_bin)
  if [ -z "$CLAUDE_BIN" ]; then
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"claude_binary_not_found"}\n'
    exit 3
  fi
fi

# --- 以降で作成するファイル/ディレクトリの permission を厳格化する（P1-3） ---
umask 077

# --- GPT 専用ディレクトリを準備する（既存なら idempotent） ---
mkdir -p "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET" "$PROXY_HOME_TARGET"

# --- strict_mcp mode 用の空 MCP config を書き込む（repository/user MCP を読み込ませない） ---
STRICT_MCP_MODE=true
cat > "$MCP_CONFIG_PATH" <<MCP_JSON_EOF
{
  "mcpServers": {}
}
MCP_JSON_EOF

# --- Claude Code セッション設定 ---
# 1. proxy credential/config/state/home ディレクトリへの read を拒否する（P0-3。
#    絶対パスは `Read(//...)` の二重スラッシュ構文でなければ機能しない）。常に有効。
# 2. sandbox（bubblewrap ベース）は CLAUDE_GPT_HARDENED_SANDBOX=true の場合のみ opt-in で
#    有効化する。実機検証（PR #2162, 2026-08-14）で、launcher 自体がすでにネストした
#    bwrap sandbox 環境下で実行されている場合、`sandbox.enabled: true` は Claude Code
#    本体の Bash tool 実行を `Maximum call stack size exceeded` で完全に破壊することを
#    確認した。Bash tool という中核機能を破壊しうる hardening を既定で有効化する方が
#    実害が大きいため、既定は無効・opt-in とする。Issue #2173 で改めて実機検証し、
#    ネスト実行環境下で sandbox.enabled: true / CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 は
#    Bash tool 呼び出しを応答なしのビジーループへ陥らせることを 2/2, 1/1 の再現率で確認、
#    既定 opt-in 無効を維持する判断を確定した
#    （docs/dev/claude-gpt-sandbox-hardening-verification.md 参照）。
# 3. session-scoped に repository/user plugin を無効化する（Parent #2154 gateway/context
#    契約。P1-2）。
SANDBOX_JSON_FRAGMENT=""
if [ "${CLAUDE_GPT_HARDENED_SANDBOX:-false}" = "true" ]; then
  SANDBOX_JSON_FRAGMENT=',
  "sandbox": {
    "enabled": true,
    "network": {
      "allowAllUnixSockets": false
    }
  }'
fi

# --- narrow observability channel(Issue #2158/#2173, structured lane #2174/PR #2176 で並行実装中)。
#     呼び出し元が任意の JSON 文字列を settings へ注入できる汎用経路は作らず、許可された固定値
#     (`subagent-start-stop`)のみを受け付ける。それ以外の値は fragment を空のままにする(拒否)。
#     既存の CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS(--settings 等 CLI 引数拒否)とは独立した経路であり、
#     それを変更・弱体化するものではない。 ---
HOOKS_JSON_FRAGMENT=""
if [ "${CLAUDE_GPT_RUNTIME_SMOKE_HOOKS:-}" = "subagent-start-stop" ]; then
  HOOKS_JSON_FRAGMENT=',
  "hooks": {
    "SubagentStart": [{"hooks": [{"type": "command", "command": "cat"}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "cat"}]}]
  }'
fi

cat > "$SETTINGS_PATH" <<SETTINGS_JSON_EOF
{
  "permissions": {
    "deny": [
      "Read(/${PROXY_CONFIG_DIR_TARGET}/**)",
      "Read(/${PROXY_STATE_DIR_TARGET}/**)",
      "Read(/${PROXY_HOME_TARGET}/**)"
    ]
  }${SANDBOX_JSON_FRAGMENT},
  "enabledPlugins": {}${HOOKS_JSON_FRAGMENT}
}
SETTINGS_JSON_EOF

# --- preflight 実行（read 制限 settings が生成済みであることを含めて再検証。sandbox 初期化含む） ---
PREFLIGHT_JSON=$("$SCRIPT_DIR/preflight.sh")
PREFLIGHT_RC=$?
if [ "$PREFLIGHT_RC" -ne 0 ]; then
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"preflight_failed","preflight":%s}\n' "$PREFLIGHT_JSON"
  exit "$PREFLIGHT_RC"
fi

# --- proxy 起動（bounded retry で port TOCTOU race を吸収する。P1-3） ---
PORT_ATTEMPTS=0
PORT_MAX_ATTEMPTS=5
READY=false
BIND_OK=false
PROXY_PID=""
PROXY_PORT=""
PROXY_LOG=""

while [ "$PORT_ATTEMPTS" -lt "$PORT_MAX_ATTEMPTS" ] && [ "$READY" != "true" ]; do
  PORT_ATTEMPTS=$((PORT_ATTEMPTS + 1))

  if command -v python3 >/dev/null 2>&1; then
    PROXY_PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
  else
    PROXY_PORT=$((14141 + PORT_ATTEMPTS))
  fi

  RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)-$$-${PROXY_PORT}"
  PROXY_LOG="${PROXY_STATE_DIR_TARGET}/launcher-proxy-${RUN_TAG}.log"

  # 親 shell から継承した CCP_* / HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 等は env -i で
  # リセットし、明示的に組み立てた変数のみを渡す。HOME は proxy 専用 HOME（P0-2）。
  # CLAUDE_GPT_ISOLATED_PROXY_USER が設定されている場合（P0-3 段階対応。opt-in。
  # scripts/claude-gpt/provision_proxy_principal.sh で事前 provisioning 済みであることは
  # preflight.sh が既に検証済み — ここでの再検証はしない）、proxy を dedicated Unix user
  # 配下で起動する。未設定なら従来通り同一 UID で起動する（既定動作は変更しない）。
  if [ -n "${CLAUDE_GPT_ISOLATED_PROXY_USER:-}" ]; then
    sudo -n -u "$CLAUDE_GPT_ISOLATED_PROXY_USER" env -i \
      "PATH=$PATH" \
      "HOME=$PROXY_HOME_TARGET" \
      "CCP_CONFIG_DIR=$PROXY_CONFIG_DIR_TARGET" \
      "XDG_STATE_HOME=$PROXY_STATE_DIR_TARGET" \
      "CCP_BIND_ADDRESS=127.0.0.1" \
      "CCP_LOG_STDERR=1" \
      claude-code-proxy serve --port "$PROXY_PORT" --no-monitor > "$PROXY_LOG" 2>&1 &
  else
    env -i \
      "PATH=$PATH" \
      "HOME=$PROXY_HOME_TARGET" \
      "CCP_CONFIG_DIR=$PROXY_CONFIG_DIR_TARGET" \
      "XDG_STATE_HOME=$PROXY_STATE_DIR_TARGET" \
      "CCP_BIND_ADDRESS=127.0.0.1" \
      "CCP_LOG_STDERR=1" \
      claude-code-proxy serve --port "$PROXY_PORT" --no-monitor > "$PROXY_LOG" 2>&1 &
  fi
  PROXY_PID=$!

  # --- readiness poll（最大 10 秒） ---
  i=0
  READY=false
  while [ "$i" -lt 20 ]; do
    if ! kill -0 "$PROXY_PID" 2>/dev/null; then
      break
    fi
    if curl --fail --show-error -s -o /dev/null -m 1 "http://127.0.0.1:${PROXY_PORT}/v1/models" 2>/dev/null; then
      READY=true
      break
    fi
    i=$((i + 1))
    sleep 0.5
  done

  if [ "$READY" != "true" ]; then
    kill "$PROXY_PID" 2>/dev/null
    wait "$PROXY_PID" 2>/dev/null
    continue
  fi

  # --- loopback bind preflight: PID が所有する listen socket 全件の bind address を
  #     厳密確認する（単なる curl 疎通成功だけで判定しない。同 PID が非 loopback listener
  #     を同時に保持していないことも確認する。P1-3） ---
  BIND_LINES=$(ss -ltnp 2>/dev/null | grep "pid=${PROXY_PID}," || true)
  BIND_OK=false
  if [ -n "$BIND_LINES" ]; then
    PORT_MATCHED=false
    NONLOOP_FOUND=false
    OLD_IFS=$IFS
    IFS='
'
    for line in $BIND_LINES; do
      case "$line" in
        *"127.0.0.1:${PROXY_PORT}"*|*"[::1]:${PROXY_PORT}"*)
          PORT_MATCHED=true
          ;;
      esac
      case "$line" in
        *"127.0.0.1:"*|*"[::1]:"*)
          : # loopback。ok
          ;;
        *)
          NONLOOP_FOUND=true
          ;;
      esac
    done
    IFS=$OLD_IFS
    if [ "$PORT_MATCHED" = "true" ] && [ "$NONLOOP_FOUND" != "true" ]; then
      BIND_OK=true
    fi
  fi

  if [ "$BIND_OK" != "true" ]; then
    kill "$PROXY_PID" 2>/dev/null
    wait "$PROXY_PID" 2>/dev/null
    READY=false
    continue
  fi
done

if [ "$READY" != "true" ] || [ "$BIND_OK" != "true" ]; then
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"failed","reason":"proxy_not_ready_or_bind_not_confirmed","attempts":%s,"port":%s}\n' "$PORT_ATTEMPTS" "$PROXY_PORT"
  exit 7
fi

# --- model alias resolution 確認（live: 実際に起動した proxy の /v1/models から確認する） ---
MODELS_JSON=$(curl --fail --show-error -s -m 3 "http://127.0.0.1:${PROXY_PORT}/v1/models" 2>/dev/null) || MODELS_JSON=""
MODEL_ALIAS_OK=true
if [ -z "$MODELS_JSON" ]; then
  MODEL_ALIAS_OK=false
fi
for m in "$CLAUDE_GPT_MODEL_MAIN" "$CLAUDE_GPT_MODEL_OPUS" "$CLAUDE_GPT_MODEL_HAIKU"; do
  case "$MODELS_JSON" in
    *"\"$m\""*) : ;;
    *) MODEL_ALIAS_OK=false ;;
  esac
done

# --- model alias 未解決は通常起動でも fail-closed で止める（従来は check-only 時のみ
#     判定していた。P1-3） ---
if [ "$MODEL_ALIAS_OK" != "true" ]; then
  kill "$PROXY_PID" 2>/dev/null
  wait "$PROXY_PID" 2>/dev/null
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"failed","reason":"model_alias_not_resolved","port":%s}\n' "$PROXY_PORT"
  exit 7
fi

# --- 呼び出し元（runtime_smoke_test.sh 等）が proxy ログ/ポートを追跡できるよう stderr へ
#     side-channel として出力する（stdout は check-only JSON または claude -p の出力専用に
#     予約するため汚さない。P0-1）。 ---
echo "CLAUDE_GPT_PROXY_PORT=${PROXY_PORT}" >&2
echo "CLAUDE_GPT_PROXY_LOG=${PROXY_LOG}" >&2
echo "CLAUDE_GPT_PROXY_PID=${PROXY_PID}" >&2

if [ "$CHECK_ONLY" = "true" ]; then
  kill "$PROXY_PID" 2>/dev/null
  wait "$PROXY_PID" 2>/dev/null
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"ok","mode":"check_only","port":%s,"bind_ok":%s,"model_alias_ok":%s,"strict_mcp_mode":%s,"mcp_config_path":"%s","settings_path":"%s","proxy_home_dir":"%s","preflight":%s}\n' \
    "$PROXY_PORT" "$BIND_OK" "$MODEL_ALIAS_OK" "$STRICT_MCP_MODE" "$MCP_CONFIG_PATH" "$SETTINGS_PATH" "$PROXY_HOME_TARGET" "$PREFLIGHT_JSON"
  exit 0
fi

# --- 通常起動: claude 本体を子プロセスとして起動する supervisor 構成（P0-4）。
#     `exec` は shell process image を claude に置き換えてしまい EXIT trap に二度と
#     到達しないため使わない。shell を supervisor として維持し、全終了経路
#     （正常/エラー/timeout/SIGINT/SIGTERM）で確実に proxy を kill/wait する。 ---

unset CLAUDE_CODE_SUBAGENT_MODEL

export CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR_TARGET"
export ANTHROPIC_BASE_URL="http://127.0.0.1:${PROXY_PORT}"
export ANTHROPIC_AUTH_TOKEN="claude-gpt-local"
export ANTHROPIC_MODEL="$CLAUDE_GPT_MODEL_MAIN"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLAUDE_GPT_MODEL_OPUS"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$CLAUDE_GPT_MODEL_SONNET"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLAUDE_GPT_MODEL_HAIKU"
# ANTHROPIC_SMALL_FAST_MODEL は意図的に使わない。ANTHROPIC_DEFAULT_HAIKU_MODEL を正本として
# 維持する（Parent #2154 gateway/context 契約。P1-2）。

# strict_mcp mode: repository/user MCP を一切読み込まない。--strict-mcp-config +
# 空の mcp-config JSON（$MCP_CONFIG_PATH）の組み合わせで実現する。
STRICT_MCP_MODE=true
export STRICT_MCP_MODE

# Parent #2154 gateway/context 契約（P1-2）。
# CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 も sandbox.enabled 同様 CLAUDE_GPT_HARDENED_SANDBOX=true
# の場合のみ opt-in で設定する。実機検証（PR #2162, 2026-08-14）で、launcher 自体がすでに
# ネストした bwrap sandbox 環境下で実行されている場合、CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1
# は単独でも Bash tool 呼び出しを `Maximum call stack size exceeded` で完全に破壊することを
# 確認した（sandbox.enabled と同根の nested-sandbox 非互換）。中核機能を破壊しうる
# hardening を既定で有効化する方が実害が大きいため、既定は無効・opt-in とする。
# Issue #2173 で CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 単独（sandbox.enabled 非依存）でも
# 同一の破壊（応答なしのビジーループ）が再現することを確認済み（1/1）。
if [ "${CLAUDE_GPT_HARDENED_SANDBOX:-false}" = "true" ]; then
  export CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1
fi
export CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=auto
export CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

CLAUDE_EXIT=0
CLEANED_UP=false

claude_gpt_cleanup() {
  if [ "$CLEANED_UP" = "true" ]; then
    return
  fi
  CLEANED_UP=true
  if [ -n "$CLAUDE_PID" ] && kill -0 "$CLAUDE_PID" 2>/dev/null; then
    kill "$CLAUDE_PID" 2>/dev/null
    wait "$CLAUDE_PID" 2>/dev/null
  fi
  if kill -0 "$PROXY_PID" 2>/dev/null; then
    kill "$PROXY_PID" 2>/dev/null
    wait "$PROXY_PID" 2>/dev/null
  fi
}

claude_gpt_forward_signal() {
  sig="$1"
  if [ -n "$CLAUDE_PID" ] && kill -0 "$CLAUDE_PID" 2>/dev/null; then
    kill "-$sig" "$CLAUDE_PID" 2>/dev/null
  fi
  wait "$CLAUDE_PID" 2>/dev/null
  CLAUDE_EXIT=$?
  claude_gpt_cleanup
  exit "$CLAUDE_EXIT"
}

trap 'claude_gpt_forward_signal INT' INT
trap 'claude_gpt_forward_signal TERM' TERM
trap 'claude_gpt_forward_signal HUP' HUP
trap 'claude_gpt_cleanup' EXIT

# shellcheck disable=SC2086
"$CLAUDE_BIN" --strict-mcp-config --mcp-config "$MCP_CONFIG_PATH" --settings "$SETTINGS_PATH" "$@" &
CLAUDE_PID=$!

wait "$CLAUDE_PID"
CLAUDE_EXIT=$?

claude_gpt_cleanup

# --- proxy PID 消失と listen socket 消失を確認する（best-effort。P0-4） ---
CLEANUP_OK=true
if kill -0 "$PROXY_PID" 2>/dev/null; then
  echo "WARNING: claude-gpt proxy pid ${PROXY_PID} は cleanup 後も残留しています。" >&2
  CLEANUP_OK=false
fi
if ss -ltnp 2>/dev/null | grep -q "pid=${PROXY_PID},"; then
  echo "WARNING: claude-gpt proxy port ${PROXY_PORT} の listen socket は cleanup 後も残留しています。" >&2
  CLEANUP_OK=false
fi
echo "CLAUDE_GPT_PROXY_CLEANUP_OK=${CLEANUP_OK}" >&2
echo "CLAUDE_GPT_CLAUDE_EXIT_CODE=${CLAUDE_EXIT}" >&2

exit "$CLAUDE_EXIT"

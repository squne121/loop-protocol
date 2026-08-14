#!/bin/sh
# scripts/claude-gpt/launch.sh
#
# repository-owned claude-gpt launcher。
#
# herdr session B（ChatGPT Pro Codex subscription 経由 GPT-5.6 Sol/Terra/Luna）を
# `raine/claude-code-proxy` 経由で起動する。Native Claude（herdr session A）とは
# config root / credential / working tree を完全分離する（Issue #2158 / Parent #2154
# アーキテクチャ決定 A〜E 準拠）。
#
# Usage:
#   scripts/claude-gpt/launch.sh [--check-only] [--dry-run] [-- <claude 追加引数...>]
#
#   --check-only  proxy を起動し preflight / bind / model 解決だけ確認して終了する
#                 （`claude` 本体は起動しない。runtime_smoke_test.sh から使う）。
#   --dry-run     ディレクトリ作成・設定ファイル書き込み・proxy 起動を一切行わず、
#                 実行予定の内容を JSON で表示するのみ（scripts/CLAUDE.md 破壊的処理不変条件）。
#
# Exit code:
#   0   起動成功（--check-only 時）/ claude exec に成功（通常時は exec のため戻らない）
#   3   claude-code-proxy バイナリが見つからない
#   4   ChatGPT subscription 認証が利用不能
#   5   canonical path 違反（repo/worktree 配下への書き込みを拒否）
#   6   read 制限 settings が未生成または不正
#   7   proxy 起動失敗（loopback bind を確認できない）

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

CHECK_ONLY=false
DRY_RUN=false
EXTRA_CLAUDE_ARGS=""

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
    --)
      shift
      EXTRA_CLAUDE_ARGS="$*"
      break
      ;;
    *)
      shift
      ;;
  esac
done

CLAUDE_CONFIG_DIR_TARGET=$(claude_gpt_claude_config_dir)
PROXY_CONFIG_DIR_TARGET=$(claude_gpt_proxy_config_dir)
PROXY_STATE_DIR_TARGET=$(claude_gpt_proxy_state_dir)
MCP_CONFIG_PATH=$(claude_gpt_mcp_config_path)
SETTINGS_PATH=$(claude_gpt_session_settings_path)

# --- canonical path safety（ディレクトリ作成前に必ず検証する） ---
for d in "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET"; do
  if ! claude_gpt_reject_if_under_repo "$d" "$SELF_PATH"; then
    echo "SKIP: 対象外" >/dev/null
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"canonical_path_under_repo_or_worktree","path":"%s"}\n' "$d"
    exit 5
  fi
done

if [ "$DRY_RUN" = "true" ]; then
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"dry_run","claude_config_dir":"%s","proxy_config_dir":"%s","proxy_state_dir":"%s","mcp_config_path":"%s","settings_path":"%s"}\n' \
    "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET" "$MCP_CONFIG_PATH" "$SETTINGS_PATH"
  exit 0
fi

# --- GPT 専用ディレクトリを準備する（既存なら idempotent） ---
mkdir -p "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET"

# --- strict_mcp mode 用の空 MCP config を書き込む（repository/user MCP を読み込ませない） ---
STRICT_MCP_MODE=true
cat > "$MCP_CONFIG_PATH" <<MCP_JSON_EOF
{
  "mcpServers": {}
}
MCP_JSON_EOF

# --- Claude Code セッション設定: proxy credential/config/state ディレクトリへの read を拒否する ---
cat > "$SETTINGS_PATH" <<SETTINGS_JSON_EOF
{
  "permissions": {
    "deny": [
      "Read(${PROXY_CONFIG_DIR_TARGET}/**)",
      "Read(${PROXY_STATE_DIR_TARGET}/**)"
    ]
  }
}
SETTINGS_JSON_EOF

# --- preflight 実行（read 制限 settings が生成済みであることを含めて再検証） ---
PREFLIGHT_JSON=$("$SCRIPT_DIR/preflight.sh")
PREFLIGHT_RC=$?
if [ "$PREFLIGHT_RC" -ne 0 ]; then
  echo "$PREFLIGHT_JSON"
  exit "$PREFLIGHT_RC"
fi

# --- proxy 起動用ポートを選ぶ（python3 があれば OS 割当の空きポートを使う） ---
if command -v python3 >/dev/null 2>&1; then
  PROXY_PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
else
  PROXY_PORT=14141
fi

PROXY_LOG="${PROXY_STATE_DIR_TARGET}/launcher-proxy.log"

# --- proxy を allowlist 限定 env で子プロセスとして起動する ---
# 親 shell から継承した CCP_* / HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 等は env -i でリセットし、
# 明示的に組み立てた変数のみを渡す。
env -i \
  "PATH=$PATH" \
  "HOME=$HOME" \
  "CCP_CONFIG_DIR=$PROXY_CONFIG_DIR_TARGET" \
  "XDG_STATE_HOME=$PROXY_STATE_DIR_TARGET" \
  "CCP_BIND_ADDRESS=127.0.0.1" \
  "CCP_LOG_STDERR=1" \
  claude-code-proxy serve --port "$PROXY_PORT" --no-monitor > "$PROXY_LOG" 2>&1 &
PROXY_PID=$!

# --- readiness poll（最大 10 秒） ---
READY=false
i=0
while [ "$i" -lt 20 ]; do
  if curl -s -o /dev/null -m 1 "http://127.0.0.1:${PROXY_PORT}/v1/models"; then
    READY=true
    break
  fi
  i=$((i + 1))
  sleep 0.5
done

if [ "$READY" != "true" ]; then
  kill "$PROXY_PID" 2>/dev/null
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"failed","reason":"proxy_not_ready","port":%s}\n' "$PROXY_PORT"
  exit 7
fi

# --- loopback bind preflight: PID が所有する listen socket の bind address を厳密確認する ---
# 単なる curl 疎通成功だけで判定しない（PID 所有 socket を明示的に確認する）。
BIND_LINE=$(ss -ltnp 2>/dev/null | grep "pid=${PROXY_PID}," || true)
case "$BIND_LINE" in
  *"127.0.0.1:${PROXY_PORT}"*)
    BIND_OK=true
    ;;
  *)
    BIND_OK=false
    ;;
esac

if [ "$BIND_OK" != "true" ]; then
  kill "$PROXY_PID" 2>/dev/null
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"failed","reason":"loopback_bind_not_confirmed","port":%s,"pid":%s}\n' "$PROXY_PORT" "$PROXY_PID"
  exit 7
fi

# --- model alias resolution 確認（live: 実際に起動した proxy の /v1/models から確認する） ---
MODELS_JSON=$(curl -s -m 3 "http://127.0.0.1:${PROXY_PORT}/v1/models")
MODEL_ALIAS_OK=true
for m in "$CLAUDE_GPT_MODEL_MAIN" "$CLAUDE_GPT_MODEL_OPUS" "$CLAUDE_GPT_MODEL_HAIKU"; do
  case "$MODELS_JSON" in
    *"\"$m\""*) : ;;
    *) MODEL_ALIAS_OK=false ;;
  esac
done

if [ "$CHECK_ONLY" = "true" ]; then
  kill "$PROXY_PID" 2>/dev/null
  wait "$PROXY_PID" 2>/dev/null
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"ok","mode":"check_only","port":%s,"bind_ok":%s,"model_alias_ok":%s,"strict_mcp_mode":%s,"mcp_config_path":"%s","settings_path":"%s"}\n' \
    "$PROXY_PORT" "$BIND_OK" "$MODEL_ALIAS_OK" "$STRICT_MCP_MODE" "$MCP_CONFIG_PATH" "$SETTINGS_PATH"
  if [ "$BIND_OK" = "true" ] && [ "$MODEL_ALIAS_OK" = "true" ]; then
    exit 0
  fi
  exit 7
fi

# --- 通常起動: claude 本体へ exec する（proxy は背後プロセスとして残る） ---
trap 'kill "$PROXY_PID" 2>/dev/null' EXIT

CLAUDE_CODE_SUBAGENT_MODEL_UNSET=1
unset CLAUDE_CODE_SUBAGENT_MODEL

export CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR_TARGET"
export ANTHROPIC_BASE_URL="http://127.0.0.1:${PROXY_PORT}"
export ANTHROPIC_AUTH_TOKEN="claude-gpt-local"
export ANTHROPIC_MODEL="$CLAUDE_GPT_MODEL_MAIN"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLAUDE_GPT_MODEL_OPUS"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$CLAUDE_GPT_MODEL_SONNET"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLAUDE_GPT_MODEL_HAIKU"

# strict_mcp mode: repository/user MCP を一切読み込まない。--strict-mcp-config +
# 空の mcp-config JSON（$MCP_CONFIG_PATH）の組み合わせで実現する。
STRICT_MCP_MODE=true
export STRICT_MCP_MODE

# shellcheck disable=SC2086
exec claude --strict-mcp-config --mcp-config "$MCP_CONFIG_PATH" --settings "$SETTINGS_PATH" $EXTRA_CLAUDE_ARGS

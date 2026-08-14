#!/bin/sh
# scripts/claude-gpt/preflight.sh
#
# claude-gpt launcher の起動前 preflight。単体実行可能。
# `launch.sh` と `runtime_smoke_test.sh` の双方から呼ばれる。
#
# チェック内容（Issue #2158 In Scope 準拠）:
#   1. `claude-code-proxy` バイナリの存在
#   2. ChatGPT subscription 認証状態（`claude-code-proxy codex auth status`）
#   3. GPT 専用ディレクトリ（CLAUDE_CONFIG_DIR / proxy config / state）の canonical path が
#      repository root / worktree 配下でないこと
#   4. Claude Code セッション設定（settings.local.json）で proxy credential/config/state
#      ディレクトリへの read を拒否する deny rule が生成されていること
#
# 出力: 構造化 JSON を stdout に返す（scripts/CLAUDE.md 不変条件準拠）。
#
# Exit code:
#   0  = 全チェック PASS
#   3  = claude-code-proxy バイナリが見つからない（環境不可）
#   4  = ChatGPT subscription 認証が利用不能（環境不可）
#   5  = canonical path 違反（repo/worktree 配下への書き込みを拒否）
#   6  = read 制限 settings が未生成または不正
#
# 3 / 4 は「実行環境が利用不能」を意味し、呼び出し元（runtime_smoke_test.sh）はこれを
# SKIP（exit 77）に変換してよい。5 / 6 は launcher の実装バグであり SKIP に変換してはならない。

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

# --env-only: バイナリ存在 + ChatGPT subscription 認証のみ確認する（起動前の環境可用性判定用）。
# runtime_smoke_test.sh の SKIP 判定はディレクトリ/設定ファイルがまだ存在しない段階で行うため、
# canonical path 検証・read 制限 settings 検証（launch.sh がディレクトリ/設定を作成した後にのみ
# 意味を持つチェック）を除外したこのモードを使う。
ENV_ONLY=false
if [ "${1:-}" = "--env-only" ]; then
  ENV_ONLY=true
fi

CLAUDE_CONFIG_DIR_TARGET=$(claude_gpt_claude_config_dir)
PROXY_CONFIG_DIR_TARGET=$(claude_gpt_proxy_config_dir)
PROXY_STATE_DIR_TARGET=$(claude_gpt_proxy_state_dir)
SETTINGS_PATH=$(claude_gpt_session_settings_path)

BINARY_OK=false
AUTH_OK=false
AUTH_DETAIL="not_checked"
PATH_OK=false
READ_RESTRICTION_OK=false
EXIT_CODE=0

# --- 1. バイナリ存在確認 ---
if command -v claude-code-proxy >/dev/null 2>&1; then
  BINARY_OK=true
else
  EXIT_CODE=3
fi

# --- 2. ChatGPT subscription 認証状態確認 ---
if [ "$BINARY_OK" = "true" ]; then
  AUTH_STATUS_OUTPUT=$(CCP_CONFIG_DIR="$PROXY_CONFIG_DIR_TARGET" claude-code-proxy codex auth status 2>&1)
  AUTH_STATUS_RC=$?
  if [ "$AUTH_STATUS_RC" -eq 0 ]; then
    case "$AUTH_STATUS_OUTPUT" in
      *Account:*)
        AUTH_OK=true
        AUTH_DETAIL="authenticated"
        ;;
      *)
        AUTH_DETAIL="unexpected_status_output"
        if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=4; fi
        ;;
    esac
  else
    AUTH_DETAIL="not_authenticated"
    if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=4; fi
  fi
else
  AUTH_DETAIL="skipped_binary_missing"
fi

if [ "$ENV_ONLY" = "true" ]; then
  # --env-only ではディレクトリ/設定ファイルがまだ存在しない前提のため、
  # canonical path / read 制限チェックはスキップし ok 扱いにする（launch.sh が後で完全検証する）。
  PATH_OK=true
  READ_RESTRICTION_OK=true
else
  # --- 3. canonical path 検証（repo / worktree 配下でないこと） ---
  PATH_VIOLATIONS=""
  for d in "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET"; do
    if ! claude_gpt_reject_if_under_repo "$d" "$SELF_PATH"; then
      PATH_VIOLATIONS="${PATH_VIOLATIONS} ${d}"
    fi
  done
  if [ -z "$PATH_VIOLATIONS" ]; then
    PATH_OK=true
  else
    if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=5; fi
  fi

  # --- 4. read 制限 settings.local.json の存在・内容確認 ---
  # launch.sh が事前に生成している前提。未生成なら preflight は fail-closed で失敗させる
  # （生成は launch.sh の責務。preflight は「有効化されていることの確認」のみ行う）。
  if [ -f "$SETTINGS_PATH" ] \
    && grep -q "Read(${PROXY_CONFIG_DIR_TARGET}" "$SETTINGS_PATH" 2>/dev/null \
    && grep -q "Read(${PROXY_STATE_DIR_TARGET}" "$SETTINGS_PATH" 2>/dev/null; then
    READ_RESTRICTION_OK=true
  else
    if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=6; fi
  fi
fi

cat <<JSON_EOF
{
  "schema": "CLAUDE_GPT_PREFLIGHT_RESULT_V1",
  "env_only": ${ENV_ONLY},
  "binary_available": ${BINARY_OK},
  "chatgpt_auth": {
    "available": ${AUTH_OK},
    "detail": "${AUTH_DETAIL}"
  },
  "canonical_paths": {
    "ok": ${PATH_OK},
    "claude_config_dir": "${CLAUDE_CONFIG_DIR_TARGET}",
    "proxy_config_dir": "${PROXY_CONFIG_DIR_TARGET}",
    "proxy_state_dir": "${PROXY_STATE_DIR_TARGET}",
    "violations": "${PATH_VIOLATIONS}"
  },
  "read_restriction": {
    "ok": ${READ_RESTRICTION_OK},
    "settings_path": "${SETTINGS_PATH}"
  },
  "exit_code": ${EXIT_CODE}
}
JSON_EOF

exit "$EXIT_CODE"

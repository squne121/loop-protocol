#!/bin/sh
# scripts/claude-gpt/preflight.sh
#
# claude-gpt launcher の起動前 preflight。単体実行可能。
# `launch.sh` と `runtime_smoke_test.sh` の双方から呼ばれる。
#
# チェック内容（Issue #2158 In Scope / Scope Reframe 2026-08-15 反映）:
#   1. `claude-code-proxy` バイナリの存在
#   2. ChatGPT subscription 認証状態（proxy 専用 HOME/CCP_CONFIG_DIR で確認。P0-2）
#   3. GPT 専用ディレクトリ（CLAUDE_CONFIG_DIR / proxy config / state / proxy home）の
#      canonical path が repository root / worktree 配下でないこと
#   4. Claude Code セッション設定（settings.local.json）で proxy credential/config/state
#      ディレクトリへの read を拒否する deny rule が正しい絶対パス構文（`Read(//...)`)
#      で生成されていること（best-effort な軽量防御。P0-3）
#
# 出力: 構造化 JSON を stdout に返す（scripts/CLAUDE.md 不変条件準拠）。
# `canonical_paths` / `read_restriction` は `--env-only` モードでは実検査を行わない
# （ディレクトリ・設定ファイルがまだ存在しない前提のため）。この場合 `applicable: false`
# を返し、`ok` を無条件 true として出力しない（PR #2162 P0-1 反映：未検証値を検証済み
# であるかのように証跡へ混入させない）。
#
# Exit code:
#   0  = 全チェック PASS
#   3  = claude-code-proxy バイナリが見つからない（環境不可）
#   4  = ChatGPT subscription 認証が利用不能（環境不可）
#   5  = canonical path 違反（repo/worktree 配下への書き込みを拒否）
#   6  = read 制限 settings が未生成または不正
#
# 3 / 4 は「実行環境が利用不能」を意味し、呼び出し元（runtime_smoke_test.sh）はこれを
# SKIP（exit 77）に変換してよい。5 / 6 は launcher の実装バグまたは host 側の不備であり
# SKIP に変換してはならない。

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

# --env-only: バイナリ存在 + ChatGPT subscription 認証のみ確認する（起動前の環境可用性判定用）。
# runtime_smoke_test.sh の SKIP 判定はディレクトリ/設定ファイルがまだ存在しない段階で行うため、
# canonical path 検証・read 制限 settings 検証（launch.sh がディレクトリ/設定を作成した後に
# のみ意味を持つチェック）を除外したこのモードを使う。
ENV_ONLY=false
if [ "${1:-}" = "--env-only" ]; then
  ENV_ONLY=true
fi

# --auto-mode-check <settings_path>: Issue #2203 AC1。launcher-generated settings の
# autoMode 設定が effective config に正しく反映されているかを `claude auto-mode config`
# / `claude auto-mode defaults` の readback で検証する独立モード。既存の完全モード
# （引数なし）の exit code 契約（0/3/4/5/6）や、それを subprocess 経由で駆動する既存
# hermetic test（test_launch_strict_mcp_config_normalization.py 等、fake claude binary
# を使う）を汚染しないよう、既定パス（launch.sh からの通常呼び出し）には組み込まず、
# 明示的な opt-in サブコマンドとして分離する（真の起動フローへの配線は followup とする）。
#
# Exit code:
#   0  = auto-mode readback 成功、$defaults 保持・narrow scope 反映・classifyAllShell
#        有効を確認
#   2  = 呼び出しエラー（settings_path 未指定・不存在）
#   3  = claude バイナリが見つからない（環境不可）
#   8  = auto-mode 未対応 version・readback mismatch・classifyAllShell 未反映
#        （launcher バグまたは host 側の不備。fail-closed）
if [ "${1:-}" = "--auto-mode-check" ]; then
  AUTO_MODE_SETTINGS_PATH="${2:-}"
  if [ -z "$AUTO_MODE_SETTINGS_PATH" ] || [ ! -f "$AUTO_MODE_SETTINGS_PATH" ]; then
    printf '{"schema":"CLAUDE_GPT_AUTO_MODE_PREFLIGHT_RESULT_V1","status":"blocked","reason":"settings_path_missing_or_not_found"}\n'
    exit 2
  fi
  CLAUDE_BIN_FOR_CHECK=$(claude_gpt_resolve_claude_bin)
  if [ -z "$CLAUDE_BIN_FOR_CHECK" ]; then
    printf '{"schema":"CLAUDE_GPT_AUTO_MODE_PREFLIGHT_RESULT_V1","status":"blocked","reason":"claude_binary_not_found"}\n'
    exit 3
  fi
  claude_gpt_auto_mode_readback "$CLAUDE_BIN_FOR_CHECK" "$AUTO_MODE_SETTINGS_PATH"
  exit "$?"
fi

CLAUDE_CONFIG_DIR_TARGET=$(claude_gpt_claude_config_dir)
PROXY_CONFIG_DIR_TARGET=$(claude_gpt_proxy_config_dir)
PROXY_STATE_DIR_TARGET=$(claude_gpt_proxy_state_dir)
PROXY_HOME_TARGET=$(claude_gpt_proxy_home_dir)
SETTINGS_PATH=$(claude_gpt_session_settings_path)

BINARY_OK=false
AUTH_OK=false
AUTH_DETAIL="not_checked"
PATH_OK=false
PATH_APPLICABLE=false
PATH_VIOLATIONS=""
READ_RESTRICTION_OK=false
READ_RESTRICTION_APPLICABLE=false
EXIT_CODE=0

# --- 1. バイナリ存在確認（P2: absolute path を一度解決し以降すべて同一値を使い回す） ---
PROXY_BIN=$(claude_gpt_resolve_proxy_bin)
PROXY_VERSION="unknown"
if [ -n "$PROXY_BIN" ]; then
  BINARY_OK=true
  PROXY_VERSION=$(claude_gpt_proxy_version "$PROXY_BIN")
else
  EXIT_CODE=3
fi

# --- proxy 専用 HOME を用意する（credential isolation の前提。P0-2） ---
# canonical path 違反チェックより前に mkdir すると repo/worktree 配下へ誤って書き込む
# 危険があるため、先に canonical チェックだけ実施し、違反でなければ作る。
if ! claude_gpt_reject_if_under_repo "$PROXY_HOME_TARGET" "$SELF_PATH"; then
  PROXY_HOME_UNDER_REPO=true
else
  PROXY_HOME_UNDER_REPO=false
  mkdir -p "$PROXY_HOME_TARGET" 2>/dev/null || true
fi

# --- 2. ChatGPT subscription 認証状態確認（proxy 専用 HOME/CCP_CONFIG_DIR で確認。P0-2） ---
if [ "$BINARY_OK" = "true" ] && [ "$PROXY_HOME_UNDER_REPO" = "false" ]; then
  AUTH_STATUS_OUTPUT=$(HOME="$PROXY_HOME_TARGET" CCP_CONFIG_DIR="$PROXY_CONFIG_DIR_TARGET" "$PROXY_BIN" codex auth status 2>&1)
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
elif [ "$BINARY_OK" != "true" ]; then
  AUTH_DETAIL="skipped_binary_missing"
else
  AUTH_DETAIL="skipped_proxy_home_under_repo"
  if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=5; fi
fi

if [ "$ENV_ONLY" = "true" ]; then
  # --env-only ではディレクトリ/設定ファイルがまだ存在しない前提のため、canonical path /
  # read 制限 / sandbox チェックは実施しない。「未検証」であることを applicable:false で
  # 明示し、無条件 true の ok を返さない（P0-1）。
  PATH_APPLICABLE=false
  READ_RESTRICTION_APPLICABLE=false
else
  # --- 3. canonical path 検証（repo / worktree 配下でないこと。proxy home も含む） ---
  PATH_APPLICABLE=true
  for d in "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET" "$PROXY_HOME_TARGET"; do
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
  # `Read(//<absolute path>/**)` の二重スラッシュ絶対パス構文であることまで確認する（P0-3）。
  READ_RESTRICTION_APPLICABLE=true
  READ_RESTRICTION_OK=true
  if [ ! -f "$SETTINGS_PATH" ]; then
    READ_RESTRICTION_OK=false
  fi
  if ! grep -q "Read(/${PROXY_CONFIG_DIR_TARGET}" "$SETTINGS_PATH" 2>/dev/null; then
    READ_RESTRICTION_OK=false
  fi
  if ! grep -q "Read(/${PROXY_STATE_DIR_TARGET}" "$SETTINGS_PATH" 2>/dev/null; then
    READ_RESTRICTION_OK=false
  fi
  if ! grep -q "Read(/${PROXY_HOME_TARGET}" "$SETTINGS_PATH" 2>/dev/null; then
    READ_RESTRICTION_OK=false
  fi
  if [ "$READ_RESTRICTION_OK" != "true" ]; then
    if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=6; fi
  fi
fi

cat <<JSON_EOF
{
  "schema": "CLAUDE_GPT_PREFLIGHT_RESULT_V1",
  "env_only": ${ENV_ONLY},
  "binary_available": ${BINARY_OK},
  "proxy": {
    "absolute_path": "${PROXY_BIN}",
    "version": "${PROXY_VERSION}"
  },
  "chatgpt_auth": {
    "available": ${AUTH_OK},
    "detail": "${AUTH_DETAIL}"
  },
  "canonical_paths": {
    "applicable": ${PATH_APPLICABLE},
    "ok": ${PATH_OK},
    "claude_config_dir": "${CLAUDE_CONFIG_DIR_TARGET}",
    "proxy_config_dir": "${PROXY_CONFIG_DIR_TARGET}",
    "proxy_state_dir": "${PROXY_STATE_DIR_TARGET}",
    "proxy_home_dir": "${PROXY_HOME_TARGET}",
    "violations": "${PATH_VIOLATIONS}"
  },
  "read_restriction": {
    "applicable": ${READ_RESTRICTION_APPLICABLE},
    "ok": ${READ_RESTRICTION_OK},
    "settings_path": "${SETTINGS_PATH}"
  },
  "exit_code": ${EXIT_CODE}
}
JSON_EOF

exit "$EXIT_CODE"

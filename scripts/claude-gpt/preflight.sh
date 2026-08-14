#!/bin/sh
# scripts/claude-gpt/preflight.sh
#
# claude-gpt launcher の起動前 preflight。単体実行可能。
# `launch.sh` と `runtime_smoke_test.sh` の双方から呼ばれる。
#
# チェック内容（Issue #2158 In Scope / PR #2162 OWNER REQUEST_CHANGES 反映）:
#   1. `claude-code-proxy` バイナリの存在
#   2. ChatGPT subscription 認証状態（proxy 専用 HOME/CCP_CONFIG_DIR で確認。P0-2）
#   3. GPT 専用ディレクトリ（CLAUDE_CONFIG_DIR / proxy config / state / proxy home）の
#      canonical path が repository root / worktree 配下でないこと
#   4. Claude Code セッション設定（settings.local.json）で proxy credential/config/state
#      ディレクトリへの read を拒否する deny rule が正しい絶対パス構文（`Read(//...)`)
#      で生成されていること（P0-3）
#   5. sandbox（bubblewrap）が実機で初期化可能であること（P0-3。--env-only では実施しない）
#   6. isolated proxy principal（CLAUDE_GPT_ISOLATED_PROXY_USER）が設定されている場合、
#      dedicated Unix user が provisioning 済みであること（P0-3 段階対応。opt-in。
#      --env-only では実施しない。scripts/claude-gpt/provision_proxy_principal.sh 参照）
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
#   8  = sandbox 初期化に失敗した（環境不可ではなく launcher/host 側の不備。SKIP に変換しない）
#   9  = CLAUDE_GPT_ISOLATED_PROXY_USER が設定されているが provisioning が未完了
#        （isolated_proxy_user_not_provisioned。silent fallback しない）
#
# 3 / 4 は「実行環境が利用不能」を意味し、呼び出し元（runtime_smoke_test.sh）はこれを
# SKIP（exit 77）に変換してよい。5 / 6 / 8 は launcher の実装バグまたは host 側の不備であり
# SKIP に変換してはならない。

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

# --env-only: バイナリ存在 + ChatGPT subscription 認証のみ確認する（起動前の環境可用性判定用）。
# runtime_smoke_test.sh の SKIP 判定はディレクトリ/設定ファイルがまだ存在しない段階で行うため、
# canonical path 検証・read 制限 settings 検証・sandbox 初期化検証（launch.sh がディレクトリ/
# 設定を作成した後にのみ意味を持つチェック）を除外したこのモードを使う。
ENV_ONLY=false
if [ "${1:-}" = "--env-only" ]; then
  ENV_ONLY=true
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
SANDBOX_OK=false
SANDBOX_APPLICABLE=false
EXIT_CODE=0

# --- 1. バイナリ存在確認 ---
if command -v claude-code-proxy >/dev/null 2>&1; then
  BINARY_OK=true
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
  AUTH_STATUS_OUTPUT=$(HOME="$PROXY_HOME_TARGET" CCP_CONFIG_DIR="$PROXY_CONFIG_DIR_TARGET" claude-code-proxy codex auth status 2>&1)
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
  SANDBOX_APPLICABLE=false
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
  # sandbox.enabled はハードニングが opt-in（CLAUDE_GPT_HARDENED_SANDBOX=true）の場合
  # のみ必須とする（既定では launch.sh が settings.local.json に sandbox ブロックを書かない）。
  if [ "${CLAUDE_GPT_HARDENED_SANDBOX:-false}" = "true" ]; then
    if ! grep -q '"enabled": *true' "$SETTINGS_PATH" 2>/dev/null; then
      READ_RESTRICTION_OK=false
    fi
  fi
  if [ "$READ_RESTRICTION_OK" != "true" ]; then
    if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=6; fi
  fi

  # --- 5. sandbox 初期化 preflight（P0-3） ---
  # CLAUDE_GPT_HARDENED_SANDBOX（既定 false）が有効な場合のみ検証する。実機検証（PR #2162,
  # 2026-08-14）で、`sandbox.enabled: true` と `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` は
  # いずれも単独で、launcher 自体がすでにネストした bwrap sandbox 環境下で実行されている
  # 場合に Claude Code 本体の Bash tool 実行を `Maximum call stack size exceeded` で
  # 完全に破壊することを確認した（bwrap 単体の起動自体は成功するため、単純な bwrap
  # self-test だけではこの非互換を検出できない）。Bash tool という中核機能を破壊する
  # hardening を既定で有効化する方が実害が大きいため、既定は無効・opt-in とする。
  # Issue #2173 で実機検証済み: ネスト実行環境下で sandbox.enabled: true /
  # CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 は Bash tool 呼び出しを応答なしのビジーループへ
  # 陥らせることを 2/2, 1/1 の再現率で確認し、既定 opt-in 無効を維持する判断を確定した
  # （docs/dev/claude-gpt-sandbox-hardening-verification.md 参照）。
  if [ "${CLAUDE_GPT_HARDENED_SANDBOX:-false}" = "true" ]; then
    SANDBOX_APPLICABLE=true
    if claude_gpt_check_sandbox_init; then
      SANDBOX_OK=true
    else
      if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=8; fi
    fi
  else
    SANDBOX_APPLICABLE=false
  fi
fi

# --- 6. isolated proxy principal 検証（P0-3 段階対応。opt-in。CLAUDE_GPT_ISOLATED_PROXY_USER
#     が未設定の場合は既存の same-uid 動作を維持する。設定されている場合は
#     scripts/claude-gpt/provision_proxy_principal.sh による事前 provisioning
#     （dedicated Unix user 作成・ディレクトリ chown・sudoers テンプレート適用）が
#     完了していることを (a) user 存在, (b) passwordless sudo 構成, (c) proxy
#     credential/config/state/home ディレクトリの所有者、の3点で検証する。
#     いずれか失敗時は silent fallback せず fail-closed で止める。 ---
ISOLATED_PROXY_USER="${CLAUDE_GPT_ISOLATED_PROXY_USER:-}"
CRED_ISO_MODE="same_uid"
CRED_ISO_APPLICABLE=false
CRED_ISO_OK=false
CRED_ISO_DETAIL="not_configured"

if [ -n "$ISOLATED_PROXY_USER" ]; then
  CRED_ISO_MODE="isolated_proxy_user"
  if [ "$ENV_ONLY" = "true" ]; then
    CRED_ISO_DETAIL="skipped_env_only"
  else
    CRED_ISO_APPLICABLE=true
    USER_EXISTS=false
    if id "$ISOLATED_PROXY_USER" >/dev/null 2>&1; then
      USER_EXISTS=true
    fi
    SUDO_OK=false
    if [ "$USER_EXISTS" = "true" ] && sudo -n -u "$ISOLATED_PROXY_USER" true >/dev/null 2>&1; then
      SUDO_OK=true
    fi
    DIR_OWNER_OK=false
    if [ "$USER_EXISTS" = "true" ]; then
      DIR_OWNER_OK=true
      for d in "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET" "$PROXY_HOME_TARGET"; do
        if [ -d "$d" ]; then
          DIR_OWNER=$(stat -c '%U' "$d" 2>/dev/null || stat -f '%Su' "$d" 2>/dev/null || echo "")
          if [ "$DIR_OWNER" != "$ISOLATED_PROXY_USER" ]; then
            DIR_OWNER_OK=false
          fi
        else
          DIR_OWNER_OK=false
        fi
      done
    fi

    if [ "$USER_EXISTS" = "true" ] && [ "$SUDO_OK" = "true" ] && [ "$DIR_OWNER_OK" = "true" ]; then
      CRED_ISO_OK=true
      CRED_ISO_DETAIL="provisioned"
    else
      CRED_ISO_DETAIL="isolated_proxy_user_not_provisioned"
      if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=9; fi
    fi
  fi
else
  CRED_ISO_MODE="same_uid"
  if [ "$ENV_ONLY" = "true" ]; then
    CRED_ISO_APPLICABLE=false
    CRED_ISO_DETAIL="skipped_env_only"
  else
    CRED_ISO_APPLICABLE=true
    CRED_ISO_OK=true
    CRED_ISO_DETAIL="same_uid_default"
  fi
fi

CRED_ISO_ISOLATED=false
if [ "$CRED_ISO_MODE" = "isolated_proxy_user" ] && [ "$CRED_ISO_OK" = "true" ]; then
  CRED_ISO_ISOLATED=true
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
  "sandbox_init": {
    "applicable": ${SANDBOX_APPLICABLE},
    "ok": ${SANDBOX_OK}
  },
  "credential_isolation": {
    "mode": "${CRED_ISO_MODE}",
    "isolated": ${CRED_ISO_ISOLATED},
    "applicable": ${CRED_ISO_APPLICABLE},
    "ok": ${CRED_ISO_OK},
    "detail": "${CRED_ISO_DETAIL}"
  },
  "exit_code": ${EXIT_CODE}
}
JSON_EOF

exit "$EXIT_CODE"

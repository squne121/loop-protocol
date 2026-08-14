#!/bin/sh
# scripts/claude-gpt/provision_proxy_principal.sh
#
# claude-gpt proxy 用の dedicated Unix user(isolated proxy principal)を provisioning
# するための root 専用スクリプト。P0-3(credential isolation の未解決部分。独立レビュー指摘)
# への段階的対応。真の分離(同一 UID 上の任意 subprocess からの credential 保護)には
# dedicated user が必要という調査結論に基づく。
#
# Issue #2158 / #2173, Parent #2154 参照。
#
# 重要(このスクリプトの実行境界):
#   - このスクリプトは root 権限で「一度だけ」手動実行する想定であり、launch.sh /
#     preflight.sh / lib.sh のいずれからも自動実行されない。
#   - このセッション(Claude Code agent session)はこのスクリプトを実行しない。
#     sudo/root 権限昇格の実実行は Stop Condition であり、OWNER が自身の判断で
#     実行するまで保留する。
#   - sudoers ルールは「このスクリプトが直接書き込むことは絶対にしない」。
#     --apply-sudoers を指定しない限り、生成した sudoers テンプレートを標準出力
#     (または --sudoers-out で指定したファイル)へ出力するだけの dry-run 動作とする。
#     --apply-sudoers を指定した場合でも、visudo -c での構文検証を経てからのみ
#     /etc/sudoers.d/ へ書き込む(誤った sudoers 内容でシステムの sudo 自体を破壊しない
#     ための最終防御)。
#
# Usage:
#   sudo scripts/claude-gpt/provision_proxy_principal.sh [--dry-run] [--user <name>]
#     [--sudoers-out <path>] [--apply-sudoers] [--invoking-user <name>]
#
#   --dry-run            useradd / mkdir / chown / chmod を一切実行せず、実行予定の
#                        コマンド列を表示するのみ(指定しない場合は実際に provisioning する)。
#   --user <name>        dedicated user 名(既定: claude-gpt-proxy)。
#   --sudoers-out <path> sudoers テンプレートの出力先(既定: 標準出力のみ)。
#   --apply-sudoers      生成した sudoers テンプレートを visudo -c 検証の上で
#                        /etc/sudoers.d/<user> へ実際に書き込む。指定しない限り
#                        書き込みは一切行わない(既定は dry-run 相当)。
#   --invoking-user <name> sudoers ルールで NOPASSWD 実行を許可する呼び出し元 user
#                          (既定: $SUDO_USER、未設定なら実行時に必須指定を要求する)。
#
# Exit code:
#   0   正常終了(--dry-run 時は「実行計画の表示」が成功したことを意味する)
#   1   引数エラー
#   2   root 権限で実行されていない
#   3   useradd 等の provisioning コマンドが失敗した
#   4   sudoers テンプレートの visudo 構文検証に失敗した(--apply-sudoers 時のみ到達しうる)

set -eu

PROXY_USER="claude-gpt-proxy"
DRY_RUN=false
SUDOERS_OUT=""
APPLY_SUDOERS=false
INVOKING_USER="${SUDO_USER:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --user)
      if [ $# -lt 2 ]; then
        echo "error: --user requires a value" >&2
        exit 1
      fi
      PROXY_USER="$2"
      shift 2
      ;;
    --sudoers-out)
      if [ $# -lt 2 ]; then
        echo "error: --sudoers-out requires a value" >&2
        exit 1
      fi
      SUDOERS_OUT="$2"
      shift 2
      ;;
    --apply-sudoers)
      APPLY_SUDOERS=true
      shift
      ;;
    --invoking-user)
      if [ $# -lt 2 ]; then
        echo "error: --invoking-user requires a value" >&2
        exit 1
      fi
      INVOKING_USER="$2"
      shift 2
      ;;
    *)
      echo "error: unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# --- root 権限確認 ---
if [ "$(id -u)" != "0" ]; then
  echo "error: this script must be run as root (e.g. via sudo). Refusing to continue." >&2
  exit 2
fi

if [ -z "$INVOKING_USER" ]; then
  echo "error: --invoking-user is required when SUDO_USER is not set (needed to scope the" >&2
  echo "  generated sudoers template to a specific caller)." >&2
  exit 1
fi

PROXY_HOME_BASE="/home/${PROXY_USER}"
# NOTE: このスクリプトが管理する対象は dedicated user 自身の HOME(nologin のため実質未使用)
# ではなく、あくまで isolation 対象ディレクトリ(呼び出し元ユーザーの $HOME/.claude-gpt/*)を
# dedicated user が所有する状態にすることが目的である。
INVOKING_HOME=$(getent passwd "$INVOKING_USER" 2>/dev/null | cut -d: -f6 || true)
if [ -z "$INVOKING_HOME" ]; then
  INVOKING_HOME="/home/${INVOKING_USER}"
fi
CLAUDE_GPT_HOME="${INVOKING_HOME}/.claude-gpt"
PROXY_CONFIG_DIR="${CLAUDE_GPT_HOME}/proxy-config"
PROXY_STATE_DIR="${CLAUDE_GPT_HOME}/state"
PROXY_HOME_DIR="${CLAUDE_GPT_HOME}/proxy-home"

echo "=== claude-gpt isolated proxy principal provisioning plan ===" >&2
echo "  dedicated user     : ${PROXY_USER}" >&2
echo "  invoking user       : ${INVOKING_USER}" >&2
echo "  invoking user home  : ${INVOKING_HOME}" >&2
echo "  target directories  :" >&2
echo "    ${PROXY_CONFIG_DIR}" >&2
echo "    ${PROXY_STATE_DIR}" >&2
echo "    ${PROXY_HOME_DIR}" >&2
echo "  dry_run              : ${DRY_RUN}" >&2
echo "" >&2

PLANNED_COMMANDS="\
id -u ${PROXY_USER} >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ${PROXY_USER}
mkdir -p ${PROXY_CONFIG_DIR} ${PROXY_STATE_DIR} ${PROXY_HOME_DIR}
chown -R ${PROXY_USER}:${PROXY_USER} ${PROXY_CONFIG_DIR} ${PROXY_STATE_DIR} ${PROXY_HOME_DIR}
chmod 700 ${PROXY_CONFIG_DIR} ${PROXY_STATE_DIR} ${PROXY_HOME_DIR}"

echo "=== planned provisioning commands ===" >&2
printf '%s\n' "$PLANNED_COMMANDS" >&2
echo "" >&2

if [ "$DRY_RUN" = "true" ]; then
  echo "dry-run: no changes applied." >&2
else
  if ! id -u "$PROXY_USER" >/dev/null 2>&1; then
    if ! useradd --system --no-create-home --shell /usr/sbin/nologin "$PROXY_USER"; then
      echo "error: useradd failed for ${PROXY_USER}" >&2
      exit 3
    fi
  fi
  if ! mkdir -p "$PROXY_CONFIG_DIR" "$PROXY_STATE_DIR" "$PROXY_HOME_DIR"; then
    echo "error: mkdir failed for proxy directories" >&2
    exit 3
  fi
  if ! chown -R "${PROXY_USER}:${PROXY_USER}" "$PROXY_CONFIG_DIR" "$PROXY_STATE_DIR" "$PROXY_HOME_DIR"; then
    echo "error: chown failed for proxy directories" >&2
    exit 3
  fi
  if ! chmod 700 "$PROXY_CONFIG_DIR" "$PROXY_STATE_DIR" "$PROXY_HOME_DIR"; then
    echo "error: chmod failed for proxy directories" >&2
    exit 3
  fi
  echo "provisioning applied." >&2
fi

# --- sudoers テンプレート生成(dry-run 方式。このスクリプト自体は自動で書き込まない) ---
# NOPASSWD 対象は claude-code-proxy serve 起動コマンドに厳密限定する。
# 汎用 sudo 昇格や任意コマンド実行は許可しない。
SUDOERS_TEMPLATE="# /etc/sudoers.d/${PROXY_USER}
# Generated by scripts/claude-gpt/provision_proxy_principal.sh (dry-run template).
# Review carefully before applying. Restricts ${INVOKING_USER} to launching the
# claude-code-proxy server process as ${PROXY_USER}, and nothing else.
${INVOKING_USER} ALL=(${PROXY_USER}) NOPASSWD: /usr/bin/env -i * claude-code-proxy serve *
Defaults!/usr/bin/env -i * claude-code-proxy serve * !requiretty"

echo "=== sudoers template (NOT applied unless --apply-sudoers is given) ===" >&2
if [ -n "$SUDOERS_OUT" ]; then
  printf '%s\n' "$SUDOERS_TEMPLATE" > "$SUDOERS_OUT"
  echo "template written to: ${SUDOERS_OUT}" >&2
else
  printf '%s\n' "$SUDOERS_TEMPLATE"
fi

if [ "$APPLY_SUDOERS" = "true" ]; then
  TMP_SUDOERS=$(mktemp)
  printf '%s\n' "$SUDOERS_TEMPLATE" > "$TMP_SUDOERS"
  if ! visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
    echo "error: generated sudoers template failed visudo syntax validation. Refusing to apply." >&2
    rm -f "$TMP_SUDOERS"
    exit 4
  fi
  install -m 0440 "$TMP_SUDOERS" "/etc/sudoers.d/${PROXY_USER}"
  rm -f "$TMP_SUDOERS"
  echo "sudoers rule applied to: /etc/sudoers.d/${PROXY_USER}" >&2
else
  echo "sudoers rule NOT applied (pass --apply-sudoers to write it, after manual review)." >&2
fi

echo "" >&2
echo "Next steps (manual, OWNER-performed):" >&2
echo "  1. Review the sudoers template above." >&2
echo "  2. If not using --apply-sudoers, apply it manually: visudo -c -f <file> && install -m 0440 <file> /etc/sudoers.d/${PROXY_USER}" >&2
echo "  3. Set CLAUDE_GPT_ISOLATED_PROXY_USER=${PROXY_USER} in the invoking shell/environment." >&2
echo "  4. Re-run scripts/claude-gpt/preflight.sh to confirm credential_isolation.ok=true." >&2

exit 0

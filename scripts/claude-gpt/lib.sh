#!/bin/sh
# scripts/claude-gpt/lib.sh
#
# claude-gpt launcher の共有関数ライブラリ。POSIX sh 準拠、外部ライブラリ依存なし。
# `launch.sh` / `preflight.sh` / `runtime_smoke_test.sh` から `. lib.sh` で source される。
# 単体実行は想定しない（source only）。
#
# Issue #2158 / Parent #2154 「アーキテクチャ決定（A〜E）」「GPT Launcher Contract」参照。

# --- 定数（GPT 専用ディレクトリ分離。CLAUDE_CONFIG_DIR / CCP_CONFIG_DIR / XDG_STATE_HOME 相当） ---

# CLAUDE_GPT_HOME を上書きしない限り $HOME/.claude-gpt を使う。
: "${CLAUDE_GPT_HOME:=${HOME}/.claude-gpt}"

claude_gpt_claude_config_dir() {
  printf '%s/claude\n' "$CLAUDE_GPT_HOME"
}

claude_gpt_proxy_config_dir() {
  printf '%s/proxy-config\n' "$CLAUDE_GPT_HOME"
}

claude_gpt_proxy_state_dir() {
  printf '%s/state\n' "$CLAUDE_GPT_HOME"
}

claude_gpt_mcp_config_path() {
  printf '%s/mcp-empty.json\n' "$(claude_gpt_claude_config_dir)"
}

claude_gpt_session_settings_path() {
  printf '%s/settings.local.json\n' "$(claude_gpt_claude_config_dir)"
}

claude_gpt_evidence_dir() {
  # スクリプト自身の場所からリポジトリ内の scripts/claude-gpt/.evidence を解決する
  script_dir=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd -P)
  printf '%s/.evidence\n' "$script_dir"
}

# --- Model alias mapping（Parent #2154 アーキテクチャ決定 E 準拠） ---
# opus -> gpt-5.6-sol / sonnet -> gpt-5.6-terra（main 推奨） / haiku -> gpt-5.6-luna
CLAUDE_GPT_MODEL_MAIN="gpt-5.6-terra"
CLAUDE_GPT_MODEL_OPUS="gpt-5.6-sol"
CLAUDE_GPT_MODEL_SONNET="gpt-5.6-terra"
CLAUDE_GPT_MODEL_HAIKU="gpt-5.6-luna"

# --- Canonical path safety check ---
#
# repository root / worktree 配下でないことを canonical path（symlink 解決後）で拒否する。
# 戻り値: 0 = 安全（repo/worktree 配下でない）、1 = 危険（repo/worktree 配下）
#
# 引数1: 検証したいディレクトリ（存在しなくてもよい。親ディレクトリまで遡って canonical 化する）
# 引数2: このスクリプトファイルへのパス（呼び出し元の $0 相当。repo root 推定に使う）
claude_gpt_reject_if_under_repo() {
  target_dir="$1"
  self_path="$2"

  # target_dir の canonical path を計算する（存在しない場合は既存の親まで遡る）
  probe="$target_dir"
  while [ ! -d "$probe" ]; do
    parent=$(dirname -- "$probe")
    if [ "$parent" = "$probe" ]; then
      break
    fi
    probe="$parent"
  done
  canonical_target=$(CDPATH= cd -- "$probe" 2>/dev/null && pwd -P) || canonical_target="$probe"
  # probe が target_dir の祖先である場合、差分サフィックスを付け戻す
  if [ "$probe" != "$target_dir" ]; then
    suffix=$(printf '%s' "$target_dir" | sed "s#^$(printf '%s' "$probe" | sed 's/[.[\*^$/]/\\&/g')##")
    canonical_target="${canonical_target}${suffix}"
  fi

  script_dir=$(CDPATH= cd -- "$(dirname -- "$self_path")" && pwd -P)
  # scripts/claude-gpt から見た repo root（worktree の場合は worktree root、main の場合は main repo root）
  repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd -P)

  case "$canonical_target" in
    "$repo_root"|"$repo_root"/*)
      return 1
      ;;
  esac

  # 既知の worktree 配置規約（.claude/worktrees/issue-*）配下も明示的に拒否する
  case "$canonical_target" in
    */.claude/worktrees/*)
      return 1
      ;;
  esac

  return 0
}

# --- proxy 子プロセス起動用 env allowlist ---
#
# 親 shell から継承した CCP_* / HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 等を
# そのまま proxy へ引き渡さず、launcher が明示的に組み立てた allowlist のみへ限定する。
# 呼び出し側は `env -i $(claude_gpt_build_proxy_env "$config_dir" "$state_dir" "$port") claude-code-proxy serve ...`
# のように使う。
claude_gpt_build_proxy_env() {
  proxy_config_dir="$1"
  proxy_state_dir="$2"
  bind_address="${3:-127.0.0.1}"

  printf 'PATH=%s\n' "$PATH"
  printf 'HOME=%s\n' "$HOME"
  printf 'CCP_CONFIG_DIR=%s\n' "$proxy_config_dir"
  printf 'XDG_STATE_HOME=%s\n' "$proxy_state_dir"
  printf 'CCP_BIND_ADDRESS=%s\n' "$bind_address"
  printf 'CCP_LOG_STDERR=1\n'
}

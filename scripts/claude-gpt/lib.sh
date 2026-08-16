#!/bin/sh
# scripts/claude-gpt/lib.sh
#
# claude-gpt launcher の共有関数ライブラリ。POSIX sh 準拠、外部ライブラリ依存なし。
# `launch.sh` / `preflight.sh` / `runtime_smoke_test.sh` から `. lib.sh` で source される。
# 単体実行は想定しない（source only）。
#
# Issue #2158 / Parent #2154 「アーキテクチャ決定（A〜E）」「GPT Launcher Contract」参照。
# PR #2162 OWNER REQUEST_CHANGES 反映（proxy 専用 HOME 分離・runtime smoke 強化）。

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

# proxy 専用 HOME（P0-2）。upstream raine/claude-code-proxy は CCP_CONFIG_DIR 配下に
# credential が無い場合 $HOME/.config/claude-code-proxy/codex/auth.json の legacy
# credential へフォールバックするため、呼び出し元の実 HOME をそのまま proxy へ渡すと
# profile 側 credential が空でも global legacy credential を暗黙利用できてしまう。
# login/status/serve すべてをこの専用 HOME で統一実行することで isolation を成立させる。
claude_gpt_proxy_home_dir() {
  printf '%s/proxy-home\n' "$CLAUDE_GPT_HOME"
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
#
# `[1m]` suffix は upstream raine/claude-code-proxy が公式に案内する起動形式
# （https://claude-code-proxy.raine.dev/using/configure-claude-code/）で、Claude Code
# 本体側の local context window policy を拡張する hint。proxy は upstream（ChatGPT
# backend）へ転送する前にこの suffix を除去するため、実際に ChatGPT 側へ送られる
# model ID は suffix なしの base 名のまま変わらない。suffix を付けずに起動すると
# Claude Code が未知 model として扱い、実際の context 上限（272k）より小さい既定値
# （200k）で誤って compaction を早期発動させ、summarization 失敗を引き起こす
# （Issue #2158/PR #2162 実機再検証, 2026-08-15）。
CLAUDE_GPT_MODEL_MAIN="gpt-5.6-terra[1m]"
CLAUDE_GPT_MODEL_OPUS="gpt-5.6-sol[1m]"
CLAUDE_GPT_MODEL_SONNET="gpt-5.6-terra[1m]"
CLAUDE_GPT_MODEL_HAIKU="gpt-5.6-luna[1m]"

# claude_gpt_strip_context_hint: model alias 末尾の `[1m]` 等 context-window hint suffix
# を取り除き、proxy `/v1/models` が返す base model 名と比較できる形にする。
# 引数1: model alias 文字列（例: "gpt-5.6-terra[1m]"）
claude_gpt_strip_context_hint() {
  printf '%s' "$1" | sed 's/\[[^]]*\]$//'
}

# --- claude 実行バイナリの解決（P1-1） ---
#
# CLAUDE_GPT_CLAUDE_BIN が明示されていればそれを使う。未指定なら command -v claude を
# 一度だけ解決する。呼び出し側は解決結果を変数に保存し、以降そのまま使い回すこと
# （固定文字列 "claude" を都度再検索しない）。
claude_gpt_resolve_claude_bin() {
  if [ -n "${CLAUDE_GPT_CLAUDE_BIN:-}" ]; then
    printf '%s\n' "$CLAUDE_GPT_CLAUDE_BIN"
    return 0
  fi
  command -v claude 2>/dev/null
}

# --- proxy 実行バイナリの解決（P2: identity/version pinning） ---
#
# `command -v claude-code-proxy` を一度だけ解決し、以降 preflight / 起動 / 証跡すべてで
# 同一の absolute path を使い回す。CLAUDE_GPT_PROXY_BIN が明示されていれば（launch.sh が
# 一度解決した値を子プロセス preflight.sh へ export する場合など）それを優先し、
# 再解決による差異（PATH mutation 等）を排除する。
claude_gpt_resolve_proxy_bin() {
  if [ -n "${CLAUDE_GPT_PROXY_BIN:-}" ]; then
    printf '%s\n' "$CLAUDE_GPT_PROXY_BIN"
    return 0
  fi
  command -v claude-code-proxy 2>/dev/null
}

# claude_gpt_proxy_version: 起動対象 proxy バイナリの version 識別子を取得する。
# `--version` 相当が使えない/失敗する場合は sha256（存在すれば）、それも取れなければ
# "unknown" を返す（証跡には常に何らかの identity 値を残す）。
# 引数1: proxy バイナリの絶対パス
claude_gpt_proxy_version() {
  proxy_bin="$1"
  if [ -z "$proxy_bin" ]; then
    printf 'unknown\n'
    return 0
  fi
  version_output=$("$proxy_bin" --version 2>/dev/null | head -n1)
  if [ -n "$version_output" ]; then
    printf '%s\n' "$version_output"
    return 0
  fi
  claude_gpt_sha256_file "$proxy_bin"
}

# claude_gpt_sha256_file: 任意ファイルの sha256 を計算する。sha256sum / shasum いずれも
# 使えない環境では "unknown" を返す（VC preflight allowlist 外コマンドへ依存しない）。
claude_gpt_sha256_file() {
  file="$1"
  if [ -z "$file" ] || [ ! -f "$file" ]; then
    printf 'unknown\n'
    return 0
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" 2>/dev/null | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" 2>/dev/null | cut -d' ' -f1
  else
    printf 'unknown\n'
  fi
}

# claude_gpt_git_head: repo_root の現行 HEAD SHA を取得する（取得不可なら "unknown"）。
claude_gpt_git_head() {
  repo_root="$1"
  head_sha=$(git -C "$repo_root" rev-parse HEAD 2>/dev/null)
  if [ -n "$head_sha" ]; then
    printf '%s\n' "$head_sha"
  else
    printf 'unknown\n'
  fi
}

# claude_gpt_git_dirty: repo_root が dirty（untracked/modified あり）かどうかを
# "true"/"false"/"unknown"（git repo でない等）で返す。
claude_gpt_git_dirty() {
  repo_root="$1"
  if ! git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    printf 'unknown\n'
    return 0
  fi
  if [ -n "$(git -C "$repo_root" status --porcelain 2>/dev/null)" ]; then
    printf 'true\n'
  else
    printf 'false\n'
  fi
}

# launcher が内部で必ず自前設定する policy flag。呼び出し側（drop-in 先の
# runtime smoke harness 等）が `--` の後ろに同名 flag を渡して弱体化させることを拒否する
# ためのチェック対象一覧（P1-1）。
#
# `--strict-mcp-config` はここに含めない（Issue #2189）。値を取らない純粋な boolean
# flag であり、Herdr が常時付与する exact 一致トークンは launcher 自身が最終的に付与
# する既定値と idempotent であるため、launch.sh の forbidden-flag 拒否ループの直前で
# exact-match の pre-filter により安全に取り除く。値付き variant
# （`--strict-mcp-config=...`）は本リストに依存せず、launch.sh 内の専用 exact-prefix
# チェック（`--permission-mode=bypassPermissions` と同型のパターン）で個別に拒否する
# （このリストに残すと exact トークンの pre-filter 後に到達する `=*` variant 判定と
# 混在してしまうため、責務を分離した）。
CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS="--settings --mcp-config --dangerously-skip-permissions --allow-dangerously-skip-permissions"

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
# HOME は proxy 専用 HOME（claude_gpt_proxy_home_dir）を渡すこと（P0-2。実 HOME をそのまま
# 渡すと legacy credential へフォールバックし credential 分離が成立しない）。
# 呼び出し側は `env -i $(claude_gpt_build_proxy_env "$config_dir" "$state_dir" "$proxy_home" "$port") claude-code-proxy serve ...`
# のように使う。
claude_gpt_build_proxy_env() {
  proxy_config_dir="$1"
  proxy_state_dir="$2"
  proxy_home="$3"
  bind_address="${4:-127.0.0.1}"

  printf 'PATH=%s\n' "$PATH"
  printf 'HOME=%s\n' "$proxy_home"
  printf 'CCP_CONFIG_DIR=%s\n' "$proxy_config_dir"
  printf 'XDG_STATE_HOME=%s\n' "$proxy_state_dir"
  printf 'CCP_BIND_ADDRESS=%s\n' "$bind_address"
  printf 'CCP_LOG_STDERR=1\n'
}

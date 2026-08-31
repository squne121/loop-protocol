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
#
# Issue #2455: launcher が後段で `HOME` を child Claude 用の isolated HOME へ
# 差し替える（launch.sh の `export HOME="$CLAUDE_ISOLATED_HOME_TARGET"`）ため、
# ここで解決した canonical runtime root を明示的に `export` しておかないと、
# child Claude セッションから同じ launcher/lib.sh を self-launch した際に
# `CLAUDE_GPT_HOME` が未継承のまま isolated HOME を基準に再導出され、nested
# `<isolated-claude-home>/.claude-gpt` root へ再基準化されてしまう
# （Background/Outcome 節参照）。復元経路は launcher が child process
# environment へ export するこの単一経路のみとし、生成済み settings 経由の
# 別経路は追加しない（AC1）。`CLAUDE_GPT_HOME_ROOT`（Latitude telemetry 用の
# derived mirror。Issue #2426）はこの root authority の fallback には使わない
# （AC1/AC2 の回帰テスト対象）。
: "${CLAUDE_GPT_HOME:=${HOME}/.claude-gpt}"
export CLAUDE_GPT_HOME

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

# --- Claude/AGY プロセス専用の隔離 HOME / XDG directories（P0-6）。
# OWNER adversarial review（PR #2214 コメント）は、Claude process が proxy 専用 HOME
# 分離とは独立に、呼び出し元の実 HOME をそのまま継承しているため ambient 実 HOME 配下の
# SSH key/GPG key 等の無関係な secret へ到達可能である点を指摘した。ここで用意する
# ディレクトリは空のまま launch.sh が Claude 子プロセスの `HOME` / `XDG_CONFIG_HOME` /
# `XDG_CACHE_HOME` として注入する。GitHub auth（`GH_TOKEN`/`GH_CONFIG_DIR` 系）は
# Issue #2299 により native 同等に共有する方針へ変更したため、`GH_CONFIG_DIR` は
# この隔離ディレクトリを使わず launch.sh が ambient 値をそのまま渡す
# （`claude_gpt_claude_isolated_gh_config_dir` は撤去済み）。
claude_gpt_claude_isolated_home_dir() {
  printf '%s/claude-home\n' "$CLAUDE_GPT_HOME"
}

claude_gpt_claude_isolated_xdg_config_dir() {
  printf '%s/claude-xdg-config\n' "$CLAUDE_GPT_HOME"
}

claude_gpt_claude_isolated_xdg_cache_dir() {
  printf '%s/claude-xdg-cache\n' "$CLAUDE_GPT_HOME"
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

# --- Latitude telemetry package identity（Issue #2426）。
# launcher-owned Latitude Stop hook adapter（scripts/claude-gpt/latitude_hook.py）が
# 起動する telemetry package の exact pinned identity を一箇所の SSOT で固定する
# （Design 4節「再現可能な exact identity を一箇所の SSOT で pin する」）。
# versionless / floating `npx -y @latitude-data/claude-code-telemetry` を新しい
# canonical production command にしない（docs/dev/secret-policy.md の
# `activation_denied_if: unpinned_npx` と同じ禁止事項）。
# docs/dev/secret-policy.md の LATITUDE_DISTRIBUTION_GATE_V1.package_spec /
# tarball_sha256 プレースホルダはこの値と同期させる（このファイルが正本、
# secret-policy.md 側は mirror）。
claude_gpt_latitude_package_name() {
  printf '@latitude-data/claude-code-telemetry\n'
}

claude_gpt_latitude_package_version() {
  printf '0.0.14\n'
}

claude_gpt_latitude_package_spec() {
  printf '%s@%s\n' "$(claude_gpt_latitude_package_name)" "$(claude_gpt_latitude_package_version)"
}

# claude_gpt_native_latitude_project: Native user settings（引数1）の `env` から
# `LATITUDE_PROJECT` の値だけを返す（Issue #2426 PR #2439 P0 fix-delta,
# OWNER REQUEST_CHANGES）。
#
# Design 3節: 「`LATITUDE_PROJECT` は secret ではないため、既存 #2375（PR #2392）
# collector が同一 project を解決できるよう runtime から解決可能にしてよい」-- した
# がってこの値は（`LATITUDE_API_KEY` とは異なり）生成済み Claude-GPT settings の
# `env` へそのまま焼き込んでよい。実際の値読み取りは、closed allowlist の実装が
# 存在する唯一の SSOT である `latitude_hook.py` の `read_native_latitude_allowlist()`
# を再利用する（同じ allowlist ロジックを shell 側に複製しない）。
#
# 引数1: Native user settings の絶対パス（`~/.claude/settings.json` 相当）
# 引数2: `latitude_hook.py` の絶対パス（`read_native_latitude_allowlist()` を
#        import するために使う。呼び出し元がすでに解決済みの
#        `CLAUDE_GPT_LATITUDE_HOOK` をそのまま渡すこと）
# 戻り値: LATITUDE_PROJECT の値（非空文字列）。未設定・読み取り失敗時は空文字列
#         （fail-open。既存 `read_native_latitude_allowlist()` の fail-open 契約と
#         同じ）。`LATITUDE_API_KEY` はこの関数が扱う値に一切含まれない
#         （`allowlist.get("LATITUDE_PROJECT")` のみを取り出す）。
claude_gpt_native_latitude_project() {
  native_settings_path="$1"
  hook_path="$2"
  if [ -z "$native_settings_path" ] || [ -z "$hook_path" ] || ! command -v python3 >/dev/null 2>&1; then
    printf ''
    return 0
  fi
  python3 -c '
import importlib.util
import sys

hook_path, settings_path = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_hook_lib_native_project", hook_path
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
allowlist = module.read_native_latitude_allowlist(settings_path)
sys.stdout.write(allowlist.get("LATITUDE_PROJECT") or "")
' "$hook_path" "$native_settings_path" 2>/dev/null
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
#
# `--permission-mode` は Issue #2203（2026-08-16 OWNER adversarial review 反映）で
# 追加した。launcher 自身が exactly one の `--permission-mode auto` を注入する契約の
# ため、caller が明示する `--permission-mode VALUE` / `--permission-mode=VALUE` は
# 値の種類（bypassPermissions を含む）を問わず一律拒否する。この一覧は
# launcher-level `--` 以降の全トークンを走査する forbidden-flag ループでチェックされる
# ため、duplicate 指定・`--` 後方の literal を含め、出現位置によらず拒否される
# （区別して全面拒否。Outcome 節参照）。
CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS="--settings --mcp-config --dangerously-skip-permissions --allow-dangerously-skip-permissions --permission-mode --agents"

# --- Issue #2203: launcher-owned autoMode policy（second-gate の判断補助）--------
#
# `autoMode` は permissions.deny / PreToolUse hook / GitHub mutation transaction
# broker の後段にある classifier ベースの判断補助であり、決定論的な authority では
# ない（Configure auto mode ドキュメント準拠）。ここで生成する narrow 文字列は
# launcher-owned `--settings` にのみ注入し、project `.claude/settings*.json` には
# 追加しない。
CLAUDE_GPT_TRUSTED_REPO="squne121/loop-protocol"

CLAUDE_GPT_AUTO_MODE_ENVIRONMENT_NARROW_LABEL="claude-gpt launcher narrow environment（second-gate 判断補助。authority ではない）: このセッションが日常的に扱う対象は GitHub host github.com 上の ${CLAUDE_GPT_TRUSTED_REPO} リポジトリ（Issue/PR の read/create/edit/comment/review/close/reopen、および同一 repository への non-force task-branch push）と、repository-owned canonical codebase-investigator -> gemini-cli-headless-delegation -> provider=agy の read-only isolated delegation route のみである。GitHub write credential は launcher-owned transaction broker のみが保持し、Claude/AGY プロセスの ambient environment には渡さない。他 repository・他 host・broad gh api・arbitrary provider・force push・branch/tag/release 削除・repository settings/IAM/secret 変更はこの環境記述の対象外である。"

CLAUDE_GPT_AUTO_MODE_ALLOW_NARROW_LABEL="claude-gpt launcher narrow allow（second-gate 判断補助。authority ではない）: ${CLAUDE_GPT_TRUSTED_REPO} に repository 固定した GitHub mutation transaction broker（canonical builder/wrapper 経由、raw gh api を使わない）による Issue の read/create/edit/comment/close と、同一 repository の PR の read/create/edit/comment/review、および同一 repository への non-force task-branch push（force push・branch/tag/release 削除・repository settings/IAM/secret 変更は含まない）。加えて repository-owned canonical codebase-investigator -> gemini-cli-headless-delegation -> provider=agy の read-only isolated delegation（direct arbitrary agy 起動・provider!=agy・canonical builder/wrapper bypass・AGY からの GitHub mutation は対象外）。決定論的な authority は permissions.deny / PreToolUse hook / transaction broker が持ち、この allow rule はその second-gate 判断補助に過ぎない。"

# Issue #2433: Auto mode では parent session の permission rules が SubAgent action
# にも適用されるため、issue-editor の canonical outer transaction だけを launcher-
# owned settings で事前許可する。末尾 wildcard は transaction input file だけを可変にし、
# raw gh・controlled executor・他 skill script・任意の uv/python invocation は含めない。
# downstream controlled transaction は repo / schema / pre-read / stale check / single PATCH /
# readback を引き続き独立に検証する。
CLAUDE_GPT_ISSUE_EDITOR_TXN_ALLOW_RULE="Bash(uv run --locked python3 .claude/skills/edit-issue/scripts/edit_issue_txn.py --input-file *)"

# hard_deny への追加分（P0-2, PR #2214 OWNER adversarial review 反映）。$defaults の
# hard_deny を置換・削除せず、default branch push・force push・remote ref
# deletion を明示的に追加する。これは defense-in-depth の second-gate 補強であり、
# 決定論的 authority は broker/PreToolUse hook 側にある（本 Issue の Allowed Paths
# 内で実装できる範囲は launcher-owned settings のこの追加分と、Claude/AGY プロセスの
# 隔離 HOME/GH_CONFIG_DIR による raw `gh` 認証遮断に限られる。raw `gh` /
# raw `git push` を PreToolUse hook で deterministic に拒否する実装は、project
# `.claude/settings.json` / hooks 設定という本 Issue の Allowed Paths 外への変更を
# 要するため、別途 follow-up Issue で扱う）。
CLAUDE_GPT_AUTO_MODE_HARD_DENY_DEFAULT_BRANCH_PUSH_LABEL="claude-gpt launcher hard_deny 追加分（second-gate 補助・defense-in-depth）: ${CLAUDE_GPT_TRUSTED_REPO} の default branch（main）への直接 push は絶対拒否する。task-branch 以外への push は行わない。"
CLAUDE_GPT_AUTO_MODE_HARD_DENY_FORCE_PUSH_LABEL="claude-gpt launcher hard_deny 追加分（second-gate 補助・defense-in-depth）: force push（--force / --force-with-lease / +refspec）は絶対拒否する。"
CLAUDE_GPT_AUTO_MODE_HARD_DENY_REF_DELETION_LABEL="claude-gpt launcher hard_deny 追加分（second-gate 補助・defense-in-depth）: remote ref（branch/tag/release）の削除は絶対拒否する。"

# `claude auto-mode config` readback で version-gate と実動作を検証する対応最小
# Claude Code version（`classifyAllShell` が黙って無視されない最小 version。P0-3）。
CLAUDE_GPT_MIN_SUPPORTED_CLAUDE_VERSION="2.1.193"

# claude_gpt_json_escape: 任意文字列を JSON 文字列リテラル（引用符込み）へ変換する。
# python3 が使えない環境では簡易 fallback（改行・制御文字は非対応）を使う。
# 引数1: エスケープしたい生文字列
claude_gpt_json_escape() {
  value="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json, sys; sys.stdout.write(json.dumps(sys.argv[1]))' "$value"
  else
    esc=$(printf '%s' "$value" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '"%s"' "$esc"
  fi
}

# claude_gpt_auto_mode_json_fragment: settings JSON の `"autoMode": {...}` フィールド
# 本体（キー名を含む）を1行の文字列として返す。`"$defaults"` を各配列の先頭に必ず
# 含め、`classifyAllShell: true` を必須化する（Issue #2203 Outcome 節）。
claude_gpt_auto_mode_json_fragment() {
  env_label_json=$(claude_gpt_json_escape "$CLAUDE_GPT_AUTO_MODE_ENVIRONMENT_NARROW_LABEL")
  allow_label_json=$(claude_gpt_json_escape "$CLAUDE_GPT_AUTO_MODE_ALLOW_NARROW_LABEL")
  hard_deny_default_branch_json=$(claude_gpt_json_escape "$CLAUDE_GPT_AUTO_MODE_HARD_DENY_DEFAULT_BRANCH_PUSH_LABEL")
  hard_deny_force_push_json=$(claude_gpt_json_escape "$CLAUDE_GPT_AUTO_MODE_HARD_DENY_FORCE_PUSH_LABEL")
  hard_deny_ref_deletion_json=$(claude_gpt_json_escape "$CLAUDE_GPT_AUTO_MODE_HARD_DENY_REF_DELETION_LABEL")
  printf '"autoMode": {"environment": ["$defaults", %s], "allow": ["$defaults", %s], "hard_deny": ["$defaults", %s, %s, %s], "classifyAllShell": true}' \
    "$env_label_json" "$allow_label_json" \
    "$hard_deny_default_branch_json" "$hard_deny_force_push_json" "$hard_deny_ref_deletion_json"
}

# claude_gpt_auto_mode_standalone_json: 上記フラグメントを単独の JSON オブジェクトとして
# 返す（hermetic test 用。生成された settings ファイル全体を経由せず、フラグメント単体の
# JSON 妥当性・$defaults 存在・narrow scope を検証できるようにする）。
claude_gpt_auto_mode_standalone_json() {
  printf '{%s}\n' "$(claude_gpt_auto_mode_json_fragment)"
}

# claude_gpt_auto_mode_readback: `claude auto-mode defaults` / `claude auto-mode config`
# の実 readback で、launcher-generated settings の autoMode が effective config に
# 正しく反映されていること（narrow environment/allow label 反映・hard_deny/soft_deny
# 不変・classifyAllShell 有効）を検証する（Issue #2203 AC1）。python3 必須（未対応
# 環境は fail-closed）。呼び出し元プロセスの env を継承せず、`env -i` で最小限のみ渡す
# （FAKE_CLAUDE_ARGV_FILE 等、他コンポーネントの hermetic test 観測用 env の汚染防止も
# 兼ねる）。
#
# 引数1: claude 実行バイナリの絶対パス
# 引数2: 検証対象の settings.local.json 絶対パス
# 戻り値: 0 = readback 成功（PASS）、8 = fail-closed（未対応 version・readback
#         mismatch・classifyAllShell 未反映）
claude_gpt_auto_mode_readback() {
  claude_bin="$1"
  settings_path="$2"

  if ! command -v python3 >/dev/null 2>&1; then
    printf '{"schema":"CLAUDE_GPT_AUTO_MODE_PREFLIGHT_RESULT_V1","status":"blocked","reason":"python3_unavailable"}\n'
    return 8
  fi

  claude_config_dir=$(CDPATH= cd -- "$(dirname -- "$settings_path")" 2>/dev/null && pwd -P)
  if [ -z "$claude_config_dir" ]; then
    claude_config_dir="${HOME:-/tmp}"
  fi

  version_tmp=$(mktemp)
  defaults_tmp=$(mktemp)
  config_tmp=$(mktemp)

  # `env -i` は呼び出し元の環境を明示指定分のみへリセットする（ambient env の
  # readback invocation への意図しない漏洩を防ぐ）。hermetic test 用の fake
  # claude binary 観測 channel（`FAKE_CLAUDE_*`）だけは、実 production では
  # 一切設定されない前提のため、明示的に forward する（未設定時は空文字列の
  # まま渡り、fake binary 側の `os.environ.get()` が falsy として扱う）。
  env -i PATH="$PATH" HOME="${HOME:-/tmp}" CLAUDE_CONFIG_DIR="$claude_config_dir" \
    FAKE_CLAUDE_ARGV_LOG="${FAKE_CLAUDE_ARGV_LOG:-}" FAKE_CLAUDE_ARGV_FILE="${FAKE_CLAUDE_ARGV_FILE:-}" \
    FAKE_CLAUDE_VERSION="${FAKE_CLAUDE_VERSION:-}" \
    FAKE_CLAUDE_AUTO_MODE_READBACK_FAIL="${FAKE_CLAUDE_AUTO_MODE_READBACK_FAIL:-}" \
    "$claude_bin" --version >"$version_tmp" 2>&1
  version_rc=$?

  env -i PATH="$PATH" HOME="${HOME:-/tmp}" CLAUDE_CONFIG_DIR="$claude_config_dir" \
    FAKE_CLAUDE_ARGV_LOG="${FAKE_CLAUDE_ARGV_LOG:-}" FAKE_CLAUDE_ARGV_FILE="${FAKE_CLAUDE_ARGV_FILE:-}" \
    FAKE_CLAUDE_VERSION="${FAKE_CLAUDE_VERSION:-}" \
    FAKE_CLAUDE_AUTO_MODE_READBACK_FAIL="${FAKE_CLAUDE_AUTO_MODE_READBACK_FAIL:-}" \
    "$claude_bin" auto-mode defaults >"$defaults_tmp" 2>&1
  defaults_rc=$?

  env -i PATH="$PATH" HOME="${HOME:-/tmp}" CLAUDE_CONFIG_DIR="$claude_config_dir" \
    FAKE_CLAUDE_ARGV_LOG="${FAKE_CLAUDE_ARGV_LOG:-}" FAKE_CLAUDE_ARGV_FILE="${FAKE_CLAUDE_ARGV_FILE:-}" \
    FAKE_CLAUDE_VERSION="${FAKE_CLAUDE_VERSION:-}" \
    FAKE_CLAUDE_AUTO_MODE_READBACK_FAIL="${FAKE_CLAUDE_AUTO_MODE_READBACK_FAIL:-}" \
    "$claude_bin" --settings "$settings_path" auto-mode config >"$config_tmp" 2>&1
  config_rc=$?

  python3 - "$version_tmp" "$version_rc" "$defaults_tmp" "$defaults_rc" "$config_tmp" "$config_rc" "$settings_path" \
    "$CLAUDE_GPT_AUTO_MODE_ENVIRONMENT_NARROW_LABEL" "$CLAUDE_GPT_AUTO_MODE_ALLOW_NARROW_LABEL" \
    "$CLAUDE_GPT_AUTO_MODE_HARD_DENY_DEFAULT_BRANCH_PUSH_LABEL" "$CLAUDE_GPT_AUTO_MODE_HARD_DENY_FORCE_PUSH_LABEL" \
    "$CLAUDE_GPT_AUTO_MODE_HARD_DENY_REF_DELETION_LABEL" "$CLAUDE_GPT_MIN_SUPPORTED_CLAUDE_VERSION" \
    "$CLAUDE_GPT_ISSUE_EDITOR_TXN_ALLOW_RULE" <<'PYEOF'
import hashlib
import json
import re
import sys

(
    version_path,
    version_rc,
    defaults_path,
    defaults_rc,
    config_path,
    config_rc,
    settings_path,
    env_label,
    allow_label,
    hard_deny_default_branch_label,
    hard_deny_force_push_label,
    hard_deny_ref_deletion_label,
    min_supported_version,
    issue_editor_txn_allow_rule,
) = sys.argv[1:15]
version_rc = int(version_rc)
defaults_rc = int(defaults_rc)
config_rc = int(config_rc)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


reasons: list[str] = []

with open(version_path, encoding="utf-8") as fh:
    version_text = fh.read()
with open(defaults_path, encoding="utf-8") as fh:
    defaults_text = fh.read()
with open(config_path, encoding="utf-8") as fh:
    config_text = fh.read()

# --- version gate（P0-3）。classifyAllShell は Claude Code v2.1.193 未満では
#     黙って無視されるため、settings 文字列の有無ではなく実際に対応 version かを
#     機械的に確認し、未対応/取得不能なら fail-closed とする。 ---
parsed_version = _parse_version(version_text) if version_rc == 0 else None
min_version = _parse_version(min_supported_version)
version_ok = False
if version_rc != 0:
    reasons.append("claude_version_command_failed")
elif parsed_version is None:
    reasons.append("claude_version_unparsable")
elif min_version is not None and parsed_version < min_version:
    reasons.append("claude_version_below_minimum_supported")
else:
    version_ok = True

defaults = None
config = None
settings = None

try:
    with open(settings_path, encoding="utf-8") as fh:
        settings = json.load(fh)
except (OSError, ValueError):
    reasons.append("launcher_settings_unparsable")

if defaults_rc != 0:
    reasons.append("auto_mode_defaults_command_failed")
else:
    try:
        defaults = json.loads(defaults_text)
    except ValueError:
        reasons.append("auto_mode_defaults_unparsable")

if config_rc != 0:
    reasons.append("auto_mode_config_command_failed")
else:
    try:
        config = json.loads(config_text)
    except ValueError:
        reasons.append("auto_mode_config_unparsable")

env_label_present = False
allow_label_present = False
issue_editor_txn_allow_rule_present = False
hard_deny_superset_ok = None
soft_deny_unmodified = None
classify_all_shell_ok = False
classify_all_shell_source = "not_evaluated"

if defaults is not None and config is not None:
    for key in ("environment", "allow", "hard_deny", "soft_deny"):
        if key not in defaults or key not in config:
            reasons.append(f"missing_key_{key}")

    env_label_present = env_label in config.get("environment", [])
    allow_label_present = allow_label in config.get("allow", [])
    if not env_label_present:
        reasons.append("environment_narrow_label_not_reflected")
    if not allow_label_present:
        reasons.append("allow_narrow_label_not_reflected")

    permissions = settings.get("permissions") if isinstance(settings, dict) else None
    configured_allow = permissions.get("allow") if isinstance(permissions, dict) else None
    issue_editor_txn_allow_rule_present = configured_allow == [issue_editor_txn_allow_rule]
    if not issue_editor_txn_allow_rule_present:
        reasons.append("issue_editor_txn_allow_rule_missing_or_broadened")

    # hard_deny は $defaults を置換・削除せず、narrow な追加分（default branch
    # push / force push / ref deletion）だけを加える契約（P0-2）。defaults の
    # 全 entry を含み、かつ3件の追加 deny 文言を含むことを確認する（任意の
    # 超集合を許容する緩い検査にはしない）。
    config_hard_deny = config.get("hard_deny", [])
    defaults_hard_deny = defaults.get("hard_deny", [])
    hard_deny_contains_defaults = all(entry in config_hard_deny for entry in defaults_hard_deny)
    hard_deny_contains_additions = (
        hard_deny_default_branch_label in config_hard_deny
        and hard_deny_force_push_label in config_hard_deny
        and hard_deny_ref_deletion_label in config_hard_deny
    )
    hard_deny_superset_ok = hard_deny_contains_defaults and hard_deny_contains_additions
    if not hard_deny_superset_ok:
        reasons.append("hard_deny_defaults_or_additions_missing")

    soft_deny_unmodified = config.get("soft_deny") == defaults.get("soft_deny")
    if not soft_deny_unmodified:
        reasons.append("soft_deny_modified")

    # classifyAllShell の readback（P0-3）。実機検証（Claude Code 2.1.233,
    # 2026-08-16）の結果、現行 CLI の `auto-mode defaults` / `auto-mode config`
    # JSON はこの key 自体を一切出力しない（settings 側で明示 true にしていても
    # effective config オブジェクトに現れない）。そのため effective config の
    # key 存在を正本にする検証は現行 CLI では原理的に成立しない（vendor CLI の
    # 制約であり、settings 文字列チェックの単純な置き換えでは代替できない）。
    # ここでは「settings 文字列存在チェックだけでは CLI が key を無視していても
    # PASS してしまう」という P0-3 の懸念に対し、二重の検証で fail-closed に
    # 倒す。
    #   1. effective config に key が現れる場合（将来 CLI がこの key を
    #      公開した場合）はそれを正本として使う。
    #   2. key が現れない場合は、(a) version gate（対応 CLI version 以上）と
    #      (b) settings ファイル上の literal `"classifyAllShell": true` の
    #      両方を要求する（version gate 単独より厳格。CLI が対応 version
    #      以上であっても readback で真偽を確認できない現状の限界を、
    #      「未対応 version は無条件 fail-closed」で部分的に補う）。
    classify_all_shell_source = "effective_config"
    if "classifyAllShell" in config:
        classify_all_shell_ok = config.get("classifyAllShell") is True
        if not classify_all_shell_ok:
            reasons.append("classify_all_shell_not_enabled")
    else:
        classify_all_shell_source = "settings_literal_plus_version_gate_best_effort"
        try:
            with open(settings_path, encoding="utf-8") as fh:
                settings_text = fh.read()
        except OSError:
            settings_text = ""
        settings_literal_ok = (
            '"classifyAllShell": true' in settings_text or '"classifyAllShell":true' in settings_text
        )
        classify_all_shell_ok = settings_literal_ok and version_ok
        if not settings_literal_ok:
            reasons.append("classify_all_shell_not_enabled")
        elif not version_ok:
            reasons.append("classify_all_shell_unverifiable_below_minimum_version")

defaults_digest = _digest(defaults_text) if defaults is not None else "unknown"
config_digest = _digest(config_text) if config is not None else "unknown"

ok = not reasons and version_ok

result = {
    "schema": "CLAUDE_GPT_AUTO_MODE_PREFLIGHT_RESULT_V1",
    "status": "ok" if ok else "blocked",
    "ok": ok,
    "claude_version": {
        "raw": version_text.strip(),
        "parsed": list(parsed_version) if parsed_version else None,
        "min_supported": list(min_version) if min_version else None,
        "ok": version_ok,
    },
    "checks": {
        "environment_narrow_label_present": env_label_present,
        "allow_narrow_label_present": allow_label_present,
        "issue_editor_txn_allow_rule_present": issue_editor_txn_allow_rule_present,
        "hard_deny_defaults_and_additions_present": bool(hard_deny_superset_ok),
        "soft_deny_unmodified": bool(soft_deny_unmodified),
        "classify_all_shell_enabled": classify_all_shell_ok,
        "classify_all_shell_verification_source": classify_all_shell_source,
    },
    "digests": {
        "auto_mode_defaults_digest": defaults_digest,
        "effective_config_digest": config_digest,
    },
    "fail_closed_reasons": reasons,
}
print(json.dumps(result))
sys.exit(0 if ok else 8)
PYEOF
  rc=$?
  rm -f "$version_tmp" "$defaults_tmp" "$config_tmp"
  return "$rc"
}

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

# --- Spark custom SubAgent（Issue #2186, Parent #2154 Gate1/Gate2 準拠）---
#
# `spark-codex` は claude-gpt session にだけ `--agents` フラグ経由で session-local
# 登録する custom SubAgent。`.claude/agents/spark-codex.md` は意図的に作らない
# （project scope の agent 定義は Native session にも露出するため。Issue #2186
# Gate1 採用済み設計）。`model` に非 Anthropic full model ID
# （`gpt-5.3-codex-spark`）を渡すことで、その SubAgent 単体の推論だけが proxy 経由で
# Codex backend へ route され、parent session の model は変化しない（Gate2 live
# canary 実証済み）。Spark は text-only research preview であり、通常 profile の
# `[1m]`/272k context suffix・閾値を継承しない（source document context ceiling は
# 128K。Issue #2186 Background）。
CLAUDE_GPT_SPARK_AGENT_NAME="spark-codex"
CLAUDE_GPT_SPARK_MODEL="gpt-5.3-codex-spark"

# claude_gpt_spark_agent_prompt: spark-codex custom SubAgent の system prompt 本文。
# 通常 profile の main model 切替や自律呼び出しではなく、user の current-turn
# 明示 `@agent-spark-codex` mention の後にだけ authorization gate を通って起動する
# delegate であることを明記する。
claude_gpt_spark_agent_prompt() {
  printf 'You are spark-codex, an explicit-only GPT-5.3-Codex-Spark delegate SubAgent. You are only ever invoked when the user has written the canonical @agent-spark-codex mention in their current turn and an explicit-only fail-closed authorization gate (UserPromptSubmit -> PreToolUse(Agent) -> consume) has allowed exactly this one invocation. You are text-only (no image/web-search capability) and must treat your context ceiling as a conservative 128K (do not assume the ordinary profile 272K/[1m] budget). Report failures explicitly to the parent; never silently fall back to another model.
'
}

# claude_gpt_spark_agents_json_fragment: `--agents` フラグへ渡す session-local JSON
# 本体を返す（このセッションだけに spark-codex を登録し、project `.claude/agents/**`
# の既存12定義・Native session には一切影響しない）。
#
# `disallowedTools`（Issue #2186 P2 fix-delta, PR #2244 adversarial review）:
# Spark は text-only research preview であり image/web-search capability を
# 持たない前提（Issue #2186 Background/Out of Scope）。この制約を prose
# だけでなく agent 定義自体の構造でも強制するため、web 系 tool を
# 明示的に disallow する。
claude_gpt_spark_agents_json_fragment() {
  description_json=$(claude_gpt_json_escape "Explicit-only GPT-5.3-Codex-Spark delegate. Invoked only via canonical current-turn @agent-spark-codex mention.")
  prompt_json=$(claude_gpt_json_escape "$(claude_gpt_spark_agent_prompt)")
  printf '{"%s": {"description": %s, "prompt": %s, "model": "%s", "disallowedTools": ["WebFetch", "WebSearch"]}}' \
    "$CLAUDE_GPT_SPARK_AGENT_NAME" "$description_json" "$prompt_json" "$CLAUDE_GPT_SPARK_MODEL"
}

# claude_gpt_spark_auth_dir: explicit-only authorization gate の nonce/session_id
# keyed sidecar file を置くディレクトリ（GPT 専用 HOME 配下。worktree/repo 配下では
# ない。claude_gpt_reject_if_under_repo による canonical path safety の対象）。
claude_gpt_spark_auth_dir() {
  printf '%s/spark-auth\n' "$CLAUDE_GPT_HOME"
}

# --- Codex transport policy（Issue #2204, Parent #2154）---
#
# isolated proxy child が Codex backend への接続に使う transport を、親 shell の
# CCP_CODEX_TRANSPORT pass-through や isolated proxy config.json の transport 指定
# よりも優先して repository-owned に固定する無条件定数。auto ではなく http 固定を
# 採る理由は Issue #2204 Outcome 直下の設計判断（decision block）を参照。
# 親環境で上書き可能な `: "${VAR:=http}"` ではなく、無条件代入にすることで
# 親 shell の値を一切参照しない（親 env は launch.sh の `env -i` により proxy
# 子プロセスへそもそも継承されないが、本定数は isolated config.json 由来の
# transport 指定にも優先する必要があるため、明示 env として常に渡す）。
CLAUDE_GPT_CODEX_TRANSPORT_POLICY=http

# --- proxy 子プロセス起動用 env allowlist ---
#
# 親 shell から継承した CCP_* / HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 等を
# そのまま proxy へ引き渡さず、launcher が明示的に組み立てた allowlist のみへ限定する。
# HOME は proxy 専用 HOME（claude_gpt_proxy_home_dir）を渡すこと（P0-2。実 HOME をそのまま
# 渡すと legacy credential へフォールバックし credential 分離が成立しない）。
# CCP_CODEX_TRANSPORT は CLAUDE_GPT_CODEX_TRANSPORT_POLICY を単一の source of truth
# として参照する（Issue #2204。isolated config.json や upstream built-in default の
# websocket を明示 env で上書きする）。
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
  printf 'CCP_CODEX_TRANSPORT=%s\n' "$CLAUDE_GPT_CODEX_TRANSPORT_POLICY"
}

# --- Smoke harness canary agent fixture（Issue #2274 AC14/AC15）---
#
# claude_gpt_agents_json_merge_validate: 複数の `--agents` JSON fragment 文字列
# （それぞれ単一 top-level key を持つ有効な JSON object であることを要求する）を
# python3 の JSON serializer/parser だけを使って安全にマージし、生成後に
# parse/readback して以下を fail-closed で拒否する:
#   - 引数のいずれかが有効な JSON object としてパースできない場合（malformed JSON）
#   - マージ後の top-level key 数が入力 fragment の合計 key 数と一致しない場合
#     （＝ 複数 fragment 間で agent name が衝突し、後勝ちで上書きされた場合。
#       duplicate agent name の検出）
#   - readback したマージ結果が serialize 直後の値と一致しない場合
# 成功時のみマージ結果 JSON を stdout へ書き、exit 0 を返す。失敗時は何も出力せず
# 非 0 を返す（呼び出し元は戻り値を必ず検査すること）。python3 未対応環境は
# fail-closed（何も出力せず exit 1）。
claude_gpt_agents_json_merge_validate() {
  if ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi
  python3 - "$@" <<'CLAUDE_GPT_AGENTS_MERGE_VALIDATE_PY'
import json
import sys

fragments = sys.argv[1:]
if not fragments:
    sys.exit(1)

merged = {}
expected_key_count = 0
for raw in fragments:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.exit(1)
    if not isinstance(parsed, dict) or not parsed:
        sys.exit(1)
    expected_key_count += len(parsed)
    merged.update(parsed)

if len(merged) != expected_key_count:
    sys.exit(1)

serialized = json.dumps(merged)
try:
    readback = json.loads(serialized)
except (json.JSONDecodeError, ValueError):
    sys.exit(1)
if readback != merged:
    sys.exit(1)

sys.stdout.write(serialized)
CLAUDE_GPT_AGENTS_MERGE_VALIDATE_PY
}

# claude_gpt_smoke_canary_agents_json_fragment: `runtime_smoke_test.sh` 専用の
# launcher-owned/session-owned canary SubAgent fixture を内部合成する（Issue #2274
# AC14/AC15）。smoke mode 以外からの呼び出しは想定しない。呼び出し元から
# name/prompt/model/tools を一切受け取らない（この関数のシグネチャ自体が受け取れる
# のは expected marker と smoke run 固有 nonce の 2 つだけ -- caller override は
# 構造的に不可能）。生成した agent name は smoke run 固有 nonce（呼び出し元が
# 生成する高エントロピー値。推測困難な値であること）を組み込み、他 run や
# session-local spark 定義名との衝突を避ける。tools は常に空配列、prompt は
# 固定の canary prompt に expected marker を埋め込んだもの。
#
# JSON serializer（python3 の `json.dumps`）で一括生成した直後に自身で
# parse/readback し、以下のいずれかを検出したら stdout へ何も書かず exit 1 する
# （fail-closed。malformed JSON をそのまま `--agents` へ渡さない）:
#   - marker/nonce が空
#   - 生成した agent name が予約済み spark 定義名（$3 に渡された値）と衝突する
#   - readback した object の top-level key が 1 個でない
#   - readback した prompt/tools が固定 spec と一致しない（tools が空配列でない・
#     model key が存在する等）
#
# 引数: $1=expected marker文字列  $2=smoke run 固有 nonce  $3=予約済み spark 定義名
#       （$CLAUDE_GPT_SPARK_AGENT_NAME 等。衝突検査専用、この値自体は生成しない）
claude_gpt_smoke_canary_agents_json_fragment() {
  marker="$1"
  nonce="$2"
  reserved_name="$3"
  if [ -z "$marker" ] || [ -z "$nonce" ]; then
    return 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi
  prompt_text="You are a launcher-owned canary SubAgent used only for claude-gpt runtime smoke test positive control (Issue #2274 AC14/AC15). You have no tools. When invoked, respond with exactly: ${marker} and nothing else."
  python3 - "$nonce" "$prompt_text" "$marker" "$reserved_name" <<'CLAUDE_GPT_CANARY_FIXTURE_PY'
import hashlib
import json
import sys

nonce, prompt_text, marker, reserved_name = sys.argv[1:5]

if not nonce or not prompt_text or not marker:
    sys.exit(1)

agent_name = "canary-smoke-" + hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:32]
if reserved_name and agent_name == reserved_name:
    sys.exit(1)

fixture = {
    agent_name: {
        "description": "Launcher-owned canary SubAgent for claude-gpt runtime smoke test positive control only (Issue #2274).",
        "prompt": prompt_text,
        "tools": [],
    }
}
serialized = json.dumps(fixture)

try:
    readback = json.loads(serialized)
except (json.JSONDecodeError, ValueError):
    sys.exit(1)
if not isinstance(readback, dict) or len(readback) != 1:
    sys.exit(1)
if reserved_name and reserved_name in readback:
    sys.exit(1)
only_key = next(iter(readback))
if only_key != agent_name:
    sys.exit(1)
entry = readback[only_key]
if not isinstance(entry, dict):
    sys.exit(1)
# Issue #2274 PR #2285 OWNER fix-delta P0-1: defense-in-depth exact key-set
# check. The fixture dict literal above already structurally cannot contain
# any other key, but this explicit check makes "no extra fields, ever" a
# tested invariant of the readback rather than an implicit property of the
# literal, and fails closed if a future edit to the literal ever widens it.
if set(entry.keys()) != {"description", "prompt", "tools"}:
    sys.exit(1)
if entry.get("tools") != []:
    sys.exit(1)
if "model" in entry:
    sys.exit(1)
if entry.get("prompt") != prompt_text:
    sys.exit(1)

sys.stdout.write(serialized)
CLAUDE_GPT_CANARY_FIXTURE_PY
}

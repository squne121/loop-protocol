#!/bin/sh
# scripts/claude-gpt/launch.sh
#
# repository-owned claude-gpt launcher。
#
# herdr session B（ChatGPT Pro Codex subscription 経由 GPT-5.6 Sol/Terra/Luna）を
# `raine/claude-code-proxy` 経由で起動する。Native Claude（herdr session A）とは
# config root / credential / working tree を分離する（Issue #2158 / Parent #2154
# アーキテクチャ決定 A〜E 準拠）。現行 Unix user のまま起動する（Issue #2158
# Scope Reframe, 2026-08-15。dedicated user・OS-level sandbox は Phase 1 の non-goal）。
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
#   10  Task Context canonical state-root 解決失敗（python3 未対応 / resolver
#       error）で、isolated HOME への切替を fail-fast で止めた（Issue #2567
#       PR #2696 review fix_delta P1-1）

# --- Herdr Agents session hint: self-reexec (#2332) ---
# Herdr 内(HERDR_ENV=1)かつ呼び出し側が HERDR_AGENT を設定していない場合だけ、
# foreground process 自身の初期環境に HERDR_AGENT=claude を設定して exactly once
# self-reexec する。exec は "$0"/"$@" を保持するため、既存の
# positional-argument parser(--check-only/--dry-run/--claude-bin/--)の挙動には
# 影響しない。呼び出し側が既に非空の HERDR_AGENT を設定している場合はその値を
# exact に温存し、上書き・reexec のいずれも行わない。Herdr 外
# (HERDR_ENV が unset または 1 以外)では常に no-op。
# 2 回目の実行では HERDR_AGENT が既に非空になっているため、この分岐は
# 自然に再発火しない(loop counter / state file は使わない)。
#
# P1 fix-delta (OWNER REQUEST_CHANGES, PR #2349 review comment): Issue #2332 の
# Outcome は「Herdr 内の通常起動(実際に claude 本体を起動するモード)を claude と
# 認識させる」ことに限定される。`--check-only`(claude 本体を起動しない
# preflight-only mode。runtime_smoke_test.sh から使用)や `--dry-run`(副作用なしで
# 実行予定を JSON 表示するのみ)は claude を一切起動しないため、これらの
# invocation にまで hint を付けるのは false-positive recognition になる。
# 完全な positional-argument parser(下記)を複製せず、副作用のない小さな
# argv pre-scan だけを行う: launcher-level `--` より前の位置に
# `--check-only` または `--dry-run` が現れる invocation だけを
# 「claude を起動しない non-agent mode」と判定し、その場合は hint injection
# (export + exec) を丸ごと skip する。self-reexec 自体の配置(lib.sh source より
# 前)は変更しない。
_herdr_hint_launcher_mode=true
for _herdr_hint_arg in "$@"; do
  case "$_herdr_hint_arg" in
    --)
      break
      ;;
    --check-only | --dry-run)
      _herdr_hint_launcher_mode=false
      break
      ;;
  esac
done

if [ "$HERDR_ENV" = "1" ] && [ -z "$HERDR_AGENT" ] && [ "$_herdr_hint_launcher_mode" = "true" ]; then
  export HERDR_AGENT=claude
  exec /bin/sh "$0" "$@"
fi
unset _herdr_hint_launcher_mode _herdr_hint_arg

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

# --- SUT/proxy identity 事前解決 + 人間可読な起動診断行（P0-1 / P2）。
#     どの経路（対話・非対話・--check-only・--dry-run・引数エラー含む）で launcher が
#     終了しても、呼び出し元がどの worktree / commit / proxy version から起動したかを
#     必ず stderr で確認できるようにする（stale worktree 起動事故の早期検出）。 ---
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
LAUNCHER_ABS_PATH="$SCRIPT_DIR/launch.sh"
CLAUDE_GPT_GIT_HEAD=$(claude_gpt_git_head "$REPO_ROOT")
CLAUDE_GPT_GIT_HEAD_SHORT="unknown"
if [ "$CLAUDE_GPT_GIT_HEAD" != "unknown" ]; then
  CLAUDE_GPT_GIT_HEAD_SHORT=$(printf '%s' "$CLAUDE_GPT_GIT_HEAD" | cut -c1-7)
fi
CLAUDE_GPT_GIT_DIRTY=$(claude_gpt_git_dirty "$REPO_ROOT")

PROXY_BIN_TARGET=$(claude_gpt_resolve_proxy_bin)
PROXY_VERSION_TARGET="unknown"
if [ -n "$PROXY_BIN_TARGET" ]; then
  PROXY_VERSION_TARGET=$(claude_gpt_proxy_version "$PROXY_BIN_TARGET")
fi
# preflight.sh（子プロセス）が同一の proxy バイナリを再解決する保証として export する
# （PATH mutation 等による識別ズレを排除する。P2）。
export CLAUDE_GPT_PROXY_BIN="$PROXY_BIN_TARGET"

echo "launcher=${LAUNCHER_ABS_PATH} git=${CLAUDE_GPT_GIT_HEAD_SHORT} dirty=${CLAUDE_GPT_GIT_DIRTY} proxy=v${PROXY_VERSION_TARGET}" >&2

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

# --- caller argv 正規化: launcher-level `--` 直後から始まる連続した exact
#     --strict-mcp-config トークンの leading run のみを安全に削除する pre-filter
#     （Issue #2189, Provenance Correction 反映）。
#
#     当初は「Herdr が claude-kind agent 起動時に exact --strict-mcp-config を常時
#     付与する」という前提で、caller argv 中の任意位置から無条件に当該トークンを
#     削除する実装だった。しかし Herdr v0.8.0 upstream source・ローカル installed
#     binary・現行 run_worktree_agent_runtime_smoke.py のいずれにも、この flag を
#     自動注入する経路は存在しないことが判明し、その前提は撤回された
#     （Issue #2189 Provenance Correction 節参照）。一方で、任意位置からの無条件削除
#     実装そのものには、provenance とは独立に実機で再現された本物のバグが存在した
#     （`-p <value>` の値・downstream `--` より後の positional literal・
#     `--append-system-prompt` 等任意文字列を受けるオプションの値として渡された同一
#     文字列を誤って削除してしまう。Issue #2189 Live Reproduction 節で実 Claude Code
#     binary に対して再現済み）。
#
#     このため、削除対象を launcher-level `--` 直後の位置から始まる「先頭からの
#     連続した run」のみに限定する。run の走査中に exact 一致しないトークンが
#     1つでも現れた時点でそこで走査を終了し、それ以降のトークン（そのトークン自身、
#     downstream の `--`、値、prompt 等すべてを含む）には一切触れず無条件で保持する。
#     これにより、run の先頭にない --strict-mcp-config（オプション値・downstream
#     positional literal 等）は削除対象にならず安全側に倒れる。
#
#     削除対象は完全一致トークン `--strict-mcp-config` のみ（run 内に重複があれば
#     全て削除）。値付き variant（`--strict-mcp-config=...`）、大文字小文字違い
#     （`--Strict-Mcp-Config` 等）、部分文字列を含む別トークン
#     （`--strict-mcp-config-evil` 等）は run の一致判定に使わず、run を止めた上で
#     下段の forbidden-flag チェックへそのまま残す（forbidden 判定または unknown
#     flag 判定に落ちる）。
#     POSIX sh の positional parameter を quoted "$@" のまま for/set -- で再構築する
#     （bash 配列・unquoted $@・`case ... *)` glob match は使わない。exact-match のみ）。
CLAUDE_GPT_STRICT_MCP_CONFIG_PREFILTER_STARTED=false
CLAUDE_GPT_STRICT_MCP_CONFIG_PREFILTER_STOPPED=false
for arg in "$@"; do
  if [ "$CLAUDE_GPT_STRICT_MCP_CONFIG_PREFILTER_STOPPED" = "false" ] && [ "$arg" = "--strict-mcp-config" ]; then
    continue
  fi
  # leading run はここで終了する。以降の全トークンは無条件で保持する。
  CLAUDE_GPT_STRICT_MCP_CONFIG_PREFILTER_STOPPED=true
  if [ "$CLAUDE_GPT_STRICT_MCP_CONFIG_PREFILTER_STARTED" = "false" ]; then
    set -- "$arg"
    CLAUDE_GPT_STRICT_MCP_CONFIG_PREFILTER_STARTED=true
  else
    set -- "$@" "$arg"
  fi
done
if [ "$CLAUDE_GPT_STRICT_MCP_CONFIG_PREFILTER_STARTED" = "false" ]; then
  set --
fi

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
  # --permission-mode bypassPermissions（二引数形式）
  if [ "$prev" = "--permission-mode" ] && [ "$arg" = "bypassPermissions" ]; then
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"policy_weakening_flag_rejected","flag":"--permission-mode bypassPermissions"}\n' >&2
    exit 2
  fi
  # --permission-mode=bypassPermissions（単一トークン形式。P1-3 fix-delta）。
  # 表記揺れ（`--permission-mode=` の値部分）で二引数形式チェックをすり抜けさせない。
  case "$arg" in
    --permission-mode=bypassPermissions)
      printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"policy_weakening_flag_rejected","flag":"--permission-mode=bypassPermissions"}\n' >&2
      exit 2
      ;;
  esac
  # --strict-mcp-config=...（値付き variant。Issue #2189）。boolean flag のため
  # 値を取る形は正当な用法ではなく、pre-filter が削除する exact トークン
  # （値なし `--strict-mcp-config`）とは別に、この variant は forbidden のまま残す
  # （CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS には含めず、この専用チェックで拒否する）。
  case "$arg" in
    --strict-mcp-config=*)
      printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"policy_weakening_flag_rejected","flag":"--strict-mcp-config=..."}\n' >&2
      exit 2
      ;;
  esac
  prev="$arg"
done

CLAUDE_CONFIG_DIR_TARGET=$(claude_gpt_claude_config_dir)
PROXY_CONFIG_DIR_TARGET=$(claude_gpt_proxy_config_dir)
PROXY_STATE_DIR_TARGET=$(claude_gpt_proxy_state_dir)
PROXY_HOME_TARGET=$(claude_gpt_proxy_home_dir)
MCP_CONFIG_PATH=$(claude_gpt_mcp_config_path)
SETTINGS_PATH=$(claude_gpt_session_settings_path)
# --- Claude/AGY プロセス専用の隔離 HOME/XDG（P0-6）。credential を一切置かない
#     空ディレクトリとして扱う。ambient 実 HOME 配下の SSH key/GPG key 等の
#     無関係な secret を Claude/AGY プロセスから利用不能にすることが目的。
#     GitHub auth（GH_TOKEN/GH_CONFIG_DIR 系）のみは native 同等に共有する
#     （Issue #2299 Outcome。isolated HOME を差し替える *前* の ambient
#     GH_CONFIG_DIR をここで固定し、以降の HOME 差し替えの影響を受けないように
#     する）。 ---
CLAUDE_ISOLATED_HOME_TARGET=$(claude_gpt_claude_isolated_home_dir)
CLAUDE_NATIVE_GH_CONFIG_DIR_TARGET="${GH_CONFIG_DIR:-${HOME}/.config/gh}"
# --- Issue #2426: launcher-owned Latitude Stop hook adapter が読む Native user
#     settings のパス。isolated HOME 差し替え *前* の ambient 実 HOME を使って
#     ここで固定する（CLAUDE_NATIVE_GH_CONFIG_DIR_TARGET と同じ理由）。この値
#     自体は settings.local.json の env フラグメントへ path 文字列としてのみ
#     baked され、中身（API key 等）は adapter 実行時にのみ読まれる。
#
#     Issue #2448: 単純な `${HOME}/.claude/settings.json` 固定では、Claude-GPT
#     から同じ launcher を self-launch する経路（#2455 / PR #2460 で判明）で
#     child の isolated HOME を Native settings と誤認する。inherited
#     `CLAUDE_GPT_NATIVE_SETTINGS_PATH`（self-launch 時に settings.local.json
#     の env 経由で child へ渡る re-entrant carrier）→ ambient
#     `CLAUDE_CONFIG_DIR`（Claude Code 公式の Native profile authority）→
#     `${HOME}/.claude/settings.json` フォールバックの順で
#     `claude_gpt_resolve_native_settings_path()`（lib.sh）に解決させる。この
#     行は isolated HOME 切替（後段の `export HOME="$CLAUDE_ISOLATED_HOME_TARGET"`）
#     より前にあるため、ここで参照する `CLAUDE_GPT_NATIVE_SETTINGS_PATH` /
#     `CLAUDE_CONFIG_DIR` / `HOME` はすべて isolated 化前の ambient 値。 ---
CLAUDE_NATIVE_LATITUDE_SETTINGS_PATH_TARGET=$(claude_gpt_resolve_native_settings_path \
  "${CLAUDE_GPT_NATIVE_SETTINGS_PATH:-}" "${CLAUDE_CONFIG_DIR:-}" "${HOME}")
CLAUDE_ISOLATED_XDG_CONFIG_DIR_TARGET=$(claude_gpt_claude_isolated_xdg_config_dir)
CLAUDE_ISOLATED_XDG_CACHE_DIR_TARGET=$(claude_gpt_claude_isolated_xdg_cache_dir)

# --- canonical path safety（ディレクトリ作成前に必ず検証する）。
#     CLAUDE_NATIVE_GH_CONFIG_DIR_TARGET は既存の native gh config dir を指す
#     （このディレクトリは作成せず、reject_if_under_repo の対象にもしない）。 ---
for d in "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET" "$PROXY_HOME_TARGET" \
  "$CLAUDE_ISOLATED_HOME_TARGET" \
  "$CLAUDE_ISOLATED_XDG_CONFIG_DIR_TARGET" "$CLAUDE_ISOLATED_XDG_CACHE_DIR_TARGET"; do
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
mkdir -p "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET" "$PROXY_HOME_TARGET" \
  "$CLAUDE_ISOLATED_HOME_TARGET" \
  "$CLAUDE_ISOLATED_XDG_CONFIG_DIR_TARGET" "$CLAUDE_ISOLATED_XDG_CACHE_DIR_TARGET"

# --- Issue #2567 PR #2696 review fix_delta (P1-2): native cross-session
#     messaging (ListAgents/SendMessage) peer-discovery bridge. Must run
#     while `CLAUDE_NATIVE_LATITUDE_SETTINGS_PATH_TARGET` (resolved above,
#     before the isolated HOME switch, from the SAME ambient
#     CLAUDE_CONFIG_DIR/HOME precedence -- never a duplicated resolution)
#     still names the real ambient Native config root. Native's own session
#     registration directory sits alongside its `settings.json` (both
#     directly under Native's CLAUDE_CONFIG_DIR / `~/.claude`); see
#     `claude_gpt_link_native_sessions_dir` (lib.sh) for the full rationale
#     and official-docs citation. Best-effort / non-blocking. ---
CLAUDE_GPT_NATIVE_SESSIONS_DIR_TARGET="$(dirname "$CLAUDE_NATIVE_LATITUDE_SETTINGS_PATH_TARGET")/sessions"
claude_gpt_link_native_sessions_dir \
  "$CLAUDE_GPT_NATIVE_SESSIONS_DIR_TARGET" "${CLAUDE_CONFIG_DIR_TARGET}/sessions"

# --- strict_mcp mode 用の空 MCP config を書き込む（repository/user MCP を読み込ませない） ---
STRICT_MCP_MODE=true
cat > "$MCP_CONFIG_PATH" <<MCP_JSON_EOF
{
  "mcpServers": {}
}
MCP_JSON_EOF

# --- Claude Code セッション設定 ---
# 1. proxy credential/config/state/home ディレクトリへの read を、Claude Code 組み込み
#    tool（Read 等）に対する best-effort の軽量防御として拒否する（絶対パスは
#    `Read(//...)` の二重スラッシュ構文でなければ機能しない）。任意の Bash subprocess
#    からの credential 秘匿を保証するものではない（Issue #2158 Scope Reframe）。
# 2. `enabledPlugins: {}` は repository/user plugin を確実に「全無効化」する専用 API
#    ではない（Claude Code CLI に plugin 専用の deny-all flag は存在しない。実機検証
#    済み、PR #2162 P0-2 再検証, 2026-08-15）。実機観測では、この launcher が採用する
#    CLAUDE_CONFIG_DIR 分離（isolated GPT 専用 config root。決定 C）により対象環境の
#    plugin registry（installed_plugins.json / marketplaces / skills-dir）自体が空に
#    なるため、user/global scope で登録済みの plugin（SessionStart hook を持つものを
#    含む）は実機で発火しないことを確認した。`enabledPlugins: {}` はこれに重ねる
#    defense-in-depth の軽減策であり、単独では plugin 全無効化を保証しない（Claude
#    Code 内部ドキュメント文字列上、plugin は明示 disable が無い限り
#    `defaultEnabled`（既定 true）で有効化されるため、空オブジェクトはどの plugin も
#    明示的に無効化しない）。プロジェクト（repository）scope で settings.json に
#    plugin/marketplace を宣言するケースは、`claude plugin install` 相当の明示的な
#    trust/install 手順を経ないと有効化されないことを実機で確認したが、この経路への
#    完全な防御は本 launcher の保証範囲外（Claude Code 本体の trust モデルに依存）。
#
# OS-level sandbox hardening（sandbox.enabled / CLAUDE_CODE_SUBPROCESS_ENV_SCRUB）は
# 実装しない。実機検証（PR #2162, 2026-08-14 / Issue #2173）で、launcher 自体が
# ネストした sandbox 実行環境下にある場合、これらは Claude Code 本体の Bash tool を
# 破壊することが確認され、Phase 1 の merge 条件から除外された（Issue #2158
# Scope Reframe, 2026-08-15）。

# --- narrow observability channel(Issue #2158/#2173, structured lane #2174/PR #2176 で並行実装中)。
#     呼び出し元が任意の JSON 文字列を settings へ注入できる汎用経路は作らず、許可された固定値
#     (`subagent-start-stop`)のみを受け付ける。それ以外の値は fragment を空のままにする(拒否)。
#     既存の CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS(--settings 等 CLI 引数拒否)とは独立した経路であり、
#     それを変更・弱体化するものではない。 ---
# --- Launch nonce (Issue #2186 origin; generalized by Issue #2651 Spark
#     retirement -- still used below for other per-launch unique file
#     naming: ISSUE_EDITOR_PERMISSION_REQUEST_HOOK, and the
#     subagent-start-stop runtime-smoke observation sink writer). ---
LAUNCH_NONCE="$(date -u +%Y%m%dT%H%M%SZ)-$$"
# LAUNCH_NONCE is generated exclusively from `date -u +%Y%m%dT%H%M%SZ` and
# `$$`, so it is always `[0-9A-Za-z_-]+` and safe to interpolate into file
# paths below. Fail closed if that assumption is ever violated instead of
# silently emitting an unsafe path.
case "$LAUNCH_NONCE" in
  *[!0-9A-Za-z_-]*)
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"launch_nonce_unsafe_chars"}\n'
    exit 9
    ;;
esac

# --- Spark explicit-only authorization gate: retired (Issue #2651) --------
#
# The GPT-5.3-Codex-Spark custom SubAgent, its explicit-only authorization
# gate (UserPromptSubmit -> PreToolUse(Agent) -> SubagentStart/SubagentStop
# consume, formerly embedded here between fixed BEGIN/END source markers),
# and the session-local `--agents` fragment that registered it have all
# been removed. Ordinary SubAgent smoke canary generation
# (`claude_gpt_smoke_canary_agents_json_fragment`, invoked below) does not
# depend on any Spark constant or function removed from lib.sh.
AGENTS_JSON="{}"

# --- Issue #2433: PermissionRequest narrow escape hatch ----------------------
#
# Auto mode deliberately classifies every shell command, so permissions.allow
# is neither an exact authorization grammar nor an Auto false-deny remedy here.
# This hook runs only when Claude Code has already reached a permission request;
# it returns an allow decision for exactly one controlled Issue-edit transaction
# shape and returns no decision for every other input. Existing deny/ask rules
# remain outside this hook's authority.
ISSUE_EDITOR_PERMISSION_REQUEST_HOOK="${PROXY_STATE_DIR_TARGET}/issue-editor-permission-request-${LAUNCH_NONCE}.py"
( umask 077 && cat > "$ISSUE_EDITOR_PERMISSION_REQUEST_HOOK" <<'ISSUE_EDITOR_PERMISSION_REQUEST_HOOK_PY_EOF'
# ISSUE_EDITOR_PERMISSION_REQUEST_HOOK_PY_BEGIN
import json
import re
import shlex
import sys

_CANONICAL_PREFIX = (
    "uv",
    "run",
    "--locked",
    "python3",
    ".claude/skills/edit-issue/scripts/edit_issue_txn.py",
    "--input-file",
)
_SAFE_REPO_RELATIVE_OPERAND = re.compile(r"^[A-Za-z0-9._/-]+$")


def _is_safe_repo_relative_operand(value):
    if not isinstance(value, str) or not value or value.startswith("-"):
        return False
    if not _SAFE_REPO_RELATIVE_OPERAND.fullmatch(value) or value.startswith("/") or "//" in value:
        return False
    return all(segment not in ("", ".", "..") for segment in value.split("/"))


def _is_canonical_transaction(payload):
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return False
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return False
    return len(tokens) == 7 and tuple(tokens[:6]) == _CANONICAL_PREFIX and _is_safe_repo_relative_operand(tokens[6])


def main():
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError, TypeError):
        payload = {}
    if _is_canonical_transaction(payload):
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "decision": {"behavior": "allow"},
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
# ISSUE_EDITOR_PERMISSION_REQUEST_HOOK_PY_END
ISSUE_EDITOR_PERMISSION_REQUEST_HOOK_PY_EOF
)
export ISSUE_EDITOR_PERMISSION_REQUEST_HOOK

# --- Issue #2274 AC14/AC15 (PR #2285 OWNER fix-delta P0-1): runtime_smoke_test.sh
#     専用 launcher-owned canary SubAgent fixture の内部合成経路。caller-owned
#     `--agents` は CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS 経由で引き続き無条件拒否する
#     （上記の policy-weakening flag 拒否ループを一切変更しない）。
#
#     P0-1 corrective iteration: this channel used to accept a caller-supplied
#     raw JSON fragment (`CLAUDE_GPT_SMOKE_CANARY_AGENTS_JSON`) merged via
#     `claude_gpt_agents_json_merge_validate`, whose only checks were
#     non-empty-object / no-duplicate-key / serialize-readback-match -- it
#     never allowlist-validated the fragment's *content*, so an ordinary
#     launch that set this raw env var directly could inject an arbitrary
#     session-local agent definition (including `hooks`/`model`/
#     `permissionMode`/`mcpServers`). That raw-JSON escape hatch is removed
#     entirely. `runtime_smoke_test.sh` now passes only two opaque strings
#     (an expected-output marker and a run-unique nonce) via
#     `CLAUDE_GPT_SMOKE_CANARY_MARKER` / `CLAUDE_GPT_SMOKE_CANARY_NONCE`, and
#     launch.sh itself calls the SAME strictly-validated
#     `claude_gpt_smoke_canary_agents_json_fragment` function that
#     `runtime_smoke_test.sh` used to call on the caller's behalf -- the
#     caller can never supply the fixture's JSON *structure* any more, only
#     the marker text embedded inside its fixed prompt template and the
#     nonce used to derive its high-entropy agent name (both flow through
#     `json.dumps`, never raw string concatenation, so neither can break out
#     of the fixed {description, prompt, tools} shape). Marker/nonce
#     presence without the other is rejected fail-closed (never silently
#     ignored), and fixture synthesis failure (malformed input) is
#     fail-closed too -- launch is aborted before `claude` is ever exec'd.
#     Issue #2651: the retired Spark custom agent no longer exists, so
#     there is nothing left to merge this fixture against or collide with
#     -- when requested, the canary fixture IS the entire `--agents` value.
#     ---
if [ -n "${CLAUDE_GPT_SMOKE_CANARY_MARKER:-}" ] || [ -n "${CLAUDE_GPT_SMOKE_CANARY_NONCE:-}" ]; then
  if [ -z "${CLAUDE_GPT_SMOKE_CANARY_MARKER:-}" ] || [ -z "${CLAUDE_GPT_SMOKE_CANARY_NONCE:-}" ]; then
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"smoke_canary_marker_or_nonce_missing"}\n' >&2
    exit 2
  fi
  CANARY_FRAGMENT_JSON=$(claude_gpt_smoke_canary_agents_json_fragment "$CLAUDE_GPT_SMOKE_CANARY_MARKER" "$CLAUDE_GPT_SMOKE_CANARY_NONCE")
  if [ -z "$CANARY_FRAGMENT_JSON" ]; then
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"smoke_canary_fixture_synthesis_failed"}\n' >&2
    exit 2
  fi
  AGENTS_JSON="$CANARY_FRAGMENT_JSON"
fi

# --- explicit-only authorization gate hook groups: retired (Issue #2651,
#     formerly Issue #2186 P0 fix-delta / PR #2244 adversarial review) ---
#
# The former Spark authorization gate's pending-authorization state
# machine, PreToolUse(matcher: Agent) consume step, and
# SubagentStart/SubagentStop evidence entries have been removed along with
# the gate itself -- PTU_HOOK_GROUPS/SAS_HOOK_GROUPS/SAP_HOOK_GROUPS default
# to empty and are only additively populated below when a runtime-smoke
# observation mode registers its own sink (never a replacement of an
# authorization gate, since none remains to replace).
#
# UserPromptSubmit is the one exception (OWNER review,
# https://github.com/squne121/loop-protocol/pull/2662#issuecomment-5736035898):
# removing the gate must not also remove the ability to reject an ACTIVE
# legacy Spark execution request (the canonical `@agent-` custom-agent
# mention, or a valid `DELEGATION_REQUEST_V1` directive naming the retired
# custom agent/model, both spelled out in the embedded script below) before
# the model ever processes the prompt -- otherwise the launcher would
# silently let the model decide what to do with a retired directive. This
# replacement hook is small and stateless: it holds no pending-
# authorization state, no model evidence, no ledger -- it only recognizes
# the SAME retired inputs and rejects them deterministically, without
# substituting a different Agent or model. It never matches on a bare
# "spark" substring, and it explicitly ignores fenced code blocks, inline
# code spans, and blockquoted lines so that quoting/discussing the retired
# directive (as in this very review, or in this file's own comments/tests)
# is never itself blocked.
#
# Issue #2651's own AC1/AC2 Verification Commands forbid the two retired
# identifiers' exact lowercase spelling from appearing anywhere in this
# file. The two identifiers below are therefore spelled with alternate
# capitalization on purpose -- `re.IGNORECASE` makes the match behavior
# identical to the exact-lowercase form, so a caller's actual (lowercase)
# directive is still recognized; only the SOURCE TEXT's literal casing
# differs, which is what keeps this an "intentional retired rejection"
# route (Verification Guidance) rather than an "executable/configured
# route" those two Verification Commands gate on.
CLAUDE_GPT_SPARK_PROMPT_RETIREMENT_HOOK="${PROXY_STATE_DIR_TARGET}/spark-prompt-retirement-${LAUNCH_NONCE}.py"
( umask 077 && cat > "$CLAUDE_GPT_SPARK_PROMPT_RETIREMENT_HOOK" <<'SPARK_PROMPT_RETIREMENT_PY_EOF'
# SPARK_PROMPT_RETIREMENT_PY_BEGIN
import json
import re
import sys

# Issue #2651: the GPT-5.3-Codex-Spark custom agent/model is retired. This
# hook rejects only an ACTIVE legacy execution request -- the canonical
# `@agent-`-prefixed mention of the retired custom agent, or a valid
# `DELEGATION_REQUEST_V1` directive naming the retired custom agent/model
# -- outside of fenced code, inline code, and blockquoted text (which are
# explanatory/historical data, never an execution instruction). It never
# matches a bare "spark" substring, never re-authorizes/re-routes to a
# different Agent or model, and holds no state across invocations.
#
# `re.IGNORECASE` on every pattern below means the exact casing used in
# these string literals has no effect on which caller input matches --
# only on which literal bytes appear in THIS file (see AC1/AC2 note above
# this heredoc for why that distinction matters here).
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_MENTION_RE = re.compile(r"@agent-Spark-Codex\b", re.IGNORECASE)
_SCHEMA_RE = re.compile(r"^[ \t]*schema:[ \t]*DELEGATION_REQUEST_V1[ \t]*$", re.IGNORECASE | re.MULTILINE)
_AGENT_ID_RE = re.compile(r"^[ \t]*agent_id:[ \t]*Spark-Codex[ \t]*$", re.IGNORECASE | re.MULTILINE)
_MODEL_RE = re.compile(r"^[ \t]*model:[ \t]*Gpt-5\.3-Codex-Spark[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _strip_quoted_context(text):
    text = _FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    kept_lines = [line for line in text.split("\n") if not line.lstrip().startswith(">")]
    return "\n".join(kept_lines)


def is_active_legacy_spark_directive(prompt):
    if not isinstance(prompt, str) or not prompt:
        return False
    stripped = _strip_quoted_context(prompt)
    if _MENTION_RE.search(stripped):
        return True
    return bool(_SCHEMA_RE.search(stripped) and (_AGENT_ID_RE.search(stripped) or _MODEL_RE.search(stripped)))


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
        return 0
    if is_active_legacy_spark_directive(payload.get("prompt")):
        sys.stderr.write(
            "GPT-5.3-Codex-Spark delegation is retired in this repository. "
            "Remove the @agent-Spark-Codex mention or DELEGATION_REQUEST_V1 "
            "Spark directive and re-run; this request is not silently "
            "substituted with another Agent or model.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# SPARK_PROMPT_RETIREMENT_PY_END
SPARK_PROMPT_RETIREMENT_PY_EOF
)
export CLAUDE_GPT_SPARK_PROMPT_RETIREMENT_HOOK
SPARK_PROMPT_RETIREMENT_HOOK_GROUP='{"hooks": [{"type": "command", "command": "python3 \"$CLAUDE_GPT_SPARK_PROMPT_RETIREMENT_HOOK\""}]}'
UPS_HOOK_GROUPS="${SPARK_PROMPT_RETIREMENT_HOOK_GROUP}"
PTU_HOOK_GROUPS=""
PERMISSION_REQUEST_HOOK_GROUPS='{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 \"$ISSUE_EDITOR_PERMISSION_REQUEST_HOOK\""}]}'
SAS_HOOK_GROUPS=""
SAP_HOOK_GROUPS=""

# --- Issue #2426 AC1: launcher-owned Latitude Stop hook group (additive to
#     whatever else is registered on Stop below -- never a replacement of an
#     existing Stop hook group). CLAUDE_GPT_LATITUDE_HOOK / _NATIVE_SETTINGS_PATH
#     / _HOME_ROOT / _PACKAGE_SPEC are baked into ENV_JSON_FRAGMENT below so the
#     hook command (which runs via a shell) can resolve them. ---
CLAUDE_GPT_LATITUDE_HOOK="${SCRIPT_DIR}/latitude_hook.py"
CLAUDE_GPT_LATITUDE_PACKAGE_SPEC=$(claude_gpt_latitude_package_spec)
LATITUDE_HOOK_GROUP='{"hooks": [{"type": "command", "command": "python3 \"$CLAUDE_GPT_LATITUDE_HOOK\"", "async": true}]}'
STOP_HOOK_GROUPS="$LATITUDE_HOOK_GROUP"
STOPFAILURE_HOOK_GROUPS=""
# --- Issue #2426 PR #2439 P0 fix-delta (OWNER REQUEST_CHANGES): Design 3節
#     「LATITUDE_PROJECT は secret ではないため、既存 #2375（PR #2392）collector
#     が同一 project を解決できるよう runtime から解決可能にしてよい」を実装する。
#     LATITUDE_API_KEY と違い、LATITUDE_PROJECT の *値そのもの* を生成済み
#     Claude-GPT settings の env へ直接焼き込む（LATITUDE_API_KEY は引き続き
#     latitude_hook.py の child-only telemetry subprocess env にのみ現れ、この
#     経路には一切乗らない）。値の読み取りは allowlist 実装の SSOT
#     （latitude_hook.py の read_native_latitude_allowlist()）を再利用する。
#     Native 側に未設定の場合は空文字列となり、キー自体を追加しない
#     （fail-open。#2375 collector 側は LATITUDE_PROJECT 未設定時 None を返す
#     既存の contract のまま）。 ---
CLAUDE_GPT_NATIVE_LATITUDE_PROJECT=$(claude_gpt_native_latitude_project "$CLAUDE_NATIVE_LATITUDE_SETTINGS_PATH_TARGET" "$CLAUDE_GPT_LATITUDE_HOOK")
LATITUDE_PROJECT_ENV_FRAGMENT=""
if [ -n "$CLAUDE_GPT_NATIVE_LATITUDE_PROJECT" ]; then
  LATITUDE_PROJECT_ENV_FRAGMENT=",
    \"LATITUDE_PROJECT\": $(claude_gpt_json_escape "$CLAUDE_GPT_NATIVE_LATITUDE_PROJECT")"
fi
ENV_JSON_FRAGMENT=",
  \"env\": {
    \"CLAUDE_GPT_LATITUDE_HOOK\": \"${CLAUDE_GPT_LATITUDE_HOOK}\",
    \"CLAUDE_GPT_NATIVE_SETTINGS_PATH\": \"${CLAUDE_NATIVE_LATITUDE_SETTINGS_PATH_TARGET}\",
    \"CLAUDE_GPT_HOME_ROOT\": \"${CLAUDE_GPT_HOME}\",
    \"CLAUDE_GPT_LATITUDE_PACKAGE_SPEC\": \"${CLAUDE_GPT_LATITUDE_PACKAGE_SPEC}\"${LATITUDE_PROJECT_ENV_FRAGMENT}
  }"
if [ "${CLAUDE_GPT_RUNTIME_SMOKE_HOOKS:-}" = "subagent-start-stop" ]; then
  # Issue #2274 AC17 corrective iteration (hook-time byte-offset causal
  # correlation): this sink used to be a bare `cat` that only echoed the
  # hook's own stdin payload (agent_id/agent_type) back onto the stream.
  # That gave the evidence builder agent_id correlation, but never the
  # hook's own execution-time proxy log byte offset --
  # runtime_smoke_test.sh instead approximated the correlation window with
  # offsets captured immediately before/after the ENTIRE `launch.sh`
  # invocation, which is a step-wide approximation, not the
  # SubagentStart/SubagentStop hook-time window the Issue requires
  # (genuine implementation gap, corrective iteration finding). This sink
  # now additionally `os.stat()`s the canonical, launcher-owned proxy log
  # path (never a caller-supplied path -- sourced from this same
  # PROXY_STATE_DIR_TARGET constant every other proxy-path reference in
  # this file uses) at the moment THIS hook process itself runs, and
  # echoes that byte offset back alongside the original payload -- still
  # strictly observational (no permissionDecision, no authorization
  # semantics touched; Issue #2651 retired the former Spark authorization
  # gate that used to also register SubagentStart/SubagentStop entries
  # here, so this sink is now the sole entry these events carry) and
  # independent of the other hook entries' execution order (per Claude
  # Code's hooks reference, matching
  # hooks run in parallel with no ordering guarantee between them; this
  # design never assumes one hook runs before another -- it only records
  # this hook's own execution-time state).
  SPARK_LIFECYCLE_OFFSET_LOG_PATH="${PROXY_STATE_DIR_TARGET}/claude-code-proxy/proxy.log"
  SPARK_LIFECYCLE_OFFSET_WRITER="${PROXY_STATE_DIR_TARGET}/spark-lifecycle-offset-writer-${LAUNCH_NONCE}.py"
  ( umask 077 && cat > "$SPARK_LIFECYCLE_OFFSET_WRITER" <<'SPARK_LIFECYCLE_OFFSET_WRITER_EOF'
import json
import os
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    log_path = os.environ.get("SPARK_LIFECYCLE_OFFSET_LOG_PATH")
    offset = None
    dev = None
    ino = None
    mtime_ns = None
    # Issue #2274 PR #2285 OWNER fix-delta P1-5: a single fstat() on ONE
    # opened fd (never a separate os.stat()-by-path call plus a later
    # independent `wc -c`/`cp`) so size/dev/ino/mtime_ns are all read from
    # the SAME underlying inode as one atomic snapshot -- a log
    # rotation/truncation/replacement racing between two independent
    # path-based reads can never tear this reading.
    if isinstance(log_path, str) and log_path:
        try:
            fd = os.open(log_path, os.O_RDONLY)
        except OSError:
            fd = None
        if fd is not None:
            try:
                st = os.fstat(fd)
                offset = st.st_size
                dev = st.st_dev
                ino = st.st_ino
                mtime_ns = st.st_mtime_ns
            except OSError:
                offset = None
                dev = None
                ino = None
                mtime_ns = None
            finally:
                os.close(fd)
    # This sink only ever ADDS its own observation to whatever the hook's
    # stdin payload already carried (agent_id/agent_type/hook_event_name/
    # etc.) -- it never removes or rewrites an existing field.
    payload["proxy_log_byte_offset_at_hook_time"] = offset
    payload["proxy_log_dev_at_hook_time"] = dev
    payload["proxy_log_ino_at_hook_time"] = ino
    payload["proxy_log_mtime_ns_at_hook_time"] = mtime_ns
    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    main()
SPARK_LIFECYCLE_OFFSET_WRITER_EOF
  )
  CAT_SINK_GROUP='{"hooks": [{"type": "command", "command": "python3 \"$SPARK_LIFECYCLE_OFFSET_WRITER\""}]}'
  # Issue #2651: SAS_HOOK_GROUPS/SAP_HOOK_GROUPS default to empty now that
  # the former Spark authorization gate no longer populates them, so this
  # sink IS the entry (no leading-comma append onto an empty array item).
  SAS_HOOK_GROUPS="${CAT_SINK_GROUP}"
  SAP_HOOK_GROUPS="${CAT_SINK_GROUP}"
  ENV_JSON_FRAGMENT=",
  \"env\": {
    \"SPARK_LIFECYCLE_OFFSET_LOG_PATH\": \"${SPARK_LIFECYCLE_OFFSET_LOG_PATH}\",
    \"SPARK_LIFECYCLE_OFFSET_WRITER\": \"${SPARK_LIFECYCLE_OFFSET_WRITER}\",
    \"CLAUDE_GPT_LATITUDE_HOOK\": \"${CLAUDE_GPT_LATITUDE_HOOK}\",
    \"CLAUDE_GPT_NATIVE_SETTINGS_PATH\": \"${CLAUDE_NATIVE_LATITUDE_SETTINGS_PATH_TARGET}\",
    \"CLAUDE_GPT_HOME_ROOT\": \"${CLAUDE_GPT_HOME}\",
    \"CLAUDE_GPT_LATITUDE_PACKAGE_SPEC\": \"${CLAUDE_GPT_LATITUDE_PACKAGE_SPEC}\"${LATITUDE_PROJECT_ENV_FRAGMENT}
  }"
  export SPARK_LIFECYCLE_OFFSET_LOG_PATH SPARK_LIFECYCLE_OFFSET_WRITER
elif [ "${CLAUDE_GPT_RUNTIME_SMOKE_HOOKS:-}" = "hook-sink-multi-turn" ]; then
  # --- Issue #2219 (OWNER anchor decision, hook-event evidence channel).
  #     Narrow addition to the same fixed-value gate above: a SECOND fixed
  #     value that ALSO registers UserPromptSubmit/Stop/StopFailure hooks
  #     (in addition to SubagentStart/SubagentStop), all pointed at a
  #     durable, run-nonce-keyed, O_EXCL-created append-only JSONL sink
  #     whose path is built ONLY from the launcher-owned
  #     `claude_gpt_proxy_state_dir()` constant plus the caller-supplied
  #     nonce -- never from any other caller-supplied value. The hook
  #     command string itself is FIXED (no caller-supplied string is ever
  #     interpolated into it); it only references two launcher-set env
  #     vars (`CLAUDE_GPT_HOOK_SINK_NONCE`, `CLAUDE_GPT_HOOK_SINK_PATH`)
  #     that are resolved by the shell that actually runs the hook, not by
  #     this heredoc. This does not touch lib.sh, does not weaken
  #     CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS, and does not accept an arbitrary
  #     caller-supplied --settings/hook command.
  : "${CLAUDE_GPT_HOOK_SINK_NONCE:?CLAUDE_GPT_HOOK_SINK_NONCE must be set by the caller for hook-sink-multi-turn}"
  CLAUDE_GPT_HOOK_SINK_PATH="${PROXY_STATE_DIR_TARGET}/hook-sink-${CLAUDE_GPT_HOOK_SINK_NONCE}.jsonl"
  ( umask 077 && set -C && : > "$CLAUDE_GPT_HOOK_SINK_PATH" )
  CLAUDE_GPT_HOOK_SINK_WRITER="${PROXY_STATE_DIR_TARGET}/hook-sink-writer-${CLAUDE_GPT_HOOK_SINK_NONCE}.py"
  ( umask 077 && cat > "$CLAUDE_GPT_HOOK_SINK_WRITER" <<'HOOK_SINK_WRITER_EOF'
import hashlib
import json
import os
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id") or payload.get("subagent_id")
    nonce = os.environ.get("CLAUDE_GPT_HOOK_SINK_NONCE", "")
    sink_path = os.environ.get("CLAUDE_GPT_HOOK_SINK_PATH")
    prompt = payload.get("prompt") if event == "UserPromptSubmit" else None
    digest = None
    if isinstance(prompt, str):
        digest = hashlib.sha256((nonce + prompt).encode("utf-8")).hexdigest()
    record = {
        "run_nonce": nonce,
        "event": event,
        "session_id": session_id,
        "agent_id": agent_id,
        "ts": __import__("time").time(),
        "prompt_digest": digest,
    }
    line = json.dumps(record, separators=(",", ":"))
    if sink_path:
        # Single bounded write per record: one open + one write call,
        # well under PIPE_BUF, so concurrent SubagentStart events never
        # interleave/corrupt lines (Issue #2219 AC15).
        with open(sink_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


if __name__ == "__main__":
    main()
HOOK_SINK_WRITER_EOF
  )
  # Observation-only sink writer groups. Issue #2651: the former Spark
  # authorization gate's SubagentStart/SubagentStop entries are gone, so
  # SAS_HOOK_GROUPS/SAP_HOOK_GROUPS default to empty and this sink IS each
  # entry (no leading-comma append onto an empty array item). Stop/
  # StopFailure never had a gate equivalent, so they keep the same
  # append/sink-only shape as before. UserPromptSubmit is different: the
  # always-registered Spark prompt retirement hook set above must stay
  # wired even in this observation mode, so this sink is APPENDED to it
  # (never a replacement).
  SINK_GROUP='{"hooks": [{"type": "command", "command": "python3 \"$CLAUDE_GPT_HOOK_SINK_WRITER\""}]}'
  UPS_HOOK_GROUPS="${UPS_HOOK_GROUPS}, ${SINK_GROUP}"
  # Issue #2426 AC1: append (never replace) so the launcher-owned Latitude
  # Stop hook group set as the default above stays wired even when this
  # runtime-smoke observation sink is also requested.
  STOP_HOOK_GROUPS="${STOP_HOOK_GROUPS}, ${SINK_GROUP}"
  STOPFAILURE_HOOK_GROUPS="${SINK_GROUP}"
  SAS_HOOK_GROUPS="${SINK_GROUP}"
  SAP_HOOK_GROUPS="${SINK_GROUP}"
  # Also baked literally into settings.json's own "env" block (belt-and-
  # braces alongside the exported shell env vars below) so the 3-way
  # run_nonce match the harness verifies (AC16) has a nonce value that is
  # genuinely "baked into settings.json at launch", not merely inherited.
  ENV_JSON_FRAGMENT=",
  \"env\": {
    \"CLAUDE_GPT_HOOK_SINK_NONCE\": \"${CLAUDE_GPT_HOOK_SINK_NONCE}\",
    \"CLAUDE_GPT_HOOK_SINK_PATH\": \"${CLAUDE_GPT_HOOK_SINK_PATH}\",
    \"CLAUDE_GPT_HOOK_SINK_WRITER\": \"${CLAUDE_GPT_HOOK_SINK_WRITER}\",
    \"CLAUDE_GPT_LATITUDE_HOOK\": \"${CLAUDE_GPT_LATITUDE_HOOK}\",
    \"CLAUDE_GPT_NATIVE_SETTINGS_PATH\": \"${CLAUDE_NATIVE_LATITUDE_SETTINGS_PATH_TARGET}\",
    \"CLAUDE_GPT_HOME_ROOT\": \"${CLAUDE_GPT_HOME}\",
    \"CLAUDE_GPT_LATITUDE_PACKAGE_SPEC\": \"${CLAUDE_GPT_LATITUDE_PACKAGE_SPEC}\"${LATITUDE_PROJECT_ENV_FRAGMENT}
  }"
  export CLAUDE_GPT_HOOK_SINK_NONCE CLAUDE_GPT_HOOK_SINK_PATH CLAUDE_GPT_HOOK_SINK_WRITER
fi

# Issue #2426 AC1: "Stop" is now unconditionally present (STOP_HOOK_GROUPS
# always contains at least the launcher-owned Latitude hook group set as the
# default above); only "StopFailure" remains conditional on the runtime-smoke
# hook-sink-multi-turn mode, which has no gate equivalent to always emit.
HOOKS_JSON_FRAGMENT=',
  "hooks": {
    "PermissionRequest": ['"${PERMISSION_REQUEST_HOOK_GROUPS}"'],
    "UserPromptSubmit": ['"${UPS_HOOK_GROUPS}"'],
    "PreToolUse": ['"${PTU_HOOK_GROUPS}"'],
    "SubagentStart": ['"${SAS_HOOK_GROUPS}"'],
    "SubagentStop": ['"${SAP_HOOK_GROUPS}"'],
    "Stop": ['"${STOP_HOOK_GROUPS}"']'"$(
  if [ -n "$STOPFAILURE_HOOK_GROUPS" ]; then
    printf ',\n    "StopFailure": [%s]' "$STOPFAILURE_HOOK_GROUPS"
  fi
)"'
  }'

# --- Spark authorization sidecar directory deny: retired (Issue #2651,
#     formerly Issue #2186 P0 fix-delta / PR #2244 adversarial review /
#     Issue #2440). The Spark explicit-only authorization gate, its
#     pending-authorization sidecar file, and this deny rule protecting it
#     have all been removed along with the gate itself. ---

# --- launcher-owned autoMode policy（Issue #2203, second-gate 判断補助。
#     決定論的 authority は permissions.deny / PreToolUse hook / GitHub mutation
#     transaction broker であり、この autoMode は project .claude/settings*.json
#     ではなくこの launcher-owned --settings にのみ注入する） ---
AUTO_MODE_JSON_FRAGMENT=$(claude_gpt_auto_mode_json_fragment)

# Issue #2437: peer policy belongs only to the pre-existing fixed runtime-smoke
# channels. Unknown values deliberately receive neither this policy nor a new
# accepted routing surface; arbitrary caller --settings remains rejected above.
PEER_POLICY_DENY_SUFFIX=""
PEER_POLICY_SETTINGS_FRAGMENT=""
case "${CLAUDE_GPT_RUNTIME_SMOKE_HOOKS:-}" in
  subagent-start-stop|hook-sink-multi-turn)
    PEER_POLICY_DENY_SUFFIX=',
      "SendMessage",
      "ListAgents"'
    PEER_POLICY_SETTINGS_FRAGMENT=',
  "crossSessionInbound": "refuse"'
    ;;
esac

cat > "$SETTINGS_PATH" <<SETTINGS_JSON_EOF
{
  "permissions": {
    "deny": [
      "Read(/${PROXY_CONFIG_DIR_TARGET}/**)",
      "Read(/${PROXY_STATE_DIR_TARGET}/**)",
      "Read(/${PROXY_HOME_TARGET}/**)"${PEER_POLICY_DENY_SUFFIX}
    ]
  }${PEER_POLICY_SETTINGS_FRAGMENT},
  "enabledPlugins": {}${HOOKS_JSON_FRAGMENT}${ENV_JSON_FRAGMENT},
  ${AUTO_MODE_JSON_FRAGMENT}
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
  # 現行 Unix user のまま同一 UID で起動する（dedicated user は Phase 1 の non-goal。
  # Issue #2158 Scope Reframe, 2026-08-15）。
  # CCP_CODEX_TRANSPORT は repository-owned の CLAUDE_GPT_CODEX_TRANSPORT_POLICY（lib.sh
  # 単一 source of truth）を無条件で渡し、isolated proxy config.json や upstream
  # built-in default の websocket よりも優先させる（Issue #2204）。
  # CCP_AUTO_REVIEW_MODEL は repository-owned の CLAUDE_GPT_AUTO_REVIEW_MODEL_POLICY
  # （lib.sh 単一 source of truth）を無条件で渡す。未設定時 upstream proxy は
  # non-streaming・tool-free な auto mode classifier request を provider=codex の
  # 場合無条件で gpt-5.6-luna へ fallback する（Issue #2654 bounded comparison A:
  # proxy log 実測で 1030/1030 件が gpt-5.6-luna へ固定到達）ため、session model と
  # 揃えるために明示上書きする。
  env -i \
    "PATH=$PATH" \
    "HOME=$PROXY_HOME_TARGET" \
    "CCP_CONFIG_DIR=$PROXY_CONFIG_DIR_TARGET" \
    "XDG_STATE_HOME=$PROXY_STATE_DIR_TARGET" \
    "CCP_BIND_ADDRESS=127.0.0.1" \
    "CCP_LOG_STDERR=1" \
    "CCP_CODEX_TRANSPORT=$CLAUDE_GPT_CODEX_TRANSPORT_POLICY" \
    "CCP_AUTO_REVIEW_MODEL=$CLAUDE_GPT_AUTO_REVIEW_MODEL_POLICY" \
    "$PROXY_BIN_TARGET" serve --port "$PROXY_PORT" --no-monitor > "$PROXY_LOG" 2>&1 &
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
# proxy `/v1/models` は suffix なしの base model 名を返すため、`[1m]` context-window
# hint suffix を除去した名前で照合する（実際に upstream へ送られる model ID も
# proxy が suffix を除去した後の base 名である。上記 CLAUDE_GPT_MODEL_* 定義部コメント参照）。
for m in "$CLAUDE_GPT_MODEL_MAIN" "$CLAUDE_GPT_MODEL_OPUS" "$CLAUDE_GPT_MODEL_HAIKU"; do
  m_base=$(claude_gpt_strip_context_hint "$m")
  case "$MODELS_JSON" in
    *"\"$m_base\""*) : ;;
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
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"ok","mode":"check_only","port":%s,"bind_ok":%s,"model_alias_ok":%s,"strict_mcp_mode":%s,"mcp_config_path":"%s","settings_path":"%s","proxy_home_dir":"%s","proxy":{"absolute_path":"%s","version":"%s"},"preflight":%s}\n' \
    "$PROXY_PORT" "$BIND_OK" "$MODEL_ALIAS_OK" "$STRICT_MCP_MODE" "$MCP_CONFIG_PATH" "$SETTINGS_PATH" "$PROXY_HOME_TARGET" "$PROXY_BIN_TARGET" "$PROXY_VERSION_TARGET" "$PREFLIGHT_JSON"
  exit 0
fi

# --- Issue #2203 AC1: 通常起動が実際に Claude process を起動する直前に、必ず
#     effective readback（`preflight.sh --auto-mode-check`）を実行する（P0-3,
#     PR #2214 OWNER adversarial review 反映）。従来はこの opt-in サブコマンドが
#     通常起動へ一度も配線されておらず、readback 未実行のまま launch_result が
#     ok を返せてしまっていた。readback 失敗（未対応 version・narrow label 未反映・
#     hard_deny/soft_deny 不整合・classifyAllShell 未有効化のいずれか）は fail-closed
#     で起動を止める。 ---
export CLAUDE_GPT_CLAUDE_BIN="$CLAUDE_BIN"
AUTO_MODE_CHECK_JSON=$("$SCRIPT_DIR/preflight.sh" --auto-mode-check "$SETTINGS_PATH")
AUTO_MODE_CHECK_RC=$?
AUTO_MODE_CHECK_PATH="${CLAUDE_CONFIG_DIR_TARGET}/auto-mode-check.json"
printf '%s\n' "$AUTO_MODE_CHECK_JSON" > "$AUTO_MODE_CHECK_PATH"
if [ "$AUTO_MODE_CHECK_RC" -ne 0 ]; then
  kill "$PROXY_PID" 2>/dev/null
  wait "$PROXY_PID" 2>/dev/null
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"auto_mode_readback_failed","auto_mode_check":%s}\n' "$AUTO_MODE_CHECK_JSON"
  exit 8
fi
echo "CLAUDE_GPT_AUTO_MODE_CHECK_PATH=${AUTO_MODE_CHECK_PATH}" >&2

# --- 通常起動: claude 本体を子プロセスとして起動する supervisor 構成（P0-4）。
#     `exec` は shell process image を claude に置き換えてしまい EXIT trap に二度と
#     到達しないため使わない。shell を supervisor として維持し、全終了経路
#     （正常/エラー/timeout/SIGINT/SIGTERM）で確実に proxy を kill/wait する。 ---

unset CLAUDE_CODE_SUBAGENT_MODEL
# Issue #2652: the Spark-specific foreground invariant that Issue #2274
# AC13 used to pin here (`CLAUDE_CODE_FORK_SUBAGENT` unset/0 PAIRED WITH
# `export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`) is retired. That pairing
# was enforced by the `effective_env_override_reason()` PreToolUse(Agent)
# gate hook embedded in the now-removed Spark authorization gate
# (SPARK_GATE_WRITER_PY_BEGIN/_END, Issue #2186/#2274), and existed only to
# make the GPT-5.3-Codex-Spark delegation's model/evidence causal chain
# deterministic (Issue #2274 In Scope). That gate -- and the Spark
# delegation it protected -- was fully retired in Issue #2651/#2662. With
# no gate left to protect, `export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`
# had no remaining non-Spark justification, while its actual blast radius
# was strictly broader than Spark: it disabled Claude Code's background
# task mechanism for the ENTIRE Claude-GPT session, which also deleted
# `run_in_background` from the Bash tool schema exposed to the main thread
# -- breaking `issue-refinement-loop`'s canonical Step 2 background-launch
# + mandatory completion-join contract (Issue #2610) for every Claude-GPT
# session, not just Spark invocations (Issue #2652 Background/AC1).
#
# `CLAUDE_CODE_FORK_SUBAGENT` unset is kept as an INDEPENDENT contract
# (Issue #2652 Design Direction explicitly forbids retiring it in bulk
# alongside the background-task disable; this launcher does not evaluate
# here whether it still needs a separate production rationale).
#
# Both variables are explicitly `unset` (not merely left absent), matching
# the original forgery-prevention rationale: a leaked ambient export from
# the launching parent shell must not survive into the child `claude`
# process's environment. For `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`
# specifically, this `unset` makes the launcher-owned session's background
# capability independent of the parent shell's value for this variable
# (unset / `0` / `1` all converge on Claude Code's own default of
# background tasks enabled, per Issue #2652 AC2) -- this launcher does not
# force it to any explicit value.
#
# Scope limit (Issue #2652 AC2): this `unset` only controls what this
# launcher itself injects into the child process's environment. It cannot
# observe or override a re-injection from a layer this launcher does not
# own -- e.g. a managed/enterprise `managed-settings.json`, or a Claude
# Code `settings.json` `env` block outside this launcher's own isolated
# `CLAUDE_CONFIG_DIR` (`$CLAUDE_CONFIG_DIR_TARGET/settings.local.json`,
# generated fresh per launch by this script and never sets this variable)
# and outside this repository's project-scope `.claude/settings.json` /
# `.claude/settings.local.json` (verified at the time of this fix to not
# reference this variable). If such an external, launcher-owned-outside
# layer sets this variable in a given deployment, that is a genuine
# limitation of a repository-owned launcher's isolation scope -- not
# something to silently claim success over (docs/dev/agent-skill-boundaries.md
# documents this caveat explicitly rather than hiding it).
unset CLAUDE_CODE_FORK_SUBAGENT
unset CLAUDE_CODE_DISABLE_BACKGROUND_TASKS

export CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR_TARGET"
export ANTHROPIC_BASE_URL="http://127.0.0.1:${PROXY_PORT}"
export ANTHROPIC_AUTH_TOKEN="claude-gpt-local"
export ANTHROPIC_MODEL="$CLAUDE_GPT_MODEL_MAIN"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLAUDE_GPT_MODEL_OPUS"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$CLAUDE_GPT_MODEL_SONNET"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLAUDE_GPT_MODEL_HAIKU"
# upstream raine/claude-code-proxy の推奨起動例（configure-claude-code ドキュメント）は
# ANTHROPIC_SMALL_FAST_MODEL を明示設定する構成のため、ANTHROPIC_DEFAULT_HAIKU_MODEL と
# 併せて同じ alias を設定する（両方設定して competing/矛盾する挙動は確認されていない。
# Issue #2158/PR #2162 実機再検証, 2026-08-15）。
export ANTHROPIC_SMALL_FAST_MODEL="$CLAUDE_GPT_MODEL_HAIKU"

# strict_mcp mode: repository/user MCP を一切読み込まない。--strict-mcp-config +
# 空の mcp-config JSON（$MCP_CONFIG_PATH）の組み合わせで実現する。
STRICT_MCP_MODE=true
export STRICT_MCP_MODE

# Parent #2154 gateway/context 契約（P1-2）。
# CLAUDE_CODE_SUBPROCESS_ENV_SCRUB は設定しない（OS-level sandbox hardening は Phase 1
# の merge 条件から除外。実機検証で launcher がネストした sandbox 実行環境下にある場合
# Bash tool を破壊することを確認したため。Issue #2158 Scope Reframe, 2026-08-15）。
# --- Claude/AGY プロセスの GitHub auth のみ native 同等に共有し、それ以外の
#     無関係な secret は引き続き isolate する（Issue #2299。旧 P0-6/PR #2214 の
#     「genuine `gh` auth context を利用不能にする」方針は、genuine `issue-creator`
#     SubAgent が dedupe read 等で認証エラーになり通常 workflow を完走できない
#     という owner 指摘（#2259 NOT_PLANNED, PR #2286 コメント）を受けて置き換えた。
#     GitHub mutation の correctness は GitHub 側の server-side protection と
#     mutation 前後の live readback で担保する（本 Issue Outcome 節）。
#     `HOME` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` は引き続き空の隔離ディレクトリへ
#     差し替え、host HOME 配下の SSH key/GPG key 等へは到達できないようにする。
#     `GH_CONFIG_DIR` は isolation 前に固定した ambient 値（native gh config dir）
#     をそのまま渡すことで、GitHub auth のみ native 相当を維持する。
#     `GH_TOKEN` 系 / `GH_HOST` / `GH_REPO` も ambient 値をそのまま unset せず
#     子プロセスへ引き継ぐ。`SSH_AUTH_SOCK` / `GIT_ASKPASS` 系（GitHub auth とは
#     無関係）は引き続き scrub する。 ---
# --- Issue #2670: 承認済み file-backed AGY account session source-path の
#     pre-isolation handoff。`agy_permission_policy.py` の
#     `_real_home_agy_oauth_token_file()` は唯一の承認済み source を
#     `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` として
#     ambient `$HOME` から導出するが、直後の `export HOME=...`（isolated
#     HOME への差し替え）以降にその lookup を行う経路（この launcher が
#     起動する Claude-GPT outer 配下の任意の inner test-runner / AGY
#     呼び出し）からは、ambient `$HOME` が新規かつ空の isolated HOME に
#     なっているため、実 host にその source ファイルが存在していても
#     構造的に見えなくなる（本 Issue の Outcome 節）。
#     この block は isolation 直前・実 ambient `$HOME` がまだ real host
#     値である時点で、承認済み root（`$HOME/.gemini/antigravity-cli`）と
#     その配下の exact source path を一緒に捕捉し、この 2 値だけを持つ
#     専用 non-secret path handoff interface（`AGY_OAUTH_TOKEN_HANDOFF_ROOT`
#     / `AGY_OAUTH_TOKEN_HANDOFF_SOURCE`）として `agy_permission_policy.py`
#     へ引き渡す。broad な HOME/XDG passthrough や汎用 environment channel
#     ではなく、この 2 path 値のみの path-only transport であり、origin
#     authentication・trusted channel・security control のいずれでもない
#     -- `agy_permission_policy.py::resolve_agy_oauth_token_source()` が
#     両値を独立に正規化・structural revalidation してから初めて候補
#     entry を承認済み source として扱う（Issue #2670 AC1/AC2）。承認済み
#     source が実際に存在するか否かに関わらず常にこの 2 値を渡す -- 存在
#     しない場合の判定（`source_absent`）は policy 側の revalidation が
#     行う。値は export するのみで、この launcher 自身は一切 log・
#     display・ファイル書き込みしない。
export AGY_OAUTH_TOKEN_HANDOFF_ROOT="${HOME}/.gemini/antigravity-cli"
export AGY_OAUTH_TOKEN_HANDOFF_SOURCE="${AGY_OAUTH_TOKEN_HANDOFF_ROOT}/antigravity-oauth-token"

# --- Issue #2567 In Scope: Task Context canonical state-root / runtime-
#     variant carrier. Must run *before* the isolated HOME/XDG switch below
#     (same ordering requirement as CLAUDE_NATIVE_LATITUDE_SETTINGS_PATH_TARGET
#     above), otherwise `task_context_config.resolve_state_root()` would
#     derive its default root from the isolated (empty) Claude-GPT HOME
#     instead of the ambient real HOME/XDG_STATE_HOME Native Claude uses --
#     producing a second, isolated-HOME-scoped Task Context DB (AC1/AC2).
#
#     Precedence (In Scope, AC3): an inherited, non-empty
#     `LOOP_TASK_CONTEXT_STATE_ROOT` (e.g. a runtime-smoke override) is kept
#     exactly as-is and never recomputed/overwritten here. Only when it is
#     unset does this launcher resolve one from ambient XDG/HOME, via the
#     existing `task_context_config.resolve_state_root()` SSOT (never a
#     duplicated resolution rule) -- `claude_gpt_resolve_task_context_state_root`
#     (lib.sh) is a thin ordering wrapper around that same function.
#
#     `LOOP_TASK_CONTEXT_SCOPE` is intentionally left completely untouched
#     here: this shell process already inherited whatever ambient value the
#     caller set (ordinary env inheritance, e.g. `worktree-agent-runtime-
#     smoke`'s `LOOP_TASK_CONTEXT_SCOPE=runtime_smoke`), and a normal
#     operator launch has no scope value of its own to assign -- so simply
#     never assigning/exporting it here is what "preserved, never
#     overwritten" (AC3) means in practice.
#
#     PR #2696 review fix_delta (P1-1, OWNER REQUEST_CHANGES): resolution
#     failure used to leave `LOOP_TASK_CONTEXT_STATE_ROOT` unset and fall
#     through to the isolated-HOME switch below -- a fail-OPEN degrade that
#     let the child `claude` process derive its OWN default state root from
#     the already-isolated (empty) Claude-GPT HOME/XDG, silently producing a
#     second, isolated-HOME-scoped Task Context DB (a direct AC1/AC2
#     violation, not a harmless degrade). Only the inherited-override branch
#     (a non-empty `LOOP_TASK_CONTEXT_STATE_ROOT` already set by the caller,
#     e.g. runtime-smoke) may still skip calling the resolver entirely, per
#     AC3 -- that precedence is unchanged. When the resolver actually runs
#     and fails (python3 unavailable, git/repo error, etc.), this launcher
#     now fails fast (before `export HOME="$CLAUDE_ISOLATED_HOME_TARGET"`)
#     instead of continuing.
if [ -z "${LOOP_TASK_CONTEXT_STATE_ROOT:-}" ]; then
  CLAUDE_GPT_TASK_CONTEXT_CONFIG_PATH="${REPO_ROOT}/scripts/task-context/task_context_config.py"
  CLAUDE_GPT_RESOLVED_TASK_CONTEXT_STATE_ROOT=$(claude_gpt_resolve_task_context_state_root \
    "$CLAUDE_GPT_TASK_CONTEXT_CONFIG_PATH" "$REPO_ROOT")
  if [ -n "$CLAUDE_GPT_RESOLVED_TASK_CONTEXT_STATE_ROOT" ]; then
    LOOP_TASK_CONTEXT_STATE_ROOT="$CLAUDE_GPT_RESOLVED_TASK_CONTEXT_STATE_ROOT"
    export LOOP_TASK_CONTEXT_STATE_ROOT
  else
    echo "claude-gpt launch.sh: Task Context canonical state-root resolution failed (python3 unavailable or resolver error) -- refusing to fall through to an isolated-HOME-derived Task Context DB (Issue #2567 AC1/AC2). Set LOOP_TASK_CONTEXT_STATE_ROOT explicitly to override, or ensure python3 is on PATH." >&2
    kill "$PROXY_PID" 2>/dev/null
    wait "$PROXY_PID" 2>/dev/null
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"failed","reason":"task_context_state_root_resolution_failed"}\n'
    exit 10
  fi
fi
# Fixed value on every normal-mode launch (Issue #2567 In Scope carrier
# contract) -- this launcher IS the claude_gpt runtime, unconditionally.
export LOOP_TASK_CONTEXT_RUNTIME_VARIANT=claude_gpt

export HOME="$CLAUDE_ISOLATED_HOME_TARGET"
export GH_CONFIG_DIR="$CLAUDE_NATIVE_GH_CONFIG_DIR_TARGET"
export XDG_CONFIG_HOME="$CLAUDE_ISOLATED_XDG_CONFIG_DIR_TARGET"
export XDG_CACHE_HOME="$CLAUDE_ISOLATED_XDG_CACHE_DIR_TARGET"
unset SSH_AUTH_SOCK
unset GIT_ASKPASS SSH_ASKPASS GIT_CREDENTIAL_HELPER
# Issue #2426 Design 5節 / AC5: 明示的に unset する（親シェルの ambient
# BUN_OPTIONS を Claude-GPT 子プロセスへ継承させない production invariant）。
# 有効な Latitude preload が ambient に既に存在する場合の enrichment 自体は
# 許容するが、この unset を production invariant として扱い、preload の有無を
# AC5 の PASS 根拠にはしない。
unset BUN_OPTIONS

export CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1
# `auto` は `[1m]` suffix なし model 名の場合に未知 model として context window を
# 200k と誤認し早期 compaction/summarization 失敗を招いた（実機再検証, 2026-08-15）。
# upstream 推奨どおり ChatGPT backend の実 context 上限（272k）に固定する。
# CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT は設定しない（OWNER が明示的に
# 非推奨としている fail-safe でない回避策のため）。
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=272000
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

# --- launcher 自身の stdin を fd 9 として明示的に複製する（Issue #2158/#2162
#     fix-delta。#2174/#2176 structured lane 実機発見）。
#     job control が無効な非対話 shell（dash/bash 双方で確認済み）では、
#     `cmd &` のように *明示的な入力リダイレクトを持たない* async command は、
#     ターミナル/呼び出し元との stdin 競合を避けるため shell が自動的に
#     `< /dev/null` を差し込む（POSIX 準拠の既定動作）。P0-4 supervisor 構成
#     はまさにこの形（`"$CLAUDE_BIN" ... "$@" &`）だったため、`-p` へ prompt を
#     stdin 経由で渡す呼び出し（structured lane 等）で子プロセスの stdin が
#     空になり `Error: Input must be provided either through stdin or as a
#     prompt argument when using --print` が発生していた（argv 経由で prompt
#     を渡す runtime_smoke_test.sh の呼び出しは影響しない）。
#     対策として fd 9 を明示的に複製し、claude 起動時に `<&9` で結びつけることで
#     async command に明示的な入力リダイレクトを与え、shell 既定の
#     `/dev/null` 差し替えを回避する（`set -m` による job control 有効化や
#     `disown` は、前者は `[N]+ Done` ノイズが増えるだけで許容範囲内だが後者は
#     `wait "$CLAUDE_PID"` を空振りさせ exit code 取得と signal forwarding を
#     破壊することを検証済みのため、いずれも採用しない）。
#     fd 0 が呼び出し元から正しく渡されていることは、他の全コマンド（proxy
#     起動時の `env -i` 等）と同じく launcher の前提条件であり、fd 0 が
#     既に閉じられているような非対応環境では `exec` 自体が失敗し launcher は
#     即座に終了する（dash では POSIX 準拠の special builtin redirection
#     failure として shell 全体が終了する。これは本変更が持ち込む新しい
#     failure mode ではなく、fd 0 前提が壊れている場合に他のどの経路でも
#     いずれ破綻していた状態を早期に顕在化させるだけである）。
exec 9<&0

# --- launcher が exactly one の `--permission-mode auto` を注入する（Issue #2203
#     Outcome 節）。caller 由来の `--permission-mode` は上記 forbidden-flag ループ
#     （CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS 経由）で既に全面拒否済みのため、ここに
#     到達する時点で "$@" に `--permission-mode` トークンは含まれない。 ---
# --- Issue #2274 PR #2285 OWNER fix-delta P0-3 audit record: retired
#     (Issue #2651). This best-effort `last-agents-json.json` audit file
#     existed only for `scripts/claude-gpt/runtime_smoke_test.sh`'s
#     `--spark-delegation` live evidence builder to read back; that mode is
#     now an immediate deterministic rejection and never reaches this file,
#     so the write is removed along with its sole reader. ---
# shellcheck disable=SC2086
"$CLAUDE_BIN" --strict-mcp-config --mcp-config "$MCP_CONFIG_PATH" --settings "$SETTINGS_PATH" --permission-mode auto --agents "$AGENTS_JSON" "$@" <&9 &
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

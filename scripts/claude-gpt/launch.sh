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
SPARK_AUTH_DIR_TARGET=$(claude_gpt_spark_auth_dir)
# --- Claude/AGY プロセス専用の隔離 HOME/GH_CONFIG_DIR/XDG（P0-6）。credential を
#     一切置かない空ディレクトリとして扱う。ambient `gh auth` credential を
#     Claude/AGY プロセスから利用不能にすることが目的であり、broker（別プロセス、
#     GitHub mutation transaction broker）はこの隔離ディレクトリを使わない。 ---
CLAUDE_ISOLATED_HOME_TARGET=$(claude_gpt_claude_isolated_home_dir)
CLAUDE_ISOLATED_GH_CONFIG_DIR_TARGET=$(claude_gpt_claude_isolated_gh_config_dir)
CLAUDE_ISOLATED_XDG_CONFIG_DIR_TARGET=$(claude_gpt_claude_isolated_xdg_config_dir)
CLAUDE_ISOLATED_XDG_CACHE_DIR_TARGET=$(claude_gpt_claude_isolated_xdg_cache_dir)

# --- canonical path safety（ディレクトリ作成前に必ず検証する） ---
for d in "$CLAUDE_CONFIG_DIR_TARGET" "$PROXY_CONFIG_DIR_TARGET" "$PROXY_STATE_DIR_TARGET" "$PROXY_HOME_TARGET" \
  "$CLAUDE_ISOLATED_HOME_TARGET" "$CLAUDE_ISOLATED_GH_CONFIG_DIR_TARGET" \
  "$CLAUDE_ISOLATED_XDG_CONFIG_DIR_TARGET" "$CLAUDE_ISOLATED_XDG_CACHE_DIR_TARGET" \
  "$SPARK_AUTH_DIR_TARGET"; do
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
  "$CLAUDE_ISOLATED_HOME_TARGET" "$CLAUDE_ISOLATED_GH_CONFIG_DIR_TARGET" \
  "$CLAUDE_ISOLATED_XDG_CONFIG_DIR_TARGET" "$CLAUDE_ISOLATED_XDG_CACHE_DIR_TARGET" \
  "$SPARK_AUTH_DIR_TARGET"

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
# --- Spark explicit-only authorization gate (Issue #2186) ------------------
#
# Session-local, nonce-keyed sidecar authorization gate. The gate writer
# python source below is embedded between fixed markers
# (`SPARK_GATE_WRITER_PY_BEGIN`/`_END`) so hermetic tests can extract the
# exact executed source (single source of truth, no drift between runtime
# and test fixtures) instead of re-implementing the same logic twice.
SPARK_LAUNCH_NONCE="$(date -u +%Y%m%dT%H%M%SZ)-$$"
SPARK_GATE_WRITER="${PROXY_STATE_DIR_TARGET}/spark-gate-${SPARK_LAUNCH_NONCE}.py"
( umask 077 && cat > "$SPARK_GATE_WRITER" <<'SPARK_GATE_WRITER_PY_END_MARKER'
# SPARK_GATE_WRITER_PY_BEGIN
import json
import os
import re
import sys
import time
import uuid

SPARK_AGENT = "spark-codex"
AUTH_DIR = os.environ.get("CLAUDE_GPT_SPARK_AUTH_DIR", "")
# Issue #2186 P0 fix-delta (PR #2244 adversarial review, forgery finding):
# the launch nonce is intentionally NOT read from a process environment
# variable. `export`ing it would make it visible to the main `claude`
# process's own env (and therefore to the unrestricted Bash tool via `env`),
# letting main Claude itself forge a fake pending-authorization sidecar file
# with the correct launch_nonce and self-authorize a spark-codex invocation
# without ever going through UserPromptSubmit. Instead, launch.sh substitutes
# this placeholder with the literal per-launch nonce value via a post-heredoc
# `sed` rewrite of this file (see `SPARK_GATE_WRITER` generation), so the
# value is only ever present inside a file under a `permissions.deny`
# Read-denied directory, never inside the claude process's own environ.
LAUNCH_NONCE = "__CLAUDE_GPT_SPARK_LAUNCH_NONCE__"

MENTION_RE = re.compile(r"(?<![\w-])@agent-spark-codex(?![\w-])")
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
DQUOTE_RE = re.compile(r'"[^"\n]*"')
SQUOTE_RE = re.compile(r"'[^'\n]*'")


def strip_non_authorizing(text):
    text = FENCE_RE.sub(" ", text)
    text = INLINE_CODE_RE.sub(" ", text)
    text = DQUOTE_RE.sub(" ", text)
    text = SQUOTE_RE.sub(" ", text)
    kept = []
    for line in text.split("\n"):
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def has_canonical_mention(prompt_text):
    if not isinstance(prompt_text, str):
        return False
    return bool(MENTION_RE.search(strip_non_authorizing(prompt_text)))


# --- Structured delegation directive (Issue #2258) -------------------------
#
# In addition to the canonical `@agent-spark-codex` mention above (kept
# fully unchanged/backward-compatible), a human's prompt text may contain a
# structured `DELEGATION_REQUEST_V1` directive as plain (non-fenced,
# non-quoted, non-blockquoted) `key: value` lines. It is parsed from the
# SAME `strip_non_authorizing()`-stripped text used for mention detection,
# so a directive appearing only inside a quoted string / fenced code block /
# inline code span / blockquote is never authorizing (Issue #2258 AC4) --
# it is entirely removed before this parser ever sees it.
DELEGATION_SCHEMA_VALUE = "DELEGATION_REQUEST_V1"
DELEGATION_AGENT_ID = "spark-codex"
DELEGATION_MODEL = "gpt-5.3-codex-spark"
DELEGATION_VALID_MODES = ("required", "preferred")
DELEGATION_VALID_FALLBACK = ("forbidden", "allowed")
DELEGATION_AUTH_SOURCE = "explicit_directive"
DELEGATION_REQUIRED_KEYS = (
    "schema",
    "agent_id",
    "model",
    "mode",
    "fallback",
    "wait",
    "authorization_source",
)

DIRECTIVE_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$")


def _candidate_directive_blocks(stripped_text):
    # Group consecutive `key: value` lines into blocks, separated by any
    # blank line or any line that does not match the `key: value` shape
    # (e.g. ordinary prose). A block never spans across such a break, so a
    # directive-looking fragment interrupted by unrelated prose never
    # accidentally merges with a genuine directive elsewhere in the prompt.
    blocks = []
    current = []
    for line in stripped_text.split("\n"):
        m = DIRECTIVE_LINE_RE.match(line)
        if m:
            current.append((m.group(1), m.group(2)))
        else:
            if current:
                blocks.append(current)
                current = []
    if current:
        blocks.append(current)
    return blocks


def parse_delegation_directive(prompt_text):
    """Parse an explicit-only DELEGATION_REQUEST_V1 directive out of
    `prompt_text`. Returns a dict with `status` of `absent` (no directive
    attempt found), `malformed` (a `schema: DELEGATION_REQUEST_V1` anchor
    line was found but the surrounding block fails strict validation --
    missing keys, wrong enum value, typo/case mismatch, wrong agent_id/model
    -- Issue #2258 AC5), or `valid` (fully well-formed directive; also
    carries `mode`/`fallback`, Issue #2258 AC1/AC2)."""
    if not isinstance(prompt_text, str):
        return {"status": "absent"}
    stripped = strip_non_authorizing(prompt_text)
    for block in _candidate_directive_blocks(stripped):
        keys_seen = [k for k, _ in block]
        if "schema" not in keys_seen:
            continue
        values = {}
        for k, v in block:
            values[k] = v
        if values.get("schema") != DELEGATION_SCHEMA_VALUE:
            # Not a spark delegation directive at all (different schema
            # value); keep scanning other candidate blocks.
            continue
        # Issue #2258 P1-7 fix-delta: hardening checks for this
        # authorization-parsing surface, applied only once a block has
        # already matched `schema: DELEGATION_REQUEST_V1` (ordinary
        # unrelated `key: value`-shaped prose elsewhere in the prompt is
        # never touched by these checks -- same scanning behavior as
        # before). A duplicated key is rejected instead of silently
        # keeping the last-write-wins value, and any key outside the
        # known schema is rejected instead of being silently ignored.
        seen_counts = {}
        for k, _ in block:
            seen_counts[k] = seen_counts.get(k, 0) + 1
        duplicated = [k for k, c in seen_counts.items() if c > 1]
        if duplicated:
            return {"status": "malformed", "reason": "duplicate_key", "key": duplicated[0]}
        unknown = [k for k in values if k not in DELEGATION_REQUIRED_KEYS]
        if unknown:
            return {"status": "malformed", "reason": "unknown_key", "key": unknown[0]}
        missing = [k for k in DELEGATION_REQUIRED_KEYS if k not in values]
        if missing:
            return {"status": "malformed", "reason": "missing_required_keys", "missing": missing}
        if values["agent_id"] != DELEGATION_AGENT_ID:
            return {"status": "malformed", "reason": "invalid_agent_id", "value": values["agent_id"]}
        if values["model"] != DELEGATION_MODEL:
            return {"status": "malformed", "reason": "invalid_model", "value": values["model"]}
        if values["mode"] not in DELEGATION_VALID_MODES:
            return {"status": "malformed", "reason": "invalid_mode", "value": values["mode"]}
        if values["fallback"] not in DELEGATION_VALID_FALLBACK:
            return {"status": "malformed", "reason": "invalid_fallback", "value": values["fallback"]}
        # Issue #2258 P1-5 fix-delta: `mode`/`fallback` are individually
        # valid enum values above, but the pairing itself must not be
        # semantically contradictory. The Issue #2258 contract is
        # `required => no fallback` / `preferred => fallback allowed`; the
        # enum value set itself is unchanged (schema stays the same).
        if (values["mode"], values["fallback"]) not in (
            ("required", "forbidden"),
            ("preferred", "allowed"),
        ):
            return {
                "status": "malformed",
                "reason": "contradictory_mode_fallback",
                "mode": values["mode"],
                "fallback": values["fallback"],
            }
        if values["wait"] != "true":
            return {"status": "malformed", "reason": "invalid_wait", "value": values["wait"]}
        if values["authorization_source"] != DELEGATION_AUTH_SOURCE:
            return {
                "status": "malformed",
                "reason": "invalid_authorization_source",
                "value": values["authorization_source"],
            }
        return {"status": "valid", "mode": values["mode"], "fallback": values["fallback"]}
    return {"status": "absent"}


def read_payload():
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def pending_path(session_id):
    return os.path.join(AUTH_DIR, "pending-" + session_id + ".json")


def counter_path(session_id):
    return os.path.join(AUTH_DIR, "counter-" + session_id + ".txt")


def next_prompt_counter(session_id):
    path = counter_path(session_id)
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            n = int((fh.read() or "0").strip() or "0")
    except (OSError, ValueError):
        n = 0
    n += 1
    tmp = path + ".tmp-" + str(os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(str(n))
    os.replace(tmp, path)
    return n


def clear_pending(session_id):
    try:
        os.remove(pending_path(session_id))
    except OSError:
        pass


def required_lock_path(session_id):
    # Issue #2258 AC3: while this file exists for a session, the current
    # turn's Agent tool calls for any subagent_type OTHER than spark-codex
    # are denied -- a `mode: required` + `fallback: forbidden` directive's
    # Spark authorization was consumed and no silent substitute delegation
    # is permitted for the remainder of this turn (structured terminal stop
    # of the Agent-delegation path, forcing the failure/result to be
    # surfaced explicitly rather than silently downgraded).
    return os.path.join(AUTH_DIR, "required-lock-" + session_id + ".json")


def preferred_marker_path(session_id):
    # Issue #2258 AC2: while this file exists for a session, the NEXT
    # non-spark Agent tool call for this turn is logged (once) as an
    # explicit fallback-from-preferred-Spark event via additionalContext,
    # then the marker is consumed (single-shot log, not a gate decision).
    return os.path.join(AUTH_DIR, "preferred-marker-" + session_id + ".json")


def clear_delegation_locks(session_id):
    # A new turn (UserPromptSubmit) always releases any delegation lock/
    # marker left over from a prior turn -- these never carry over, exactly
    # like the pending mention/directive authorization itself.
    for path in (required_lock_path(session_id), preferred_marker_path(session_id)):
        try:
            os.remove(path)
        except OSError:
            pass


def has_nested_agent_origin(payload):
    # Issue #2186 P1 fix-delta: a genuinely top-level user turn's hook
    # payload never carries an `agent_id`/`parent_tool_use_id` (or
    # `parentToolUseId`) field -- those are only present when the event
    # originates from within a SubAgent's own execution context. Requiring
    # their absence at BOTH UserPromptSubmit and PreToolUse(Agent) time
    # closes the gap where a different SubAgent sharing the same
    # `session_id` could otherwise piggyback on (or independently trigger)
    # an authorization intended only for the top-level main session.
    for key in ("agent_id", "parent_tool_use_id", "parentToolUseId"):
        if payload.get(key):
            return True
    return False


def _write_malformed_directive_marker(session_id, counter, directive):
    # Issue #2258 AC5: a malformed directive attempt is recorded as an
    # explicit, independently-inspectable diagnostic artifact (never a
    # silent no-op indistinguishable from "no directive present at all",
    # and never an authorization).
    path = os.path.join(AUTH_DIR, "malformed-" + session_id + "-" + str(counter) + ".json")
    tmp = path + ".tmp-" + str(os.getpid())
    record = {"session_id": session_id, "prompt_id": counter, "directive": directive}
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def cmd_user_prompt_submit(payload):
    session_id = payload.get("session_id") or ""
    prompt = payload.get("prompt")
    if not session_id or not AUTH_DIR:
        return
    if has_nested_agent_origin(payload):
        # A prompt-submit event that itself carries nested-agent origin
        # fields cannot be a genuine top-level user turn; never record a
        # pending authorization from it (fail closed).
        return
    try:
        os.makedirs(AUTH_DIR, exist_ok=True)
    except OSError:
        return
    counter = next_prompt_counter(session_id)
    # A new turn always invalidates any previous unconsumed authorization for
    # this session first -- authorization never carries over to the next turn
    # (Issue #2186 AC3), and likewise releases any delegation lock/marker
    # left over from a prior turn (Issue #2258).
    clear_pending(session_id)
    clear_delegation_locks(session_id)

    # Issue #2258: a structured DELEGATION_REQUEST_V1 directive is an
    # independent, additional authorization source alongside (never a
    # replacement for) the canonical @agent-spark-codex mention above.
    directive = parse_delegation_directive(prompt)
    delegation_mode = None
    delegation_fallback = None
    authorization_source = None
    if directive.get("status") == "valid":
        delegation_mode = directive["mode"]
        delegation_fallback = directive["fallback"]
        authorization_source = DELEGATION_AUTH_SOURCE
    elif has_canonical_mention(prompt):
        authorization_source = "canonical_mention"

    # Issue #2258 P1-6 fix-delta: a malformed directive attempt is always
    # recorded as an explicit diagnostic artifact, independent of whether a
    # SEPARATE canonical-mention authorization is granted this same turn --
    # both happen together (grant authorization AND record the diagnostic),
    # never either/or. Previously this write was skipped whenever
    # canonical-mention authorization already set `authorization_source`,
    # silently suppressing the diagnostic.
    if directive.get("status") == "malformed":
        _write_malformed_directive_marker(session_id, counter, directive)

    if authorization_source is None:
        if directive.get("status") == "malformed":
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "CLAUDE_GPT_SPARK_DIRECTIVE_MALFORMED_V1 "
                    + json.dumps({"reason": directive.get("reason")}),
                }
            }
            sys.stdout.write(json.dumps(output))
        return

    # Issue #2258 P0-1/P0-2 fix-delta: for `mode: required` + `fallback:
    # forbidden`, establish the no-fallback delegation lock RIGHT NOW (at
    # UserPromptSubmit time), before any Agent tool call this turn can
    # happen -- not only after the Spark authorization is later consumed.
    # This closes the loophole where a non-Spark Agent call made BEFORE the
    # FIRST Spark PreToolUse call this turn would otherwise pass through the
    # ordinary allow no-op path untouched (the lock file didn't exist yet).
    # `clear_delegation_locks()` above already released any prior turn's
    # lock, so this never leaks across turns.
    #
    # Fail-CLOSED: if the lock file cannot be written, treat this the same
    # as "no authorization granted this turn" -- do not write the pending
    # authorization record either. A `required`+`forbidden` directive must
    # never result in Spark being allowed with no lock enforced.
    if delegation_mode == "required" and delegation_fallback == "forbidden":
        try:
            with open(required_lock_path(session_id), "w", encoding="utf-8") as fh:
                json.dump({"session_id": session_id, "created_ts": time.time()}, fh)
        except OSError:
            return

    record = {
        "session_id": session_id,
        "prompt_id": counter,
        "launch_nonce": LAUNCH_NONCE,
        "authorization_turn_id": session_id + ":" + str(counter),
        "consumed": False,
        "created_ts": time.time(),
        "nonce": uuid.uuid4().hex,
        "authorization_source": authorization_source,
        "delegation_mode": delegation_mode,
        "delegation_fallback": delegation_fallback,
    }
    target = pending_path(session_id)
    tmp = target + ".tmp-" + str(os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    os.replace(tmp, target)


def cmd_pre_tool_use_agent(payload):
    tool_input = payload.get("tool_input")
    subagent_type = tool_input.get("subagent_type") if isinstance(tool_input, dict) else None
    session_id = payload.get("session_id") or ""
    if subagent_type != SPARK_AGENT:
        # Issue #2258 AC3: a `mode: required` + `fallback: forbidden`
        # directive whose Spark authorization was already consumed this
        # turn forbids ANY other Agent subagent_type for the remainder of
        # the turn -- no silent substitute/fallback delegation. This is a
        # gate decision (fail-closed deny), unlike the ordinary no-op
        # pass-through below.
        if session_id and AUTH_DIR and os.path.exists(required_lock_path(session_id)):
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "required_delegation_lock_active_no_fallback_agent_allowed",
                }
            }
            sys.stdout.write(json.dumps(output))
            sys.exit(0)
        # Issue #2258 AC2: a `mode: preferred` directive whose Spark
        # authorization was consumed this turn allows fallback to any other
        # subagent_type, but the fallback event is logged explicitly
        # (advisory additionalContext only, single-shot -- never a gate
        # decision, never interferes with ordinary SubAgent mapping).
        if session_id and AUTH_DIR:
            marker = preferred_marker_path(session_id)
            if os.path.exists(marker):
                try:
                    os.remove(marker)
                except OSError:
                    pass
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": "CLAUDE_GPT_SPARK_FALLBACK_LOGGED_V1 "
                        + json.dumps(
                            {
                                "session_id": session_id,
                                "reason": "preferred_mode_fallback_to_non_spark_agent",
                                "subagent_type": subagent_type,
                            }
                        ),
                    }
                }
                sys.stdout.write(json.dumps(output))
                sys.exit(0)
        # Not our concern -- explicit allow, no interference with ordinary
        # SubAgent mapping (Issue #2186 AC1).
        return
    decision = "deny"
    reason = "no_pending_authorization"
    record = None
    if has_nested_agent_origin(payload):
        # Issue #2186 P1 fix-delta (AC5): the Agent tool_use itself
        # originates from a nested SubAgent context (or reports fields
        # consistent with one), never from the top-level main session --
        # deny unconditionally, independent of whether a pending
        # authorization otherwise exists for this session_id.
        reason = "nested_agent_origin_forbidden"
    elif session_id and AUTH_DIR:
        pending = pending_path(session_id)
        try:
            with open(pending, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict) and record.get("consumed") is False and record.get("launch_nonce") == LAUNCH_NONCE:
            consumed_marker = pending + ".consumed-" + uuid.uuid4().hex
            try:
                os.rename(pending, consumed_marker)
                decision = "allow"
                reason = "authorization_consumed"
            except OSError:
                decision = "deny"
                reason = "authorization_consume_race_lost"
        elif isinstance(record, dict) and record.get("launch_nonce") != LAUNCH_NONCE:
            reason = "authorization_carryover_stale_launch_nonce"
        elif isinstance(record, dict) and record.get("consumed") is not False:
            reason = "authorization_already_consumed"
    # Issue #2258 P0-3 fix-delta: an explicit `model` field in the actual
    # Agent tool_input payload that does not exactly match the pinned Spark
    # model is a hard negative control -- an attacker/confused-model trying
    # to invoke spark-codex with a different model parameter must not be
    # silently allowed, even if a genuine pending authorization would
    # otherwise have been consumed above (consumption already happened;
    # denying now is still fail-closed since the authorization is spent
    # either way).
    if decision == "allow" and isinstance(tool_input, dict):
        model_override = tool_input.get("model")
        if model_override is not None and model_override != DELEGATION_MODEL:
            decision = "deny"
            reason = "explicit_model_override_mismatch"
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    if decision == "allow":
        # Issue #2258 P0-3/P0-4 fix-delta: pin the actual executed model to
        # the Spark model (present or absent in the original tool_input --
        # either way the pinned value wins) and force foreground execution
        # (`run_in_background: false`) so a `wait: true` directive's Spark
        # invocation is deterministically blocking, regardless of what
        # run_in_background value Claude Code itself proposed. Paired with
        # `permissionDecision: "allow"` (never `"defer"`, since `defer`
        # drops `updatedInput` per the PreToolUse hook contract).
        base_input = tool_input if isinstance(tool_input, dict) else {}
        updated_input = dict(base_input)
        updated_input["model"] = DELEGATION_MODEL
        updated_input["run_in_background"] = False
        output["hookSpecificOutput"]["updatedInput"] = updated_input
    if decision == "allow" and isinstance(record, dict) and session_id and AUTH_DIR:
        # Issue #2258 AC2/AC3: wire the directive's mode/fallback policy
        # into the delegation lock/marker state (see required_lock_path /
        # preferred_marker_path) and surface it to the model via
        # additionalContext, so a `required`+`forbidden` invocation is
        # explicitly told never to silently substitute another
        # model/agent on failure, and a `preferred` invocation's eventual
        # fallback gets logged (Issue #2258 AC2).
        mode_v = record.get("delegation_mode")
        fallback_v = record.get("delegation_fallback")
        if mode_v == "required" and fallback_v == "forbidden":
            # Issue #2258 P0-1 fix-delta: the no-fallback lock was already
            # established at UserPromptSubmit time (see
            # cmd_user_prompt_submit), not here -- this branch now only
            # surfaces the additionalContext instruction message.
            output["hookSpecificOutput"]["additionalContext"] = "CLAUDE_GPT_SPARK_DELEGATION_V1 " + json.dumps(
                {
                    "mode": mode_v,
                    "fallback": fallback_v,
                    "instruction": (
                        "If this Spark invocation fails for any reason, you MUST NOT "
                        "silently substitute a different agent or model; report the "
                        "failure explicitly to the user and stop this delegation path."
                    ),
                }
            )
        elif mode_v == "preferred":
            try:
                with open(preferred_marker_path(session_id), "w", encoding="utf-8") as fh:
                    json.dump({"session_id": session_id, "created_ts": time.time()}, fh)
            except OSError:
                pass
    # Issue #2186 P1 fix-delta (PR #2244 adversarial review): allow/deny is
    # communicated EXCLUSIVELY via exit-0 stdout structured JSON
    # (`hookSpecificOutput.permissionDecision`), never via `exit 2` +
    # stderr blocking-error semantics. Claude Code's own hook contract
    # treats structured JSON decisions and exit-2 blocking errors as two
    # separate mechanisms; mixing them risked `permissionDecisionReason`
    # diagnostic values (e.g. `authorization_carryover_stale_launch_nonce`)
    # never actually being interpreted because they were written to stdout
    # while exit 2 signals a stderr-based blocking error path instead.
    sys.stdout.write(json.dumps(output))
    sys.exit(0)


def cmd_subagent_lifecycle(payload):
    # SubagentStart/SubagentStop are audit-only for this gate; causal evidence
    # itself is derived independently from `--include-hook-events` stream-json
    # lifecycle events (PR #2220 `extract_claude_hook_lifecycle_events()`),
    # reused unchanged (Issue #2186 In Scope).
    return


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = read_payload()
    try:
        if event == "user-prompt-submit":
            cmd_user_prompt_submit(payload)
        elif event == "pre-tool-use-agent":
            cmd_pre_tool_use_agent(payload)
        elif event in ("subagent-start", "subagent-stop"):
            cmd_subagent_lifecycle(payload)
    except SystemExit:
        raise
    except Exception:
        # Fail closed: never let an unexpected exception here silently permit
        # a spark-codex invocation nor crash the parent claude-gpt session.
        # Decision channel is exit-0 stdout JSON only (Issue #2186 P1
        # fix-delta) -- never exit 2, so this path stays consistent with
        # cmd_pre_tool_use_agent's normal deny path.
        if event == "pre-tool-use-agent":
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "gate_internal_error",
                }
            }
            sys.stdout.write(json.dumps(output))
        sys.exit(0)


if __name__ == "__main__":
    main()
# SPARK_GATE_WRITER_PY_END
SPARK_GATE_WRITER_PY_END_MARKER
)
# Issue #2186 P0 fix-delta: SPARK_LAUNCH_NONCE is generated exclusively from
# `date -u +%Y%m%dT%H%M%SZ` and `$$` (see SPARK_LAUNCH_NONCE= above), so it is
# always `[0-9A-Za-z_-]+` and safe as a sed replacement (no `/`, `&`,
# backslash, or newline). Fail closed if that assumption is ever violated
# instead of silently emitting a broken gate script.
case "$SPARK_LAUNCH_NONCE" in
  *[!0-9A-Za-z_-]*)
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"spark_launch_nonce_unsafe_chars"}\n'
    exit 9
    ;;
esac
# Substitute the literal nonce into the gate script file directly, rather
# than exporting CLAUDE_GPT_SPARK_LAUNCH_NONCE into the claude process's own
# environment (P0 forgery fix -- see the LAUNCH_NONCE placeholder comment
# embedded in the gate script above).
sed -i "s/__CLAUDE_GPT_SPARK_LAUNCH_NONCE__/${SPARK_LAUNCH_NONCE}/" "$SPARK_GATE_WRITER"
export CLAUDE_GPT_SPARK_AUTH_DIR="$SPARK_AUTH_DIR_TARGET"
export SPARK_GATE_WRITER
SPARK_AGENTS_JSON=$(claude_gpt_spark_agents_json_fragment)

# --- explicit-only authorization gate hook groups (Issue #2186 P0 fix-delta,
#     PR #2244 adversarial review) ---
#
# The gate-owning hook groups below MUST always be present, regardless of
# CLAUDE_GPT_RUNTIME_SMOKE_HOOKS. Prior to this fix, setting
# CLAUDE_GPT_RUNTIME_SMOKE_HOOKS reassigned HOOKS_JSON_FRAGMENT wholesale,
# which silently dropped the "PreToolUse"(matcher: Agent) authorization gate
# entry (and the UserPromptSubmit/SubagentStart/SubagentStop gate entries)
# whenever runtime-smoke observation hooks were requested -- leaving the
# spark-codex authorization gate structurally absent (Claude Code
# default-allows tool calls when no PreToolUse hook matches). Runtime-smoke
# observation commands are now appended as ADDITIONAL hook-group objects
# within the same event's array, never as a replacement.
UPS_HOOK_GROUPS='{"hooks": [{"type": "command", "command": "python3 \"$SPARK_GATE_WRITER\" user-prompt-submit"}]}'
PTU_HOOK_GROUPS='{"matcher": "Agent", "hooks": [{"type": "command", "command": "python3 \"$SPARK_GATE_WRITER\" pre-tool-use-agent"}]}'
SAS_HOOK_GROUPS='{"hooks": [{"type": "command", "command": "python3 \"$SPARK_GATE_WRITER\" subagent-start"}]}'
SAP_HOOK_GROUPS='{"hooks": [{"type": "command", "command": "python3 \"$SPARK_GATE_WRITER\" subagent-stop"}]}'
STOP_HOOK_GROUPS=""
STOPFAILURE_HOOK_GROUPS=""
ENV_JSON_FRAGMENT=",
  \"env\": {\"SPARK_GATE_WRITER\": \"${SPARK_GATE_WRITER}\"}"
if [ "${CLAUDE_GPT_RUNTIME_SMOKE_HOOKS:-}" = "subagent-start-stop" ]; then
  # Observation-only "cat" sinks appended as an additional matcher-group,
  # alongside (never instead of) the gate's SubagentStart/SubagentStop
  # audit entries above.
  CAT_SINK_GROUP='{"hooks": [{"type": "command", "command": "cat"}]}'
  SAS_HOOK_GROUPS="${SAS_HOOK_GROUPS}, ${CAT_SINK_GROUP}"
  SAP_HOOK_GROUPS="${SAP_HOOK_GROUPS}, ${CAT_SINK_GROUP}"
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
    # Issue #2259 AC10 fix_delta: SubagentStart/SubagentStop hook payloads
    # carry an `agent_type` field identifying which named subagent
    # (matching a .claude/agents/<name>.md frontmatter `name:`) was
    # actually spawned/stopped (see
    # .claude/hooks/capture_scope_rollup_final_response.py's own use of
    # this same field for an existing, independently-verified precedent).
    # Recording it here lets runtime_smoke_test.sh's issue_create scenario
    # independently confirm that the genuine `issue-creator` SubAgent (not
    # merely a text mention of it) was spawned and stopped, without
    # relying on the root model's self-reported transcript text.
    agent_type = payload.get("agent_type")
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
        "agent_type": agent_type if isinstance(agent_type, str) else None,
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
  # Observation-only sink writer groups appended alongside (never instead
  # of) the gate's UserPromptSubmit/SubagentStart/SubagentStop entries
  # above; gate command runs first within each event's hook array.
  # Stop/StopFailure have no gate equivalent, so they get sink-only groups.
  SINK_GROUP='{"hooks": [{"type": "command", "command": "python3 \"$CLAUDE_GPT_HOOK_SINK_WRITER\""}]}'
  UPS_HOOK_GROUPS="${UPS_HOOK_GROUPS}, ${SINK_GROUP}"
  STOP_HOOK_GROUPS="${SINK_GROUP}"
  STOPFAILURE_HOOK_GROUPS="${SINK_GROUP}"
  SAS_HOOK_GROUPS="${SAS_HOOK_GROUPS}, ${SINK_GROUP}"
  SAP_HOOK_GROUPS="${SAP_HOOK_GROUPS}, ${SINK_GROUP}"
  # Also baked literally into settings.json's own "env" block (belt-and-
  # braces alongside the exported shell env vars below) so the 3-way
  # run_nonce match the harness verifies (AC16) has a nonce value that is
  # genuinely "baked into settings.json at launch", not merely inherited.
  # SPARK_GATE_WRITER is retained here too, since the authorization gate
  # hook commands above remain wired and still need it resolved.
  ENV_JSON_FRAGMENT=",
  \"env\": {
    \"SPARK_GATE_WRITER\": \"${SPARK_GATE_WRITER}\",
    \"CLAUDE_GPT_HOOK_SINK_NONCE\": \"${CLAUDE_GPT_HOOK_SINK_NONCE}\",
    \"CLAUDE_GPT_HOOK_SINK_PATH\": \"${CLAUDE_GPT_HOOK_SINK_PATH}\",
    \"CLAUDE_GPT_HOOK_SINK_WRITER\": \"${CLAUDE_GPT_HOOK_SINK_WRITER}\"
  }"
  export CLAUDE_GPT_HOOK_SINK_NONCE CLAUDE_GPT_HOOK_SINK_PATH CLAUDE_GPT_HOOK_SINK_WRITER
fi

HOOKS_JSON_FRAGMENT=',
  "hooks": {
    "UserPromptSubmit": ['"${UPS_HOOK_GROUPS}"'],
    "PreToolUse": ['"${PTU_HOOK_GROUPS}"'],
    "SubagentStart": ['"${SAS_HOOK_GROUPS}"'],
    "SubagentStop": ['"${SAP_HOOK_GROUPS}"']'"$(
  if [ -n "$STOP_HOOK_GROUPS" ]; then
    printf ',\n    "Stop": [%s],\n    "StopFailure": [%s]' "$STOP_HOOK_GROUPS" "$STOPFAILURE_HOOK_GROUPS"
  fi
)"'
  }'

# --- Spark authorization sidecar directory deny (Issue #2186 P0 fix-delta,
#     PR #2244 adversarial review, forgery finding) ---
#
# `Read`/`Write`/`Edit` deny on SPARK_AUTH_DIR_TARGET is a best-effort
# defense against main Claude's built-in tools directly forging or reading
# the pending-authorization sidecar file. Like the existing
# PROXY_CONFIG_DIR_TARGET/PROXY_STATE_DIR_TARGET/PROXY_HOME_TARGET denies
# above, this does NOT constrain the unrestricted Bash tool (`cat`,
# `python3 -c`, etc. can still read/write under this directory); combined
# with removing CLAUDE_GPT_SPARK_LAUNCH_NONCE from the claude process's own
# environment (see the sed substitution above), forging a pending record
# with the correct `launch_nonce` value now additionally requires
# discovering that value via a Bash-tool file read rather than a trivial
# `env` inspection, but is not architecturally impossible for an
# unrestricted Bash tool. This residual gap is a known limitation, not a
# closed security boundary (documented for the reviewer, consistent with
# Issue #2158's Scope Reframe treatment of the analogous proxy-dir denies).

# --- launcher-owned autoMode policy（Issue #2203, second-gate 判断補助。
#     決定論的 authority は permissions.deny / PreToolUse hook / GitHub mutation
#     transaction broker であり、この autoMode は project .claude/settings*.json
#     ではなくこの launcher-owned --settings にのみ注入する） ---
AUTO_MODE_JSON_FRAGMENT=$(claude_gpt_auto_mode_json_fragment)

cat > "$SETTINGS_PATH" <<SETTINGS_JSON_EOF
{
  "permissions": {
    "deny": [
      "Read(/${PROXY_CONFIG_DIR_TARGET}/**)",
      "Read(/${PROXY_STATE_DIR_TARGET}/**)",
      "Read(/${PROXY_HOME_TARGET}/**)",
      "Read(/${SPARK_AUTH_DIR_TARGET}/**)",
      "Write(/${SPARK_AUTH_DIR_TARGET}/**)",
      "Edit(/${SPARK_AUTH_DIR_TARGET}/**)"
    ]
  },
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
  env -i \
    "PATH=$PATH" \
    "HOME=$PROXY_HOME_TARGET" \
    "CCP_CONFIG_DIR=$PROXY_CONFIG_DIR_TARGET" \
    "XDG_STATE_HOME=$PROXY_STATE_DIR_TARGET" \
    "CCP_BIND_ADDRESS=127.0.0.1" \
    "CCP_LOG_STDERR=1" \
    "CCP_CODEX_TRANSPORT=$CLAUDE_GPT_CODEX_TRANSPORT_POLICY" \
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

# --- Issue #2259: 隔離 Claude-GPT の issue.create 要求を処理する parent bridge の
#     endpoint（socket path / run nonce）をここで計算する。実際の bridge server
#     process 起動は通常起動経路（HOME isolation 切り替え直前）でのみ行うが、
#     endpoint 自体は check-only でも計算し JSON へ含めることで、fake claude/proxy
#     を用意せず本経路を hermetic に検証できるようにする（AC2/AC3 の配線確認）。 ---
BRIDGE_SOCKET_PATH=$(claude_gpt_issue_create_bridge_socket_path "$$")
BRIDGE_LEDGER_PATH=$(claude_gpt_issue_create_bridge_ledger_path)
BRIDGE_RUN_NONCE=$(claude_gpt_generate_run_nonce)
BRIDGE_RUN_NONCE_LEN=${#BRIDGE_RUN_NONCE}

if [ "$CHECK_ONLY" = "true" ]; then
  kill "$PROXY_PID" 2>/dev/null
  wait "$PROXY_PID" 2>/dev/null
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"ok","mode":"check_only","port":%s,"bind_ok":%s,"model_alias_ok":%s,"strict_mcp_mode":%s,"mcp_config_path":"%s","settings_path":"%s","proxy_home_dir":"%s","proxy":{"absolute_path":"%s","version":"%s"},"preflight":%s,"issue_create_bridge":{"socket_path":"%s","ledger_path":"%s","run_nonce_len":%s}}\n' \
    "$PROXY_PORT" "$BIND_OK" "$MODEL_ALIAS_OK" "$STRICT_MCP_MODE" "$MCP_CONFIG_PATH" "$SETTINGS_PATH" "$PROXY_HOME_TARGET" "$PROXY_BIN_TARGET" "$PROXY_VERSION_TARGET" "$PREFLIGHT_JSON" \
    "$BRIDGE_SOCKET_PATH" "$BRIDGE_LEDGER_PATH" "$BRIDGE_RUN_NONCE_LEN"
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
# --- Claude/AGY プロセスから genuine `gh` auth context を利用不能にする（P0-6,
#     PR #2214 OWNER adversarial review 反映）。GitHub write credential は
#     GitHub mutation transaction broker（別プロセス、ambient 実 HOME/GH_CONFIG_DIR
#     を使う）のみが保持する。ここで Claude 子プロセスの HOME/GH_CONFIG_DIR/
#     XDG_CONFIG_HOME/XDG_CACHE_HOME を空の隔離ディレクトリへ差し替え、
#     GH_TOKEN 系 / SSH_AUTH_SOCK / GIT_ASKPASS 系を scrub することで、raw `gh` /
#     raw `git push`（SSH 経由を含む）を Claude セッション内から実行しても
#     既存 authentication に到達できない状態にする。 ---
# --- Issue #2259: parent bridge server の起動。launcher 自身の trusted ambient
#     env（実 HOME/GH_CONFIG_DIR/GH_TOKEN 等）がまだ生きているこの時点で起動する
#     ことで、bridge server プロセスだけが GitHub write credential へ到達できる
#     状態を作る（Outcome 節）。子 Claude プロセスは isolation profile 環境変数
#     経由でのみこの bridge と通信し、raw gh には到達しない。 ---
BRIDGE_REPO=$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null | sed -E 's#.*github\.com[:/]##; s#\.git$##')
# CLAUDE_GPT_ISSUE_CREATE_BRIDGE_GH_BIN: bridge server 起動時にのみ参照する gh 実行
# ファイルの上書き（既定 "gh"）。runtime_smoke_test.sh の issue_create シナリオが
# 実 GitHub network を叩かない fake gh provider に差し替えるための唯一の入口。
BRIDGE_GH_BIN="${CLAUDE_GPT_ISSUE_CREATE_BRIDGE_GH_BIN:-gh}"
mkdir -p "$(dirname "$BRIDGE_SOCKET_PATH")"
uv run --locked python3 "$SCRIPT_DIR/issue_create_bridge_server.py" \
  --socket-path "$BRIDGE_SOCKET_PATH" \
  --run-nonce "$BRIDGE_RUN_NONCE" \
  --repo "$BRIDGE_REPO" \
  --gh "$BRIDGE_GH_BIN" \
  --ledger-path "$BRIDGE_LEDGER_PATH" \
  --repo-root "$REPO_ROOT" \
  >/dev/null 2>&1 &
BRIDGE_PID=$!

# bridge server がソケットを bind するまで bounded wait（最大 5 秒）。
BRIDGE_WAIT_ATTEMPTS=0
while [ ! -S "$BRIDGE_SOCKET_PATH" ] && [ "$BRIDGE_WAIT_ATTEMPTS" -lt 50 ]; do
  sleep 0.1
  BRIDGE_WAIT_ATTEMPTS=$((BRIDGE_WAIT_ATTEMPTS + 1))
done
echo "CLAUDE_GPT_ISSUE_CREATE_BRIDGE_PID=${BRIDGE_PID}" >&2
echo "CLAUDE_GPT_ISSUE_CREATE_BRIDGE_SOCKET=${BRIDGE_SOCKET_PATH}" >&2

export CLAUDE_GPT_ISOLATION_PROFILE=1
export CLAUDE_GPT_ISSUE_CREATE_SOCKET="$BRIDGE_SOCKET_PATH"
export CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE="$BRIDGE_RUN_NONCE"

export HOME="$CLAUDE_ISOLATED_HOME_TARGET"
export GH_CONFIG_DIR="$CLAUDE_ISOLATED_GH_CONFIG_DIR_TARGET"
export XDG_CONFIG_HOME="$CLAUDE_ISOLATED_XDG_CONFIG_DIR_TARGET"
export XDG_CACHE_HOME="$CLAUDE_ISOLATED_XDG_CACHE_DIR_TARGET"
unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
unset GH_HOST GH_REPO
unset SSH_AUTH_SOCK
unset GIT_ASKPASS SSH_ASKPASS GIT_CREDENTIAL_HELPER

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
  # Issue #2259: parent bridge server も launcher が所有する終了/cleanup 対象。
  # SIGTERM を送ると server 側が自身のソケットファイルを削除して終了するが
  # (issue_create_bridge_server.py の SIGTERM handler)、念のためここでも
  # best-effort でソケットファイルを削除する。
  if [ -n "$BRIDGE_PID" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    kill "$BRIDGE_PID" 2>/dev/null
    wait "$BRIDGE_PID" 2>/dev/null
  fi
  if [ -n "$BRIDGE_SOCKET_PATH" ] && [ -S "$BRIDGE_SOCKET_PATH" ]; then
    rm -f "$BRIDGE_SOCKET_PATH" 2>/dev/null
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
# shellcheck disable=SC2086
"$CLAUDE_BIN" --strict-mcp-config --mcp-config "$MCP_CONFIG_PATH" --settings "$SETTINGS_PATH" --permission-mode auto --agents "$SPARK_AGENTS_JSON" "$@" <&9 &
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

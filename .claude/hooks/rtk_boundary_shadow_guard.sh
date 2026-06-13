#!/usr/bin/env bash
# rtk_boundary_shadow_guard.sh
#
# Claude Code PreToolUse hook: rtk trust boundary の direct bypass を shadow mode で記録する
#
# AGENTS.md が rtk 経由を求める mutating コマンドの direct 実行を
# JSONL に記録のみ行う。block は一切行わない（常に exit 0）。
#
# 記録形式は guard-japanese-prose.sh / shadow_log.py の shadow log 方式に準拠。
#
# Fail-open 設計: stdin 不正 / jq 不在 / ログ書き込み失敗 / 未定義変数 のいずれでも
# exit 0 で通過し、可能な場合のみ JSONL に記録する。
#
# Exit codes:
#   0 = always (shadow mode: block しない)

# fail-open: エラーが起きても exit 0 を保証するため set -euo pipefail は使わない
# 代わりに各操作で個別エラーハンドリングを行う

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" 2>/dev/null && pwd)" || SCRIPT_DIR=""
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." 2>/dev/null && pwd)" || PROJECT_DIR="${CLAUDE_PROJECT_DIR:-}"
SHADOW_LOG_PY="${SCRIPT_DIR}/shadow_log.py"
SHADOW_LOG_FILE="${RTK_SHADOW_LOG:-${PROJECT_DIR}/.guard_shadow_log.jsonl}"

# stdin を全部読む（空でも可）
INPUT="$(cat 2>/dev/null)" || INPUT=""

# stdin が空なら記録不要で終了
if [ -z "$INPUT" ]; then
    echo "rtk_boundary_shadow_guard: empty stdin, skipping" >&2
    exit 0
fi

# jq が使えるか確認（fail-open）
if ! command -v jq >/dev/null 2>&1; then
    echo "rtk_boundary_shadow_guard: jq not found, skipping classification" >&2
    exit 0
fi

# tool_name と tool_use_id を抽出
TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)" || TOOL_NAME=""
TOOL_USE_ID="$(echo "$INPUT" | jq -r '.tool_use_id // ""' 2>/dev/null)" || TOOL_USE_ID=""

# Bash ツール以外はスコープ外
if [ "$TOOL_NAME" != "Bash" ]; then
    exit 0
fi

# command を抽出
COMMAND="$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)" || COMMAND=""

# command が空なら記録不要
if [ -z "$COMMAND" ]; then
    exit 0
fi

# ============================================================
# command 分類ロジック
# ============================================================

# command の先頭トークンを抽出（先頭空白除去）
FIRST_TOKEN="$(echo "$COMMAND" | sed 's/^[[:space:]]*//' | awk '{print $1}')"

# 分類結果
CATEGORY=""
MATCHED_RULE=""
DECISION_WOULD_BE="allow"

# --- git 分類 ---
if [ "$FIRST_TOKEN" = "git" ]; then
    # git サブコマンドを取得
    GIT_SUBCMD="$(echo "$COMMAND" | sed 's/^[[:space:]]*git[[:space:]]*//' | awk '{print $1}')"

    case "$GIT_SUBCMD" in
        commit|push|reset|rebase|tag|merge|cherry-pick|revert|am|apply|fetch|pull|rm|mv|add)
            CATEGORY="mutating_git"
            MATCHED_RULE="git_${GIT_SUBCMD}"
            DECISION_WOULD_BE="deny"
            ;;
        status|log|diff|show|branch|describe|blame|shortlog|reflog|grep|ls-files|ls-tree|stash|worktree|checkout|switch|restore)
            # stash / worktree / checkout / switch / restore は状況によって mutating だが
            # 本 shadow guard では safe_readonly_git として記録（follow-up で精緻化）
            CATEGORY="safe_readonly_git"
            MATCHED_RULE="git_${GIT_SUBCMD}"
            DECISION_WOULD_BE="allow"
            ;;
        *)
            # 不明なサブコマンドは mutating_git として保守的に記録
            CATEGORY="mutating_git"
            MATCHED_RULE="git_unknown_${GIT_SUBCMD}"
            DECISION_WOULD_BE="deny"
            ;;
    esac

# --- gh 分類 ---
elif [ "$FIRST_TOKEN" = "gh" ]; then
    # gh サブコマンドを取得
    GH_SUBCMD="$(echo "$COMMAND" | sed 's/^[[:space:]]*gh[[:space:]]*//' | awk '{print $1}')"
    GH_ACTION="$(echo "$COMMAND" | sed 's/^[[:space:]]*gh[[:space:]]*//' | awk '{print $2}')"

    case "$GH_SUBCMD" in
        api)
            # -X PATCH / PUT / POST / DELETE は mutating
            if echo "$COMMAND" | grep -qE '(-X[[:space:]]*(PATCH|PUT|POST|DELETE)|--method[[:space:]]*(PATCH|PUT|POST|DELETE))'; then
                CATEGORY="mutating_gh_api"
                MATCHED_RULE="gh_api_mutating_method"
                DECISION_WOULD_BE="deny"
            else
                CATEGORY="safe_readonly_gh"
                MATCHED_RULE="gh_api_readonly"
                DECISION_WOULD_BE="allow"
            fi
            ;;
        issue)
            case "$GH_ACTION" in
                edit|create|comment|close|reopen|transfer|delete|pin|unpin|lock|unlock|develop)
                    CATEGORY="mutating_gh"
                    MATCHED_RULE="gh_issue_${GH_ACTION}"
                    DECISION_WOULD_BE="deny"
                    ;;
                view|list|status)
                    CATEGORY="safe_readonly_gh"
                    MATCHED_RULE="gh_issue_${GH_ACTION}"
                    DECISION_WOULD_BE="allow"
                    ;;
                *)
                    CATEGORY="mutating_gh"
                    MATCHED_RULE="gh_issue_unknown_${GH_ACTION}"
                    DECISION_WOULD_BE="deny"
                    ;;
            esac
            ;;
        pr)
            case "$GH_ACTION" in
                create|merge|review|comment|edit|close|reopen|ready|draft|lock|unlock)
                    CATEGORY="mutating_gh"
                    MATCHED_RULE="gh_pr_${GH_ACTION}"
                    DECISION_WOULD_BE="deny"
                    ;;
                view|list|status|checks|diff)
                    CATEGORY="safe_readonly_gh"
                    MATCHED_RULE="gh_pr_${GH_ACTION}"
                    DECISION_WOULD_BE="allow"
                    ;;
                *)
                    CATEGORY="mutating_gh"
                    MATCHED_RULE="gh_pr_unknown_${GH_ACTION}"
                    DECISION_WOULD_BE="deny"
                    ;;
            esac
            ;;
        release|repo|workflow|run|gist|secret)
            # これらは mutating が多い — 保守的に mutating_gh
            CATEGORY="mutating_gh"
            MATCHED_RULE="gh_${GH_SUBCMD}_${GH_ACTION}"
            DECISION_WOULD_BE="deny"
            ;;
        auth|config|extension|alias|completion|status|version)
            CATEGORY="safe_readonly_gh"
            MATCHED_RULE="gh_${GH_SUBCMD}"
            DECISION_WOULD_BE="allow"
            ;;
        *)
            CATEGORY="mutating_gh"
            MATCHED_RULE="gh_unknown_${GH_SUBCMD}"
            DECISION_WOULD_BE="deny"
            ;;
    esac

# --- pnpm / npm / yarn / bun 分類 ---
elif [ "$FIRST_TOKEN" = "pnpm" ] || [ "$FIRST_TOKEN" = "npm" ] || \
     [ "$FIRST_TOKEN" = "yarn" ] || [ "$FIRST_TOKEN" = "bun" ]; then
    PKG_SUBCMD="$(echo "$COMMAND" | sed "s/^[[:space:]]*${FIRST_TOKEN}[[:space:]]*//" | awk '{print $1}')"

    case "$PKG_SUBCMD" in
        add|install|remove|uninstall|update|upgrade|link|unlink|rebuild|pack|publish|version)
            CATEGORY="dependency_mutation"
            MATCHED_RULE="${FIRST_TOKEN}_${PKG_SUBCMD}"
            DECISION_WOULD_BE="deny"
            ;;
        test|build|run|exec|dlx|check-types|typecheck|lint|format|dev|start|preview)
            CATEGORY="safe_validation"
            MATCHED_RULE="${FIRST_TOKEN}_${PKG_SUBCMD}"
            DECISION_WOULD_BE="allow"
            ;;
        # npm install (no subcommand) も dependency_mutation
        "")
            if [ "$FIRST_TOKEN" = "npm" ] || [ "$FIRST_TOKEN" = "yarn" ] || [ "$FIRST_TOKEN" = "bun" ]; then
                CATEGORY="dependency_mutation"
                MATCHED_RULE="${FIRST_TOKEN}_bare_install"
                DECISION_WOULD_BE="deny"
            else
                CATEGORY="safe_validation"
                MATCHED_RULE="${FIRST_TOKEN}_bare"
                DECISION_WOULD_BE="allow"
            fi
            ;;
        *)
            # 不明なサブコマンドは safe_validation として許容
            CATEGORY="safe_validation"
            MATCHED_RULE="${FIRST_TOKEN}_unknown_${PKG_SUBCMD}"
            DECISION_WOULD_BE="allow"
            ;;
    esac

# --- curl / env direct bypass ---
elif [ "$FIRST_TOKEN" = "curl" ] || [ "$FIRST_TOKEN" = "wget" ]; then
    CATEGORY="out_of_scope_logged"
    MATCHED_RULE="curl_or_wget_direct"
    DECISION_WOULD_BE="allow"

# --- その他 ---
else
    # スコープ外: 記録なしで通過
    exit 0
fi

# ============================================================
# command_sha256 と command_preview_redacted を生成
# raw command は JSONL に保存しない
# ============================================================

# SHA256 計算（fail-open）
COMMAND_SHA256="sha256:$(echo -n "$COMMAND" | sha256sum 2>/dev/null | awk '{print $1}')" || \
    COMMAND_SHA256="sha256:unavailable"

COMMAND_BYTES="${#COMMAND}"

# command_preview_redacted: 200 bytes に切り、機密情報を redact
# redact 対象: Authorization: / GH_TOKEN / GITHUB_TOKEN / --header / -H / HEREDOC / URL query token
PREVIEW="$(echo "$COMMAND" | head -c 200)" || PREVIEW=""
# Authorization ヘッダーを redact
PREVIEW="$(echo "$PREVIEW" | sed 's/Authorization:[[:space:]]*[^[:space:]]*/Authorization: <redacted>/g' 2>/dev/null)" || true
# GH_TOKEN / GITHUB_TOKEN を redact
PREVIEW="$(echo "$PREVIEW" | sed 's/GH_TOKEN=[^[:space:]]*/GH_TOKEN=<redacted>/g;s/GITHUB_TOKEN=[^[:space:]]*/GITHUB_TOKEN=<redacted>/g' 2>/dev/null)" || true
# --header / -H の値を redact
PREVIEW="$(echo "$PREVIEW" | sed 's/\(--header\|-H\)[[:space:]]*[^[:space:]]*/\1 <redacted>/g' 2>/dev/null)" || true
# HEREDOC (EOF) の内容を示唆するマーカーを redact
PREVIEW="$(echo "$PREVIEW" | sed 's/<<[[:space:]]*['"'"'"]*/<<HEREDOC_REDACTED /g' 2>/dev/null)" || true
# URL query string の token / key パラメータを redact
PREVIEW="$(echo "$PREVIEW" | sed 's/[?&]\(token\|key\|access_token\|api_key\)=[^&[:space:]]*/\&\1=<redacted>/g' 2>/dev/null)" || true

# SESSION_ID 取得（環境変数 CLAUDE_SESSION_ID があれば使う）
SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"

# ============================================================
# JSONL 記録（shadow_log.py 経由）
# shadow_log.py が使えない場合は直接書き込み（fail-open）
# ============================================================

_write_jsonl_direct() {
    # shadow_log.py が使えない場合の fallback（fail-open）
    local log_file="$1"
    local entry
    entry="$(jq -n \
        --arg guard_name "rtk_boundary_shadow_guard" \
        --arg category "${CATEGORY:-unknown}" \
        --arg matched_rule "${MATCHED_RULE:-unknown}" \
        --arg decision_would_be "${DECISION_WOULD_BE:-allow}" \
        --arg command_sha256 "${COMMAND_SHA256:-}" \
        --arg command_preview_redacted "${PREVIEW:-}" \
        --argjson command_bytes "${COMMAND_BYTES:-0}" \
        --arg session_id "${SESSION_ID:-unknown}" \
        --arg tool_use_id "${TOOL_USE_ID:-}" \
        --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)" \
        '{
            guard_name: $guard_name,
            category: $category,
            matched_rule: $matched_rule,
            decision_would_be: $decision_would_be,
            command_sha256: $command_sha256,
            command_preview_redacted: $command_preview_redacted,
            command_bytes: $command_bytes,
            session_id: $session_id,
            tool_use_id: $tool_use_id,
            timestamp: $timestamp
        }' 2>/dev/null)" || return 1

    # ログディレクトリを作成（fail-open）
    local log_dir
    log_dir="$(dirname "$log_file")"
    if [ -n "$log_dir" ] && [ "$log_dir" != "." ]; then
        mkdir -p "$log_dir" 2>/dev/null || true
    fi

    echo "$entry" >> "$log_file" 2>/dev/null || return 1
}

# shadow_log.py 経由で記録を試みる
if [ -f "$SHADOW_LOG_PY" ] && command -v uv >/dev/null 2>&1; then
    FIELDS_JSON="$(jq -n \
        --arg guard_name "rtk_boundary_shadow_guard" \
        --arg category "${CATEGORY:-unknown}" \
        --arg matched_rule "${MATCHED_RULE:-unknown}" \
        --arg decision_would_be "${DECISION_WOULD_BE:-allow}" \
        --arg command_sha256 "${COMMAND_SHA256:-}" \
        --arg command_preview_redacted "${PREVIEW:-}" \
        --argjson command_bytes "${COMMAND_BYTES:-0}" \
        --arg session_id "${SESSION_ID:-unknown}" \
        --arg tool_use_id "${TOOL_USE_ID:-}" \
        '{
            guard_name: $guard_name,
            category: $category,
            matched_rule: $matched_rule,
            decision_would_be: $decision_would_be,
            command_sha256: $command_sha256,
            command_preview_redacted: $command_preview_redacted,
            command_bytes: $command_bytes,
            session_id: $session_id,
            tool_use_id: $tool_use_id
        }' 2>/dev/null)" || FIELDS_JSON=""

    if [ -n "$FIELDS_JSON" ]; then
        if ! uv run python3 "$SHADOW_LOG_PY" \
            --log-file "$SHADOW_LOG_FILE" \
            --fields-json "$FIELDS_JSON" >/dev/null 2>&1; then
            echo "rtk_boundary_shadow_guard: shadow_log.py write failed, trying direct write" >&2
            _write_jsonl_direct "$SHADOW_LOG_FILE" || \
                echo "rtk_boundary_shadow_guard: direct write also failed, continuing" >&2
        fi
    else
        echo "rtk_boundary_shadow_guard: jq fields build failed, trying direct write" >&2
        _write_jsonl_direct "$SHADOW_LOG_FILE" || \
            echo "rtk_boundary_shadow_guard: direct write failed, continuing" >&2
    fi
else
    # shadow_log.py / uv が使えない場合は直接書き込み
    _write_jsonl_direct "$SHADOW_LOG_FILE" || \
        echo "rtk_boundary_shadow_guard: write failed (shadow_log.py unavailable), continuing" >&2
fi

# 常に exit 0（block しない）
exit 0

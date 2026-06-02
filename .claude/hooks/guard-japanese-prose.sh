#!/usr/bin/env bash
# guard-japanese-prose.sh
#
# Claude Code PreToolUse hook: 日本語 prose 比率不足の GitHub 送信・下書きファイルをブロックする
#
# Mode A: Bash ツール + gh コマンドで Issue/PR/comment body を送信しようとする場合
# Mode B: Write/Edit/MultiEdit ツールで tmp/ 下書き候補 Markdown を書こうとする場合
#
# Exit codes:
#   0 = allow (日本語比率 OK、またはガード対象外)
#   2 = block (日本語比率不足 — blocking error として Claude Code に通知)

set -euo pipefail

# スクリプトのディレクトリから validate_japanese_content.py を探す
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
VALIDATOR="${PROJECT_DIR}/.claude/skills/create-issue/scripts/validate_japanese_content.py"

# stdin から JSON を読む
INPUT="$(cat)"

# tool_name を取得
TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")"

# validator が存在しない場合は通す（bootstrap時の安全弁）
if [ ! -f "$VALIDATOR" ]; then
    exit 0
fi

# ============================================================
# ヘルパー関数
# ============================================================

# 日本語比率を検証する
# $1: チェック対象の本文テキスト
# $2: 対象の説明（stderr 出力用）
validate_body() {
    local body="$1"
    local context="$2"

    if [ -z "$body" ]; then
        return 0
    fi

    # validator を実行
    if echo "$body" | uv run python3 "$VALIDATOR" --threshold 0.1 >/dev/null 2>/dev/null; then
        return 0
    else
        local exit_code=$?
        # validator の詳細情報を stderr に出力
        local details
        details="$(echo "$body" | uv run python3 "$VALIDATOR" --threshold 0.1 2>&1 || true)"
        echo "GUARD: 日本語比率不足 [${context}]" >&2
        echo "${details}" >&2
        return 2
    fi
}

# ============================================================
# Mode A: Bash ツール + gh コマンドのガード
# ============================================================

if [ "$TOOL_NAME" = "Bash" ]; then
    COMMAND="$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null || echo "")"

    # gh コマンドが含まれているか確認
    if ! echo "$COMMAND" | grep -qE '\bgh\b'; then
        exit 0
    fi

    # 対象の gh サブコマンドを確認
    # gh issue create/edit/comment, gh pr create/edit/comment/review, gh api
    if ! echo "$COMMAND" | grep -qE 'gh (issue (create|edit|comment)|pr (create|edit|comment|review)|api)'; then
        exit 0
    fi

    # body を抽出する試み（複数パターン）

    # 1. --body "..." または --body '...' パターン
    BODY=""

    # Python の shlex を使って引数を安全に解析
    BODY_EXTRACT=$(uv run python3 - "$COMMAND" <<'PYEOF' 2>/dev/null || echo "")
import sys
import shlex
import re

command = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    tokens = shlex.split(command)
except ValueError:
    # shlex 失敗時は正規表現でフォールバック
    tokens = []

body_value = None

# --body / -b の次のトークン
i = 0
while i < len(tokens):
    if tokens[i] in ('--body', '-b') and i + 1 < len(tokens):
        body_value = tokens[i + 1]
        break
    # --body=value 形式
    if tokens[i].startswith('--body='):
        body_value = tokens[i][len('--body='):]
        break
    # --field body=... / --raw-field body=... / -f body=
    if tokens[i] in ('--field', '--raw-field', '-f') and i + 1 < len(tokens):
        if tokens[i + 1].startswith('body='):
            body_value = tokens[i + 1][5:]
            break
    i += 1

if body_value and body_value != '-':
    print(body_value)
PYEOF

    if [ -n "$BODY_EXTRACT" ]; then
        BODY="$BODY_EXTRACT"
    fi

    # 2. --body-file でファイルを読む場合
    BODY_FILE_EXTRACT=$(uv run python3 - "$COMMAND" <<'PYEOF' 2>/dev/null || echo "")
import sys
import shlex

command = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    tokens = shlex.split(command)
except ValueError:
    tokens = []

i = 0
while i < len(tokens):
    if tokens[i] == '--body-file' and i + 1 < len(tokens):
        filepath = tokens[i + 1]
        if filepath != '-':
            print(filepath)
        break
    i += 1
PYEOF

    if [ -n "$BODY_FILE_EXTRACT" ] && [ -f "$BODY_FILE_EXTRACT" ]; then
        # ファイルの中身が非日本語なら block
        # ただし log / json / fixture ファイルは除外
        if echo "$BODY_FILE_EXTRACT" | grep -qiE '\.(log|json)$|fixture|test_'; then
            exit 0
        fi
        if ! uv run python3 "$VALIDATOR" --file "$BODY_FILE_EXTRACT" --threshold 0.1 >/dev/null 2>/dev/null; then
            echo "GUARD: 日本語比率不足 [--body-file: ${BODY_FILE_EXTRACT}]" >&2
            uv run python3 "$VALIDATOR" --file "$BODY_FILE_EXTRACT" --threshold 0.1 2>&1 || true
            exit 2
        fi
    fi

    # body が取れた場合に検証
    if [ -n "$BODY" ]; then
        if ! echo "$BODY" | uv run python3 "$VALIDATOR" --threshold 0.1 >/dev/null 2>/dev/null; then
            echo "GUARD: 日本語比率不足 [gh body]" >&2
            echo "$BODY" | uv run python3 "$VALIDATOR" --threshold 0.1 2>&1 || true
            exit 2
        fi
    fi

    exit 0
fi

# ============================================================
# Mode B: Write/Edit/MultiEdit ツールの tmp/ 下書きファイルガード
# ============================================================

if echo "$TOOL_NAME" | grep -qE '^(Write|Edit|MultiEdit)$'; then
    # file_path を取得（Write: file_path, Edit: file_path, MultiEdit: edits[0].file_path 等）
    FILE_PATH="$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.edits[0].file_path // ""' 2>/dev/null || echo "")"

    if [ -z "$FILE_PATH" ]; then
        exit 0
    fi

    # tmp/ 下書き候補パターンに一致するか確認
    # 対象: tmp/**/*.md, /tmp/*issue*body*.md, /tmp/*pr*body*.md, /tmp/*comment*.md, *_draft.md
    IS_DRAFT=false

    case "$FILE_PATH" in
        tmp/*.md|tmp/**/*.md)
            IS_DRAFT=true
            ;;
        /tmp/*.md)
            # /tmp/ 配下の Issue/PR/comment 下書き候補
            if echo "$FILE_PATH" | grep -qiE '(issue.*body|pr.*body|comment|draft)'; then
                IS_DRAFT=true
            fi
            ;;
        *_draft.md)
            IS_DRAFT=true
            ;;
    esac

    if [ "$IS_DRAFT" = "false" ]; then
        exit 0
    fi

    # 誤検知回避: log / json / fixture / test_ ファイルはスキップ
    # AC9: tmp/ 配下のログ、JSON、test fixture、code block 主体ファイルは誤検知しない
    if echo "$FILE_PATH" | grep -qiE '\.(log|json)$|fixture|test_'; then
        exit 0
    fi

    # Write ツールの場合は content から検証
    if [ "$TOOL_NAME" = "Write" ]; then
        CONTENT="$(echo "$INPUT" | jq -r '.tool_input.content // ""' 2>/dev/null || echo "")"

        if [ -z "$CONTENT" ]; then
            exit 0
        fi

        # content の大部分がコードブロックならスキップ（code block 主体ファイル）
        # コードブロック行数が全行数の 50% 超なら code block 主体とみなす
        CODE_LINES=$(echo "$CONTENT" | grep -c '^\s*```' 2>/dev/null || echo 0)
        TOTAL_LINES=$(echo "$CONTENT" | wc -l 2>/dev/null || echo 1)
        if [ "$TOTAL_LINES" -gt 0 ] && [ "$((CODE_LINES * 2))" -gt "$TOTAL_LINES" ]; then
            exit 0
        fi

        if ! echo "$CONTENT" | uv run python3 "$VALIDATOR" --threshold 0.1 >/dev/null 2>/dev/null; then
            echo "GUARD: 日本語比率不足 [Write: ${FILE_PATH}]" >&2
            echo "$CONTENT" | uv run python3 "$VALIDATOR" --threshold 0.1 2>&1 || true
            exit 2
        fi
    fi

    # Edit/MultiEdit の場合はファイルが存在すれば検証（新規ファイルは Write で処理）
    if [ "$TOOL_NAME" = "Edit" ] || [ "$TOOL_NAME" = "MultiEdit" ]; then
        if [ -f "$FILE_PATH" ]; then
            if ! uv run python3 "$VALIDATOR" --file "$FILE_PATH" --threshold 0.1 >/dev/null 2>/dev/null; then
                echo "GUARD: 日本語比率不足 [${TOOL_NAME}: ${FILE_PATH}]" >&2
                uv run python3 "$VALIDATOR" --file "$FILE_PATH" --threshold 0.1 2>&1 || true
                exit 2
            fi
        fi
    fi

    exit 0
fi

# ガード対象外は通す
exit 0

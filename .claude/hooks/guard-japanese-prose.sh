#!/usr/bin/env bash
# guard-japanese-prose.sh
#
# Claude Code PreToolUse hook: 日本語 prose 比率不足の GitHub 送信・下書きファイルをブロックする
#
# Mode A: Bash ツール + gh コマンドで Issue/PR/comment body を送信しようとする場合
# Mode B: Write/Edit/MultiEdit ツールで tmp/ 下書き候補 Markdown を書こうとする場合
#
# Delta mode (AC2): gh issue edit / gh pr edit + --body-file <path> の場合、
#   既存 body を取得して prose 差分のみを検査する。
#   - --body-file - (stdin): fail-closed (AC7)
#   - target 複数: exit 2 + target_ambiguous (AC10)
#   - target 解決不可: exit 2 + target_resolution_failed (AC11)
#
# API input mode (#594 AC4): gh api --input <file> の場合、
#   payload を解析して Issue/PR body mutation か判定し、delta 検査を適用する。
#   - --input - (stdin): fail-closed (AC19)
#   - invalid JSON payload: fail-closed + api_payload_parse_failed (AC20)
#   - body mutation でない場合: pass (AC5)
#   - PATCH repos/{owner}/{repo}/issues/{n} + body key: delta check (AC17)
#   - PATCH repos/{owner}/{repo}/pulls/{n} + body key: delta check (AC18)
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
        # validator の詳細情報を stderr に出力
        local details
        details="$(echo "$body" | uv run python3 "$VALIDATOR" --threshold 0.1 2>&1 || true)"
        echo "GUARD: 日本語比率不足 [${context}]" >&2
        echo "${details}" >&2
        return 2
    fi
}

# delta mode: changed prose blocks のみを検査する
# $1: 新 body テキスト
# $2: 旧 body テキスト
# $3: target（issue #N / pr #N の識別子。stderr 出力用）
validate_delta_prose() {
    local new_body="$1"
    local old_body="$2"
    local target="$3"

    # 一時ファイルに old/new body を書き出して --delta-check サブコマンドに渡す
    local tmp_old tmp_new
    tmp_old="$(mktemp /tmp/guard_delta_old_XXXXXX.md)"
    tmp_new="$(mktemp /tmp/guard_delta_new_XXXXXX.md)"

    printf '%s' "$old_body" > "$tmp_old"
    printf '%s' "$new_body" > "$tmp_new"

    local result
    result="$(uv run python3 "$VALIDATOR" --delta-check --old-file "$tmp_old" --new-file "$tmp_new" --threshold 0.1 2>/dev/null || echo "DELTA_ERROR")"

    rm -f "$tmp_old" "$tmp_new"

    if [ "$result" = "DELTA_PASS" ]; then
        return 0
    fi

    if echo "$result" | grep -q "^DELTA_FAIL:"; then
        local changed_count
        local failed_count
        changed_count="$(echo "$result" | cut -d: -f2)"
        failed_count="$(echo "$result" | cut -d: -f3)"
        echo "GUARD: changed prose block failure [${target}]" >&2
        echo "  target: ${target}" >&2
        echo "  changed_prose_blocks: ${changed_count}" >&2
        echo "  failed_blocks: ${failed_count}" >&2
        echo "  ratio_min: 0.000" >&2
        return 2
    fi

    # DELTA_ERROR や予期しない出力の場合は fail-closed
    echo "GUARD: delta mode internal error (fail-closed) [${target}]" >&2
    echo "  target: ${target}" >&2
    echo "  changed_prose_blocks: unknown" >&2
    echo "  failed_blocks: unknown" >&2
    echo "  ratio_min: 0.000" >&2
    return 2
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

    # 1. --body / -b オプションの値を抽出（--parse-body サブコマンド使用）
    BODY=""
    BODY_EXTRACT="$(uv run python3 "$VALIDATOR" --parse-body "$COMMAND" 2>/dev/null || echo "")"
    if [ -n "$BODY_EXTRACT" ]; then
        BODY="$BODY_EXTRACT"
    fi

    # 2. --body-file / -F でファイルを読む場合（--parse-body-file サブコマンド使用）
    BODY_FILE_EXTRACT="$(uv run python3 "$VALIDATOR" --parse-body-file "$COMMAND" 2>/dev/null || echo "")"

    # delta mode: --body-file stdin (-) は fail-closed (AC7)
    # is_delta_edit には stdin body-file を適用しない
    if [ "$BODY_FILE_EXTRACT" = "STDIN_FAIL_CLOSED" ]; then
        echo "GUARD: --body-file - (stdin) は検証不可のため fail-closed でブロックします" >&2
        echo "  target: unknown" >&2
        echo "  changed_prose_blocks: unknown" >&2
        echo "  failed_blocks: unknown" >&2
        echo "  ratio_min: 0.000" >&2
        exit 2
    fi

    # --body-file が指定されている場合の delta mode (AC2)
    if [ -n "$BODY_FILE_EXTRACT" ] && [ -f "$BODY_FILE_EXTRACT" ]; then
        # Mode A (gh issue/pr create/edit/comment/review) では拡張子による無条件 exit 0 は禁止
        # fixture / tmp draft 除外が必要なら /tmp/ 下の明示的なパスに限定する
        # (注: Mode B の tmp/ 下書き検査では log/json 除外を維持する)

        # gh issue edit / gh pr edit かどうかを判定して delta mode を適用
        IS_DELTA_EDIT=false
        EDIT_TYPE=""  # "issue" or "pr"

        if echo "$COMMAND" | grep -qE 'gh issue edit'; then
            IS_DELTA_EDIT=true
            EDIT_TYPE="issue"
        elif echo "$COMMAND" | grep -qE 'gh pr edit'; then
            IS_DELTA_EDIT=true
            EDIT_TYPE="pr"
        fi

        if [ "$IS_DELTA_EDIT" = "true" ]; then
            # target（issue/PR 番号または URL）を解決する (AC10, AC11)
            # --parse-edit-target サブコマンドで解析
            DELTA_TARGET_INFO="$(uv run python3 "$VALIDATOR" --parse-edit-target "$COMMAND" --parse-edit-type "$EDIT_TYPE" 2>/dev/null || echo "RESOLVE_ERROR")"

            if [ "$DELTA_TARGET_INFO" = "AMBIGUOUS" ]; then
                # AC10: 複数 target -> exit 2 + target_ambiguous
                echo "GUARD: gh ${EDIT_TYPE} edit: 複数 target のため delta 検査対象を一意にできません" >&2
                echo "  target_ambiguous" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                exit 2
            fi

            if [ "$DELTA_TARGET_INFO" = "RESOLVE_ERROR" ] || [ -z "$DELTA_TARGET_INFO" ]; then
                # AC11: target 解決不可 -> exit 2 + target_resolution_failed
                echo "GUARD: gh ${EDIT_TYPE} edit: target を解決できません" >&2
                echo "  target_resolution_failed" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                exit 2
            fi

            # NUMBER:<N> 形式から番号を取得
            TARGET_NUM="${DELTA_TARGET_INFO#NUMBER:}"
            TARGET_LABEL="${EDIT_TYPE} #${TARGET_NUM}"

            # 既存 body を取得 (AC2, AC15)
            # API 呼び出しの成否とコンテンツの有無を分離する
            # 空文字は成功として OLD_BODY="" で delta check に渡す（新本文全体を新規 block として検査）
            OLD_BODY=""
            if [ "$EDIT_TYPE" = "issue" ]; then
                if ! OLD_BODY="$(gh issue view "$TARGET_NUM" --json body --jq .body 2>/dev/null)"; then
                    # API 呼び出し失敗: fail-closed (AC11)
                    echo "GUARD: ${TARGET_LABEL} の既存 body を取得できません (fail-closed)" >&2
                    echo "  target_resolution_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    exit 2
                fi
            else
                if ! OLD_BODY="$(gh pr view "$TARGET_NUM" --json body --jq .body 2>/dev/null)"; then
                    # API 呼び出し失敗: fail-closed (AC11)
                    echo "GUARD: ${TARGET_LABEL} の既存 body を取得できません (fail-closed)" >&2
                    echo "  target_resolution_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    exit 2
                fi
            fi
            # OLD_BODY が空文字でも続行（新本文全体を新規 block として検査）

            # 新 body を読み込む
            NEW_BODY="$(cat "$BODY_FILE_EXTRACT")"

            # delta mode で changed prose blocks のみ検査 (AC2, AC8)
            if validate_delta_prose "$NEW_BODY" "$OLD_BODY" "$TARGET_LABEL"; then
                exit 0
            else
                exit 2
            fi
        fi

        # delta_mode 非対象（create/comment 等）: full-body 検査
        if ! uv run python3 "$VALIDATOR" --file "$BODY_FILE_EXTRACT" --threshold 0.1 >/dev/null 2>/dev/null; then
            echo "GUARD: 日本語比率不足 [--body-file: ${BODY_FILE_EXTRACT}]" >&2
            uv run python3 "$VALIDATOR" --file "$BODY_FILE_EXTRACT" --threshold 0.1 2>&1 || true
            exit 2
        fi
    fi

    # gh api --input <file> の検査 (AC4, AC10, AC17, AC18, AC19, AC20)
    # --body-file が指定されていない場合のみ gh api --input を検査する
    if [ -z "$BODY_FILE_EXTRACT" ] && echo "$COMMAND" | grep -qE 'gh.*api'; then
        API_INPUT_RESULT="$(uv run python3 "$VALIDATOR" --parse-api-input "$COMMAND" 2>/dev/null || echo "API_INPUT_ERROR")"

        if [ "$API_INPUT_RESULT" = "API_INPUT_STDIN" ]; then
            # AC19: --input - (stdin) は fail-closed
            echo "GUARD: gh api --input - (stdin) は検証不可のため fail-closed でブロックします" >&2
            echo "  target: unknown" >&2
            echo "  changed_prose_blocks: unknown" >&2
            echo "  failed_blocks: unknown" >&2
            echo "  ratio_min: 0.000" >&2
            exit 2
        fi

        if echo "$API_INPUT_RESULT" | grep -q "^API_INPUT_FILE:"; then
            API_INPUT_FILE="${API_INPUT_RESULT#API_INPUT_FILE:}"

            if [ ! -f "$API_INPUT_FILE" ]; then
                # ファイルが存在しない: fail-closed (AC20)
                echo "GUARD: gh api --input file not found: ${API_INPUT_FILE} (fail-closed)" >&2
                echo "  api_payload_parse_failed" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                exit 2
            fi

            # endpoint を解析して body mutation かどうかを判定 (AC4, AC5, AC17, AC18)
            API_ENDPOINT="$(uv run python3 "$VALIDATOR" --extract-api-command-endpoint "$COMMAND" 2>/dev/null || echo "ENDPOINT_PARSE_FAILED")"

            if [ "$API_ENDPOINT" = "ENDPOINT_PARSE_FAILED" ]; then
                # endpoint 解析失敗: fail-closed (AC20)
                echo "GUARD: gh api endpoint を解析できません (fail-closed)" >&2
                echo "  api_payload_parse_failed" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                exit 2
            fi

            # payload を分類 (AC17, AC18, AC20)
            MUTATION_CLASS="$(uv run python3 "$VALIDATOR" --classify-api-mutation "$API_INPUT_FILE" --api-endpoint "$API_ENDPOINT" 2>/dev/null || echo "PAYLOAD_PARSE_FAILED")"

            if [ "$MUTATION_CLASS" = "PAYLOAD_PARSE_FAILED" ]; then
                # JSON parse 失敗: fail-closed (AC20)
                echo "GUARD: gh api --input payload の JSON 解析失敗 (fail-closed)" >&2
                echo "  api_payload_parse_failed" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                exit 2
            fi

            if [ "$MUTATION_CLASS" = "NOT_BODY_MUTATION" ]; then
                # AC5: body mutation でない場合は guard 対象外として pass
                exit 0
            fi

            # BODY_MUTATION_ISSUE:<N> or BODY_MUTATION_PR:<N>
            if echo "$MUTATION_CLASS" | grep -q "^BODY_MUTATION_ISSUE:"; then
                API_TARGET_NUM="${MUTATION_CLASS#BODY_MUTATION_ISSUE:}"
                API_TARGET_LABEL="issue #${API_TARGET_NUM}"
                API_TARGET_TYPE="issue"
            elif echo "$MUTATION_CLASS" | grep -q "^BODY_MUTATION_PR:"; then
                API_TARGET_NUM="${MUTATION_CLASS#BODY_MUTATION_PR:}"
                API_TARGET_LABEL="pr #${API_TARGET_NUM}"
                API_TARGET_TYPE="pr"
            else
                # 不明な分類: fail-closed
                echo "GUARD: gh api --input mutation 分類不明 (fail-closed): ${MUTATION_CLASS}" >&2
                echo "  api_payload_parse_failed" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                exit 2
            fi

            # 既存 body を取得して delta 検査 (AC4, AC17, AC18)
            OLD_BODY=""
            if [ "$API_TARGET_TYPE" = "issue" ]; then
                if ! OLD_BODY="$(gh issue view "$API_TARGET_NUM" --json body --jq .body 2>/dev/null)"; then
                    echo "GUARD: ${API_TARGET_LABEL} の既存 body を取得できません (fail-closed)" >&2
                    echo "  target_resolution_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    exit 2
                fi
            else
                if ! OLD_BODY="$(gh pr view "$API_TARGET_NUM" --json body --jq .body 2>/dev/null)"; then
                    echo "GUARD: ${API_TARGET_LABEL} の既存 body を取得できません (fail-closed)" >&2
                    echo "  target_resolution_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    exit 2
                fi
            fi

            # payload の body フィールドを抽出して delta 検査
            NEW_BODY="$(uv run python3 -c "
import json, sys
try:
    with open('${API_INPUT_FILE}', 'r', encoding='utf-8') as f:
        payload = json.load(f)
    print(payload.get('body', ''))
except Exception as e:
    print('', end='')
" 2>/dev/null || echo "")"

            # delta mode で changed prose blocks のみ検査 (AC4, AC17, AC18)
            if validate_delta_prose "$NEW_BODY" "$OLD_BODY" "${API_TARGET_LABEL}"; then
                exit 0
            else
                exit 2
            fi
        fi
        # API_INPUT_NONE: --input なし → 通常の body 検査へ
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

    # Edit の場合は new_string を検査。old_string が見つからない場合は fail-closed (AC12)
    if [ "$TOOL_NAME" = "Edit" ]; then
        NEW_STRING="$(echo "$INPUT" | jq -r '.tool_input.new_string // ""' 2>/dev/null || echo "")"
        OLD_STRING="$(echo "$INPUT" | jq -r '.tool_input.old_string // ""' 2>/dev/null || echo "")"

        # old_string が指定されてファイルが存在するが old_string が見つからない → fail-closed
        if [ -n "$OLD_STRING" ] && [ -f "$FILE_PATH" ]; then
            OLD_CHECK=$(OLD_STRING_ENV="$OLD_STRING" FILE_PATH_ENV="$FILE_PATH" uv run python3 -c "
import os
old = os.environ.get('OLD_STRING_ENV', '')
fp = os.environ.get('FILE_PATH_ENV', '')
try:
    with open(fp, 'r', encoding='utf-8') as f:
        content = f.read()
    print('found' if old in content else 'notfound')
except Exception:
    print('notfound')
" 2>/dev/null || echo "notfound")
            if [ "$OLD_CHECK" = "notfound" ]; then
                echo "GUARD: old_string がファイルに見つかりません (fail-closed) [Edit: ${FILE_PATH}]" >&2
                exit 2
            fi
        fi

        # new_string を検証
        if [ -n "$NEW_STRING" ]; then
            if ! echo "$NEW_STRING" | uv run python3 "$VALIDATOR" --threshold 0.1 >/dev/null 2>/dev/null; then
                echo "GUARD: 日本語比率不足 [Edit new_string: ${FILE_PATH}]" >&2
                echo "$NEW_STRING" | uv run python3 "$VALIDATOR" --threshold 0.1 2>&1 || true
                exit 2
            fi
        fi
    fi

    # MultiEdit の場合は各 edit の new_string を検証 (AC12)
    if [ "$TOOL_NAME" = "MultiEdit" ]; then
        EDITS_COUNT="$(echo "$INPUT" | jq '.tool_input.edits | length' 2>/dev/null || echo 0)"
        for idx in $(seq 0 $((EDITS_COUNT - 1))); do
            EDIT_NEW_STRING="$(echo "$INPUT" | jq -r ".tool_input.edits[${idx}].new_string // \"\"" 2>/dev/null || echo "")"
            if [ -n "$EDIT_NEW_STRING" ]; then
                if ! echo "$EDIT_NEW_STRING" | uv run python3 "$VALIDATOR" --threshold 0.1 >/dev/null 2>/dev/null; then
                    echo "GUARD: 日本語比率不足 [MultiEdit new_string[${idx}]]" >&2
                    echo "$EDIT_NEW_STRING" | uv run python3 "$VALIDATOR" --threshold 0.1 2>&1 || true
                    exit 2
                fi
            fi
        done
    fi

    exit 0
fi

# ガード対象外は通す
exit 0

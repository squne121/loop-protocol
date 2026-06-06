#!/usr/bin/env bash
# guard-japanese-prose.sh
#
# Claude Code PreToolUse hook: 日本語 prose 比率不足の GitHub 送信・下書きファイルをブロックする
#
# GUARD_JAPANESE_PROSE_MODE の動作:
#   未設定 / shadow: block せず exit 0、would-block 判定を JSONL に記録する（AC1, AC4, AC7）
#   enforce:         従来どおり block する（AC3）
#   不正値:          shadow として動作 + invalid_mode を JSONL に記録（AC7）
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
#   0 = allow (日本語比率 OK、またはガード対象外、または shadow mode)
#   2 = block (日本語比率不足 — blocking error として Claude Code に通知 / enforce mode のみ)

set -euo pipefail

# スクリプトのディレクトリから validate_japanese_content.py を探す
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
VALIDATOR="${PROJECT_DIR}/.claude/skills/create-issue/scripts/validate_japanese_content.py"
MATRIX="${PROJECT_DIR}/.claude/skills/create-issue/scripts/mutation_route_matrix.py"
SHADOW_LOG_PY="${SCRIPT_DIR}/shadow_log.py"

# ============================================================
# GUARD_JAPANESE_PROSE_MODE の解決（AC4, AC7）
# 未設定 = shadow（default）
# shadow / enforce は有効値
# 不正値 = shadow として動作（invalid_mode フラグ付き）
# ============================================================
_GUARD_MODE_RAW="${GUARD_JAPANESE_PROSE_MODE:-}"
_GUARD_MODE_EFFECTIVE="shadow"   # default
_GUARD_MODE_INVALID=false

if [ -z "$_GUARD_MODE_RAW" ]; then
    _GUARD_MODE_EFFECTIVE="shadow"
elif [ "$_GUARD_MODE_RAW" = "shadow" ]; then
    _GUARD_MODE_EFFECTIVE="shadow"
elif [ "$_GUARD_MODE_RAW" = "enforce" ]; then
    _GUARD_MODE_EFFECTIVE="enforce"
else
    # 不正値: shadow として動作 + invalid_mode 記録
    _GUARD_MODE_EFFECTIVE="shadow"
    _GUARD_MODE_INVALID=true
fi

# JSONL 出力先（環境変数でオーバーライド可能、デフォルトは PROJECT_DIR 直下）
SHADOW_LOG_FILE="${GUARD_JAPANESE_PROSE_SHADOW_LOG:-${PROJECT_DIR}/.guard_shadow_log.jsonl}"

# ============================================================
# shadow_log ヘルパー関数（AC9: instrumentation 失敗は silent allow しない）
# ============================================================
_guard_shadow_log() {
    # $1: JSON フィールド文字列
    local fields_json="$1"
    if [ -f "$SHADOW_LOG_PY" ]; then
        if ! uv run python3 "$SHADOW_LOG_PY" \
            --log-file "$SHADOW_LOG_FILE" \
            --fields-json "$fields_json" 2>&1; then
            # AC9: instrumentation 失敗を stderr に記録する（silent allow しない）
            echo "GUARD: shadow_log instrumentation_error (logging failed)" >&2
            echo "  instrumentation_error: shadow_log.py write failed" >&2
        fi
    else
        # shadow_log.py が存在しない場合も stderr に記録（AC9）
        echo "GUARD: shadow_log instrumentation_error (shadow_log.py not found)" >&2
        echo "  instrumentation_error: shadow_log.py not found at ${SHADOW_LOG_PY}" >&2
    fi
}

# shadow mode で would-block 判定を記録して exit 0 で通過する
# $1: route_id  $2: body_source  $3: public_mutation  $4: reason_code
# $5: failed_block_count（数値、不明なら -1）  $6: duration_ms  $7: body_sha256  $8: body_bytes
_shadow_allow() {
    local route_id="$1"
    local body_source="$2"
    local public_mutation="$3"
    local reason_code="$4"
    local failed_block_count="${5:--1}"
    local duration_ms="${6:-0}"
    local body_sha256="${7:-}"
    local body_bytes="${8:-0}"

    local mode_configured="$_GUARD_MODE_RAW"
    local mode_effective="$_GUARD_MODE_EFFECTIVE"

    # invalid_mode の場合は reason_code に付記
    if [ "$_GUARD_MODE_INVALID" = "true" ]; then
        reason_code="${reason_code}+invalid_mode"
    fi

    # AC8: raw body / full command / token / Authorization header は記録しない
    local fields_json
    fields_json="$(cat <<EOF
{
  "hook_event": "PreToolUse",
  "mode_configured": "${mode_configured}",
  "mode_effective": "${mode_effective}",
  "tool_name": "${TOOL_NAME}",
  "route_id": "${route_id}",
  "body_source": "${body_source}",
  "public_mutation": ${public_mutation},
  "decision_would_be": "deny",
  "reason_code": "${reason_code}",
  "failed_block_count": ${failed_block_count},
  "duration_ms": ${duration_ms},
  "body_sha256": "${body_sha256}",
  "body_bytes": ${body_bytes}
}
EOF
)"
    _guard_shadow_log "$fields_json"
    exit 0
}

# shadow mode での allow pass（would-block でない場合の計測記録）
# $1: route_id  $2: body_source  $3: public_mutation
_shadow_pass() {
    local route_id="$1"
    local body_source="$2"
    local public_mutation="$3"

    local mode_configured="$_GUARD_MODE_RAW"
    local mode_effective="$_GUARD_MODE_EFFECTIVE"

    if [ "$_GUARD_MODE_INVALID" = "true" ]; then
        local extra_reason="invalid_mode"
    else
        local extra_reason=""
    fi

    local fields_json
    fields_json="$(cat <<EOF
{
  "hook_event": "PreToolUse",
  "mode_configured": "${mode_configured}",
  "mode_effective": "${mode_effective}",
  "tool_name": "${TOOL_NAME}",
  "route_id": "${route_id}",
  "body_source": "${body_source}",
  "public_mutation": ${public_mutation},
  "decision_would_be": "allow",
  "reason_code": "${extra_reason}",
  "failed_block_count": 0,
  "duration_ms": 0,
  "body_sha256": "",
  "body_bytes": 0
}
EOF
)"
    _guard_shadow_log "$fields_json"
    exit 0
}

# stdin から JSON を読む
INPUT="$(cat)"

# tool_name を取得
TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")"

# validator が存在しない場合は通す（bootstrap時の安全弁）
if [ ! -f "$VALIDATOR" ]; then
    exit 0
fi

# mutation_route_matrix.py が存在しない場合は通す（bootstrap時の安全弁）
if [ ! -f "$MATRIX" ]; then
    exit 0
fi

# ============================================================
# ヘルパー関数
# ============================================================

# shadow / enforce モード判定で block または allow を返す
# enforce mode: exit 2（従来どおり block）
# shadow mode:  _shadow_allow 経由で JSONL 記録 + exit 0
# $1: route_id  $2: body_source  $3: public_mutation（true|false）
# $4: reason_code  $5: failed_block_count  $6: body_text（sha256/bytes 計算用）
_block_or_shadow() {
    local route_id="$1"
    local body_source="$2"
    local public_mutation="$3"
    local reason_code="$4"
    local failed_block_count="${5:-1}"
    local body_text="${6:-}"

    local body_sha256=""
    local body_bytes=0
    if [ -n "$body_text" ]; then
        body_sha256="$(printf '%s' "$body_text" | sha256sum | awk '{print $1}')"
        body_bytes="${#body_text}"
    fi

    if [ "$_GUARD_MODE_EFFECTIVE" = "enforce" ]; then
        # enforce mode: 従来どおり exit 2 でブロック（stderr は呼び出し元が出力済み）
        exit 2
    else
        # shadow mode: JSONL 記録 + exit 0
        _shadow_allow "$route_id" "$body_source" "$public_mutation" \
            "$reason_code" "$failed_block_count" "0" "$body_sha256" "$body_bytes"
        # _shadow_allow 内で exit 0 するので以下には到達しない
    fi
}

# 日本語比率を検証する
# $1: チェック対象の本文テキスト
# $2: 対象の説明（stderr 出力用）
# 戻り値:
#   0 = pass
#   2 = fail (borderline または clear fail)
# stderr に reason code を出力する:
#   borderline case: "borderline_japanese_prose_repair_required" reason code を含める
validate_body() {
    local body="$1"
    local context="$2"

    if [ -z "$body" ]; then
        return 0
    fi

    # Step 1: per-block validator を primary gate として使用する
    # per-block gate: 全 prose block が threshold を満たす場合のみ pass
    if printf '%s' "$body" | uv run python3 "$VALIDATOR" --threshold 0.1 >/dev/null 2>/dev/null; then
        return 0  # per-block gate 通過 → allow
    fi

    # Step 2: per-block gate が fail した場合のみ borderline 分類する
    local borderline_result
    borderline_result="$(printf '%s' "$body" | uv run python3 "$VALIDATOR" \
        --borderline-check \
        --threshold 0.1 \
        --lower-threshold 0.05 2>/dev/null || echo "BORDERLINE_ERROR")"

    case "$borderline_result" in
        BORDERLINE)
            echo "GUARD: 日本語比率不足 (borderline - 自己修正してください) [${context}]" >&2
            echo "  borderline_japanese_prose_repair_required: 日本語 prose への修正・再試行が必要です" >&2
            return 2
            ;;
        *)
            # PASS 以外（CLEAR_FAIL, BORDERLINE_ERROR など）は既存の fail-closed ロジックに従う
            local details
            details="$(printf '%s' "$body" | uv run python3 "$VALIDATOR" --threshold 0.1 2>&1 || true)"
            echo "GUARD: 日本語比率不足 [${context}]" >&2
            echo "${details}" >&2
            return 2
            ;;
    esac
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

        # delta mode では fail-closed: changed prose block の失敗は常に exit 2
        # aggregate ratio による borderline バイパスを行わない（新規追加の英語 prose が通過しないよう）
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
        _block_or_shadow "body_file_stdin" "body_file_stdin" "true" "stdin_fail_closed" 1 ""
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
                _block_or_shadow "delta_edit_${EDIT_TYPE}" "body_file" "true" "target_ambiguous" 1 ""
            fi

            if [ "$DELTA_TARGET_INFO" = "RESOLVE_ERROR" ] || [ -z "$DELTA_TARGET_INFO" ]; then
                # AC11: target 解決不可 -> exit 2 + target_resolution_failed
                echo "GUARD: gh ${EDIT_TYPE} edit: target を解決できません" >&2
                echo "  target_resolution_failed" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                _block_or_shadow "delta_edit_${EDIT_TYPE}" "body_file" "true" "target_resolution_failed" 1 ""
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
                    _block_or_shadow "delta_edit_issue" "body_file" "true" "target_resolution_failed" 1 ""
                fi
            else
                if ! OLD_BODY="$(gh pr view "$TARGET_NUM" --json body --jq .body 2>/dev/null)"; then
                    # API 呼び出し失敗: fail-closed (AC11)
                    echo "GUARD: ${TARGET_LABEL} の既存 body を取得できません (fail-closed)" >&2
                    echo "  target_resolution_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    _block_or_shadow "delta_edit_pr" "body_file" "true" "target_resolution_failed" 1 ""
                fi
            fi
            # OLD_BODY が空文字でも続行（新本文全体を新規 block として検査）

            # 新 body を読み込む
            NEW_BODY="$(cat "$BODY_FILE_EXTRACT")"

            # delta mode で changed prose blocks のみ検査 (AC2, AC8)
            if validate_delta_prose "$NEW_BODY" "$OLD_BODY" "$TARGET_LABEL"; then
                exit 0
            else
                _block_or_shadow "delta_edit_${EDIT_TYPE}" "body_file" "true" \
                    "delta_prose_failed" 1 "$NEW_BODY"
            fi
        fi

        # delta_mode 非対象（create/comment 等）: full-body 検査
        FILE_BODY="$(cat "$BODY_FILE_EXTRACT" 2>/dev/null || echo "")"
        if validate_body "$FILE_BODY" "--body-file: ${BODY_FILE_EXTRACT}"; then
            : # pass
        else
            _block_or_shadow "gh_body_file" "body_file" "true" \
                "japanese_prose_insufficient" 1 "$FILE_BODY"
        fi
    fi

    # (#655 AC3/AC8/AC9/AC10/AC11) gh api -f/-F body= フィールドの検査
    # mutation_route_matrix.py の resolve_body_source() に委譲して body source を解決する。
    # --input が指定されている場合は --input 側が body source になり、-f/-F は query param 扱い（AC8）。
    # -F body=@<file>: ファイル内容を読んで検査（AC9）
    # -f body=@file: literal "@file"（@ dereference なし）として検査（AC9）
    # -F body=@-: stdin fail-closed (AC10)
    # gh api コマンドでは -F は field フラグ。BODY_FILE_EXTRACT は gh api には適用しない。
    IS_GH_API_CMD=false
    if echo "$COMMAND" | grep -qE 'gh[[:space:]].*api' && ! echo "$COMMAND" | grep -qE 'gh.*api.*graphql'; then
        IS_GH_API_CMD=true
    fi
    if [ "$IS_GH_API_CMD" = "true" ]; then
        FIELD_BODY_RESULT="$(uv run python3 "$MATRIX" resolve-body-source "$COMMAND" 2>/dev/null || echo '{"deny_reason":"deny_invalid_json"}')"

        FIELD_DENY_REASON="$(echo "$FIELD_BODY_RESULT" | uv run python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('deny_reason') or '')" 2>/dev/null || echo "")"
        FIELD_SOURCE_KIND="$(echo "$FIELD_BODY_RESULT" | uv run python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('source_kind') or '')" 2>/dev/null || echo "")"
        FIELD_BODY_TEXT="$(echo "$FIELD_BODY_RESULT" | uv run python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('body_text') or '')" 2>/dev/null || echo "")"

        # body source が解決できた場合（-f/-F body= 系）
        # source_kind が field body 系（-f/-F body=）のときのみ処理する
        # --input 系は既存の gh api --input 検査ブロックで処理する
        if echo "$FIELD_SOURCE_KIND" | grep -qE '^api_(raw_field_body_literal|field_body_literal|field_body_file|field_body_stdin)$'; then
            # fail-closed 系 reason codes
            if [ -n "$FIELD_DENY_REASON" ]; then
                case "$FIELD_DENY_REASON" in
                    deny_stdin_body_uninspectable)
                        echo "GUARD: stdin body は検証不可のため fail-closed でブロックします (${FIELD_DENY_REASON})" >&2
                        echo "  reason: ${FIELD_DENY_REASON}" >&2
                        echo "  source_kind: ${FIELD_SOURCE_KIND}" >&2
                        echo "  changed_prose_blocks: unknown" >&2
                        echo "  failed_blocks: unknown" >&2
                        echo "  ratio_min: 0.000" >&2
                        _block_or_shadow "api_field_body" "${FIELD_SOURCE_KIND}" "true" "$FIELD_DENY_REASON" 1 ""
                        ;;
                    deny_unreadable_body_file)
                        echo "GUARD: body ファイルを読み取れません (${FIELD_DENY_REASON}, fail-closed)" >&2
                        echo "  reason: ${FIELD_DENY_REASON}" >&2
                        echo "  source_kind: ${FIELD_SOURCE_KIND}" >&2
                        echo "  changed_prose_blocks: unknown" >&2
                        echo "  failed_blocks: unknown" >&2
                        echo "  ratio_min: 0.000" >&2
                        _block_or_shadow "api_field_body" "${FIELD_SOURCE_KIND}" "true" "$FIELD_DENY_REASON" 1 ""
                        ;;
                    deny_null_body_public_mutation)
                        echo "GUARD: body が null です (${FIELD_DENY_REASON}, fail-closed)" >&2
                        echo "  reason: ${FIELD_DENY_REASON}" >&2
                        echo "  source_kind: ${FIELD_SOURCE_KIND}" >&2
                        echo "  changed_prose_blocks: unknown" >&2
                        echo "  failed_blocks: unknown" >&2
                        echo "  ratio_min: 0.000" >&2
                        _block_or_shadow "api_field_body" "${FIELD_SOURCE_KIND}" "true" "$FIELD_DENY_REASON" 1 ""
                        ;;
                    deny_empty_body_public_mutation)
                        echo "GUARD: body が空です (${FIELD_DENY_REASON}, fail-closed)" >&2
                        echo "  reason: ${FIELD_DENY_REASON}" >&2
                        echo "  source_kind: ${FIELD_SOURCE_KIND}" >&2
                        echo "  changed_prose_blocks: unknown" >&2
                        echo "  failed_blocks: unknown" >&2
                        echo "  ratio_min: 0.000" >&2
                        _block_or_shadow "api_field_body" "${FIELD_SOURCE_KIND}" "true" "$FIELD_DENY_REASON" 1 ""
                        ;;
                    deny_missing_body_for_public_body_route)
                        echo "GUARD: body キーが欠落しています (${FIELD_DENY_REASON}, fail-closed)" >&2
                        echo "  reason: ${FIELD_DENY_REASON}" >&2
                        echo "  source_kind: ${FIELD_SOURCE_KIND}" >&2
                        echo "  changed_prose_blocks: unknown" >&2
                        echo "  failed_blocks: unknown" >&2
                        echo "  ratio_min: 0.000" >&2
                        _block_or_shadow "api_field_body" "${FIELD_SOURCE_KIND}" "true" "$FIELD_DENY_REASON" 1 ""
                        ;;
                    deny_invalid_json)
                        echo "GUARD: JSON 解析失敗 (${FIELD_DENY_REASON}, fail-closed)" >&2
                        echo "  reason: ${FIELD_DENY_REASON}" >&2
                        echo "  source_kind: ${FIELD_SOURCE_KIND}" >&2
                        echo "  changed_prose_blocks: unknown" >&2
                        echo "  failed_blocks: unknown" >&2
                        echo "  ratio_min: 0.000" >&2
                        _block_or_shadow "api_field_body" "${FIELD_SOURCE_KIND}" "true" "$FIELD_DENY_REASON" 1 ""
                        ;;
                    *)
                        # unknown deny reason: fail-closed
                        echo "GUARD: 不明な deny reason (${FIELD_DENY_REASON}, fail-closed)" >&2
                        echo "  reason: ${FIELD_DENY_REASON}" >&2
                        echo "  changed_prose_blocks: unknown" >&2
                        echo "  failed_blocks: unknown" >&2
                        echo "  ratio_min: 0.000" >&2
                        _block_or_shadow "api_field_body" "${FIELD_SOURCE_KIND}" "true" "$FIELD_DENY_REASON" 1 ""
                        ;;
                esac
            fi

            # body_text が取れた場合のみ検証
            # source_kind が api_raw_field_body_literal / api_field_body_literal / api_field_body_file の場合に body_text がある
            if [ -n "$FIELD_BODY_TEXT" ]; then
                # REST endpoint を確認して public body route かどうかを判定
                FIELD_ENDPOINT="$(uv run python3 "$VALIDATOR" --extract-api-command-endpoint "$COMMAND" 2>/dev/null || echo "ENDPOINT_PARSE_FAILED")"
                FIELD_METHOD="$(uv run python3 "$VALIDATOR" --extract-api-command-method "$COMMAND" 2>/dev/null || echo "GET")"

                # GET / DELETE は non-mutation として pass
                if [ "$FIELD_METHOD" = "GET" ] || [ "$FIELD_METHOD" = "DELETE" ]; then
                    exit 0
                fi

                # route matrix で endpoint を分類（公開 body route かどうか）
                ROUTE_CLASS="$(uv run python3 "$MATRIX" classify-rest "$FIELD_ENDPOINT" --method "$FIELD_METHOD" 2>/dev/null || echo "no_match")"

                if [ "$ROUTE_CLASS" = "no_match" ]; then
                    # 認識できない endpoint: body がある → full body 検査
                    if validate_body "$FIELD_BODY_TEXT" "field-body: ${FIELD_SOURCE_KIND}"; then
                        exit 0
                    else
                        _block_or_shadow "api_field_body" "${FIELD_SOURCE_KIND}" "true" \
                            "japanese_prose_insufficient" 1 "$FIELD_BODY_TEXT"
                    fi
                fi

                # 公開 body route: full body 検査
                if validate_body "$FIELD_BODY_TEXT" "field-body: ${ROUTE_CLASS}"; then
                    exit 0
                else
                    _block_or_shadow "api_field_body" "${FIELD_SOURCE_KIND}" "true" \
                        "japanese_prose_insufficient" 1 "$FIELD_BODY_TEXT"
                fi
            fi
        fi
        # source_kind が empty の場合（-f/-F body= なし）: 通常の body / --input 検査へ
    fi

    # gh api graphql の検査 (#655 AC4/AC14: Phase 1 conservative deny)
    # mutation_route_matrix.py の graphql_mutation_phase1 route:
    #   validation=conservative_deny, action=deny, reason=deny_graphql_mutation_unsupported
    # graphql コマンドは -F/-f query=... のインライン形式も使用するため、
    # --parse-body-file が -F query=... を誤って body file として解析する場合があっても
    # graphql 判定を優先する（BODY_FILE_EXTRACT 条件を graphql では除外）。
    if echo "$COMMAND" | grep -qE 'gh.*api.*graphql'; then
        GRAPHQL_RESULT="$(uv run python3 "$MATRIX" classify-graphql "$COMMAND" 2>/dev/null || echo "deny_invalid_json")"

        if [ "$GRAPHQL_RESULT" = "deny_graphql_mutation_unsupported" ]; then
            echo "GUARD: gh api graphql mutation は Phase 1 conservative deny します (deny_graphql_mutation_unsupported)" >&2
            echo "  target: graphql" >&2
            echo "  reason: deny_graphql_mutation_unsupported" >&2
            echo "  api_graphql_mutation_denied api_graphql_body_mutation_blocked (legacy compat)" >&2
            echo "  changed_prose_blocks: unknown" >&2
            echo "  failed_blocks: unknown" >&2
            echo "  ratio_min: 0.000" >&2
            _block_or_shadow "graphql_mutation" "graphql" "true" "deny_graphql_mutation_unsupported" 1 ""
        fi

        if [ "$GRAPHQL_RESULT" = "deny_stdin_body_uninspectable" ]; then
            echo "GUARD: gh api graphql --input - (stdin) は検証不可のため fail-closed でブロックします (deny_stdin_body_uninspectable)" >&2
            echo "  target: graphql" >&2
            echo "  reason: deny_stdin_body_uninspectable" >&2
            echo "  changed_prose_blocks: unknown" >&2
            echo "  failed_blocks: unknown" >&2
            echo "  ratio_min: 0.000" >&2
            _block_or_shadow "graphql_mutation" "graphql_stdin" "true" "deny_stdin_body_uninspectable" 1 ""
        fi

        if [ "$GRAPHQL_RESULT" = "deny_invalid_json" ]; then
            echo "GUARD: gh api graphql payload 解析失敗 (deny_invalid_json, fail-closed)" >&2
            echo "  target: graphql" >&2
            echo "  reason: deny_invalid_json" >&2
            echo "  changed_prose_blocks: unknown" >&2
            echo "  failed_blocks: unknown" >&2
            echo "  ratio_min: 0.000" >&2
            _block_or_shadow "graphql_mutation" "graphql" "true" "deny_invalid_json" 1 ""
        fi

        # graphql_not_mutation または graphql_no_input: 通常の body 検査へ
        exit 0
    fi

    # gh api --input の検査 (AC4, AC10, AC17, AC18, AC19, AC20)
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
            _block_or_shadow "api_input" "api_input_stdin" "true" "stdin_fail_closed" 1 ""
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
                _block_or_shadow "api_input" "api_input_file" "true" "api_payload_parse_failed" 1 ""
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
                _block_or_shadow "api_input" "api_input_file" "true" "api_payload_parse_failed" 1 ""
            fi

            # HTTP method を確認して GET / DELETE は non-mutation として pass
            API_METHOD="$(uv run python3 "$VALIDATOR" --extract-api-command-method "$COMMAND" 2>/dev/null || echo "METHOD_UNKNOWN")"

            if [ "$API_METHOD" = "GET" ] || [ "$API_METHOD" = "DELETE" ]; then
                exit 0
            fi

            # payload を分類 (comment route は PATCH 限定)
            MUTATION_CLASS="$(uv run python3 "$VALIDATOR" --classify-api-mutation "$API_INPUT_FILE" --api-endpoint "$API_ENDPOINT" --api-method "$API_METHOD" 2>/dev/null || echo "PAYLOAD_PARSE_FAILED")"

            if [ "$MUTATION_CLASS" = "PAYLOAD_PARSE_FAILED" ]; then
                # JSON parse 失敗: fail-closed (AC20)
                echo "GUARD: gh api --input payload の JSON 解析失敗 (fail-closed)" >&2
                echo "  api_payload_parse_failed" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                _block_or_shadow "api_input" "api_input_file" "true" "api_payload_parse_failed" 1 ""
            fi

            if [ "$MUTATION_CLASS" = "NOT_BODY_MUTATION" ]; then
                # PATCH 対象外 route / method は guard 対象外として pass
                exit 0
            fi

            if [ "$MUTATION_CLASS" = "INVALID_BODY_TYPE" ]; then
                echo "GUARD: gh api --input body 型が不正です (fail-closed)" >&2
                echo "  api_input_invalid_body_type" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                _block_or_shadow "api_input" "api_input_file" "true" "api_input_invalid_body_type" 1 ""
            fi

            # BODY_MUTATION_ISSUE:<N>, BODY_MUTATION_PR:<N>,
            # BODY_MUTATION_ISSUE_COMMENT:<owner>:<repo>:<comment_id>,
            # BODY_MUTATION_PR_REVIEW_COMMENT:<owner>:<repo>:<comment_id>
            API_TARGET_NUM=""
            API_TARGET_OWNER=""
            API_TARGET_REPO=""
            API_TARGET_COMMENT_ID=""
            API_TARGET_FETCH_PATH=""
            if echo "$MUTATION_CLASS" | grep -q "^BODY_MUTATION_ISSUE:"; then
                API_TARGET_NUM="${MUTATION_CLASS#BODY_MUTATION_ISSUE:}"
                API_TARGET_LABEL="issue #${API_TARGET_NUM}"
                API_TARGET_TYPE="issue"
            elif echo "$MUTATION_CLASS" | grep -q "^BODY_MUTATION_PR:"; then
                API_TARGET_NUM="${MUTATION_CLASS#BODY_MUTATION_PR:}"
                API_TARGET_LABEL="pr #${API_TARGET_NUM}"
                API_TARGET_TYPE="pr"
            elif echo "$MUTATION_CLASS" | grep -q "^BODY_MUTATION_ISSUE_COMMENT:"; then
                API_TARGET_META="${MUTATION_CLASS#BODY_MUTATION_ISSUE_COMMENT:}"
                IFS=':' read -r API_TARGET_OWNER API_TARGET_REPO API_TARGET_COMMENT_ID <<EOF
$API_TARGET_META
EOF
                if [ -z "$API_TARGET_OWNER" ] || [ -z "$API_TARGET_REPO" ] || [ -z "$API_TARGET_COMMENT_ID" ]; then
                    echo "GUARD: gh api --input issue comment 分類の payload が不正です (fail-closed): ${MUTATION_CLASS}" >&2
                    echo "  api_payload_parse_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    _block_or_shadow "api_input_comment" "api_input_file" "true" "api_payload_parse_failed" 1 ""
                fi
                API_TARGET_LABEL="issue comment #${API_TARGET_COMMENT_ID}"
                API_TARGET_TYPE="issue_comment"
                API_TARGET_FETCH_PATH="repos/${API_TARGET_OWNER}/${API_TARGET_REPO}/issues/comments/${API_TARGET_COMMENT_ID}"
            elif echo "$MUTATION_CLASS" | grep -q "^BODY_MUTATION_PR_REVIEW_COMMENT:"; then
                API_TARGET_META="${MUTATION_CLASS#BODY_MUTATION_PR_REVIEW_COMMENT:}"
                IFS=':' read -r API_TARGET_OWNER API_TARGET_REPO API_TARGET_COMMENT_ID <<EOF
$API_TARGET_META
EOF
                if [ -z "$API_TARGET_OWNER" ] || [ -z "$API_TARGET_REPO" ] || [ -z "$API_TARGET_COMMENT_ID" ]; then
                    echo "GUARD: gh api --input PR review comment 分類の payload が不正です (fail-closed): ${MUTATION_CLASS}" >&2
                    echo "  api_payload_parse_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    _block_or_shadow "api_input_pr_review_comment" "api_input_file" "true" "api_payload_parse_failed" 1 ""
                fi
                API_TARGET_LABEL="pr review comment #${API_TARGET_COMMENT_ID}"
                API_TARGET_TYPE="pr_review_comment"
                API_TARGET_FETCH_PATH="repos/${API_TARGET_OWNER}/${API_TARGET_REPO}/pulls/comments/${API_TARGET_COMMENT_ID}"
            else
                # 不明な分類: fail-closed
                echo "GUARD: gh api --input mutation 分類不明 (fail-closed): ${MUTATION_CLASS}" >&2
                echo "  api_payload_parse_failed" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                _block_or_shadow "api_input_unknown" "api_input_file" "true" "api_payload_parse_failed" 1 ""
            fi

            # 既存 body を取得して delta 検査 (AC3, AC4, AC17, AC18)
            OLD_BODY=""
            if [ "$API_TARGET_TYPE" = "issue" ]; then
                if ! OLD_BODY="$(gh issue view "$API_TARGET_NUM" --json body --jq .body 2>/dev/null)"; then
                    echo "GUARD: ${API_TARGET_LABEL} の既存 body を取得できません (fail-closed)" >&2
                    echo "  target_resolution_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    _block_or_shadow "api_input_issue" "api_input_file" "true" "target_resolution_failed" 1 ""
                fi
            elif [ "$API_TARGET_TYPE" = "pr" ]; then
                if ! OLD_BODY="$(gh pr view "$API_TARGET_NUM" --json body --jq .body 2>/dev/null)"; then
                    echo "GUARD: ${API_TARGET_LABEL} の既存 body を取得できません (fail-closed)" >&2
                    echo "  target_resolution_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    _block_or_shadow "api_input_pr" "api_input_file" "true" "target_resolution_failed" 1 ""
                fi
            else
                if ! OLD_BODY="$(gh api "$API_TARGET_FETCH_PATH" --jq .body 2>/dev/null)"; then
                    echo "GUARD: ${API_TARGET_LABEL} の既存 body を取得できません (fail-closed)" >&2
                    echo "  comment_old_body_fetch_failed" >&2
                    echo "  changed_prose_blocks: unknown" >&2
                    echo "  failed_blocks: unknown" >&2
                    echo "  ratio_min: 0.000" >&2
                    _block_or_shadow "api_input_comment" "api_input_file" "true" "comment_old_body_fetch_failed" 1 ""
                fi
            fi

            # payload の body フィールドを抽出して delta 検査 (B2: 環境変数経由 fail-closed)
            NEW_BODY="$(GUARD_API_INPUT_FILE="$API_INPUT_FILE" uv run python3 - <<'PY'
import json, os, sys
fp = os.environ.get("GUARD_API_INPUT_FILE", "")
if not fp:
    raise SystemExit(20)
try:
    with open(fp, encoding="utf-8") as f:
        payload = json.load(f)
except Exception:
    raise SystemExit(20)
body = payload.get("body")
if not isinstance(body, str):
    raise SystemExit(21)
print(body, end="")
PY
)" || {
                echo "GUARD: api_input_body_extract_failed (fail-closed)" >&2
                echo "  api_payload_parse_failed" >&2
                echo "  changed_prose_blocks: unknown" >&2
                echo "  failed_blocks: unknown" >&2
                echo "  ratio_min: 0.000" >&2
                _block_or_shadow "api_input_${API_TARGET_TYPE}" "api_input_file" "true" "api_input_body_extract_failed" 1 ""
            }

            # delta mode で changed prose blocks のみ検査 (AC4, AC17, AC18)
            if validate_delta_prose "$NEW_BODY" "$OLD_BODY" "${API_TARGET_LABEL}"; then
                exit 0
            else
                _block_or_shadow "api_input_${API_TARGET_TYPE}" "api_input_file" "true" \
                    "delta_prose_failed" 1 "$NEW_BODY"
            fi
        fi
        # API_INPUT_NONE: --input なし → 通常の body 検査へ
    fi

    # body が取れた場合に検証
    if [ -n "$BODY" ]; then
        if validate_body "$BODY" "gh body"; then
            : # pass
        else
            _block_or_shadow "gh_inline_body" "gh_body_inline" "true" \
                "japanese_prose_insufficient" 1 "$BODY"
        fi
    fi

    exit 0
fi

# ============================================================
# Mode B: Write/Edit/MultiEdit ツールの tmp/ 下書きファイルガード
# (#655: tmp 下書きは public_side_effect=false → block せずに pass する)
# ============================================================

if echo "$TOOL_NAME" | grep -qE '^(Write|Edit|MultiEdit)$'; then
    # #655 AC2 / AC15: Write/Edit/MultiEdit は全て tmp_draft_write_edit route として扱う。
    # mutation_route_matrix.py の tmp_draft_write_edit route: public_side_effect=false, action=pass。
    # ファイルシステムへの書き込みは GitHub への公開送信ではないため guard 対象外。
    # is_tmp_draft_path() は path-based のヒューリスティックだが、Write/Edit/MultiEdit 全体が
    # public_side_effect=false であるためパス判定不要（AC15 SSOT 整合）。
    exit 0
fi


# ガード対象外は通す
exit 0

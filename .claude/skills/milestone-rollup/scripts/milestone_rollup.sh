#!/usr/bin/env bash
# milestone_rollup.sh
# GitHub Milestone 進捗を rollup し、固定フォーマットで出力する。
# Usage: milestone_rollup.sh [milestone-number] [--post <issue-number>]
#
# Options:
#   milestone-number  Milestone の number（デフォルト: 1）
#   --post <issue>    rollup 結果を指定 Issue にコメント投稿する
#   --help, -h        使い方を表示する
#
# SSOT: docs/dev/milestone-ops.md
# AI は Milestone close を実行しない。

set -euo pipefail

usage() {
  cat >&2 <<USAGE_EOF
Usage: $(basename "$0") [milestone-number] [--post <issue-number>]

Options:
  milestone-number  Milestone の number（デフォルト: 1, 数値のみ）
  --post <issue>    rollup 結果を指定 Issue にコメント投稿する
  --help, -h        この使い方を表示する

Examples:
  $(basename "$0")           # Milestone 1 を rollup（read-only）
  $(basename "$0") 2         # Milestone 2 を rollup（read-only）
  $(basename "$0") --post 147  # Milestone 1 を rollup して Issue #147 にコメント投稿
  $(basename "$0") 2 --post 147  # Milestone 2 を rollup して Issue #147 にコメント投稿
USAGE_EOF
}

# --------------------------------------------------------------------------
# 引数パース（while ループで先に全引数を処理）
# --------------------------------------------------------------------------
MILESTONE_NUMBER=1
POST_TO_ISSUE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --post)
      [[ $# -ge 2 ]] || { echo "ERROR: --post requires issue number" >&2; exit 2; }
      POST_TO_ISSUE="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    ''|*[!0-9]*)
      echo "ERROR: milestone-number must be numeric: $1" >&2
      exit 2
      ;;
    *)
      MILESTONE_NUMBER="$1"
      shift
      ;;
  esac
done

REPO="squne121/loop-protocol"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# --------------------------------------------------------------------------
# Step 1: Milestone 主要フィールドの取得（AC2）
# --------------------------------------------------------------------------
echo "[Step 1] Milestone 取得中: MILESTONE_NUMBER=${MILESTONE_NUMBER}" >&2

MILESTONE_JSON=$(gh api "repos/${REPO}/milestones/${MILESTONE_NUMBER}" 2>/dev/null) || {
  echo "ERROR: Milestone ${MILESTONE_NUMBER} の取得に失敗しました" >&2
  exit 1
}

M_TITLE=$(echo "$MILESTONE_JSON" | jq -r '.title')
M_STATE=$(echo "$MILESTONE_JSON" | jq -r '.state')
M_OPEN=$(echo "$MILESTONE_JSON" | jq -r '.open_issues')
M_CLOSED=$(echo "$MILESTONE_JSON" | jq -r '.closed_issues')
M_DUE=$(echo "$MILESTONE_JSON" | jq -r '.due_on // "null"')
M_URL=$(echo "$MILESTONE_JSON" | jq -r '.html_url')

# --------------------------------------------------------------------------
# Step 2: 全 item 取得と PR 混入チェック（AC3）
# --paginate --slurp で全ページを外側配列に包んで安定した JSON 配列として扱う
# --------------------------------------------------------------------------
echo "[Step 2] 全 item 取得中..." >&2

ALL_ITEMS_JSON=$(gh api --paginate \
  "repos/${REPO}/issues?milestone=${MILESTONE_NUMBER}&state=all&per_page=100" \
  --slurp 2>/dev/null) || {
  echo "ERROR: milestone=${MILESTONE_NUMBER} の issue 一覧取得に失敗しました" >&2
  exit 1
}

# jq '[...] | length' で count
PR_MIXED_COUNT=$(jq '[.[][] | select(.pull_request != null)] | length' <<< "$ALL_ITEMS_JSON")
ISSUES_ONLY_JSON=$(jq '[.[][] | select(.pull_request == null)]' <<< "$ALL_ITEMS_JSON")

OPEN_ISSUES=$(jq -c '.[] | select(.state == "open")' <<< "$ISSUES_ONLY_JSON" 2>/dev/null || true)
CLOSED_ISSUES=$(jq -c '.[] | select(.state == "closed")' <<< "$ISSUES_ONLY_JSON" 2>/dev/null || true)

# --------------------------------------------------------------------------
# Step 3: Assignment Drift の検出（AC4）
# #146 の M-A3 Milestone Assignment Readback コメントから expected_set を動的取得
# フォールバック廃止（H1）: 取得失敗時は assignment_readback_complete=false として close 不可
# --------------------------------------------------------------------------
echo "[Step 3] #146 M-A3 Readback から expected_set を取得中..." >&2

ASSIGNMENT_READBACK_COMPLETE=false
EXPECTED_SET=()

EXPECTED_SET_JSON=$(
  gh issue view 146 --repo "$REPO" --comments --json comments \
    --jq '
      .comments
      | map(select(.body | startswith("## M-A3 Milestone Assignment Readback")))
      | last.body
    ' 2>/dev/null |
  grep -E '^- expected:' |
  grep -oE '#[0-9]+' |
  tr -d '#' |
  sort -nu |
  jq -R . | jq -s 'map(tonumber)' 2>/dev/null
) || true

if [[ -z "$EXPECTED_SET_JSON" || "$EXPECTED_SET_JSON" == "[]" ]]; then
  echo "[Step 3] WARN: #146 readback 取得失敗。assignment_readback_complete=false として close 不可扱い" >&2
  ASSIGNMENT_READBACK_COMPLETE=false
  EXPECTED_SET=()
else
  # JSON 配列からbash配列に変換
  mapfile -t EXPECTED_SET < <(jq -r '.[]' <<< "$EXPECTED_SET_JSON")
  ASSIGNMENT_READBACK_COMPLETE=true
  echo "[Step 3] expected_set 取得完了: ${EXPECTED_SET[*]}" >&2
fi

ACTUAL_SET=$(jq -r '.[].number' <<< "$ISSUES_ONLY_JSON" 2>/dev/null | sort -n | tr '\n' ' ' || echo "")

IN_EXPECTED_NOT_ACTUAL=()
IN_ACTUAL_NOT_EXPECTED=()

for exp_num in "${EXPECTED_SET[@]}"; do
  if ! echo "$ACTUAL_SET" | grep -qw "$exp_num"; then
    IN_EXPECTED_NOT_ACTUAL+=("$exp_num")
  fi
done

for act_num in $ACTUAL_SET; do
  found=false
  for exp_num in "${EXPECTED_SET[@]}"; do
    if [[ "$act_num" == "$exp_num" ]]; then
      found=true
      break
    fi
  done
  if [[ "$found" == "false" ]]; then
    IN_ACTUAL_NOT_EXPECTED+=("$act_num")
  fi
done

DRIFT_STATUS="clean"
if [[ ${#IN_EXPECTED_NOT_ACTUAL[@]} -gt 0 || ${#IN_ACTUAL_NOT_EXPECTED[@]} -gt 0 ]]; then
  DRIFT_STATUS="drift_detected"
fi

# assignment_drift_list: 差分 issue 番号のリスト（H2）
DRIFT_LIST_JSON="[]"
if [[ "$DRIFT_STATUS" == "drift_detected" ]]; then
  ALL_DRIFT=()
  for n in "${IN_EXPECTED_NOT_ACTUAL[@]}"; do
    ALL_DRIFT+=("$n")
  done
  for n in "${IN_ACTUAL_NOT_EXPECTED[@]}"; do
    ALL_DRIFT+=("$n")
  done
  DRIFT_LIST_JSON=$(printf '%s\n' "${ALL_DRIFT[@]}" | jq -R . | jq -s 'map(tonumber)' 2>/dev/null || echo "[]")
fi

# --------------------------------------------------------------------------
# Step 4: 各 open issue の blocked/ready 分類（AC5）
# --------------------------------------------------------------------------
READY_ISSUES=()
BLOCKED_ISSUES=()
NEEDS_HUMAN_ISSUES=()
UNKNOWN_ISSUES=()

while IFS= read -r issue_json; do
  [[ -z "$issue_json" ]] && continue
  issue_num=$(echo "$issue_json" | jq -r '.number')
  issue_title=$(echo "$issue_json" | jq -r '.title')
  issue_labels=$(echo "$issue_json" | jq -r '[.labels[].name] | join(",")' 2>/dev/null || echo "")

  # ラベル確認
  has_blocked=false
  has_needs_human=false
  if echo "$issue_labels" | grep -q "state/blocked"; then
    has_blocked=true
  fi
  if echo "$issue_labels" | grep -q "state/needs-human"; then
    has_needs_human=true
  fi

  # issue 本文から Depends On セクションのみを抽出して確認
  issue_body=$(gh api "repos/${REPO}/issues/${issue_num}" --jq '.body' 2>/dev/null || echo "")
  has_open_dep=false
  dep_reason=""

  dep_issues=$(
    awk '
      /^## Depends On[[:space:]]*$/ {in_dep=1; next}
      /^## / && in_dep {in_dep=0}
      in_dep {print}
    ' <<< "$issue_body" |
    grep -oE '#[0-9]+' |
    tr -d '#' |
    sort -nu
  ) || true

  for dep_num in $dep_issues; do
    dep_state=$(gh api "repos/${REPO}/issues/${dep_num}" --jq '.state' 2>/dev/null || echo "unknown")
    if [[ "$dep_state" == "open" ]]; then
      has_open_dep=true
      dep_reason="Depends On: #${dep_num} (open)"
      break
    fi
  done

  if [[ "$has_needs_human" == "true" ]]; then
    NEEDS_HUMAN_ISSUES+=("| #${issue_num} | ${issue_title} | state/needs-human ラベルあり |")
  elif [[ "$has_open_dep" == "true" ]]; then
    BLOCKED_ISSUES+=("| #${issue_num} | ${issue_title} | ${dep_reason} |")
  elif [[ "$has_blocked" == "true" ]]; then
    BLOCKED_ISSUES+=("| #${issue_num} | ${issue_title} | state/blocked ラベルあり |")
  else
    READY_ISSUES+=("| #${issue_num} | ${issue_title} | Depends On なし・blocked ラベルなし |")
  fi

done <<< "$OPEN_ISSUES"

# --------------------------------------------------------------------------
# Step 5: Next-action の優先順位判定（AC6）
# --------------------------------------------------------------------------
NEXT_ACTIONS=()
HUMAN_ESCALATIONS=()

# 優先度 1: PR 混入 / silent drop / assignment drift
if [[ "$PR_MIXED_COUNT" -gt 0 ]]; then
  NEXT_ACTIONS+=("**[最優先] PR 混入 ${PR_MIXED_COUNT} 件あり**: Milestone から PR を外す（\`PATCH /issues/{N}\` with \`milestone: null\`）")
  HUMAN_ESCALATIONS+=("PR 混入 ${PR_MIXED_COUNT} 件: docs/dev/milestone-ops.md の運用不変条件違反。PR を Milestone から外してください。")
fi

if [[ "$DRIFT_STATUS" == "drift_detected" ]]; then
  if [[ ${#IN_EXPECTED_NOT_ACTUAL[@]} -gt 0 ]]; then
    missing=$(printf '#%s ' "${IN_EXPECTED_NOT_ACTUAL[@]}")
    NEXT_ACTIONS+=("**[最優先] Assignment drift（silent drop の可能性）**: expected に存在するが live に不在: ${missing}")
    HUMAN_ESCALATIONS+=("Assignment drift: expected set に ${missing}が存在するが actual set に不在。silent drop を確認してください。")
  fi
  if [[ ${#IN_ACTUAL_NOT_EXPECTED[@]} -gt 0 ]]; then
    extra=$(printf '#%s ' "${IN_ACTUAL_NOT_EXPECTED[@]}")
    NEXT_ACTIONS+=("**[最優先] Assignment drift（無断追加の可能性）**: expected にないが live に存在: ${extra}")
    HUMAN_ESCALATIONS+=("Assignment drift: ${extra}が expected set に存在しないが actual set に存在。無断追加を確認してください。")
  fi
fi

# 優先度 2: parent close を阻害する open child
for blocked_item in "${BLOCKED_ISSUES[@]}"; do
  issue_num_raw=$(echo "$blocked_item" | grep -oE '#[0-9]+' | head -1 | tr -d '#')
  if [[ -n "$issue_num_raw" ]]; then
    NEXT_ACTIONS+=("parent close 阻害: blocked issue #${issue_num_raw} の blocker を解消する")
  fi
done

# 優先度 3: ready issues
for ready_item in "${READY_ISSUES[@]}"; do
  issue_num_raw=$(echo "$ready_item" | grep -oE '#[0-9]+' | head -1 | tr -d '#')
  if [[ -n "$issue_num_raw" ]]; then
    NEXT_ACTIONS+=("着手可能: #${issue_num_raw} を実装キューに追加する")
  fi
done

# 優先度 4: needs-human
for nh_item in "${NEEDS_HUMAN_ISSUES[@]}"; do
  issue_num_raw=$(echo "$nh_item" | grep -oE '#[0-9]+' | head -1 | tr -d '#')
  if [[ -n "$issue_num_raw" ]]; then
    NEXT_ACTIONS+=("human escalation: #${issue_num_raw} に対して人間の判断が必要")
    HUMAN_ESCALATIONS+=("#${issue_num_raw}: state/needs-human ラベルあり。人間の判断を要請します。")
  fi
done

if [[ ${#NEXT_ACTIONS[@]} -eq 0 ]]; then
  NEXT_ACTIONS+=("現時点で優先アクションなし（ready issue 着手待ち、または全 issue closed）")
fi

# --------------------------------------------------------------------------
# close_readiness 判定（B1: Python evaluate_close_readiness() を使用）
# --------------------------------------------------------------------------
CLOSE_JUDGMENT=false
CLOSE_REASON=""

if [[ "$ASSIGNMENT_READBACK_COMPLETE" != "true" ]]; then
  CLOSE_JUDGMENT=false
  CLOSE_REASON="assignment_readback_complete=false（#146 readback 取得失敗）"
else
  # Python report を取得して evaluate_close_readiness() で判定
  PYTHON_REPORT_TMPFILE=$(mktemp --suffix=.json)
  PYTHON_REPORT_OK=false

  if uv run python3 scripts/milestone_rollup.py "${MILESTONE_NUMBER}" --format json \
      --repo "${REPO}" 2>/dev/null > "$PYTHON_REPORT_TMPFILE"; then
    PYTHON_REPORT_OK=true
  fi

  if [[ "$PYTHON_REPORT_OK" == "true" && -s "$PYTHON_REPORT_TMPFILE" ]]; then
    EVAL_OUTPUT=$(uv run python3 - <<PYEOF 2>/dev/null
import sys, json
sys.path.insert(0, 'scripts')
import milestone_rollup as mr
with open('$PYTHON_REPORT_TMPFILE') as f:
    r = json.load(f)
ok, errs = mr.evaluate_close_readiness(r)
print('true' if ok else 'false')
for e in errs:
    print(e)
PYEOF
    ) || true

    CLOSE_JUDGMENT=$(echo "$EVAL_OUTPUT" | head -1)
    CLOSE_REASON=$(echo "$EVAL_OUTPUT" | tail -n +2 | tr '\n' '; ')
    if [[ -z "$CLOSE_JUDGMENT" ]]; then
      CLOSE_JUDGMENT=false
      CLOSE_REASON="evaluate_close_readiness() 呼び出し失敗"
    fi
  else
    CLOSE_JUDGMENT=false
    CLOSE_REASON="Python report 取得失敗"
  fi

  rm -f "$PYTHON_REPORT_TMPFILE"
fi

# --------------------------------------------------------------------------
# Step 6: 出力（AC7）
# --------------------------------------------------------------------------

# ready_issues テーブル生成
READY_TABLE="| # | title | reason |\n|---|---|---|\n"
if [[ ${#READY_ISSUES[@]} -eq 0 ]]; then
  READY_TABLE+="| — | （なし） | — |"
else
  for item in "${READY_ISSUES[@]}"; do
    READY_TABLE+="${item}\n"
  done
fi

# blocked_issues テーブル生成
BLOCKED_TABLE="| # | title | reason |\n|---|---|---|\n"
if [[ ${#BLOCKED_ISSUES[@]} -eq 0 ]]; then
  BLOCKED_TABLE+="| — | （なし） | — |"
else
  for item in "${BLOCKED_ISSUES[@]}"; do
    BLOCKED_TABLE+="${item}\n"
  done
fi

# needs_human テーブル
if [[ ${#NEEDS_HUMAN_ISSUES[@]} -gt 0 ]]; then
  for item in "${NEEDS_HUMAN_ISSUES[@]}"; do
    BLOCKED_TABLE+="${item}\n"
  done
fi

# next_action リスト
NEXT_ACTION_LIST=""
for i in "${!NEXT_ACTIONS[@]}"; do
  NEXT_ACTION_LIST+="$((i+1)). ${NEXT_ACTIONS[$i]}\n"
done

# human_escalations リスト
HUMAN_ESC_LIST=""
if [[ ${#HUMAN_ESCALATIONS[@]} -eq 0 ]]; then
  HUMAN_ESC_LIST="なし"
else
  for esc in "${HUMAN_ESCALATIONS[@]}"; do
    HUMAN_ESC_LIST+="- ${esc}\n"
  done
fi

# expected / actual set の表示用
if [[ ${#EXPECTED_SET[@]} -gt 0 ]]; then
  EXPECTED_STR=$(printf '%s,' "${EXPECTED_SET[@]}" | sed 's/,$//')
else
  EXPECTED_STR="(readback 失敗)"
fi
ACTUAL_STR=$(echo "$ACTUAL_SET" | tr ' ' ',' | sed 's/,$//')
if [[ ${#IN_EXPECTED_NOT_ACTUAL[@]} -eq 0 ]]; then
  NOT_ACTUAL_STR="なし"
else
  NOT_ACTUAL_STR=$(printf '#%s,' "${IN_EXPECTED_NOT_ACTUAL[@]}" | sed 's/,$//')
fi
if [[ ${#IN_ACTUAL_NOT_EXPECTED[@]} -eq 0 ]]; then
  NOT_EXPECTED_STR="なし"
else
  NOT_EXPECTED_STR=$(printf '#%s,' "${IN_ACTUAL_NOT_EXPECTED[@]}" | sed 's/,$//')
fi

OUTPUT=$(cat <<OUTPUT_EOF
## Milestone Rollup: ${M_TITLE} (#${MILESTONE_NUMBER})

実行時刻: ${TIMESTAMP}

### milestone_state_summary

\`\`\`yaml
milestone_number: ${MILESTONE_NUMBER}
title: "${M_TITLE}"
state: ${M_STATE}
open_issues: ${M_OPEN}
closed_issues: ${M_CLOSED}
due_on: ${M_DUE}
html_url: "${M_URL}"
\`\`\`

### assignment_integrity

\`\`\`yaml
# expected_set: #146 M-A3 Milestone Assignment Readback コメントより (readback: assignment_drift 参照)
assignment_readback_complete: ${ASSIGNMENT_READBACK_COMPLETE}
expected_set: [${EXPECTED_STR}]
actual_set: [${ACTUAL_STR}]
pr_mixed_count: ${PR_MIXED_COUNT}
assignment_drift:
  in_expected_not_actual: [${NOT_ACTUAL_STR}]
  in_actual_not_expected: [${NOT_EXPECTED_STR}]
drift_status: ${DRIFT_STATUS}
assignment_drift_list: ${DRIFT_LIST_JSON}
\`\`\`

### ready_issues

$(echo -e "$READY_TABLE")

### blocked_issues

$(echo -e "$BLOCKED_TABLE")

### next_action

$(echo -e "$NEXT_ACTION_LIST")

### human_escalations

$(echo -e "$HUMAN_ESC_LIST")

### close_readiness

\`\`\`yaml
open_issues: ${M_OPEN}
pr_mixed_count: ${PR_MIXED_COUNT}
assignment_readback_complete: ${ASSIGNMENT_READBACK_COMPLETE}
drift_status: ${DRIFT_STATUS}
# close_judgment_available は scripts/milestone_rollup.py の evaluate_close_readiness() で判定
# 判定条件: open_issues=0, pr_mixed_count=0, partial=false, warnings=[], open_blocker_count=0, scope_conflict_count=0
close_judgment_available: ${CLOSE_JUDGMENT}
close_reason: "${CLOSE_REASON}"
# AI は Milestone close を実行しない（docs/dev/milestone-ops.md 参照）
\`\`\`
OUTPUT_EOF
)

echo "$OUTPUT"

# --------------------------------------------------------------------------
# Comment posting（--post 指定時のみ）
# --------------------------------------------------------------------------
if [[ -n "$POST_TO_ISSUE" ]]; then
  echo "" >&2
  echo "[Comment Posting] Issue #${POST_TO_ISSUE} にコメントを投稿中..." >&2
  TMPFILE=$(mktemp)
  echo "$OUTPUT" > "$TMPFILE"
  gh issue comment "$POST_TO_ISSUE" --repo "$REPO" --body-file "$TMPFILE"
  rm -f "$TMPFILE"
  echo "[Comment Posting] 投稿完了" >&2
fi

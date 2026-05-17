#!/usr/bin/env bash
# check-issue-contract.sh
# GitHub Issue が「実装契約」として必須項目を満たしているか機械判定する。
# 欠落時は終了コード 2 を返し、issue-driven-dev skill のフロー継続をブロックする。
#
# 使い方: scripts/check-issue-contract.sh <issue番号>
# 例:    scripts/check-issue-contract.sh 8

set -euo pipefail

ISSUE_NUMBER="${1:-}"
if [[ -z "$ISSUE_NUMBER" ]]; then
  echo "ERROR: Issue 番号を指定してください。" >&2
  echo "Usage: $0 <issue-number>" >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI が必要です。" >&2
  exit 1
fi

# Issue Forms で生成された Issue は固定の Markdown 見出しを含む。
# `.github/ISSUE_TEMPLATE/implementation.yml` で定義した見出しと一致させる。
REQUIRED_HEADINGS=(
  "### 背景"
  "### 目的"
  "### 受け入れ条件"
  "### 非ゴール"
  "### テスト観点"
  "### 変更許可領域"
)

ISSUE_BODY=$(gh issue view "$ISSUE_NUMBER" --json body --jq .body 2>/dev/null) || {
  echo "ERROR: Issue #${ISSUE_NUMBER} を取得できませんでした。" >&2
  exit 1
}

MISSING=()
for heading in "${REQUIRED_HEADINGS[@]}"; do
  if ! grep -qxF "$heading" <<<"$ISSUE_BODY"; then
    MISSING+=("$heading")
    continue
  fi
  # 見出し直後に「_No response_」または空のみが続く場合は未記入扱い
  if awk -v h="$heading" '
    $0 == h { found=1; next }
    found && /^###[[:space:]]/ { exit }
    found && /[^[:space:]]/ && $0 != "_No response_" { hasContent=1 }
    END { exit (hasContent ? 0 : 1) }
  ' <<<"$ISSUE_BODY"; then
    :
  else
    MISSING+=("$heading (未記入)")
  fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "BLOCKED: Issue #${ISSUE_NUMBER} は実装契約として不備があります。" >&2
  echo "以下の必須項目が欠落または未記入です:" >&2
  for m in "${MISSING[@]}"; do
    echo "  - $m" >&2
  done
  echo "" >&2
  echo "Issue Forms (.github/ISSUE_TEMPLATE/implementation.yml) に従って必須項目を埋めてください。" >&2
  exit 2
fi

echo "OK: Issue #${ISSUE_NUMBER} は必須項目を満たしています。"
exit 0

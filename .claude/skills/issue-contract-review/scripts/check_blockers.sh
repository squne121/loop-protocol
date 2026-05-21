#!/usr/bin/env bash
# check_blockers.sh — blocker/dependency 全 close 確認
# Usage: check_blockers.sh <issue_number> [<owner>/<repo>]
#
# Exit codes:
#   0 — blocker が 0 件、または全て closed（go 判定へ継続）
#   1 — blocker が 1 件以上 open、または native / fallback 不一致（human_escalation）

set -euo pipefail

ISSUE_NUMBER="${1:?Usage: check_blockers.sh <issue_number> [<owner>/<repo>]}"
REPO="${2:-}"

if [[ -z "$REPO" ]]; then
  REPO=$(git remote get-url origin 2>/dev/null | sed 's/.*github.com[:/]//' | sed 's/\.git$//')
fi

if [[ -z "$REPO" ]]; then
  echo "ERROR: could not determine repository. Pass <owner>/<repo> as second argument." >&2
  exit 1
fi

# ---------- helper: get issue state ----------
get_issue_state() {
  local num="$1"
  gh issue view "$num" --repo "$REPO" --json state --jq '.state' 2>/dev/null || echo "UNKNOWN"
}

# ---------- 1. native dependency API (primary) ----------
native_blockers=()
native_api_available=false

raw=$(gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/dependencies/blocked_by" --paginate 2>/dev/null) || true

if [[ -n "$raw" ]]; then
  native_api_available=true
  while IFS= read -r num; do
    [[ -z "$num" ]] && continue
    native_blockers+=("$num")
  done < <(echo "$raw" | jq -r '.[].number // empty' 2>/dev/null || true)
fi

# ---------- 2. Depends on #N fallback ----------
issue_body=$(gh issue view "$ISSUE_NUMBER" --repo "$REPO" --json body --jq '.body' 2>/dev/null || echo "")

fallback_blockers=()
while IFS= read -r num; do
  [[ -z "$num" ]] && continue
  fallback_blockers+=("$num")
done < <(echo "$issue_body" | grep -oP '(?i)Depends on #\K[0-9]+' || true)

# ---------- 3. mismatch check (native available かつ fallback も存在する場合) ----------
if $native_api_available && [[ ${#fallback_blockers[@]} -gt 0 ]]; then
  # native に含まれていない fallback blocker が存在するか確認
  mismatch=false
  for fb in "${fallback_blockers[@]}"; do
    found=false
    for nb in "${native_blockers[@]}"; do
      if [[ "$fb" == "$nb" ]]; then
        found=true
        break
      fi
    done
    if ! $found; then
      mismatch=true
      echo "human_escalation: native dependency と 'Depends on #N' が不一致です。" >&2
      echo "  native blockers : ${native_blockers[*]:-<none>}" >&2
      echo "  fallback (body) : ${fallback_blockers[*]}" >&2
      echo "  不一致 issue    : #${fb}" >&2
      break
    fi
  done
  if $mismatch; then
    exit 1
  fi
fi

# ---------- 4. 判定対象 blocker リストを決定 ----------
if $native_api_available; then
  blockers=("${native_blockers[@]+"${native_blockers[@]}"}")
else
  blockers=("${fallback_blockers[@]+"${fallback_blockers[@]}"}")
fi

if [[ ${#blockers[@]} -eq 0 ]]; then
  echo "OK: blocker なし（Issue #${ISSUE_NUMBER} は着手可能）"
  exit 0
fi

# ---------- 5. 各 blocker の state を確認 ----------
open_blockers=()
for num in "${blockers[@]}"; do
  state=$(get_issue_state "$num")
  if [[ "$state" != "CLOSED" ]]; then
    open_blockers+=("$num (state=$state)")
  fi
done

if [[ ${#open_blockers[@]} -eq 0 ]]; then
  echo "OK: 全 blocker が closed（Issue #${ISSUE_NUMBER} は着手可能）"
  exit 0
else
  echo "human_escalation: blocker が open です。着手不可。" >&2
  for b in "${open_blockers[@]}"; do
    echo "  open blocker: #${b}" >&2
  done
  exit 1
fi

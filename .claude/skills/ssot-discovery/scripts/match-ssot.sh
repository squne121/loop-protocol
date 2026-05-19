#!/usr/bin/env bash
# match-ssot.sh
#
# SSOT_DISCOVERY_RESULT_V1 マッチ判定スクリプト。
# キーワードとパスから docs/ 配下の関連 SSOT を抽出する。
#
# Usage:
#   .claude/skills/ssot-discovery/scripts/match-ssot.sh \
#     --keywords "worktree,issue contract" \
#     --paths "src/systems/,tests/"

set -euo pipefail

KEYWORDS=""
PATHS=""
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keywords) KEYWORDS="${2:-}"; shift 2 ;;
    --paths)    PATHS="${2:-}"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

docs_dir="$REPO_ROOT/docs"
if [[ ! -d "$docs_dir" ]]; then
  echo "status: failed"
  echo "errors:"
  echo "  - \"docs/ directory not found at $docs_dir\""
  exit 2
fi

# 一時ファイル準備
tmp_match="$(mktemp)"
tmp_unmatched="$(mktemp)"
trap 'rm -f "$tmp_match" "$tmp_unmatched"' EXIT

# ディレクトリ → SSOT マッピング（ssot-catalog.md と整合）
declare -A DIR_MAP=(
  ["src/state"]="docs/adr/0001-architecture-baseline.md"
  ["src/render"]="docs/adr/0001-architecture-baseline.md"
  ["src/systems"]="docs/adr/0001-architecture-baseline.md"
  ["src/storage"]="docs/adr/0001-architecture-baseline.md"
  ["src/ui"]="docs/adr/0001-architecture-baseline.md"
  ["src/data"]="docs/dev/workflow.md"
  ["tests"]="docs/dev/workflow.md"
  [".claude/skills"]="docs/dev/agent-skill-boundaries.md"
  [".claude/agents"]="docs/dev/agent-skill-boundaries.md"
  [".github/workflows"]="docs/dev/workflow.md"
  [".github"]="docs/dev/github-ops.md"
  ["scripts"]="docs/dev/workflow.md"
)

# パスマッチ（low relevance）
if [[ -n "$PATHS" ]]; then
  IFS=',' read -ra PATH_ARR <<< "$PATHS"
  for p in "${PATH_ARR[@]}"; do
    p_clean="${p%/}"
    for prefix in "${!DIR_MAP[@]}"; do
      if [[ "$p_clean" == "$prefix"* ]]; then
        ssot="${DIR_MAP[$prefix]}"
        echo "low|$ssot|directory mapping from $prefix" >> "$tmp_match"
      fi
    done
  done
fi

# キーワードマッチ（high / medium）
if [[ -n "$KEYWORDS" ]]; then
  IFS=',' read -ra KW_ARR <<< "$KEYWORDS"
  for kw in "${KW_ARR[@]}"; do
    kw_trim="$(echo "$kw" | sed 's/^ *//;s/ *$//')"
    [[ -z "$kw_trim" ]] && continue
    matched_for_kw=""
    # 見出し（high）優先で探す
    while IFS= read -r f; do
      rel="${f#"$REPO_ROOT/"}"
      if grep -iqE "^#+\\s+.*${kw_trim}" "$f" 2>/dev/null; then
        echo "high|$rel|heading match for '$kw_trim'" >> "$tmp_match"
        matched_for_kw="yes"
      elif grep -iqF "$kw_trim" "$f" 2>/dev/null; then
        echo "medium|$rel|body match for '$kw_trim'" >> "$tmp_match"
        matched_for_kw="yes"
      fi
    done < <(find "$docs_dir" -type f -name '*.md')
    if [[ -z "$matched_for_kw" ]]; then
      echo "$kw_trim" >> "$tmp_unmatched"
    fi
  done
fi

# YAML 出力
echo "SSOT_DISCOVERY_RESULT_V1:"
echo "  status: $( [[ -s "$tmp_unmatched" ]] && echo "partial" || echo "ok" )"
echo "  generated_at: \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
echo "  generated_by: \"ssot-discovery\""
echo "  inputs:"
echo "    task_keywords: ${KEYWORDS:-\"\"}"
echo "    target_paths: ${PATHS:-\"\"}"
echo "  matched_documents:"

# 重複除去 + relevance 順 (high > medium > low)
if [[ -s "$tmp_match" ]]; then
  sort -u "$tmp_match" \
    | awk -F'|' 'BEGIN{order["high"]=1;order["medium"]=2;order["low"]=3}{print order[$1]"\t"$0}' \
    | sort -k1,1n \
    | cut -f2- \
    | while IFS='|' read -r rel path reason; do
        echo "    - path: \"$path\""
        echo "      relevance: \"$rel\""
        echo "      reason: \"$reason\""
      done
else
  echo "    []"
fi

echo "  unmatched_keywords:"
if [[ -s "$tmp_unmatched" ]]; then
  while IFS= read -r kw; do
    echo "    - \"$kw\""
  done < "$tmp_unmatched"
else
  echo "    []"
fi
echo "  warnings: []"
echo "  errors: []"

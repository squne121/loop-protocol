#!/usr/bin/env bash
# match-ssot.sh
#
# SSOT_DISCOVERY_RESULT_V1 マッチ判定スクリプト。
# キーワードとパスから docs/ 配下の関連 SSOT を抽出する。
# SSOT エントリは docs/dev/ssot-registry.md を動的に読み取る（ハードコード禁止）。
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
  echo "SSOT_DISCOVERY_RESULT_V1:"
  echo "  status: failed"
  echo "  generated_at: \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
  echo "  generated_by: \"ssot-discovery\""
  echo "  inputs:"
  echo "    task_keywords: []"
  echo "    target_paths: []"
  echo "  matched_documents: []"
  echo "  unmatched_keywords: []"
  echo "  notes: []"
  echo "  warnings: []"
  echo "  errors:"
  echo "    - \"docs/ directory not found at $docs_dir\""
  exit 2
fi

# ssot-registry.md のパス
registry_file="$REPO_ROOT/docs/dev/ssot-registry.md"
if [[ ! -f "$registry_file" ]]; then
  echo "SSOT_DISCOVERY_RESULT_V1:"
  echo "  status: failed"
  echo "  generated_at: \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
  echo "  generated_by: \"ssot-discovery\""
  echo "  inputs:"
  echo "    task_keywords: []"
  echo "    target_paths: []"
  echo "  matched_documents: []"
  echo "  unmatched_keywords: []"
  echo "  notes: []"
  echo "  warnings: []"
  echo "  errors:"
  echo "    - \"ssot-registry.md not found at $registry_file\""
  exit 2
fi

# 一時ファイル準備
tmp_match="$(mktemp)"
tmp_unmatched="$(mktemp)"
tmp_dir_map="$(mktemp)"
trap 'rm -f "$tmp_match" "$tmp_unmatched" "$tmp_dir_map"' EXIT

# ssot-registry.md からディレクトリ → SSOT マッピングを動的に読み取る
# 「ディレクトリ → SSOT マッピング」セクションのテーブル行を解析する
in_dir_map_section=0
while IFS= read -r line; do
  if echo "$line" | grep -q "^## ディレクトリ.*SSOT.*マッピング"; then
    in_dir_map_section=1
    continue
  fi
  if [[ "$in_dir_map_section" -eq 1 ]]; then
    # テーブル行の解析: | `src/state/**` | `docs/adr/...` |
    if echo "$line" | grep -qE '^\|[[:space:]]*`[^`]+`[[:space:]]*\|'; then
      # パスとSSOTを抽出
      dir_path=$(echo "$line" | sed 's/|[[:space:]]*`\([^`]*\)`.*/\1/' | sed 's/\/\*\*$//' | sed 's/^ *//;s/ *$//')
      ssot_path=$(echo "$line" | sed -n 's/.*|[[:space:]]*`\([^`]*\.md\)`.*/\1/p' | sed 's/^ *//;s/ *$//')
      if [[ -n "$dir_path" && -n "$ssot_path" ]]; then
        echo "$dir_path|$ssot_path" >> "$tmp_dir_map"
      fi
    fi
    # 次のセクション開始で終了
    if echo "$line" | grep -qE "^## " && ! echo "$line" | grep -q "ディレクトリ.*SSOT.*マッピング"; then
      in_dir_map_section=0
    fi
  fi
done < "$registry_file"

# パスマッチ（low relevance）
if [[ -n "$PATHS" ]]; then
  IFS=',' read -ra PATH_ARR <<< "$PATHS"
  for p in "${PATH_ARR[@]}"; do
    p_clean="${p%/}"
    if [[ -s "$tmp_dir_map" ]]; then
      while IFS='|' read -r prefix ssot; do
        if [[ "$p_clean" == "$prefix"* ]]; then
          echo "low|$ssot|directory mapping from $prefix" >> "$tmp_match"
        fi
      done < "$tmp_dir_map"
    fi
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
      if grep -iqE "^#+\s+.*${kw_trim}" "$f" 2>/dev/null; then
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

# YAML 出力用ヘルパー: コンマ区切り文字列を YAML 配列形式で出力
yaml_list() {
  local input="$1"
  if [[ -z "$input" ]]; then
    echo "[]"
    return
  fi
  local result="["
  local first=1
  IFS=',' read -ra items <<< "$input"
  for item in "${items[@]}"; do
    item_trim="$(echo "$item" | sed 's/^ *//;s/ *$//')"
    [[ -z "$item_trim" ]] && continue
    if [[ "$first" -eq 1 ]]; then
      result="${result}\"${item_trim}\""
      first=0
    else
      result="${result}, \"${item_trim}\""
    fi
  done
  result="${result}]"
  echo "$result"
}

# ssot-registry.md からパスの sections を動的に取得する
get_sections_for_path() {
  local target_path="$1"
  local in_entry=0
  local in_sections=0
  local sections=""
  while IFS= read -r line; do
    # エントリの path フィールドを探す
    if echo "$line" | grep -qE "^[[:space:]]*path:[[:space:]]*${target_path}[[:space:]]*$"; then
      in_entry=1
      in_sections=0
      continue
    fi
    if [[ "$in_entry" -eq 1 ]]; then
      # sections フィールドの開始（インライン配列）
      if echo "$line" | grep -qE "^[[:space:]]*sections:[[:space:]]*\["; then
        secs=$(echo "$line" | sed 's/^[[:space:]]*sections:[[:space:]]*//')
        # インライン配列の内容を取得
        inner=$(echo "$secs" | sed 's/^\[//;s/\]$//')
        # クォートされた項目を抽出
        local tmp_secs=""
        while IFS= read -r item; do
          item=$(echo "$item" | sed 's/^ *"//;s/" *$//')
          [[ -z "$item" ]] && continue
          if [[ -n "$tmp_secs" ]]; then
            tmp_secs="${tmp_secs}, \"${item}\""
          else
            tmp_secs="\"${item}\""
          fi
        done < <(echo "$inner" | tr ',' '\n')
        sections="$tmp_secs"
        in_entry=0
        break
      fi
      # sections フィールドの開始（リスト形式）
      if echo "$line" | grep -qE "^[[:space:]]*sections:[[:space:]]*$"; then
        in_sections=1
        continue
      fi
      if [[ "$in_sections" -eq 1 ]]; then
        # リスト項目
        if echo "$line" | grep -qE "^[[:space:]]*-[[:space:]]*\""; then
          item=$(echo "$line" | sed 's/^[[:space:]]*-[[:space:]]*//' | sed 's/^"//;s/"$//')
          if [[ -n "$sections" ]]; then
            sections="${sections}, \"${item}\""
          else
            sections="\"${item}\""
          fi
          continue
        fi
        # リスト終了（別フィールドまたは新エントリ）
        in_sections=0
      fi
      # 次のエントリの開始（- id: 等）で終了
      if echo "$line" | grep -qE "^- id:"; then
        break
      fi
    fi
  done < "$registry_file"
  echo "$sections"
}

# YAML 出力
echo "SSOT_DISCOVERY_RESULT_V1:"
echo "  status: $( [[ -s "$tmp_unmatched" ]] && echo "partial" || echo "ok" )"
echo "  generated_at: \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
echo "  generated_by: \"ssot-discovery\""
echo "  inputs:"
echo "    task_keywords: $(yaml_list "$KEYWORDS")"
echo "    target_paths: $(yaml_list "$PATHS")"
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
        # sections を取得
        secs=$(get_sections_for_path "$path")
        if [[ -n "$secs" ]]; then
          echo "      sections: [$secs]"
        else
          echo "      sections: []"
        fi
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
echo "  notes:"
echo "    - \"SSOT registry: docs/dev/ssot-registry.md\""
echo "  warnings: []"
echo "  errors: []"

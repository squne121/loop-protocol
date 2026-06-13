#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE' >&2
Usage:
  wait_ci_checks.sh --repo <owner/repo> --pr <number> --head-sha <sha> [--required] [--interval <seconds>] [--timeout-seconds <seconds>]

Options:
  --repo              GitHub repository in owner/repo format
  --pr                Pull request number
  --head-sha          Expected PR head SHA (reviewed head SHA)
  --required          Limit to required checks only
  --interval          Poll interval in seconds (default: 15)
  --timeout-seconds   Max wait time in seconds (default: 1800)
  --help              Show this help and exit
USAGE
}

emit_result() {
  local status="$1"
  local error_code="$2"
  local checks_json="$3"
  local message="$4"
  local final_head_sha="$5"
  local elapsed_seconds="$6"

  local result_json
  result_json=$(jq -n \
    --arg schema "CI_WAIT_RESULT_V1" \
    --arg status "$status" \
    --arg repo "$REPO" \
    --argjson pr_number "$PR_NUMBER" \
    --arg head_sha "$TARGET_HEAD_SHA" \
    --arg current_head_sha "$final_head_sha" \
    --argjson required_only "$REQUIRED_ONLY" \
    --arg exit_code "$error_code" \
    --arg message "$message" \
    --argjson checks "$checks_json" \
    --argjson elapsed "$elapsed_seconds" \
    --argjson interval "$INTERVAL" \
    --argjson timeout "$TIMEOUT_SECONDS" \
    '{
      schema: $schema,
      status: $status,
      repo: $repo,
      pr_number: $pr_number,
      head_sha: $head_sha,
      current_head_sha: $current_head_sha,
      required_only: $required_only,
      checks: $checks,
      elapsed_seconds: $elapsed,
      interval_seconds: $interval,
      timeout_seconds: $timeout,
      error_code: ($exit_code | if . == "" then null else . end),
      message: ($message | if . == "" then null else . end)
    }')

  printf 'CI_WAIT_RESULT_V1_JSON=%s\n' "$result_json"

  if [ "$status" = "passed" ]; then
    exit 0
  fi
  exit 1
}

get_current_head_sha() {
  gh pr view "$PR_NUMBER" --repo "$REPO" --json headRefOid --jq .headRefOid
}

fetch_checks() {
  local raw=""
  local rc=0

  raw=$(gh pr checks "$PR_NUMBER" --repo "$REPO" --required --json name,bucket,state,workflow,link,startedAt,completedAt 2>&1) || rc=$?

  if [ "$rc" -ne 0 ]; then
    if grep -qiE "authenticat|bad credentials|rate limit|forbidden|not authorized|resource not accessible" <<<"$raw"; then
      jq -nc --arg raw "$raw" '{status:"error", error_code:"auth_error", raw:$raw, checks: []}'
      return 10
    fi
    jq -nc --arg raw "$raw" '{status:"error", error_code:"gh_error", raw:$raw, checks: []}'
    return 11
  fi

  if ! jq -e . >/dev/null 2>&1 <<<"$raw"; then
    jq -nc --arg raw "$raw" '{status:"error", error_code:"malformed_gh_response", raw:$raw, checks: []}'
    return 12
  fi

  printf '%s' "$raw"
  return 0
}

REPO=""
PR_NUMBER=""
TARGET_HEAD_SHA=""
REQUIRED_ONLY=true
INTERVAL=15
TIMEOUT_SECONDS=1800

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)
      REPO="$2"
      shift 2
      ;;
    --pr)
      PR_NUMBER="$2"
      shift 2
      ;;
    --head-sha)
      TARGET_HEAD_SHA="$2"
      shift 2
      ;;
    --required)
      REQUIRED_ONLY=true
      shift
      ;;
    --interval)
      INTERVAL="$2"
      shift 2
      ;;
    --timeout-seconds)
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ -z "$REPO" ] || [ -z "$PR_NUMBER" ] || [ -z "$TARGET_HEAD_SHA" ]; then
  usage
  exit 2
fi

if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || ! [[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "--interval and --timeout-seconds must be integers" >&2
  exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
  emit_result "gh_error" "gh_error" "[]" "jq not found" "$TARGET_HEAD_SHA" 0
fi

if ! command -v gh >/dev/null 2>&1; then
  emit_result "gh_error" "gh_error" "[]" "gh CLI not found" "$TARGET_HEAD_SHA" 0
fi

export GH_PROMPT_DISABLED=1

start_ts=$(date +%s)
current_head_sha="$(get_current_head_sha)"
if [ "$current_head_sha" != "$TARGET_HEAD_SHA" ]; then
  emit_result "head_sha_changed" "head_sha_changed" "[]" "head SHA changed before wait" "$current_head_sha" 0
fi

while true; do
  elapsed=$(( $(date +%s) - start_ts ))
  if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
    current_head_sha="$(get_current_head_sha)"
    emit_result "pending_timeout" "timed_out" "[]" "timeout waiting for required checks" "$current_head_sha" "$elapsed"
  fi

  check_result="$(fetch_checks)"
  parse_rc=$?

  if [ "$parse_rc" -ne 0 ]; then
    current_head_sha="$(get_current_head_sha)"
    error_code="$(jq -r '.error_code // empty' <<<"$check_result")"
    raw_message="$(jq -r '.raw // empty' <<<"$check_result")"
    emit_result "${error_code}" "$error_code" "[]" "$raw_message" "$current_head_sha" "$elapsed"
  fi

  checks_count=$(jq 'length' <<<"$check_result")
  if [ "$checks_count" -eq 0 ]; then
    current_head_sha="$(get_current_head_sha)"
    emit_result "no_checks" "no_checks" "[]" "required checks are not available" "$current_head_sha" "$elapsed"
  fi

  has_pending=$(jq '[.[] | select(.bucket == "pending")] | length' <<<"$check_result")
  has_failed=$(jq '[.[] | select(.bucket == "fail" or .bucket == "cancel")] | length' <<<"$check_result")

  if [ "$has_failed" -gt 0 ]; then
    current_head_sha="$(get_current_head_sha)"
    if [ "$current_head_sha" != "$TARGET_HEAD_SHA" ]; then
      emit_result "head_sha_changed" "head_sha_changed" "$check_result" "head SHA changed while checks failed" "$current_head_sha" "$elapsed"
    fi
    emit_result "failed" "failed" "$check_result" "required checks failed" "$current_head_sha" "$elapsed"
  fi

  if [ "$has_pending" -gt 0 ]; then
    sleep "$INTERVAL"
    continue
  fi

  current_head_sha="$(get_current_head_sha)"
  if [ "$current_head_sha" != "$TARGET_HEAD_SHA" ]; then
    emit_result "head_sha_changed" "head_sha_changed" "$check_result" "head SHA changed after checks passed" "$current_head_sha" "$elapsed"
  fi

  emit_result "passed" "" "$check_result" "all required checks passed" "$current_head_sha" "$elapsed"
done

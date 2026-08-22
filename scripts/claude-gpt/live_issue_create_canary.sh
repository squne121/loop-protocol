#!/usr/bin/env bash
# scripts/claude-gpt/live_issue_create_canary.sh
#
# Issue #2299 AC8: opt-in, disposable-Issue live canary that confirms an
# isolated Claude-GPT session (launched via `launch.sh`, with GitHub auth
# shared native-equivalent per Issue #2299 AC1) can complete a genuine
# `issue-creator` create-issue workflow end-to-end against the REAL trusted
# repository, creating and then closing one disposable Issue.
#
# This is NOT a repeatable regression test and is NOT run by default. Unlike
# `runtime_smoke_test.sh --scenario issue_create` (Issue #2299 AC2/AC5, which
# always runs against a fake provider and never touches real GitHub state),
# this script performs one real, disposable GitHub mutation and is meant to
# be run manually/opt-in once per PR (Delivery Rule), following the existing
# opt-in canary pattern established by `scripts/claude-gpt/auto_mode_canary.sh`
# and `.claude/skills/create-issue/tests/live_canary_blocking_direction.sh`.
#
# Opt-in requirement:
#   Unless BOTH of the following are explicitly given, this script always
#   SKIPs (exit 77) without touching GitHub or launching claude-gpt:
#     1. env var CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_CONFIRM=1
#     2. flag --confirm on the command line
#   Absence of either is treated as "no opt-in" (fail-closed default SKIP).
#
# Exit codes:
#   0   PASS (disposable Issue created via genuine isolated Claude-GPT
#             issue-creator workflow, exact-identity-verified, then closed
#             and closed-state-readback-confirmed)
#   1   FAIL (opted-in but the workflow did not complete / identity could
#             not be established / cleanup failed / close state mismatch)
#   77  SKIP (no explicit opt-in given -- the default, always-safe path)
#
# Out of Scope (Issue #2299): this canary does not expand to other
# repositories, does not exercise PR/branch push/merge, and is not wired
# into CI as a required check.
#
# Artifact requirements: request/response transcripts (secrets redacted) are
# written under scripts/claude-gpt/.evidence/. Tokens/credentials/raw
# environment are never written.
#
# --- Issue #2306 (OWNER REQUEST_CHANGES, issuecomment-5381144385) ---------
# Two structural gaps identified by adversarial review of the pre-#2306
# version of this script are fixed here:
#
#   1. Missing exact-identity verification: after parsing
#      `CANARY_ISSUE_NUMBER=<n>` from Claude's stdout, the old script only
#      confirmed the numbered Issue *existed* (`gh issue view` exit 0), never
#      that it was actually the disposable probe Issue just created. A
#      misparsed or hallucinated number pointing at an unrelated existing
#      Issue (e.g. #2299) would previously be treated as success -- and then
#      closed. This version verifies title+body equality
#      (`verify_identity`) before ever calling `gh issue close`, and falls
#      back to a title/marker-based search (`fallback_search`) whenever the
#      number is missing OR identity verification fails. The fallback only
#      acts on an unambiguous, single exact-title match; 0 or >=2 candidates
#      are treated as "could not establish identity" and nothing is closed.
#
#   2. Missing EXIT trap: cleanup previously only ran via the normal
#      sequential tail of the script, so a `Ctrl-C`/`SIGTERM`/disconnect
#      between number-parse and close could orphan the disposable Issue
#      (and the old evidence schema even reported `cleanup_ok:true` when no
#      number had been parsed at all, i.e. when nothing was ever attempted).
#      This version registers `cleanup_handler` as an EXIT trap *before* the
#      launcher subprocess runs, so cleanup is attempted whether the run
#      finishes normally, fails, or is interrupted (INT/TERM re-raise
#      through the ordinary `exit` builtin, which itself triggers EXIT).
#      Close success is now independently confirmed via a post-close
#      `gh issue view --json state` readback (`state == "CLOSED"`), not just
#      the exit code of `gh issue close`.
#
# The functions below are intentionally kept import/source-able (guarded
# by LIVE_ISSUE_CREATE_CANARY_TEST_SOURCE, see the bottom of this file) so
# `scripts/claude-gpt/tests/test_live_issue_create_canary.py` (Issue #2306
# AC5) can exercise verify_identity / fallback_search / close_and_verify /
# cleanup_handler deterministically against the fake `gh` provider
# (scripts/claude-gpt/tests/fixtures/fake_gh.py) without ever launching a
# real claude-gpt session or touching real GitHub.

command set -u

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

# --- gh(1) JSON helpers -----------------------------------------------------

gh_issue_view_json() {
  # $1=number $2=repo -> prints a JSON object with number/title/body/state/url
  # on success, prints nothing and returns non-zero if the Issue could not be
  # read back (not found / transient gh failure -- both are treated the same:
  # "identity could not be confirmed", never assumed to be a match).
  # contract shape: gh issue view --json number,title,body,state,url (number
  # and --repo are interpolated positionally/as flags below).
  gh issue view "$1" --repo "$2" --json number,title,body,state,url 2>/dev/null
}

# --- AC3: exact identity verification --------------------------------------

verify_identity() {
  # args: number repo expected_title expected_body
  # Prints a JSON object {"status": "match"|"mismatch"|"not_found", ...}.
  # "match" is only returned when BOTH title and body are exactly equal to
  # what was requested -- existence alone is never sufficient (Issue #2306
  # gap 1).
  local number="$1" repo="$2" expected_title="$3" expected_body="$4"
  local json
  json=$(gh_issue_view_json "$number" "$repo")
  if [ -z "$json" ]; then
    printf '{"status":"not_found","number":%s}' "$number"
    return 1
  fi
  IDENTITY_JSON="$json" IDENTITY_EXPECTED_TITLE="$expected_title" IDENTITY_EXPECTED_BODY="$expected_body" python3 <<'IDENTITY_PY_EOF'
import json
import os

data = json.loads(os.environ["IDENTITY_JSON"])
title_match = data.get("title") == os.environ["IDENTITY_EXPECTED_TITLE"]
# trailing-newline-tolerant: gh --body-file (and most GitHub write paths)
# commonly normalize a trailing newline onto stored body text; this is not
# a content difference worth failing identity verification over.
body_match = (data.get("body") or "").rstrip(chr(10)) == (os.environ["IDENTITY_EXPECTED_BODY"] or "").rstrip(chr(10))
result = dict(data)
result["title_match"] = title_match
result["body_match"] = body_match
result["status"] = "match" if (title_match and body_match) else "identity_mismatch"
print(json.dumps(result))
IDENTITY_PY_EOF
}

# --- AC3: title/marker-based fallback search --------------------------------

fallback_search() {
  # args: repo expected_title marker
  # Prints a JSON object {"status": "match"|"none"|"ambiguous",
  # "candidate_count": N, ...}. Only acts (status "match") on an unambiguous
  # single exact-title match among the search results; 0 or >=2 candidates
  # never trigger a close (Issue #2306 AC3).
  local repo="$1" expected_title="$2" marker="$3"
  local json
  json=$(gh issue list --repo "$repo" --search "$marker" --state all --json number,title,body,state,url --limit 20 2>/dev/null)
  if [ -z "$json" ]; then
    json="[]"
  fi
  FALLBACK_LIST_JSON="$json" FALLBACK_EXPECTED_TITLE="$expected_title" python3 <<'FALLBACK_PY_EOF'
import json
import os

items = json.loads(os.environ["FALLBACK_LIST_JSON"])
title = os.environ["FALLBACK_EXPECTED_TITLE"]
candidates = [item for item in items if item.get("title") == title]
result = {"candidate_count": len(candidates)}
if len(candidates) == 1:
    result.update(candidates[0])
    result["status"] = "match"
elif len(candidates) == 0:
    result["status"] = "none"
else:
    result["status"] = "ambiguous"
print(json.dumps(result))
FALLBACK_PY_EOF
}

# --- AC4: close + post-close state readback ---------------------------------

close_and_verify() {
  # args: number repo comment
  # Prints a JSON object {"close_ok": true|false, "state": "..."|null}.
  # A close is only ever reported successful when the post-close readback
  # independently confirms state == "CLOSED" -- the exit code of
  # `gh issue close` alone is not sufficient (Issue #2306 gap 2).
  local number="$1" repo="$2" comment="$3"
  if ! gh issue close "$number" --repo "$repo" --comment "$comment" >/dev/null 2>&1; then
    printf '{"close_ok":false,"state":null}'
    return 1
  fi
  local json
  json=$(gh_issue_view_json "$number" "$repo")
  if [ -z "$json" ]; then
    printf '{"close_ok":true,"state":null}'
    return 1
  fi
  CLOSE_VERIFY_JSON="$json" python3 <<'CLOSE_VERIFY_PY_EOF'
import json
import os

data = json.loads(os.environ["CLOSE_VERIFY_JSON"])
state = data.get("state")
print(json.dumps({"close_ok": True, "state": state}))
CLOSE_VERIFY_PY_EOF
}

# --- AC4: cleanup handler (EXIT trap) ---------------------------------------
#
# Registered (see main()) BEFORE the launcher subprocess runs, so it fires
# on normal completion, on any early `exit`, and on INT/TERM (which are
# themselves wired to call `exit` so the same EXIT trap always fires --
# see the trap registrations at the bottom of main()). Idempotent via
# CLEANUP_DONE so INT/TERM followed by the script's own tail-end exit never
# double-runs cleanup.

CLEANUP_DONE=false
CANARY_ISSUE_NUMBER=""
CANARY_REPO=""
CANARY_TITLE=""
CANARY_BODY=""
CANARY_MARKER=""
CANARY_TIMESTAMP=""
EVIDENCE_FILE=""
LAUNCH_RC=""
IDENTITY_STATUS="not_attempted"
FALLBACK_STATUS="not_attempted"
FALLBACK_CANDIDATE_COUNT="null"
RESOLVED_ISSUE_NUMBER="null"
CLOSE_OK="false"
CLOSE_STATE="null"
FINAL_STATUS="failure"

cleanup_handler() {
  local exit_code=$?
  if [ "$CLEANUP_DONE" = "true" ]; then
    return 0
  fi
  CLEANUP_DONE=true

  if [ -n "$CANARY_ISSUE_NUMBER" ] || [ "$RESOLVED_ISSUE_NUMBER" != "null" ]; then
    local target_number="$RESOLVED_ISSUE_NUMBER"
    if [ "$target_number" = "null" ]; then
      target_number="$CANARY_ISSUE_NUMBER"
    fi
    if [ -n "$target_number" ] && [ "$target_number" != "null" ]; then
      local close_json
      close_json=$(close_and_verify "$target_number" "$CANARY_REPO" "claude-gpt live_issue_create_canary: disposable probe cleanup (${CANARY_TIMESTAMP})")
      CLOSE_OK=$(printf '%s' "$close_json" | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["close_ok"]).lower())' 2>/dev/null || echo false)
      CLOSE_STATE=$(printf '%s' "$close_json" | python3 -c 'import json,sys; v=json.load(sys.stdin)["state"]; print(json.dumps(v))' 2>/dev/null || echo null)
    fi
  fi

  local close_state_closed=false
  if [ "$CLOSE_STATE" = '"CLOSED"' ] || [ "$CLOSE_STATE" = '"closed"' ]; then
    close_state_closed=true
  fi

  if [ "$FINAL_STATUS" = "success" ] && { [ "$CLOSE_OK" != "true" ] || [ "$close_state_closed" != "true" ]; }; then
    FINAL_STATUS="failure"
  fi

  if [ -n "$EVIDENCE_FILE" ]; then
    printf '{"schema":"CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_RESULT_V1","status":"%s","generated_at":"%s","repo":"%s","canary_issue_number":%s,"launch_exit_code":%s,"identity_status":"%s","fallback_status":"%s","fallback_candidate_count":%s,"resolved_issue_number":%s,"close_ok":%s,"close_state":%s}\n' \
      "$FINAL_STATUS" "$CANARY_TIMESTAMP" "$CANARY_REPO" "${CANARY_ISSUE_NUMBER:-null}" "${LAUNCH_RC:-null}" \
      "$IDENTITY_STATUS" "$FALLBACK_STATUS" "$FALLBACK_CANDIDATE_COUNT" "$RESOLVED_ISSUE_NUMBER" "$CLOSE_OK" "$CLOSE_STATE" > "$EVIDENCE_FILE"
  fi

  if [ "$FINAL_STATUS" = "success" ]; then
    echo "PASS: live_issue_create_canary が disposable Issue #${RESOLVED_ISSUE_NUMBER} を作成/identity検証/close/close状態確認しました。証跡: ${EVIDENCE_FILE}" >&2
  else
    echo "FAIL: live_issue_create_canary が完走しませんでした（identity_status=${IDENTITY_STATUS} fallback_status=${FALLBACK_STATUS} close_ok=${CLOSE_OK} close_state=${CLOSE_STATE}）。証跡: ${EVIDENCE_FILE}" >&2
  fi

  return "$exit_code"
}

# --- AC3: resolve the actual disposable Issue number to act on -------------
#
# args: repo expected_title expected_body marker
# Sets IDENTITY_STATUS / FALLBACK_STATUS / FALLBACK_CANDIDATE_COUNT /
# RESOLVED_ISSUE_NUMBER globals. Never sets RESOLVED_ISSUE_NUMBER unless an
# unambiguous identity match was established either directly (parsed number
# + matching title/body) or via fallback (single exact-title candidate).
resolve_target_issue_number() {
  local repo="$1" expected_title="$2" expected_body="$3" marker="$4"

  if [ -n "$CANARY_ISSUE_NUMBER" ]; then
    local identity_json
    identity_json=$(verify_identity "$CANARY_ISSUE_NUMBER" "$repo" "$expected_title" "$expected_body")
    IDENTITY_STATUS=$(printf '%s' "$identity_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status","not_found"))' 2>/dev/null || echo not_found)
    if [ "$IDENTITY_STATUS" = "match" ]; then
      RESOLVED_ISSUE_NUMBER="$CANARY_ISSUE_NUMBER"
      return 0
    fi
  else
    IDENTITY_STATUS="no_number_parsed"
  fi

  # parsed number missing OR identity mismatch/not_found -> fallback search
  local fallback_json
  fallback_json=$(fallback_search "$repo" "$expected_title" "$marker")
  FALLBACK_STATUS=$(printf '%s' "$fallback_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status","none"))' 2>/dev/null || echo none)
  FALLBACK_CANDIDATE_COUNT=$(printf '%s' "$fallback_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("candidate_count",0))' 2>/dev/null || echo 0)
  if [ "$FALLBACK_STATUS" = "match" ]; then
    RESOLVED_ISSUE_NUMBER=$(printf '%s' "$fallback_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["number"])')
    return 0
  fi
  return 1
}

main() {
  local confirm_flag=false
  local _arg
  for _arg in "$@"; do
    case "$_arg" in
      --confirm) confirm_flag=true ;;
    esac
  done

  local evidence_dir
  evidence_dir=$(claude_gpt_evidence_dir "$SELF_PATH")
  mkdir -p "$evidence_dir"
  CANARY_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
  EVIDENCE_FILE="${evidence_dir}/live-issue-create-canary-${CANARY_TIMESTAMP}.json"

  if [ "${CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_CONFIRM:-}" != "1" ] || [ "$confirm_flag" != "true" ]; then
    printf '{"schema":"CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_RESULT_V1","status":"skip","reason":"opt_in_not_given","generated_at":"%s"}\n' \
      "$CANARY_TIMESTAMP" > "$EVIDENCE_FILE"
    echo "SKIP: opt-in が与えられていないため live_issue_create_canary を実行しません（既定の安全な経路）。" >&2
    echo "opt-in するには CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_CONFIRM=1 を設定し、--confirm を渡してください。" >&2
    echo "証跡: ${EVIDENCE_FILE}" >&2
    exit 77
  fi

  CANARY_REPO="squne121/loop-protocol"

  # --- 環境可用性判定（バイナリ / ChatGPT subscription 認証 / gh 認証）。opt-in 後も
  #     実行不能な環境では SKIP を返す（opt-in は「実行して良い」の同意であり、
  #     「実行環境が揃っている」ことの保証ではない）。 ---
  local preflight_env_json preflight_env_rc
  preflight_env_json=$("$SCRIPT_DIR/preflight.sh" --env-only)
  preflight_env_rc=$?
  if [ "$preflight_env_rc" -eq 3 ] || [ "$preflight_env_rc" -eq 4 ]; then
    printf '{"schema":"CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_RESULT_V1","status":"skip","reason":"environment_unavailable","preflight_env_only":%s,"generated_at":"%s"}\n' \
      "$preflight_env_json" "$CANARY_TIMESTAMP" > "$EVIDENCE_FILE"
    echo "SKIP: claude-gpt 実行環境が利用不能なため live canary を実行できません。証跡: ${EVIDENCE_FILE}" >&2
    exit 77
  fi

  if ! gh auth status --hostname github.com >/dev/null 2>&1; then
    printf '{"schema":"CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_RESULT_V1","status":"skip","reason":"gh_auth_unavailable","generated_at":"%s"}\n' \
      "$CANARY_TIMESTAMP" > "$EVIDENCE_FILE"
    echo "SKIP: ambient gh auth が利用不能なため live canary を実行できません。証跡: ${EVIDENCE_FILE}" >&2
    exit 77
  fi

  # --- opted-in 実行: genuine issue-creator を isolated claude-gpt session 経由で
  #     起動し、通常の create-issue workflow（dedupe read -> create_issue_txn.py
  #     -> authoritative readback）を real trusted repository に対して完走させる。
  #     作成した disposable Issue は EXIT trap（cleanup_handler、上で登録済み）
  #     により正常終了・失敗・INT・TERM いずれの経路でも close を試みる。 ---
  # Issue #2306 Background「title の一意性不足」対応: PID + 乱数を含めることで
  # 同秒複数実行での title 衝突可能性を下げる。
  CANARY_MARKER="$$-${RANDOM}${RANDOM}"
  CANARY_TITLE="claude-gpt live_issue_create_canary disposable probe (${CANARY_TIMESTAMP}-${CANARY_MARKER})"
  CANARY_BODY="## Acceptance Criteria

- [ ] AC1: disposable probe issue created by scripts/claude-gpt/live_issue_create_canary.sh

## Verification Commands

\`\`\`bash
true  # AC1
\`\`\`

## Allowed Paths

- scripts/claude-gpt/**

This is a disposable canary Issue created by \`scripts/claude-gpt/live_issue_create_canary.sh\`
(Issue #2299 AC8, Issue #2306). It will be closed immediately by the same run."

  local prompt
  prompt="isolated claude-gpt live_issue_create_canary: create-issue skill の通常
procedure（dedupe read を含む）に従って、repo ${CANARY_REPO} に以下のタイトル/本文で
Issue を1件だけ作成してください。作成後、作成した Issue 番号を
\`CANARY_ISSUE_NUMBER=<番号>\` という1行として stdout に出力し、それ以外の
説明文は出力しないでください。

タイトル: ${CANARY_TITLE}

本文:
${CANARY_BODY}"

  # cleanup_handler を launcher 実行前に登録する（Issue #2306 gap 2）。
  trap cleanup_handler EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  local claude_output
  claude_output=$("$SCRIPT_DIR/launch.sh" -- -p "$prompt" --output-format text --no-session-persistence 2>>"$EVIDENCE_FILE.stderr.log")
  LAUNCH_RC=$?

  CANARY_ISSUE_NUMBER=$(printf '%s\n' "$claude_output" | sed -n 's/^CANARY_ISSUE_NUMBER=\([0-9]\+\).*/\1/p' | head -n1)

  if [ "$LAUNCH_RC" -eq 0 ]; then
    if resolve_target_issue_number "$CANARY_REPO" "$CANARY_TITLE" "$CANARY_BODY" "$CANARY_MARKER"; then
      FINAL_STATUS="success"
    fi
  fi

  # cleanup_handler runs via the EXIT trap registered above (also covers the
  # close + close-state-readback that determines the final FINAL_STATUS).
  if [ "$FINAL_STATUS" = "success" ]; then
    exit 0
  fi
  exit 1
}

if [ "${LIVE_ISSUE_CREATE_CANARY_TEST_SOURCE:-0}" != "1" ]; then
  main "$@"
fi

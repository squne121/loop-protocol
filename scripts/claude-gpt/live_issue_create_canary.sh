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
#             issue-creator workflow, exact-identity-verified as OPEN, then
#             closed and closed-state-readback-confirmed)
#   1   FAIL (opted-in but the workflow did not complete / identity could
#             not be established as OPEN / cleanup failed / close state
#             mismatch)
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
# --- Issue #2306 follow-up (OWNER REQUEST_CHANGES, issuecomment-5381684379,
#     superseding the earlier pr-reviewer APPROVE on PR #2309) -------------
# Adversarial review of `main -> EXIT trap -> cleanup_handler -> final exit
# code` identified two further structural gaps, fixed here:
#
#   P0. `cleanup_handler` previously fell back to the raw, unverified
#       `CANARY_ISSUE_NUMBER` (parsed straight from Claude stdout) whenever
#       `RESOLVED_ISSUE_NUMBER` was still "null" (i.e. identity verification
#       AND fallback search both failed to establish identity). This meant
#       an unverified/possibly-hallucinated number could still be closed.
#       Fixed: `cleanup_handler` now only ever acts on `RESOLVED_ISSUE_NUMBER`
#       (identity-verified, either directly or via a fallback candidate that
#       is itself re-confirmed against an authoritative `gh issue view`
#       readback -- see P1-c below). If it is "null", nothing is closed.
#
#   P1-a. `cleanup_handler` previously ended with `return "$exit_code"`,
#       where `$exit_code` was captured from `$?` at function entry (i.e.
#       *before* the trap fired) -- but bash's EXIT trap does not use a
#       trap handler's `return` value as the process's final exit status;
#       once `main()` has already called `exit 0`, the process exits 0
#       regardless of what `cleanup_handler` computes internally (e.g.
#       downgrading `FINAL_STATUS` to "failure" on a close failure had no
#       effect on the actual process exit code). Fixed:
#       `cleanup_handler` now disables further trap re-entry (`trap - EXIT`)
#       immediately, then explicitly calls `exit <code>` itself at the end,
#       deriving the authoritative final code from (in priority order) an
#       INT/TERM-original interrupt code (130/143, preserved verbatim), else
#       0 iff `FINAL_STATUS == success`, else 1. `close_and_verify` now also
#       returns non-zero (in addition to the JSON payload) whenever the
#       post-close readback state is not "closed"/"CLOSED", so a `close_ok`
#       apparent-success with a state mismatch is never silently treated as
#       cleanup success by `cleanup_handler`.
#
#   P1-b. Cleanup-target resolution (`resolve_target_issue_number`) was
#       previously only attempted when the launcher exited 0. If the
#       launcher itself exited non-zero after already creating the
#       disposable Issue (e.g. it crashed after `issue-creator` succeeded
#       but before printing `CANARY_ISSUE_NUMBER=<n>`), the created Issue
#       would never be discovered by fallback search and would leak. Fixed:
#       `resolve_target_issue_number` is now always attempted regardless of
#       `LAUNCH_RC`; only the PASS/FAIL determination (not cleanup attempt)
#       is gated on `LAUNCH_RC == 0`.
#
#   P1-c. `verify_identity` never inspected `state`, so an already-CLOSED
#       Issue that happened to match on title/body (e.g. a stale duplicate
#       from a prior interrupted run) could be reported as a PASS. Also,
#       `fallback_search`'s candidates are filtered by `gh issue list`
#       title-only matching (never body), so a wrong-body candidate could
#       previously be trusted without further check. Fixed: PASS
#       (`FINAL_STATUS=success`) now additionally requires the resolved
#       Issue's state to be OPEN at resolution time, and any fallback-search
#       candidate is re-confirmed via a direct, authoritative `verify_identity`
#       (`gh issue view` readback: title AND body AND state) before it is
#       ever trusted as `RESOLVED_ISSUE_NUMBER` -- cleanup may still act on a
#       wrong-body or already-closed candidate once identity is otherwise
#       confirmed, but such a run is never reported PASS. The disposable
#       probe Issue body additionally now embeds a machine-checkable run
#       marker HTML comment (`<!-- claude-gpt-live-canary-run:<marker> -->`)
#       to strengthen exact-identity confirmation.
#
# The functions below are intentionally kept import/source-able (guarded
# by LIVE_ISSUE_CREATE_CANARY_TEST_SOURCE, see the bottom of this file) so
# `scripts/claude-gpt/tests/test_live_issue_create_canary.py` (Issue #2306
# AC5) can exercise verify_identity / fallback_search / close_and_verify /
# cleanup_handler / main() end-to-end (via a real subprocess with faked
# launcher/preflight/gh) deterministically against the fake `gh` provider
# (scripts/claude-gpt/tests/fixtures/fake_gh.py) without ever launching a
# real claude-gpt session or touching real GitHub.

command set -u

# Issue #2306 follow-up P1-d: `$0` only reflects the invoked script path
# when this file is executed directly; when it is instead `source`d (as the
# test suite does, guarded by LIVE_ISSUE_CREATE_CANARY_TEST_SOURCE=1) inside
# a `bash -c '...'` one-liner, `$0` resolves to `bash` itself, not this
# file, which previously broke `SCRIPT_DIR`/`lib.sh` source resolution
# silently (no `set -e`, so the broken `. "$SCRIPT_DIR/lib.sh"` failure was
# swallowed). `${BASH_SOURCE[0]:-$0}` always resolves to this file's own
# path in both the direct-execution and sourced cases.
SELF_PATH=${BASH_SOURCE[0]:-$0}
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
  # Prints a JSON object {"status": "match"|"identity_mismatch"|"not_found",
  # "state": "open"|"closed"|null, ...}. "match" is only returned when BOTH
  # title and body are exactly equal to what was requested -- existence
  # alone is never sufficient (Issue #2306 gap 1), and (Issue #2306
  # follow-up P1-c) "match" says nothing about whether the Issue is OPEN --
  # callers that need PASS-eligibility must additionally check "state".
  local number="$1" repo="$2" expected_title="$3" expected_body="$4"
  local json
  json=$(gh_issue_view_json "$number" "$repo")
  if [ -z "$json" ]; then
    printf '{"status":"not_found","number":%s,"state":null}' "$number"
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
  # never trigger a close (Issue #2306 AC3). Note: this candidate is
  # title-matched only (gh issue list does not filter on body) -- callers
  # MUST re-confirm identity via `verify_identity` (title AND body AND
  # state) against an authoritative `gh issue view` readback before trusting
  # a "match" candidate (Issue #2306 follow-up P1-c).
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
  # Prints a JSON object {"close_ok": true|false, "state": "..."|null} and
  # returns non-zero whenever cleanup did NOT authoritatively confirm the
  # post-close state as closed -- this includes both `gh issue close`
  # exiting non-zero AND the exit-0 case where the post-close readback state
  # is not "closed"/"CLOSED" (Issue #2306 follow-up P1-a: the exit code of
  # `gh issue close` alone is never sufficient).
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
  local result rc
  result=$(CLOSE_VERIFY_JSON="$json" python3 <<'CLOSE_VERIFY_PY_EOF'
import json
import os
import sys

data = json.loads(os.environ["CLOSE_VERIFY_JSON"])
state = data.get("state")
print(json.dumps({"close_ok": True, "state": state}))
sys.exit(0 if (state or "").lower() == "closed" else 1)
CLOSE_VERIFY_PY_EOF
)
  rc=$?
  printf '%s' "$result"
  return "$rc"
}

# --- AC4: cleanup handler (EXIT trap) ---------------------------------------
#
# Registered (see main()) BEFORE the launcher subprocess runs, so it fires
# on normal completion, on any early `exit`, and on INT/TERM (which are
# themselves wired to call `exit` so the same EXIT trap always fires --
# see the trap registrations at the bottom of main()). Idempotent via
# CLEANUP_DONE so INT/TERM followed by the script's own tail-end exit never
# double-runs cleanup.
#
# Issue #2306 follow-up P0/P1-a: this function is now the SOLE authority for
# both (a) which Issue number cleanup acts on (always RESOLVED_ISSUE_NUMBER,
# the identity-verified target -- never the raw, unverified
# CANARY_ISSUE_NUMBER) and (b) the final process exit code (explicit `exit`
# at the end, after disabling further EXIT trap re-entry via `trap - EXIT`,
# since bash does not propagate a trap handler's `return` value as the
# process exit status).

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
RESOLVED_ISSUE_STATE="null"
CLOSE_OK="false"
CLOSE_STATE="null"
CLOSE_VERIFIED="false"
FINAL_STATUS="failure"

cleanup_handler() {
  local original_rc=$?
  # Disable further EXIT trap re-entry immediately: this function ends with
  # an explicit `exit`, which would otherwise re-trigger the same trap.
  trap - EXIT

  if [ "$CLEANUP_DONE" = "true" ]; then
    if [ "$original_rc" = "130" ] || [ "$original_rc" = "143" ]; then
      exit "$original_rc"
    fi
    if [ "$FINAL_STATUS" = "success" ]; then
      exit 0
    fi
    exit 1
  fi
  CLEANUP_DONE=true

  # P0: close target is EXCLUSIVELY the identity-verified
  # RESOLVED_ISSUE_NUMBER. The raw, unverified CANARY_ISSUE_NUMBER parsed
  # from launcher stdout is never used as a close-target fallback.
  if [ "$RESOLVED_ISSUE_NUMBER" != "null" ]; then
    local close_json close_rc
    close_json=$(close_and_verify "$RESOLVED_ISSUE_NUMBER" "$CANARY_REPO" "claude-gpt live_issue_create_canary: disposable probe cleanup (${CANARY_TIMESTAMP})")
    close_rc=$?
    CLOSE_OK=$(printf '%s' "$close_json" | python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("close_ok", False)).lower())' 2>/dev/null || echo false)
    CLOSE_STATE=$(printf '%s' "$close_json" | python3 -c 'import json,sys; v=json.load(sys.stdin).get("state"); print(json.dumps(v))' 2>/dev/null || echo null)
    if [ "$close_rc" -eq 0 ]; then
      CLOSE_VERIFIED="true"
    else
      CLOSE_VERIFIED="false"
    fi
  fi

  if [ "$FINAL_STATUS" = "success" ] && { [ "$RESOLVED_ISSUE_NUMBER" = "null" ] || [ "$CLOSE_VERIFIED" != "true" ]; }; then
    FINAL_STATUS="failure"
  fi

  if [ -n "$EVIDENCE_FILE" ]; then
    printf '{"schema":"CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_RESULT_V1","status":"%s","generated_at":"%s","repo":"%s","canary_issue_number":%s,"launch_exit_code":%s,"identity_status":"%s","fallback_status":"%s","fallback_candidate_count":%s,"resolved_issue_number":%s,"resolved_issue_state":%s,"close_ok":%s,"close_state":%s,"close_verified":%s}\n' \
      "$FINAL_STATUS" "$CANARY_TIMESTAMP" "$CANARY_REPO" "${CANARY_ISSUE_NUMBER:-null}" "${LAUNCH_RC:-null}" \
      "$IDENTITY_STATUS" "$FALLBACK_STATUS" "$FALLBACK_CANDIDATE_COUNT" "$RESOLVED_ISSUE_NUMBER" "$RESOLVED_ISSUE_STATE" "$CLOSE_OK" "$CLOSE_STATE" "$CLOSE_VERIFIED" > "$EVIDENCE_FILE"
  fi

  if [ "$FINAL_STATUS" = "success" ]; then
    echo "PASS: live_issue_create_canary が disposable Issue #${RESOLVED_ISSUE_NUMBER} を作成/identity検証(OPEN)/close/close状態確認しました。証跡: ${EVIDENCE_FILE}" >&2
  else
    echo "FAIL: live_issue_create_canary が完走しませんでした（identity_status=${IDENTITY_STATUS} fallback_status=${FALLBACK_STATUS} resolved_issue_state=${RESOLVED_ISSUE_STATE} close_verified=${CLOSE_VERIFIED}）。証跡: ${EVIDENCE_FILE}" >&2
  fi

  if [ "$original_rc" = "130" ] || [ "$original_rc" = "143" ]; then
    exit "$original_rc"
  fi
  if [ "$FINAL_STATUS" = "success" ]; then
    exit 0
  fi
  exit 1
}

# --- AC3: resolve the actual disposable Issue number to act on -------------
#
# args: repo expected_title expected_body marker
# Sets IDENTITY_STATUS / FALLBACK_STATUS / FALLBACK_CANDIDATE_COUNT /
# RESOLVED_ISSUE_NUMBER / RESOLVED_ISSUE_STATE globals. Never sets
# RESOLVED_ISSUE_NUMBER unless an unambiguous identity match (title AND
# body, via an authoritative `gh issue view` readback) was established
# either directly (parsed number) or via fallback (single exact-title
# candidate, re-confirmed -- Issue #2306 follow-up P1-c). This is the
# CLEANUP-target identity resolution; it deliberately does NOT gate on
# state == open (an already-closed or wrong-body-but-title-matching
# candidate may still be resolved here so cleanup can act on it) -- PASS
# eligibility is a separate, stricter check performed by main().
resolve_target_issue_number() {
  local repo="$1" expected_title="$2" expected_body="$3" marker="$4"
  RESOLVED_ISSUE_STATE="null"

  if [ -n "$CANARY_ISSUE_NUMBER" ]; then
    local identity_json
    identity_json=$(verify_identity "$CANARY_ISSUE_NUMBER" "$repo" "$expected_title" "$expected_body")
    IDENTITY_STATUS=$(printf '%s' "$identity_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status","not_found"))' 2>/dev/null || echo not_found)
    if [ "$IDENTITY_STATUS" = "match" ]; then
      RESOLVED_ISSUE_NUMBER="$CANARY_ISSUE_NUMBER"
      RESOLVED_ISSUE_STATE=$(printf '%s' "$identity_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("state"); print(json.dumps(s.lower() if isinstance(s, str) else s))' 2>/dev/null || echo null)
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
    local fallback_number reverify_json reverify_status
    fallback_number=$(printf '%s' "$fallback_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["number"])')
    # Issue #2306 follow-up P1-c: fallback candidates are title-matched only
    # (gh issue list never filters on body) -- always re-confirm identity
    # (title AND body AND state) via a direct, authoritative `gh issue view`
    # readback before ever trusting this candidate as the cleanup target.
    reverify_json=$(verify_identity "$fallback_number" "$repo" "$expected_title" "$expected_body")
    reverify_status=$(printf '%s' "$reverify_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status","not_found"))' 2>/dev/null || echo not_found)
    FALLBACK_STATUS="$reverify_status"
    if [ "$reverify_status" = "match" ]; then
      RESOLVED_ISSUE_NUMBER="$fallback_number"
      RESOLVED_ISSUE_STATE=$(printf '%s' "$reverify_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("state"); print(json.dumps(s.lower() if isinstance(s, str) else s))' 2>/dev/null || echo null)
      return 0
    fi
    return 1
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
  # Issue #2306 Background「title の一意性不足」対応 + follow-up P2「evidence
  # filename が秒単位 timestamp のみで衝突しうる」対応: PID + 乱数を含めた
  # marker を、title 一意化とevidence filename 衝突回避の双方に使う（同秒複数
  # run でも title と証跡ファイルの両方が衝突しない）。
  CANARY_MARKER="$$-${RANDOM}${RANDOM}"
  EVIDENCE_FILE="${evidence_dir}/live-issue-create-canary-${CANARY_TIMESTAMP}-${CANARY_MARKER}.json"

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
(Issue #2299 AC8, Issue #2306). It will be closed immediately by the same run.

<!-- claude-gpt-live-canary-run:${CANARY_MARKER} -->"

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

  # Issue #2306 follow-up P1-b: cleanup-target resolution is always attempted,
  # regardless of LAUNCH_RC -- a launcher that exits non-zero AFTER already
  # creating the disposable Issue (e.g. crashes before printing the number)
  # must not leak it. Only the PASS/FAIL determination below is gated on
  # LAUNCH_RC == 0.
  resolve_target_issue_number "$CANARY_REPO" "$CANARY_TITLE" "$CANARY_BODY" "$CANARY_MARKER" || true

  # Issue #2306 follow-up P1-c: PASS requires launch success AND an
  # identity-verified target AND that target's state to be OPEN at
  # resolution time (an already-closed or otherwise-tainted match is a
  # cleanup concern, never a PASS).
  if [ "$LAUNCH_RC" -eq 0 ] && [ "$RESOLVED_ISSUE_NUMBER" != "null" ] && [ "$RESOLVED_ISSUE_STATE" = '"open"' ]; then
    FINAL_STATUS="success"
  fi

  # cleanup_handler runs via the EXIT trap registered above (also covers the
  # close + close-state-readback, and is the sole authority for the actual
  # final process exit code -- see Issue #2306 follow-up P1-a).
  if [ "$FINAL_STATUS" = "success" ]; then
    exit 0
  fi
  exit 1
}

if [ "${LIVE_ISSUE_CREATE_CANARY_TEST_SOURCE:-0}" != "1" ]; then
  main "$@"
fi

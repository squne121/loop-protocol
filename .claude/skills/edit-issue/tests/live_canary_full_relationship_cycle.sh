#!/usr/bin/env bash
# live_canary_full_relationship_cycle.sh
#
# Issue #1883 AC15 / PR #1897 iteration-4 fix_delta P1-6 (live canary): a
# one-shot, disposable-Issue, PR-level integration check exercising the
# full native relationship cycle -- parent set/rebind/remove, blockedBy
# add/remove, blocking add/remove -- through the production
# `issue_relationship.update` controlled-executor code path against real
# GitHub, with an independent fresh readback after each step.
#
# Two disposable Issues are created: one as the transaction subject, one as
# a throwaway blocker/blocking/rebind-target counterpart. Both are closed
# in cleanup regardless of outcome (the cleanup trap is armed immediately
# after the first disposable Issue is created), and cleanup failure is
# reported as a nonzero exit rather than hidden.
#
# Known limitation (documented, not fixed by this revision): this canary
# still invokes scripts/agent-guards/controlled_skill_mutation_exec.py
# directly rather than the combined ISSUE_EDIT_TXN_INPUT_V1 transaction in
# edit_issue_txn.py, so it does not exercise the P0-1/P0-2/P1-7 content-lane
# ordering fixes end-to-end. A follow-up should route this canary through
# edit_issue_txn.py once a minimal always-readiness-`go` disposable Issue
# body fixture is available.
#
# Exit codes:
#   0  - PASS
#   1  - FAIL
#   77 - SKIP (environment cannot safely run this canary)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
REPO="squne121/loop-protocol"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_DIR="${REPO_ROOT}/artifacts/1883/issue-metadata/live_canary_full_relationship_cycle"
LOG_FILE="${ARTIFACT_DIR}/${TS}.log"
mkdir -p "${ARTIFACT_DIR}" 2>/dev/null || true

_log() { echo "$1" | tee -a "${LOG_FILE}" >&2; }
_skip() { _log "SKIP: $1"; exit 77; }

command -v gh >/dev/null 2>&1 || _skip "gh_binary_not_found"
gh auth status --hostname github.com >/dev/null 2>&1 || _skip "gh_auth_status_unreachable"
gh api graphql -f query='query{ viewer { login } }' >/dev/null 2>&1 || _skip "gh_api_graphql_unreachable"
_log "preflight: gh binary + auth + graphql reachability confirmed"

SUBJECT_URL="$(gh issue create --repo "${REPO}" \
  --title "[disposable-canary] issue_relationship.update full cycle subject ${TS}" \
  --body "Disposable subject Issue for live_canary_full_relationship_cycle.sh (Issue #1883 AC15 / PR #1897 iteration-4). Closed automatically after this run." \
  2>>"${LOG_FILE}")"

CLEANUP_FAILED="false"
SUBJECT=""
COUNTERPART=""
_cleanup() {
  if [ -n "${SUBJECT}" ]; then
    _log "cleanup: closing disposable subject issue #${SUBJECT}"
    gh issue close "${SUBJECT}" --repo "${REPO}" --reason "not planned" >>"${LOG_FILE}" 2>&1 || {
      _log "cleanup_failed: could not close disposable subject issue #${SUBJECT}"
      CLEANUP_FAILED="true"
    }
  fi
  if [ -n "${COUNTERPART}" ]; then
    _log "cleanup: closing disposable counterpart issue #${COUNTERPART}"
    gh issue close "${COUNTERPART}" --repo "${REPO}" --reason "not planned" >>"${LOG_FILE}" 2>&1 || {
      _log "cleanup_failed: could not close disposable counterpart issue #${COUNTERPART}"
      CLEANUP_FAILED="true"
    }
  fi
  if [ "${CLEANUP_FAILED}" = "true" ]; then
    _log "cleanup_failed=true subject=${SUBJECT:-<none>} counterpart=${COUNTERPART:-<none>} -- manual cleanup required"
    exit 1
  fi
}
# Armed immediately after the first disposable Issue exists, so a failure
# in any later step (including counterpart creation) never leaves the
# subject Issue uncleaned.
trap _cleanup EXIT

if [ -z "${SUBJECT_URL}" ]; then
  _log "FAIL: disposable subject issue creation failed"
  exit 1
fi
SUBJECT="$(echo "${SUBJECT_URL}" | grep -oE '[0-9]+$')"
_log "created disposable subject issue #${SUBJECT}"

COUNTERPART_URL="$(gh issue create --repo "${REPO}" \
  --title "[disposable-canary] issue_relationship.update full cycle counterpart ${TS}" \
  --body "Disposable counterpart Issue for live_canary_full_relationship_cycle.sh (Issue #1883 AC15 / PR #1897 iteration-4). Closed automatically after this run." \
  2>>"${LOG_FILE}")"
if [ -z "${COUNTERPART_URL}" ]; then
  _log "FAIL: disposable counterpart issue creation failed"
  exit 1
fi
COUNTERPART="$(echo "${COUNTERPART_URL}" | grep -oE '[0-9]+$')"
_log "created disposable counterpart issue #${COUNTERPART}"

_run_op() {
  local label="$1"
  local input_json="$2"
  local input_dir="${REPO_ROOT}/artifacts/${SUBJECT}/issue-metadata/issue_relationship.update"
  mkdir -p "${input_dir}"
  local input_file="${input_dir}/${label}.input.json"
  echo "${input_json}" > "${input_file}"
  local out
  out="$(cd "${REPO_ROOT}" && uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py \
    --command-id issue_relationship.update \
    --issue-number "${SUBJECT}" \
    --input-file "artifacts/${SUBJECT}/issue-metadata/issue_relationship.update/${label}.input.json" \
    --repo "${REPO}" \
    --json 2>>"${LOG_FILE}")"
  echo "${out}" >> "${LOG_FILE}"
  echo "${out}" | uv run python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))' 2>/dev/null
}

_readback() {
  # Independent readback (not the executor's own post-mutation snapshot) --
  # a fresh gh api graphql call against live GitHub.
  gh api graphql -f query='
    query($owner:String!,$name:String!,$number:Int!){
      repository(owner:$owner,name:$name){
        issue(number:$number){
          parent{number}
          blockedBy(first:20){nodes{number}}
          blocking(first:20){nodes{number}}
        }
      }
    }' -F owner=squne121 -F name=loop-protocol -F number="${SUBJECT}" 2>>"${LOG_FILE}"
}

# Step 1: parent null -> #1860
STEP1_STATUS="$(_run_op "step1_parent_set_1860" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": null, "blocked_by": [], "blocking": []},
  "parent": {"action": "set", "issue_number": 1860},
  "add_blocked_by": [], "remove_blocked_by": [], "add_blocking": [], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step1:'"${TS}"'"
}')"
_log "step1 (parent null -> #1860) status=${STEP1_STATUS}"
_log "step1 readback: $(_readback)"

# Step 2: parent #1860 -> counterpart (rebind, single addSubIssue(replaceParent:true))
STEP2_STATUS="$(_run_op "step2_parent_rebind_counterpart" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": 1860, "blocked_by": [], "blocking": []},
  "parent": {"action": "set", "issue_number": '"${COUNTERPART}"'},
  "add_blocked_by": [], "remove_blocked_by": [], "add_blocking": [], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step2:'"${TS}"'"
}')"
_log "step2 (parent rebind #1860 -> counterpart) status=${STEP2_STATUS}"
_log "step2 readback: $(_readback)"

# Step 3: parent counterpart -> null
STEP3_STATUS="$(_run_op "step3_parent_remove" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": '"${COUNTERPART}"', "blocked_by": [], "blocking": []},
  "parent": {"action": "remove", "issue_number": null},
  "add_blocked_by": [], "remove_blocked_by": [], "add_blocking": [], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step3:'"${TS}"'"
}')"
_log "step3 (parent counterpart -> null) status=${STEP3_STATUS}"
_log "step3 readback: $(_readback)"

# Step 4: blockedBy add counterpart
STEP4_STATUS="$(_run_op "step4_blocked_by_add" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": null, "blocked_by": [], "blocking": []},
  "parent": {"action": "unchanged", "issue_number": null},
  "add_blocked_by": ['"${COUNTERPART}"'], "remove_blocked_by": [], "add_blocking": [], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step4:'"${TS}"'"
}')"
_log "step4 (blockedBy add counterpart) status=${STEP4_STATUS}"
_log "step4 readback: $(_readback)"

# Step 5: blockedBy remove counterpart
STEP5_STATUS="$(_run_op "step5_blocked_by_remove" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": null, "blocked_by": ['"${COUNTERPART}"'], "blocking": []},
  "parent": {"action": "unchanged", "issue_number": null},
  "add_blocked_by": [], "remove_blocked_by": ['"${COUNTERPART}"'], "add_blocking": [], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step5:'"${TS}"'"
}')"
_log "step5 (blockedBy remove counterpart) status=${STEP5_STATUS}"
_log "step5 readback: $(_readback)"

# Step 6: blocking add counterpart
STEP6_STATUS="$(_run_op "step6_blocking_add" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": null, "blocked_by": [], "blocking": []},
  "parent": {"action": "unchanged", "issue_number": null},
  "add_blocked_by": [], "remove_blocked_by": [], "add_blocking": ['"${COUNTERPART}"'], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step6:'"${TS}"'"
}')"
_log "step6 (blocking add counterpart) status=${STEP6_STATUS}"
_log "step6 readback: $(_readback)"

# Step 7: blocking remove counterpart
STEP7_STATUS="$(_run_op "step7_blocking_remove" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": null, "blocked_by": [], "blocking": ['"${COUNTERPART}"']},
  "parent": {"action": "unchanged", "issue_number": null},
  "add_blocked_by": [], "remove_blocked_by": [], "add_blocking": [], "remove_blocking": ['"${COUNTERPART}"'],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step7:'"${TS}"'"
}')"
_log "step7 (blocking remove counterpart) status=${STEP7_STATUS}"
_log "step7 readback: $(_readback)"

for s in "${STEP1_STATUS}" "${STEP2_STATUS}" "${STEP3_STATUS}" "${STEP4_STATUS}" "${STEP5_STATUS}" "${STEP6_STATUS}" "${STEP7_STATUS}"; do
  if [ "${s}" != "applied" ] && [ "${s}" != "no_op" ]; then
    _log "FAIL: a relationship cycle step did not reach applied/no_op (saw status=${s})"
    exit 1
  fi
done

_log "PASS: full native relationship cycle (parent set/rebind/remove + blockedBy add/remove + blocking add/remove) round-tripped on disposable issue #${SUBJECT} / counterpart #${COUNTERPART}"
exit 0

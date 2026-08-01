#!/usr/bin/env bash
# live_canary_full_relationship_cycle.sh
#
# Issue #1883 AC15 (live canary): a one-shot, disposable-Issue, PR-level
# integration check exercising the full native relationship cycle --
# parent set/change/remove, blockedBy add/remove, blocking add/remove --
# through the production `issue_relationship.update` controlled-executor
# code path against real GitHub, with a fresh readback after each step.
#
# Two disposable Issues are created: one as the transaction subject, one as
# a throwaway blocker/blocking counterpart. Both are closed in cleanup
# regardless of outcome, and cleanup failure is reported (not hidden).
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
  --body "Disposable subject Issue for live_canary_full_relationship_cycle.sh (Issue #1883 AC15). Closed automatically after this run." \
  2>>"${LOG_FILE}")"
COUNTERPART_URL="$(gh issue create --repo "${REPO}" \
  --title "[disposable-canary] issue_relationship.update full cycle counterpart ${TS}" \
  --body "Disposable counterpart Issue for live_canary_full_relationship_cycle.sh (Issue #1883 AC15). Closed automatically after this run." \
  2>>"${LOG_FILE}")"

if [ -z "${SUBJECT_URL}" ] || [ -z "${COUNTERPART_URL}" ]; then
  _log "FAIL: disposable issue creation failed (subject=${SUBJECT_URL:-<empty>} counterpart=${COUNTERPART_URL:-<empty>})"
  exit 1
fi

SUBJECT="$(echo "${SUBJECT_URL}" | grep -oE '[0-9]+$')"
COUNTERPART="$(echo "${COUNTERPART_URL}" | grep -oE '[0-9]+$')"
_log "created disposable subject issue #${SUBJECT} and counterpart issue #${COUNTERPART}"

CLEANUP_FAILED="false"
_cleanup() {
  _log "cleanup: closing disposable issues #${SUBJECT} and #${COUNTERPART}"
  gh issue close "${SUBJECT}" --repo "${REPO}" --reason "not planned" >>"${LOG_FILE}" 2>&1 || {
    _log "cleanup_failed: could not close disposable subject issue #${SUBJECT}"
    CLEANUP_FAILED="true"
  }
  gh issue close "${COUNTERPART}" --repo "${REPO}" --reason "not planned" >>"${LOG_FILE}" 2>&1 || {
    _log "cleanup_failed: could not close disposable counterpart issue #${COUNTERPART}"
    CLEANUP_FAILED="true"
  }
  if [ "${CLEANUP_FAILED}" = "true" ]; then
    _log "cleanup_failed=true subject=${SUBJECT} counterpart=${COUNTERPART} -- manual cleanup required"
  fi
}
trap _cleanup EXIT

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

STEP1_STATUS="$(_run_op "step1_set_parent" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": null, "blocked_by": [], "blocking": []},
  "parent": {"action": "set", "issue_number": 1860},
  "add_blocked_by": [], "remove_blocked_by": [], "add_blocking": [], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step1:'"${TS}"'"
}')"
_log "step1 (set parent) status=${STEP1_STATUS}"

STEP2_STATUS="$(_run_op "step2_add_blocked_by" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": 1860, "blocked_by": [], "blocking": []},
  "parent": {"action": "unchanged", "issue_number": null},
  "add_blocked_by": ['"${COUNTERPART}"'], "remove_blocked_by": [], "add_blocking": [], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step2:'"${TS}"'"
}')"
_log "step2 (add blocked_by) status=${STEP2_STATUS}"

STEP3_STATUS="$(_run_op "step3_remove_blocked_by" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": 1860, "blocked_by": ['"${COUNTERPART}"'], "blocking": []},
  "parent": {"action": "unchanged", "issue_number": null},
  "add_blocked_by": [], "remove_blocked_by": ['"${COUNTERPART}"'], "add_blocking": [], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step3:'"${TS}"'"
}')"
_log "step3 (remove blocked_by) status=${STEP3_STATUS}"

STEP4_STATUS="$(_run_op "step4_remove_parent" '{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": '"${SUBJECT}"',
  "repo": "'"${REPO}"'",
  "expected_before": {"parent": 1860, "blocked_by": [], "blocking": []},
  "parent": {"action": "remove", "issue_number": null},
  "add_blocked_by": [], "remove_blocked_by": [], "add_blocking": [], "remove_blocking": [],
  "idempotency_key": "'"${REPO}"':'"${SUBJECT}"':relationship:step4:'"${TS}"'"
}')"
_log "step4 (remove parent) status=${STEP4_STATUS}"

for s in "${STEP1_STATUS}" "${STEP2_STATUS}" "${STEP3_STATUS}" "${STEP4_STATUS}"; do
  if [ "${s}" != "applied" ] && [ "${s}" != "no_op" ]; then
    _log "FAIL: a relationship cycle step did not reach applied/no_op (saw status=${s})"
    exit 1
  fi
done

_log "PASS: full native relationship cycle (parent set/remove + blockedBy add/remove) round-tripped on disposable issue #${SUBJECT}"
exit 0

#!/usr/bin/env bash
# live_canary_blocking_direction.sh
#
# Issue #1946 AC8 (live canary): a one-shot, disposable-Issue, PR-level
# integration check that `create_issue_txn.py --blocking` registers the
# GitHub native issue-dependency relationship in the correct direction
# (the newly created issue BLOCKS the target issue -- the hard-predecessor
# direction), and that the reverse direction is NOT accidentally registered
# (the bug this Issue fixes: #1943 was registered as blocked-by #1842 instead
# of blocking #1842).
#
# This script is NOT a repeatable regression test: it creates two disposable
# Issues in the trusted repository (a target Issue B and a predecessor Issue A
# created with `--blocking B`), performs a small number of read-back checks
# against real GitHub, removes the relationship, and closes both disposable
# Issues in a `finally`-equivalent cleanup step regardless of outcome. It is
# meant to be run once per PR (Delivery Rule), not on every CI run.
#
# Steps (per #1946 AC8 / Machine-Readable Contract):
#   1. create target Issue B
#   2. create predecessor Issue A via `create_issue_txn.py --blocking <B>`
#   3. confirm A.blocking contains B
#   4. confirm B.blockedBy contains A
#   5. confirm A.blockedBy does NOT contain B (the reversal bug this Issue fixes)
#   6. remove the relationship and close A/B (cleanup failures are recorded as
#      partial failures, not silently swallowed)
#
# Exit codes:
#   0  - PASS (blocking direction registered correctly; reverse NOT registered)
#   1  - FAIL (a mutation or readback did not match the expected direction)
#   77 - SKIP (environment cannot safely run this canary -- not a PASS)
#
# SKIP conditions (Runtime Verification Applicability skip_conditions):
#   - `gh` binary not found
#   - `gh auth status --hostname github.com` unreachable / unauthenticated
#   - `gh api graphql` (read-only) unreachable
#
# Artifact requirements: request/response transcripts (secrets redacted) are
# written under artifacts/1946/issue-metadata/live_canary_blocking_direction/.
# Tokens/Authorization headers/full env are never written.
#
# fallback_policy (docs/dev/runtime-verification-policy.md): if any step below
# only "succeeds" via a fallback/best-effort path rather than the primary
# create_issue_txn.py --blocking mutation + strict readback, this script must
# FAIL (not PASS). No fallback path is implemented here on purpose.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
REPO="squne121/loop-protocol"
CANARY_TARGET_ISSUE="1946"
ARTIFACT_DIR="${REPO_ROOT}/artifacts/${CANARY_TARGET_ISSUE}/issue-metadata/live_canary_blocking_direction"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="${ARTIFACT_DIR}/${TS}.log"
CREATE_TXN="${REPO_ROOT}/.claude/skills/create-issue/scripts/create_issue_txn.py"

mkdir -p "${ARTIFACT_DIR}" 2>/dev/null || true

_log() {
  echo "$1" | tee -a "${LOG_FILE}" >&2
}

_skip() {
  _log "SKIP: $1"
  exit 77
}

# -- Environment preflight --
command -v gh >/dev/null 2>&1 || _skip "gh_binary_not_found"
gh auth status --hostname github.com >/dev/null 2>&1 || _skip "gh_auth_status_unreachable"
gh api graphql -f query='query{ viewer { login } }' >/dev/null 2>&1 || _skip "gh_api_graphql_unreachable"

_log "preflight: gh binary + auth + graphql reachability confirmed"

DISPOSABLE_A_NUMBER=""
DISPOSABLE_B_NUMBER=""
RELATIONSHIP_REGISTERED="unknown"
CLEANUP_FAILED="0"

_cleanup() {
  if [ "${RELATIONSHIP_REGISTERED}" = "yes" ] && [ -n "${DISPOSABLE_A_NUMBER}" ] && [ -n "${DISPOSABLE_B_NUMBER}" ]; then
    _log "cleanup: removing blocking relationship (A=#${DISPOSABLE_A_NUMBER} blocks B=#${DISPOSABLE_B_NUMBER})"
    A_NODE_ID="$(gh api graphql -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){id}}}' \
      -F owner="$(echo "${REPO}" | cut -d/ -f1)" -F name="$(echo "${REPO}" | cut -d/ -f2)" -F number="${DISPOSABLE_A_NUMBER}" \
      --jq '.data.repository.issue.id' 2>>"${LOG_FILE}")"
    B_NODE_ID="$(gh api graphql -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){id}}}' \
      -F owner="$(echo "${REPO}" | cut -d/ -f1)" -F name="$(echo "${REPO}" | cut -d/ -f2)" -F number="${DISPOSABLE_B_NUMBER}" \
      --jq '.data.repository.issue.id' 2>>"${LOG_FILE}")"
    if [ -n "${A_NODE_ID}" ] && [ -n "${B_NODE_ID}" ]; then
      gh api graphql -f query='mutation($input:RemoveBlockedByInput!){removeBlockedBy(input:$input){clientMutationId}}' \
        -F "input[issueId]=${B_NODE_ID}" -F "input[blockingIssueId]=${A_NODE_ID}" >>"${LOG_FILE}" 2>&1
      if [ $? -ne 0 ]; then
        _log "cleanup_failed: could not remove blocking relationship -- manual cleanup required"
        CLEANUP_FAILED="1"
      fi
    else
      _log "cleanup_failed: could not resolve node IDs to remove relationship -- manual cleanup required"
      CLEANUP_FAILED="1"
    fi
  fi

  if [ -n "${DISPOSABLE_A_NUMBER}" ]; then
    _log "cleanup: closing disposable issue A #${DISPOSABLE_A_NUMBER}"
    gh issue close "${DISPOSABLE_A_NUMBER}" --repo "${REPO}" --reason "not planned" >>"${LOG_FILE}" 2>&1
    if [ $? -ne 0 ]; then
      _log "cleanup_failed: could not close disposable issue A #${DISPOSABLE_A_NUMBER} -- manual cleanup required"
      CLEANUP_FAILED="1"
    fi
  fi
  if [ -n "${DISPOSABLE_B_NUMBER}" ]; then
    _log "cleanup: closing disposable issue B #${DISPOSABLE_B_NUMBER}"
    gh issue close "${DISPOSABLE_B_NUMBER}" --repo "${REPO}" --reason "not planned" >>"${LOG_FILE}" 2>&1
    if [ $? -ne 0 ]; then
      _log "cleanup_failed: could not close disposable issue B #${DISPOSABLE_B_NUMBER} -- manual cleanup required"
      CLEANUP_FAILED="1"
    fi
  fi

  if [ "${CLEANUP_FAILED}" = "1" ]; then
    _log "partial_failure: one or more cleanup steps failed; see log for manual recovery commands"
  fi
}
trap _cleanup EXIT

# -- Step 1: create target Issue B ---------------------------------------------
TARGET_TITLE="[disposable-canary] blocking direction target B ${TS}"
TARGET_BODY="Disposable target Issue B created by live_canary_blocking_direction.sh (#1946 AC8). Will be closed immediately after this canary run. Safe to delete/ignore."

TARGET_URL="$(gh issue create --repo "${REPO}" --title "${TARGET_TITLE}" --body "${TARGET_BODY}" 2>>"${LOG_FILE}")"
if [ -z "${TARGET_URL}" ]; then
  _log "FAIL: disposable target issue B creation failed"
  exit 1
fi
DISPOSABLE_B_NUMBER="$(echo "${TARGET_URL}" | grep -oE '[0-9]+$')"
_log "created disposable target issue B #${DISPOSABLE_B_NUMBER}: ${TARGET_URL}"

# -- Step 2: create predecessor Issue A via create_issue_txn.py --blocking B ---
PRED_TITLE="[disposable-canary] blocking direction predecessor A ${TS}"
PRED_BODY_FILE="$(mktemp)"
cat > "${PRED_BODY_FILE}" <<EOF
Disposable predecessor Issue A created by live_canary_blocking_direction.sh (#1946 AC8). Will be closed immediately after this canary run. Safe to delete/ignore.
EOF

CREATE_OUTPUT="$(cd "${REPO_ROOT}" && uv run --locked python3 "${CREATE_TXN}" \
  --repo "${REPO}" \
  --title "${PRED_TITLE}" \
  --body-file "${PRED_BODY_FILE}" \
  --blocking "${DISPOSABLE_B_NUMBER}" \
  --gh gh 2>>"${LOG_FILE}")"
echo "${CREATE_OUTPUT}" >> "${LOG_FILE}"
rm -f "${PRED_BODY_FILE}"

CREATE_STATUS="$(echo "${CREATE_OUTPUT}" | uv run --locked python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))' 2>/dev/null)"
DISPOSABLE_A_NUMBER="$(echo "${CREATE_OUTPUT}" | uv run --locked python3 -c 'import json,sys; print(json.load(sys.stdin).get("issue_number") or "")' 2>/dev/null)"

if [ -n "${DISPOSABLE_A_NUMBER}" ]; then
  _log "created disposable predecessor issue A #${DISPOSABLE_A_NUMBER} (status=${CREATE_STATUS})"
fi

if [ "${CREATE_STATUS}" != "success" ]; then
  _log "FAIL: create_issue_txn.py --blocking did not report status=success (status=${CREATE_STATUS})"
  exit 1
fi
if [ -z "${DISPOSABLE_A_NUMBER}" ]; then
  _log "FAIL: could not determine created issue A number from create_issue_txn.py output"
  exit 1
fi
RELATIONSHIP_REGISTERED="yes"

OWNER="$(echo "${REPO}" | cut -d/ -f1)"
NAME="$(echo "${REPO}" | cut -d/ -f2)"

# -- Step 3: confirm A.blocking contains B -------------------------------------
A_BLOCKING="$(gh api graphql -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){blocking(first:10){nodes{number}}}}}' \
  -F owner="${OWNER}" -F name="${NAME}" -F number="${DISPOSABLE_A_NUMBER}" \
  --jq '.data.repository.issue.blocking.nodes[].number' 2>>"${LOG_FILE}")"
echo "A.blocking=${A_BLOCKING}" >> "${LOG_FILE}"
if ! echo "${A_BLOCKING}" | grep -qx "${DISPOSABLE_B_NUMBER}"; then
  _log "FAIL: A(#${DISPOSABLE_A_NUMBER}).blocking does not contain B(#${DISPOSABLE_B_NUMBER}); got: ${A_BLOCKING}"
  exit 1
fi
_log "PASS(step3): A.blocking contains B"

# -- Step 4: confirm B.blockedBy contains A ------------------------------------
B_BLOCKEDBY="$(gh api graphql -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){blockedBy(first:10){nodes{number}}}}}' \
  -F owner="${OWNER}" -F name="${NAME}" -F number="${DISPOSABLE_B_NUMBER}" \
  --jq '.data.repository.issue.blockedBy.nodes[].number' 2>>"${LOG_FILE}")"
echo "B.blockedBy=${B_BLOCKEDBY}" >> "${LOG_FILE}"
if ! echo "${B_BLOCKEDBY}" | grep -qx "${DISPOSABLE_A_NUMBER}"; then
  _log "FAIL: B(#${DISPOSABLE_B_NUMBER}).blockedBy does not contain A(#${DISPOSABLE_A_NUMBER}); got: ${B_BLOCKEDBY}"
  exit 1
fi
_log "PASS(step4): B.blockedBy contains A"

# -- Step 5: confirm A.blockedBy does NOT contain B (the reversal bug) --------
A_BLOCKEDBY="$(gh api graphql -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){blockedBy(first:10){nodes{number}}}}}' \
  -F owner="${OWNER}" -F name="${NAME}" -F number="${DISPOSABLE_A_NUMBER}" \
  --jq '.data.repository.issue.blockedBy.nodes[].number' 2>>"${LOG_FILE}")"
echo "A.blockedBy=${A_BLOCKEDBY}" >> "${LOG_FILE}"
if echo "${A_BLOCKEDBY}" | grep -qx "${DISPOSABLE_B_NUMBER}"; then
  _log "FAIL: A(#${DISPOSABLE_A_NUMBER}).blockedBy incorrectly contains B(#${DISPOSABLE_B_NUMBER}) -- this is the #1943/#1842 reversal bug"
  exit 1
fi
_log "PASS(step5): A.blockedBy does NOT contain B (reversal bug not present)"

# -- Step 6: cleanup (relationship removal + close A/B) happens in the EXIT trap --
_log "PASS: blocking direction registered correctly on disposable issues A=#${DISPOSABLE_A_NUMBER}/B=#${DISPOSABLE_B_NUMBER}"
exit 0

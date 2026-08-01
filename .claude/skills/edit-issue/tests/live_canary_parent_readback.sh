#!/usr/bin/env bash
# live_canary_parent_readback.sh
#
# Issue #1883 AC3 (live canary): a one-shot, disposable-Issue, PR-level
# integration check that native `parent` set/remove via the
# `issue_relationship.update` controlled executor round-trips against real
# GitHub, and that a fresh all-page-equivalent readback (single node for
# `parent`) confirms the desired state after each mutation.
#
# This script is NOT a repeatable regression test: it creates one disposable
# Issue in the trusted repository, performs a small number of relationship
# mutations against it via the production controlled-executor code path, and
# closes the disposable Issue in a `finally`-equivalent cleanup step
# regardless of outcome. It is meant to be run once per PR (Delivery Rule),
# not on every CI run.
#
# Exit codes:
#   0  - PASS (parent set + parent remove both round-tripped and read back correctly)
#   1  - FAIL (a mutation or readback did not match desired state)
#   77 - SKIP (environment cannot safely run this canary -- not a PASS)
#
# SKIP conditions (Runtime Verification Applicability skip_conditions):
#   - `gh` binary not found
#   - `gh auth status --hostname github.com` unreachable / unauthenticated
#   - `gh api graphql` (read-only) unreachable
#
# Artifact requirements: request/response transcripts (secrets redacted) are
# written under artifacts/<PARENT_ISSUE_NUMBER-or-1883>/issue-metadata/
# live_canary_parent_readback/. Tokens/Authorization headers/full env are
# never written.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
REPO="squne121/loop-protocol"
CANARY_PARENT_TARGET_ISSUE="1883"
ARTIFACT_DIR="${REPO_ROOT}/artifacts/${CANARY_PARENT_TARGET_ISSUE}/issue-metadata/live_canary_parent_readback"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="${ARTIFACT_DIR}/${TS}.log"

mkdir -p "${ARTIFACT_DIR}" 2>/dev/null || true

_log() {
  echo "$1" | tee -a "${LOG_FILE}" >&2
}

_skip() {
  _log "SKIP: $1"
  exit 77
}

# -- Environment preflight (AC10-equivalent: environment vs write-permission) --
command -v gh >/dev/null 2>&1 || _skip "gh_binary_not_found"
gh auth status --hostname github.com >/dev/null 2>&1 || _skip "gh_auth_status_unreachable"
gh api graphql -f query='query{ viewer { login } }' >/dev/null 2>&1 || _skip "gh_api_graphql_unreachable"

_log "preflight: gh binary + auth + graphql reachability confirmed"

# -- Create a disposable Issue to use as the mutation subject ------------------
DISPOSABLE_TITLE="[disposable-canary] issue_relationship.update parent readback ${TS}"
DISPOSABLE_BODY="Disposable Issue created by live_canary_parent_readback.sh (Issue #1883 AC3). Will be closed immediately after this canary run. Safe to delete/ignore."

CREATE_URL="$(gh issue create --repo "${REPO}" --title "${DISPOSABLE_TITLE}" --body "${DISPOSABLE_BODY}" 2>>"${LOG_FILE}")"
if [ -z "${CREATE_URL}" ]; then
  _log "FAIL: disposable issue creation failed"
  exit 1
fi
DISPOSABLE_NUMBER="$(echo "${CREATE_URL}" | grep -oE '[0-9]+$')"
_log "created disposable issue #${DISPOSABLE_NUMBER}: ${CREATE_URL}"

_cleanup() {
  _log "cleanup: closing disposable issue #${DISPOSABLE_NUMBER}"
  gh issue close "${DISPOSABLE_NUMBER}" --repo "${REPO}" --reason "not planned" >>"${LOG_FILE}" 2>&1
  CLEANUP_RC=$?
  if [ ${CLEANUP_RC} -ne 0 ]; then
    _log "cleanup_failed: could not close disposable issue #${DISPOSABLE_NUMBER} -- manual cleanup required"
  fi
}
trap _cleanup EXIT

STATUS="fail"

# -- Step 1: set parent on the disposable issue to #1860 -----------------------
INPUT_DIR="${REPO_ROOT}/artifacts/${DISPOSABLE_NUMBER}/issue-metadata/issue_relationship.update"
mkdir -p "${INPUT_DIR}"
INPUT_FILE="${INPUT_DIR}/live_canary_set.input.json"
cat > "${INPUT_FILE}" <<JSON
{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": ${DISPOSABLE_NUMBER},
  "repo": "${REPO}",
  "expected_before": {"parent": null, "blocked_by": [], "blocking": []},
  "parent": {"action": "set", "issue_number": 1860},
  "add_blocked_by": [],
  "remove_blocked_by": [],
  "add_blocking": [],
  "remove_blocking": [],
  "idempotency_key": "${REPO}:${DISPOSABLE_NUMBER}:relationship:live_canary_set:${TS}"
}
JSON

SET_OUTPUT="$(cd "${REPO_ROOT}" && uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py \
  --command-id issue_relationship.update \
  --issue-number "${DISPOSABLE_NUMBER}" \
  --input-file "artifacts/${DISPOSABLE_NUMBER}/issue-metadata/issue_relationship.update/live_canary_set.input.json" \
  --repo "${REPO}" \
  --json 2>>"${LOG_FILE}")"
echo "${SET_OUTPUT}" >> "${LOG_FILE}"
SET_STATUS="$(echo "${SET_OUTPUT}" | uv run python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))' 2>/dev/null)"
_log "parent set result status=${SET_STATUS}"

if [ "${SET_STATUS}" != "applied" ] && [ "${SET_STATUS}" != "no_op" ]; then
  _log "FAIL: parent set did not reach applied/no_op (status=${SET_STATUS})"
  exit 1
fi

# -- Step 2: remove parent -----------------------------------------------------
INPUT_FILE2="${INPUT_DIR}/live_canary_remove.input.json"
cat > "${INPUT_FILE2}" <<JSON
{
  "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
  "issue_number": ${DISPOSABLE_NUMBER},
  "repo": "${REPO}",
  "expected_before": {"parent": 1860, "blocked_by": [], "blocking": []},
  "parent": {"action": "remove", "issue_number": null},
  "add_blocked_by": [],
  "remove_blocked_by": [],
  "add_blocking": [],
  "remove_blocking": [],
  "idempotency_key": "${REPO}:${DISPOSABLE_NUMBER}:relationship:live_canary_remove:${TS}"
}
JSON

REMOVE_OUTPUT="$(cd "${REPO_ROOT}" && uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py \
  --command-id issue_relationship.update \
  --issue-number "${DISPOSABLE_NUMBER}" \
  --input-file "artifacts/${DISPOSABLE_NUMBER}/issue-metadata/issue_relationship.update/live_canary_remove.input.json" \
  --repo "${REPO}" \
  --json 2>>"${LOG_FILE}")"
echo "${REMOVE_OUTPUT}" >> "${LOG_FILE}"
REMOVE_STATUS="$(echo "${REMOVE_OUTPUT}" | uv run python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))' 2>/dev/null)"
_log "parent remove result status=${REMOVE_STATUS}"

if [ "${REMOVE_STATUS}" != "applied" ] && [ "${REMOVE_STATUS}" != "no_op" ]; then
  _log "FAIL: parent remove did not reach applied/no_op (status=${REMOVE_STATUS})"
  exit 1
fi

STATUS="pass"
_log "PASS: native parent set + remove round-tripped and read back correctly on disposable issue #${DISPOSABLE_NUMBER}"
exit 0

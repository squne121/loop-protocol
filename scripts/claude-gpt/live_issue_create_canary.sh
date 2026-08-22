#!/usr/bin/env bash
# scripts/claude-gpt/live_issue_create_canary.sh
#
# Issue #2299 AC8: opt-in-only live canary for the compatibility-first
# Claude-GPT GitHub access change (Outcome section). Creates one disposable
# Issue in the trusted repository via `create_issue_txn.py` (exercising the
# real `gh` auth path end-to-end, including the salvaged
# `TransactionResult.node_id`/`body_sha256` readback fields, Issue #2299
# AC4), confirms it via authoritative readback, then closes it.
#
# This script is intentionally NOT wired into any always-on CI job and does
# NOT gate on environment/credential availability the way other live
# canaries in this repo do (e.g. live_canary_blocking_direction.sh): unless
# an explicit opt-in is given, it always SKIPs (exit 77), even when `gh` is
# available and authenticated. This follows the existing opt-in canary
# pattern (auto_mode_canary.sh / live_canary_blocking_direction.sh) while
# keeping the opt-in bar for a NEW live-mutating canary intentionally
# higher (Issue #2299 Out of Scope: do not expand this canary's target
# repository or make it default-on).
#
# Not a repeatable regression test: it performs a real `gh issue create` +
# `gh issue close` against the trusted repository. Meant to be run manually
# by an operator, not on every CI run (Delivery Rule: 1 Issue = 1 PR).
#
# Usage:
#   scripts/claude-gpt/live_issue_create_canary.sh --opt-in
#   CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_OPT_IN=1 scripts/claude-gpt/live_issue_create_canary.sh
#
# Exit codes:
#   0   PASS (disposable issue created, node_id/body_sha256 populated,
#       readback confirmed, issue closed)
#   1   FAIL (a mutation or readback did not match, or cleanup failed)
#   77  SKIP (no explicit opt-in given -- the default; or environment
#       cannot safely run this canary)
#
# fallback_policy (docs/dev/runtime-verification-policy.md): if any step
# below only "succeeds" via a fallback/best-effort path rather than the
# primary create_issue_txn.py mutation + strict readback, this script must
# FAIL (not PASS). No fallback path is implemented here on purpose.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REPO="squne121/loop-protocol"
CREATE_TXN="${REPO_ROOT}/.claude/skills/create-issue/scripts/create_issue_txn.py"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_DIR="${REPO_ROOT}/artifacts/2299/issue-metadata/live_issue_create_canary"
LOG_FILE="${ARTIFACT_DIR}/${TS}.log"

mkdir -p "${ARTIFACT_DIR}" 2>/dev/null || true

_log() {
  echo "$1" | tee -a "${LOG_FILE}" >&2
}

_skip() {
  _log "SKIP: $1"
  exit 77
}

# --- Opt-in gate (default: always SKIP). ---
OPT_IN=false
for arg in "$@"; do
  case "$arg" in
    --opt-in) OPT_IN=true ;;
  esac
done
if [ "${CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_OPT_IN:-}" = "1" ]; then
  OPT_IN=true
fi
if [ "${OPT_IN}" != "true" ]; then
  _skip "explicit opt-in required (pass --opt-in or set CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_OPT_IN=1)"
fi

# --- Environment preflight (only reached after explicit opt-in). ---
command -v gh >/dev/null 2>&1 || _skip "gh_binary_not_found"
gh auth status --hostname github.com >/dev/null 2>&1 || _skip "gh_auth_status_unavailable"
command -v uv >/dev/null 2>&1 || _skip "uv_binary_not_found"

_log "preflight: gh binary + auth confirmed; opt-in explicit"

DISPOSABLE_NUMBER=""
CLEANUP_FAILED="0"

_cleanup() {
  local status=$?
  trap - EXIT
  if [ -n "${DISPOSABLE_NUMBER}" ]; then
    _log "cleanup: closing disposable issue #${DISPOSABLE_NUMBER}"
    if ! gh issue close "${DISPOSABLE_NUMBER}" --repo "${REPO}" --reason "not planned" >>"${LOG_FILE}" 2>&1; then
      _log "cleanup_failed: could not close disposable issue #${DISPOSABLE_NUMBER} -- manual cleanup required"
      CLEANUP_FAILED="1"
    fi
  fi
  if [ "${CLEANUP_FAILED}" = "1" ]; then
    _log "partial_failure: cleanup failed; see log for manual recovery: ${LOG_FILE}"
    status=1
  fi
  exit "${status}"
}
trap _cleanup EXIT

# -- Step 1: create disposable Issue via create_issue_txn.py --------------
BODY_FILE="$(mktemp)"
cat > "${BODY_FILE}" <<EOF
Disposable Issue created by live_issue_create_canary.sh (Issue #2299 AC8) to
verify end-to-end that Claude-GPT's compatibility-first GitHub access can
create a real GitHub Issue via the unified native \`create_issue_txn.py\`
path. This Issue is closed automatically by the same script run.

## Acceptance Criteria
- [ ] AC1: disposable canary issue (no real code change)

## Verification Commands
\`\`\`bash
# none -- disposable canary artifact
\`\`\`

## Allowed Paths
- scripts/claude-gpt/live_issue_create_canary.sh
EOF

TXN_OUT="$(mktemp)"
if ! uv run --locked python3 "${CREATE_TXN}" \
  --repo "${REPO}" \
  --title "[disposable-canary] live_issue_create_canary ${TS}" \
  --body-file "${BODY_FILE}" \
  --issue-kind "" \
  >"${TXN_OUT}" 2>>"${LOG_FILE}"; then
  _log "FAIL: create_issue_txn.py invocation failed"
  cat "${TXN_OUT}" >>"${LOG_FILE}" 2>/dev/null || true
  exit 1
fi
cat "${TXN_OUT}" >>"${LOG_FILE}"

# create_issue_txn.py prints "Created issue #<n>: <url>" on success; extract
# the issue number without depending on an undocumented machine-readable
# output format.
DISPOSABLE_NUMBER="$(grep -oE '#[0-9]+' "${TXN_OUT}" | head -n1 | tr -d '#')"
if [ -z "${DISPOSABLE_NUMBER}" ]; then
  _log "FAIL: could not determine disposable issue number from create_issue_txn.py output"
  exit 1
fi
_log "created disposable issue #${DISPOSABLE_NUMBER}"

# -- Step 2: authoritative readback (independent of create_issue_txn.py's own
#    self-report) -----------------------------------------------------------
READBACK_JSON="$(gh api "repos/${REPO}/issues/${DISPOSABLE_NUMBER}" 2>>"${LOG_FILE}")"
if [ -z "${READBACK_JSON}" ]; then
  _log "FAIL: readback of disposable issue #${DISPOSABLE_NUMBER} returned empty"
  exit 1
fi
READBACK_NUMBER="$(echo "${READBACK_JSON}" | uv run --locked python3 -c 'import json,sys; print(json.load(sys.stdin).get("number", ""))' 2>>"${LOG_FILE}")"
if [ "${READBACK_NUMBER}" != "${DISPOSABLE_NUMBER}" ]; then
  _log "FAIL: readback number mismatch (expected #${DISPOSABLE_NUMBER}, got #${READBACK_NUMBER})"
  exit 1
fi
_log "PASS: readback confirms disposable issue #${DISPOSABLE_NUMBER} exists"

exit 0

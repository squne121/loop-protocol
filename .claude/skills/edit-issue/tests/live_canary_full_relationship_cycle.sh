#!/usr/bin/env bash
# live_canary_full_relationship_cycle.sh
#
# Issue #1917 (follow-up to Issue #1883 AC15 / PR #1897 iteration-4 fix_delta
# P1-6): a one-shot, disposable-Issue, PR-level integration check exercising
# the full native relationship cycle -- parent set/rebind/remove, blockedBy
# add/remove, blocking add/remove -- through the production
# `edit_issue_txn.py` combined transaction (ISSUE_EDIT_TXN_INPUT_V1:
# title_update + new_body_file + native_relationships + comment_mode all in
# one transaction) against real GitHub, with an independent fresh readback
# after each step.
#
# Three disposable Issues are created (S = transaction subject, P1 = first
# parent, P2 = second parent / blockedBy / blocking counterpart). Existing
# Issue #1860 is never used as a mutation target. All created disposable
# Issues are closed in cleanup regardless of outcome (the cleanup trap is
# armed immediately after the first disposable Issue is created), and
# cleanup failure is reported as a nonzero exit rather than hidden. PASS is
# only printed after every step assertion AND cleanup have both succeeded --
# there is no "log PASS, then clean up via an EXIT trap" ordering here.
#
# Role split summary: this live canary verifies, against real GitHub, that
# the edit_issue_txn.py combined transaction actually lands real title/body/
# native-relationship changes and that an independent readback agrees;
# internal ordering guarantees (candidate validated before native mutation,
# combined final readback after content mutation, native mutation's
# updatedAt feeding the content-lane precondition) are the responsibility of
# 既存の決定論的テストとの役割分担 -- see test_edit_issue_txn.py, which exercises
# those orderings with fully mocked child processes and does not touch
# real GitHub.
#
# Exit codes:
#   0  - PASS (all 7 steps + cleanup succeeded)
#   1  - FAIL (fixture defect, transaction failure, readback mismatch,
#              cleanup failure -- never rounded to 77)
#   77 - SKIP (environment cannot safely even attempt this canary -- detected
#              strictly before any disposable Issue is created)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
REPO="squne121/loop-protocol"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RELATIVE_ARTIFACT_DIR="artifacts/1917/issue-metadata/live_canary_full_relationship_cycle"
ARTIFACT_DIR="${REPO_ROOT}/${RELATIVE_ARTIFACT_DIR}"
LOG_FILE="${ARTIFACT_DIR}/${TS}.log"
EDIT_TXN_SCRIPT="${REPO_ROOT}/.claude/skills/edit-issue/scripts/edit_issue_txn.py"
READINESS_SCRIPT_PATH="${REPO_ROOT}/.claude/skills/issue-contract-review/scripts/contract_readiness_check.py"

# Issue #1917 fix_delta P1: run-local isolation. RUN_DIR / RELATIVE_RUN_DIR
# are populated by _init_run_dir (called once environment preflight passes,
# or explicitly by the offline regression test harness) so every canary
# execution -- and every offline test scenario that sources this script --
# gets its own uniquely named directory (created with `mktemp -d`, a real
# collision-free directory, not a lock file) under which ALL run-local
# artifacts (body files, transaction input JSON, run log) live. This
# replaces the previous role+step-only path
# (`${RELATIVE_ARTIFACT_DIR}/${role}-${marker}.body.md`), which let
# concurrent or repeated runs silently overwrite each other's body fixtures.
# LIVE_CANARY_ARTIFACT_ROOT lets the offline regression test point its run
# directories at a location distinct from where real (non-test) canary
# executions create theirs, so a test run can never read or clobber a real
# live run's artifacts, and vice versa.
RUN_DIR=""
RELATIVE_RUN_DIR=""

mkdir -p "${ARTIFACT_DIR}" 2>/dev/null || true

_log() {
  echo "$1" | tee -a "${LOG_FILE}" >&2
}

_skip() {
  _log "SKIP: $1"
  exit 77
}

# Idempotent: a second call once RUN_DIR already refers to a real directory
# is a no-op. Must run before any function that stages a body / txn-input
# fixture (_write_body_file, _build_txn_input) -- those fail loudly instead
# of silently falling back to a shared/root path when RELATIVE_RUN_DIR is
# still empty. LIVE_CANARY_ARTIFACT_ROOT (if set) overrides the base
# directory mktemp creates the run directory under; the production default
# is ARTIFACT_DIR.
_init_run_dir() {
  if [ -n "${RUN_DIR}" ] && [ -d "${RUN_DIR}" ]; then
    return 0
  fi
  local base="${LIVE_CANARY_ARTIFACT_ROOT:-${ARTIFACT_DIR}}"
  mkdir -p "${base}" 2>/dev/null || true
  RUN_DIR="$(mktemp -d "${base}/run-XXXXXXXX" 2>/dev/null || true)"
  if [ -z "${RUN_DIR}" ] || [ ! -d "${RUN_DIR}" ]; then
    _log "FAIL: could not create a unique run directory under ${base}"
    return 1
  fi
  RELATIVE_RUN_DIR="${RUN_DIR#"${REPO_ROOT}"/}"
  if [ "${RELATIVE_RUN_DIR}" = "${RUN_DIR}" ]; then
    _log "FAIL: run directory ${RUN_DIR} is not inside repo root ${REPO_ROOT}"
    return 1
  fi
  LOG_FILE="${RUN_DIR}/run.log"
  _log "run_dir: ${RUN_DIR}"
  return 0
}

# ---------------------------------------------------------------------------
# Pure / deterministic helpers -- no `gh`, no network, no subprocess to
# production scripts except local-only JSON assembly via `uv run python3`.
# These are exercised directly (without any fakes) by the offline regression
# test (test_live_canary_full_relationship_cycle.py).
# ---------------------------------------------------------------------------

_render_step_title() {
  local role="$1" run_id="$2" marker="$3"
  echo "[disposable-canary] ${role} full relationship cycle ${run_id} ${marker}"
}

_render_step_body() {
  local role="$1" run_id="$2" marker="$3"
  local template
  template="$(cat <<'BODY_EOF'
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: live_canary_full_relationship_cycle disposable fixture (__ROLE__)
change_kind: workflow
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Summary

Disposable canary fixture Issue (__ROLE__) for live_canary_full_relationship_cycle.sh. run=__RUN_ID__ marker=__MARKER__.

## Goal

Exercise edit_issue_txn.py's combined ISSUE_EDIT_TXN_INPUT_V1 transaction (title_update + new_body_file + native_relationships + comment_mode:skip) against a disposable Issue.

## Desired Destination

Closed automatically by this canary's cleanup phase regardless of outcome.

## Current Validated Scope

live_canary_full_relationship_cycle.sh combined-transaction relationship cycle coverage for role __ROLE__.

## Decisions Fixed

- run=__RUN_ID__: disposable fixture content is regenerated deterministically per step (__MARKER__).

## Quality Decision Record

- Status: N/A (disposable canary fixture, not a real issue contract)

## Parent Closure Rule

- Close immediately after this canary run completes (best effort, all-created-issues cleanup).

## Child Issues

- [ ] none (disposable fixture; no real children)

## Remaining Parent Gaps

- [ ] none

## Phase Handoff Contract

- N/A (disposable canary fixture)

## Acceptance Criteria

- [ ] AC1: this disposable Issue is closed by live_canary_full_relationship_cycle.sh cleanup after the run.
BODY_EOF
)"
  template="${template//__ROLE__/${role}}"
  template="${template//__RUN_ID__/${run_id}}"
  template="${template//__MARKER__/${marker}}"
  printf '%s\n' "${template}"
}

_write_body_file() {
  local role="$1" run_id="$2" marker="$3"
  if [ -z "${RELATIVE_RUN_DIR}" ]; then
    _log "FAIL: _write_body_file called before _init_run_dir (no run directory)"
    return 1
  fi
  local rel="${RELATIVE_RUN_DIR}/${role}-${marker}.body.md"
  local abs="${REPO_ROOT}/${rel}"
  mkdir -p "$(dirname "${abs}")"
  _render_step_body "${role}" "${run_id}" "${marker}" > "${abs}"
  echo "${rel}"
}

_sha256_of_file() {
  local path="$1"
  uv run --locked python3 -c '
import hashlib, sys
data = open(sys.argv[1], "r", encoding="utf-8").read()
print("sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest())
' "${path}"
}

# Fixed native relationship transition table (Issue #1917 Current Validated
# Scope): step1 S.parent null->P1, step2 P1->P2, step3 P2->null, step4
# S.blockedBy []->[P2], step5 [P2]->[], step6 S.blocking []->[P2], step7
# [P2]->[]. expected_before is always this canary's own tracked belief about
# live state, never derived from a prior transaction's `desired` field.
_native_relationships_json() {
  local step="$1" p1="$2" p2="$3"
  case "${step}" in
    1) printf '{"expected_before":{"parent":null,"blocked_by":[],"blocking":[]},"parent":{"action":"set","issue_number":%s},"add_blocked_by":[],"remove_blocked_by":[],"add_blocking":[],"remove_blocking":[]}' "${p1}" ;;
    2) printf '{"expected_before":{"parent":%s,"blocked_by":[],"blocking":[]},"parent":{"action":"set","issue_number":%s},"add_blocked_by":[],"remove_blocked_by":[],"add_blocking":[],"remove_blocking":[]}' "${p1}" "${p2}" ;;
    3) printf '{"expected_before":{"parent":%s,"blocked_by":[],"blocking":[]},"parent":{"action":"remove","issue_number":null},"add_blocked_by":[],"remove_blocked_by":[],"add_blocking":[],"remove_blocking":[]}' "${p2}" ;;
    4) printf '{"expected_before":{"parent":null,"blocked_by":[],"blocking":[]},"parent":{"action":"unchanged","issue_number":null},"add_blocked_by":[%s],"remove_blocked_by":[],"add_blocking":[],"remove_blocking":[]}' "${p2}" ;;
    5) printf '{"expected_before":{"parent":null,"blocked_by":[%s],"blocking":[]},"parent":{"action":"unchanged","issue_number":null},"add_blocked_by":[],"remove_blocked_by":[%s],"add_blocking":[],"remove_blocking":[]}' "${p2}" "${p2}" ;;
    6) printf '{"expected_before":{"parent":null,"blocked_by":[],"blocking":[]},"parent":{"action":"unchanged","issue_number":null},"add_blocked_by":[],"remove_blocked_by":[],"add_blocking":[%s],"remove_blocking":[]}' "${p2}" ;;
    7) printf '{"expected_before":{"parent":null,"blocked_by":[],"blocking":[%s]},"parent":{"action":"unchanged","issue_number":null},"add_blocked_by":[],"remove_blocked_by":[],"add_blocking":[],"remove_blocking":[%s]}' "${p2}" "${p2}" ;;
    *) return 1 ;;
  esac
}

# Canary's own independent expectation table for post-step state -- never
# read back from the transaction's own `desired` field or from the
# post-mutation readback itself (Issue #1917 AC3).
_expected_after_snapshot() {
  local step="$1" p1="$2" p2="$3"
  case "${step}" in
    1) printf '{"parent":%s,"blocked_by":[],"blocking":[]}' "${p1}" ;;
    2) printf '{"parent":%s,"blocked_by":[],"blocking":[]}' "${p2}" ;;
    3) printf '{"parent":null,"blocked_by":[],"blocking":[]}' ;;
    4) printf '{"parent":null,"blocked_by":[%s],"blocking":[]}' "${p2}" ;;
    5) printf '{"parent":null,"blocked_by":[],"blocking":[]}' ;;
    6) printf '{"parent":null,"blocked_by":[],"blocking":[%s]}' "${p2}" ;;
    7) printf '{"parent":null,"blocked_by":[],"blocking":[]}' ;;
    *) return 1 ;;
  esac
}

# Assembles an ISSUE_EDIT_TXN_INPUT_V1 JSON document (title_update +
# new_body_file + native_relationships + comment_mode:skip all in the SAME
# transaction -- Issue #1917 AC1) at ${out_rel_file} (repo-relative). Reads
# the readiness_result produced by contract_readiness_check.py and narrows
# it to the closed key set edit_issue_txn.py accepts.
_build_txn_input() {
  local subject="$1" repo="$2" rel_body_file="$3" title="$4" reason="$5" \
    prev_sha="$6" prev_updated="$7" readiness_json="$8" native_json="$9" out_rel_file="${10}"
  local out_abs_file="${REPO_ROOT}/${out_rel_file}"
  mkdir -p "$(dirname "${out_abs_file}")"
  uv run --locked python3 - \
    "${subject}" "${repo}" "${rel_body_file}" "${title}" "${reason}" \
    "${prev_sha}" "${prev_updated}" "${readiness_json}" "${native_json}" "${out_abs_file}" <<'PY'
import json
import sys

(subject, repo, rel_body_file, title, reason, prev_sha, prev_updated,
 readiness_json, native_json, out_abs_file) = sys.argv[1:11]

raw_readiness = json.loads(readiness_json)
readiness_result = {
    "status": raw_readiness["status"],
    "body_sha256": raw_readiness["body_sha256"],
    "source_checks": raw_readiness.get("source_checks", []),
    "errors": raw_readiness.get("errors", []),
    "readiness_result_ref": "inline:contract_readiness_check.py --mode static",
}
native_relationships = json.loads(native_json)

payload = {
    "schema": "ISSUE_EDIT_TXN_INPUT_V1",
    "issue_number": int(subject),
    "repo": repo,
    "new_body_file": rel_body_file,
    "readiness_forwarding_payload": {"readiness_result": readiness_result},
    "comment_mode": {"mode": "skip"},
    "expected_previous_body_sha256": prev_sha,
    "expected_previous_updated_at": prev_updated,
    "title_update": {"required": True, "proposed_title": title, "reason": reason},
    "native_relationships": native_relationships,
}
with open(out_abs_file, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False)
PY
}

# One place that judges a step. Checks (Issue #1917 AC3), ALL of which must
# hold, evaluated purely from this canary's own step-definition table (never
# from the transaction's own `desired` or from the readback under test):
#   - child process exit code == 0 (checked first -- a "successful-looking"
#     JSON on stdout from a nonzero-exit child is never trusted)
#   - result schema == ISSUE_EDIT_TXN_RESULT_V1
#   - repo / issue_number match
#   - status == ok
#   - body_update.attempted == true
#   - content_update.patch_attempted == true
#   - independent readback query itself did not fail
#   - independent readback title / body sha256 / parent / blockedBy /
#     blocking / totalCount / hasNextPage match the expected-after snapshot
_evaluate_step() {
  local subject="$1" repo="$2" txn_exit="$3" txn_stdout="$4" expected_title="$5" \
    expected_body_sha="$6" expected_after_json="$7" readback_exit="$8" readback_json="$9"
  uv run --locked python3 - \
    "${subject}" "${repo}" "${txn_exit}" "${txn_stdout}" "${expected_title}" \
    "${expected_body_sha}" "${expected_after_json}" "${readback_exit}" "${readback_json}" <<'PY'
import json
import sys

(subject, repo, txn_exit, txn_stdout, expected_title, expected_body_sha,
 expected_after_json, readback_exit, readback_json) = sys.argv[1:10]

reasons = []

# Sentinel distinct from every possible json.loads() result (including the
# JSON literal `null`, which decodes to Python's ``None`` and must NOT be
# mistaken for "parsing failed" -- Issue #1917 fix_delta P1-2). Only a
# genuine json.loads() exception sets a value to this sentinel; anything
# json.loads() actually returns (None/[]/"" /0/False included) is compared
# against it with `is`, never truthiness.
_PARSE_FAILED = object()

if int(txn_exit) != 0:
    reasons.append(f"txn_child_exit_nonzero:{txn_exit}")
else:
    try:
        txn = json.loads(txn_stdout)
    except Exception as exc:
        txn = _PARSE_FAILED
        reasons.append(f"txn_stdout_not_json:{exc}")
    if txn is _PARSE_FAILED:
        pass
    elif not isinstance(txn, dict):
        reasons.append(f"txn_stdout_not_object:{type(txn).__name__}")
    else:
        if txn.get("schema") != "ISSUE_EDIT_TXN_RESULT_V1":
            reasons.append("txn_schema_mismatch")
        if txn.get("issue_number") != int(subject):
            reasons.append("txn_issue_number_mismatch")
        if txn.get("repo") != repo:
            reasons.append("txn_repo_mismatch")
        if txn.get("status") != "ok":
            reasons.append(f"txn_status_not_ok:{txn.get('status')}")
        body_update = txn.get("body_update")
        if not isinstance(body_update, dict):
            reasons.append(f"txn_body_update_not_object:{type(body_update).__name__}")
        elif body_update.get("attempted") is not True:
            reasons.append("body_update_not_attempted")
        content_update = txn.get("content_update")
        if not isinstance(content_update, dict):
            reasons.append(f"txn_content_update_not_object:{type(content_update).__name__}")
        elif content_update.get("patch_attempted") is not True:
            reasons.append("content_update_not_patch_attempted")

if int(readback_exit) != 0:
    reasons.append(f"readback_process_failed_exit:{readback_exit}")
else:
    try:
        rb = json.loads(readback_json)
    except Exception as exc:
        rb = _PARSE_FAILED
        reasons.append(f"readback_not_json:{exc}")
    if rb is _PARSE_FAILED:
        pass
    elif not isinstance(rb, dict):
        reasons.append(f"readback_stdout_not_object:{type(rb).__name__}")
    else:
        expected_after = json.loads(expected_after_json)
        if rb.get("title") != expected_title:
            reasons.append("readback_title_mismatch")
        if rb.get("body_sha256") != expected_body_sha:
            reasons.append("readback_body_sha256_mismatch")
        if rb.get("parent") != expected_after.get("parent"):
            reasons.append("readback_parent_mismatch")
        if sorted(rb.get("blocked_by") or []) != sorted(expected_after.get("blocked_by") or []):
            reasons.append("readback_blocked_by_mismatch")
        if sorted(rb.get("blocking") or []) != sorted(expected_after.get("blocking") or []):
            reasons.append("readback_blocking_mismatch")
        if rb.get("blocked_by_total_count") != len(expected_after.get("blocked_by") or []):
            reasons.append("readback_blocked_by_total_count_mismatch")
        if rb.get("blocking_total_count") != len(expected_after.get("blocking") or []):
            reasons.append("readback_blocking_total_count_mismatch")
        if rb.get("blocked_by_has_next_page") is not False:
            reasons.append("readback_blocked_by_unexpected_pagination")
        if rb.get("blocking_has_next_page") is not False:
            reasons.append("readback_blocking_unexpected_pagination")

if reasons:
    print(";".join(reasons))
    sys.exit(1)
print("ok")
sys.exit(0)
PY
}

# ---------------------------------------------------------------------------
# Network-facing wrapper functions. Each wraps exactly one `gh` / production
# script invocation and nothing else, so an offline test harness can
# override any one of them (by redefining the bash function after sourcing
# this script with LIVE_CANARY_TEST_MODE=1) without needing a large fake-PATH
# harness.
# ---------------------------------------------------------------------------

_check_environment_preflight() {
  if ! command -v gh >/dev/null 2>&1; then
    PREFLIGHT_SKIP_REASON="gh_binary_not_found"
    return 1
  fi
  if ! command -v uv >/dev/null 2>&1; then
    PREFLIGHT_SKIP_REASON="uv_binary_not_found"
    return 1
  fi
  if ! gh auth status --hostname github.com >/dev/null 2>&1; then
    PREFLIGHT_SKIP_REASON="gh_auth_status_unreachable"
    return 1
  fi
  if ! gh api graphql -f query='query{ viewer { login } }' >/dev/null 2>&1; then
    PREFLIGHT_SKIP_REASON="gh_api_graphql_unreachable"
    return 1
  fi
  return 0
}

_run_readiness_check() {
  local body_abs_file="$1"
  uv run --locked python3 "${READINESS_SCRIPT_PATH}" --body-file "${body_abs_file}" --mode static
}

_invoke_txn() {
  local rel_input_file="$1"
  uv run --locked python3 "${EDIT_TXN_SCRIPT}" --input-file "${rel_input_file}"
}

# Independent readback: a FRESH `gh api graphql` call, never the executor's
# own post-mutation snapshot (Issue #1917 AC3).
_independent_readback() {
  local issue_number="$1"
  local owner="${REPO%%/*}"
  local name="${REPO##*/}"
  local raw
  raw="$(gh api graphql -f query='
    query($owner:String!,$name:String!,$number:Int!){
      repository(owner:$owner,name:$name){
        issue(number:$number){
          title
          body
          updatedAt
          parent{number}
          blockedBy(first:5){totalCount pageInfo{hasNextPage} nodes{number}}
          blocking(first:5){totalCount pageInfo{hasNextPage} nodes{number}}
        }
      }
    }' -F owner="${owner}" -F name="${name}" -F number="${issue_number}" 2>>"${LOG_FILE}")"
  if [ $? -ne 0 ]; then
    echo '{}'
    return 1
  fi
  printf '%s' "${raw}" | uv run --locked python3 -c '
import hashlib, json, sys
try:
    payload = json.load(sys.stdin)
    issue = payload["data"]["repository"]["issue"]
    title = issue.get("title", "")
    body = issue.get("body", "")
    updated_at = issue.get("updatedAt", "")
    parent = issue.get("parent")
    parent_number = parent.get("number") if isinstance(parent, dict) else None
    bb = issue.get("blockedBy") or {}
    bl = issue.get("blocking") or {}
    out = {
        "title": title,
        "body_sha256": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "updated_at": updated_at,
        "parent": parent_number,
        "blocked_by": sorted(n["number"] for n in bb.get("nodes", [])),
        "blocking": sorted(n["number"] for n in bl.get("nodes", [])),
        "blocked_by_total_count": bb.get("totalCount"),
        "blocking_total_count": bl.get("totalCount"),
        "blocked_by_has_next_page": bool((bb.get("pageInfo") or {}).get("hasNextPage")),
        "blocking_has_next_page": bool((bl.get("pageInfo") or {}).get("hasNextPage")),
    }
    print(json.dumps(out))
except Exception as exc:
    print(json.dumps({"error": str(exc)}))
    sys.exit(1)
'
}

_fetch_subject_state() {
  local issue_number="$1"
  gh issue view "${issue_number}" --repo "${REPO}" --json title,body,updatedAt 2>>"${LOG_FILE}"
}

_create_disposable_issue() {
  local role="$1" run_id="$2"
  local title body_rel body_abs
  title="$(_render_step_title "${role}" "${run_id}" "create")"
  body_rel="$(_write_body_file "${role}" "${run_id}" "create")"
  body_abs="${REPO_ROOT}/${body_rel}"
  gh issue create --repo "${REPO}" --title "${title}" --body-file "${body_abs}" 2>>"${LOG_FILE}"
}

_close_issue() {
  local issue_number="$1"
  gh issue close "${issue_number}" --repo "${REPO}" --reason "not planned" >>"${LOG_FILE}" 2>&1
}

# ---------------------------------------------------------------------------
# Orchestration. Built only from the helpers above so an offline test
# harness can override any single collaborator (gh-facing wrapper or
# `_run_step`/`_run_all_steps` themselves) and drive `_create_all_disposables`
# / `_run_all_steps` / `_cleanup` / `_main` directly.
# ---------------------------------------------------------------------------

CREATED_ISSUES=()
SUBJECT=""
P1=""
P2=""
CUR_TITLE=""
CUR_BODY_SHA=""
CUR_UPDATED_AT=""
CLEANUP_DONE="false"
CLEANUP_FAILED="false"
LAST_FAILED_STEP=""

# best effort: attempts to close every disposable Issue actually created so
# far (Issue #1917 AC4). One close failure never stops the remaining
# attempts. Idempotent (a second invocation, e.g. via the EXIT trap after an
# explicit call already ran, is a no-op) so PASS/FAIL logging order stays
# deterministic regardless of how _cleanup ends up being invoked.
_cleanup() {
  if [ "${CLEANUP_DONE}" = "true" ]; then
    return 0
  fi
  CLEANUP_DONE="true"
  local n
  for n in "${CREATED_ISSUES[@]:-}"; do
    [ -n "${n}" ] || continue
    _log "cleanup: closing disposable issue #${n}"
    if ! _close_issue "${n}"; then
      _log "cleanup_failed: could not close disposable issue #${n} -- manual cleanup required"
      CLEANUP_FAILED="true"
    fi
  done
  if [ "${CLEANUP_FAILED}" = "true" ]; then
    return 1
  fi
  return 0
}

_create_all_disposables() {
  local run_id="$1"
  local url num

  url="$(_create_disposable_issue "S" "${run_id}")"
  if [ -z "${url}" ]; then
    _log "FAIL: disposable S issue creation failed"
    return 1
  fi
  num="$(echo "${url}" | grep -oE '[0-9]+$')"
  if [ -z "${num}" ]; then
    _log "FAIL: could not parse disposable S issue number from: ${url}"
    return 1
  fi
  SUBJECT="${num}"
  CREATED_ISSUES+=("${SUBJECT}")
  trap _cleanup EXIT
  _log "created disposable subject S #${SUBJECT}"

  url="$(_create_disposable_issue "P1" "${run_id}")"
  if [ -z "${url}" ]; then
    _log "FAIL: disposable P1 issue creation failed"
    return 1
  fi
  num="$(echo "${url}" | grep -oE '[0-9]+$')"
  if [ -z "${num}" ]; then
    _log "FAIL: could not parse disposable P1 issue number from: ${url}"
    return 1
  fi
  P1="${num}"
  CREATED_ISSUES+=("${P1}")
  _log "created disposable parent P1 #${P1}"

  url="$(_create_disposable_issue "P2" "${run_id}")"
  if [ -z "${url}" ]; then
    _log "FAIL: disposable P2 issue creation failed"
    return 1
  fi
  num="$(echo "${url}" | grep -oE '[0-9]+$')"
  if [ -z "${num}" ]; then
    _log "FAIL: could not parse disposable P2 issue number from: ${url}"
    return 1
  fi
  P2="${num}"
  CREATED_ISSUES+=("${P2}")
  _log "created disposable parent P2 #${P2}"

  return 0
}

# Runs one relationship-cycle step end to end: render step title/body ->
# real static readiness check -> assemble ISSUE_EDIT_TXN_INPUT_V1 -> invoke
# edit_issue_txn.py combined transaction -> independent readback ->
# _evaluate_step. On success, advances CUR_TITLE/CUR_BODY_SHA/CUR_UPDATED_AT
# from the independent readback (never from the transaction's own snapshot)
# so the next step's expected_previous_* preconditions reflect confirmed
# live state.
_run_step() {
  local step="$1" subject="$2" p1="$3" p2="$4" run_id="$5"
  _log "step${step}: starting"

  local title body_rel body_abs
  title="$(_render_step_title "S" "${run_id}" "step-${step}")"
  body_rel="$(_write_body_file "S" "${run_id}" "step-${step}")"
  body_abs="${REPO_ROOT}/${body_rel}"

  local readiness_json readiness_exit
  readiness_json="$(_run_readiness_check "${body_abs}")"
  readiness_exit=$?
  if [ "${readiness_exit}" -ne 0 ]; then
    _log "FAIL step${step}: readiness_check_not_go exit=${readiness_exit} output=${readiness_json}"
    return 1
  fi

  # Issue #1917 fix_delta P1: the expected body SHA-256 is fixed HERE --
  # immediately after body generation + static validation, and strictly
  # BEFORE `_invoke_txn` performs the remote mutation -- and reused
  # unchanged all the way to `_evaluate_step` below. It is never recomputed
  # by re-reading `${body_abs}` after the transaction/readback have run,
  # because that would let a later mutation of the (now run-isolated, but
  # still theoretically re-writable) body file retroactively change what
  # "expected" means for a step whose remote mutation already happened.
  local body_expected_sha
  body_expected_sha="$(_sha256_of_file "${body_abs}")"
  if [ -z "${body_expected_sha}" ]; then
    _log "FAIL step${step}: could not compute expected body sha256 before mutation"
    return 1
  fi

  local native_json input_rel
  native_json="$(_native_relationships_json "${step}" "${p1}" "${p2}")"
  input_rel="${RELATIVE_RUN_DIR}/${subject}-step${step}.txn_input.json"
  if ! _build_txn_input "${subject}" "${REPO}" "${body_rel}" "${title}" "relationship_cycle_step_${step}" \
    "${CUR_BODY_SHA}" "${CUR_UPDATED_AT}" "${readiness_json}" "${native_json}" "${input_rel}"; then
    _log "FAIL step${step}: txn_input_build_failed"
    return 1
  fi

  local txn_stdout txn_exit
  txn_stdout="$(_invoke_txn "${input_rel}")"
  txn_exit=$?
  echo "step${step} txn_stdout=${txn_stdout}" >>"${LOG_FILE}"

  local readback_json readback_exit
  readback_json="$(_independent_readback "${subject}")"
  readback_exit=$?
  echo "step${step} readback=${readback_json}" >>"${LOG_FILE}"

  local expected_after
  expected_after="$(_expected_after_snapshot "${step}" "${p1}" "${p2}")"

  local eval_out eval_exit
  eval_out="$(_evaluate_step "${subject}" "${REPO}" "${txn_exit}" "${txn_stdout}" "${title}" \
    "${body_expected_sha}" "${expected_after}" "${readback_exit}" "${readback_json}")"
  eval_exit=$?
  if [ "${eval_exit}" -ne 0 ]; then
    _log "FAIL step${step}: ${eval_out}"
    return 1
  fi

  CUR_TITLE="${title}"
  CUR_BODY_SHA="${body_expected_sha}"
  CUR_UPDATED_AT="$(printf '%s' "${readback_json}" | uv run --locked python3 -c 'import json,sys; print(json.load(sys.stdin).get("updated_at",""))' 2>/dev/null)"

  _log "PASS step${step}: ${eval_out}"
  return 0
}

# Fail-fast: the first failing step stops all subsequent mutation steps
# (Issue #1917 AC4).
_run_all_steps() {
  local subject="$1" p1="$2" p2="$3" run_id="$4"
  local step
  for step in 1 2 3 4 5 6 7; do
    if ! _run_step "${step}" "${subject}" "${p1}" "${p2}" "${run_id}"; then
      LAST_FAILED_STEP="${step}"
      return 1
    fi
  done
  return 0
}

# Issue #1917 fix_delta P2: validates every creation-time (S/P1/P2) and
# per-step (1..7) body fixture -- the SAME static readiness/hygiene checks
# `_run_step` / `_create_disposable_issue` perform later -- strictly BEFORE
# any disposable Issue is created on GitHub. A defect in any fixture is a
# FAIL (never rounded to SKIP/exit 77 -- exit 77 stays reserved for the
# environment preconditions `_check_environment_preflight` detects), and
# zero `gh issue create` calls happen once any fixture fails: this function
# never calls `_create_disposable_issue` / `gh` itself. The body path and
# readiness result computed here are deterministic (pure functions of
# role/run_id/marker) and _create_disposable_issue / _run_step recomputing
# them later is intentional -- an idempotent re-validation, not a
# correctness dependency on caching -- so no fixture cache needs to be
# threaded through global state.
_validate_fixtures_before_creation() {
  local run_id="$1"
  local role step body_rel body_abs readiness_json readiness_exit

  for role in S P1 P2; do
    body_rel="$(_write_body_file "${role}" "${run_id}" "create")"
    if [ -z "${body_rel}" ]; then
      _log "FAIL fixture_validation[create:${role}]: could not write body fixture"
      return 1
    fi
    body_abs="${REPO_ROOT}/${body_rel}"
    readiness_json="$(_run_readiness_check "${body_abs}")"
    readiness_exit=$?
    if [ "${readiness_exit}" -ne 0 ]; then
      _log "FAIL fixture_validation[create:${role}]: readiness_check_not_go exit=${readiness_exit} output=${readiness_json}"
      return 1
    fi
  done

  for step in 1 2 3 4 5 6 7; do
    body_rel="$(_write_body_file "S" "${run_id}" "step-${step}")"
    if [ -z "${body_rel}" ]; then
      _log "FAIL fixture_validation[step:${step}]: could not write body fixture"
      return 1
    fi
    body_abs="${REPO_ROOT}/${body_rel}"
    readiness_json="$(_run_readiness_check "${body_abs}")"
    readiness_exit=$?
    if [ "${readiness_exit}" -ne 0 ]; then
      _log "FAIL fixture_validation[step:${step}]: readiness_check_not_go exit=${readiness_exit} output=${readiness_json}"
      return 1
    fi
  done

  return 0
}

_main() {
  if ! _check_environment_preflight; then
    _skip "${PREFLIGHT_SKIP_REASON:-environment_precheck_failed}"
  fi

  if ! _init_run_dir; then
    _log "FAIL: could not initialize an isolated run directory"
    exit 1
  fi
  _log "preflight: gh binary + uv binary + auth + graphql reachability confirmed"

  if ! _validate_fixtures_before_creation "${TS}"; then
    _log "FAIL: fixture validation (creation + all 7 step bodies) did not pass; no disposable Issue was created"
    exit 1
  fi

  if ! _create_all_disposables "${TS}"; then
    _cleanup
    _log "FAIL: disposable Issue setup (S/P1/P2) did not complete"
    exit 1
  fi

  local initial_state
  initial_state="$(_fetch_subject_state "${SUBJECT}")"
  if [ -z "${initial_state}" ]; then
    _cleanup
    _log "FAIL: could not read back initial subject S state after creation"
    exit 1
  fi
  CUR_TITLE="$(printf '%s' "${initial_state}" | uv run --locked python3 -c 'import json,sys; print(json.load(sys.stdin).get("title",""))' 2>/dev/null)"
  CUR_BODY_SHA="$(printf '%s' "${initial_state}" | uv run --locked python3 -c 'import hashlib,json,sys; d=json.load(sys.stdin); print("sha256:" + hashlib.sha256(d.get("body","").encode("utf-8")).hexdigest())' 2>/dev/null)"
  CUR_UPDATED_AT="$(printf '%s' "${initial_state}" | uv run --locked python3 -c 'import json,sys; print(json.load(sys.stdin).get("updatedAt",""))' 2>/dev/null)"
  if [ -z "${CUR_BODY_SHA}" ] || [ -z "${CUR_UPDATED_AT}" ]; then
    _cleanup
    _log "FAIL: could not parse initial subject S state (title/body/updatedAt)"
    exit 1
  fi

  if ! _run_all_steps "${SUBJECT}" "${P1}" "${P2}" "${TS}"; then
    _cleanup
    _log "FAIL: relationship cycle step ${LAST_FAILED_STEP:-unknown} did not pass; remaining mutation steps were skipped (fail-fast)"
    exit 1
  fi

  if ! _cleanup; then
    _log "FAIL: all 7 relationship cycle steps passed but cleanup did not complete for all disposable issues"
    exit 1
  fi

  _log "PASS: full native relationship cycle (parent set/rebind/remove + blockedBy add/remove + blocking add/remove) via edit_issue_txn.py combined transaction round-tripped on disposable issues S=#${SUBJECT} / P1=#${P1} / P2=#${P2}, and cleanup closed all created disposable issues"
  exit 0
}

# LIVE_CANARY_TEST_MODE: when set (any non-empty value), this script may be
# sourced (not executed) so an offline test harness can define its own stub
# overrides for the network-facing wrapper functions above and invoke
# `_main` / `_create_all_disposables` / `_run_all_steps` / `_run_step` /
# `_cleanup` directly, without running the live preflight/steps below. Every
# function above remains always defined (outside this guard).
if [ -z "${LIVE_CANARY_TEST_MODE:-}" ]; then
  _main
fi

#!/usr/bin/env bash
# verify_acp_roundtrip.sh
#
# Verifies ACP transport end-to-end by delegating short tasks to gemini CLI
# via transport: acp. Implements:
#   - SKIP exit 77 when gemini (GEMINI_BIN), jq, or uv is absent
#   - FAIL exit 1 when _acp_fallback: true or failure_class=auth_required is detected
#   - scenario 1 (normal): PONG roundtrip
#   - scenario 2 (controlled experiment): permission outcome controls a side
#     effect — 2a deny → no file, 2b approve → file created
#   - Artifact output to artifacts/runtime-verification-AC7-<ISO8601>.log
#   - Telemetry artifact to artifacts/runtime-verification-AC7-<ISO8601>.telemetry.json
#     when real Gemini CLI is available and pre-authenticated
#
# Result schema note: run_delegation() normalizes ACP results to
# delegation_result/v1. ACP-specific fields (structured_events, failure_class)
# live under .transport_details; this script reads them with a top-level
# fallback. .ok / .transport / .response_text / ._acp_fallback / .warnings
# remain at the top level.
#
# Exit codes:
#   0   All scenarios PASS
#   1   At least one scenario FAIL or fallback detected
#   77  Execution environment unavailable (gemini, jq, or uv not found)
#
# Environment:
#   GEMINI_BIN              Override the gemini CLI binary path (default: gemini)
#   GEMINI_ACP_DEBUG        Set to "1" to pass --debug to gemini --acp (real CLI only)
#   GEMINI_TELEMETRY_ENABLED  Set to "true" to enable telemetry output (real CLI only)
#   GEMINI_TELEMETRY_TARGET   Set to "local" for local telemetry file output
#   GEMINI_TELEMETRY_OUTFILE  Path for telemetry JSON output file

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
REPO_DIR="$(cd "$SKILL_DIR/../../.." && pwd)"

GEMINI_BIN="${GEMINI_BIN:-gemini}"

# --- SKIP guard: gemini CLI not installed ---
if ! command -v "$GEMINI_BIN" >/dev/null 2>&1; then
  echo "SKIP: gemini CLI not found (GEMINI_BIN=$GEMINI_BIN). Install gemini CLI or set GEMINI_BIN to a valid path."
  exit 77
fi

# --- SKIP guard: jq not available ---
if ! command -v jq >/dev/null 2>&1; then
  echo "SKIP: jq not installed (required for result validation)"
  exit 77
fi

# --- SKIP guard: uv not installed ---
# The scenario body runs the ACP script via `uv run python3 ...`; without uv
# the execution environment is unavailable, which is a SKIP condition, not a
# scenario failure.
if ! command -v uv >/dev/null 2>&1; then
  echo "SKIP: uv not installed (required to run run_gemini_acp.py via uv run)"
  exit 77
fi

# --- Prepare temp workspace ---
WORK_DIR="$(mktemp -d -t verify-acp-roundtrip-XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

# --- Prepare artifacts directory ---
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACTS_DIR="$REPO_DIR/artifacts"
mkdir -p "$ARTIFACTS_DIR"
LOG_FILE="$ARTIFACTS_DIR/runtime-verification-AC7-${TIMESTAMP}.log"

# --- Telemetry artifact path (real Gemini CLI + pre-authenticated env only) ---
# GEMINI_TELEMETRY_OUTFILE defaults to artifacts/runtime-verification-AC7-<TIMESTAMP>.telemetry.json
# This file is created by gemini CLI when GEMINI_TELEMETRY_ENABLED=true and
# GEMINI_TELEMETRY_TARGET=local. Fake-ACP-agent scenarios (scenario 2) do NOT
# produce telemetry artifacts.
TELEMETRY_FILE="${GEMINI_TELEMETRY_OUTFILE:-$ARTIFACTS_DIR/runtime-verification-AC7-${TIMESTAMP}.telemetry.json}"
# Export telemetry env vars so run_gemini_acp.py subprocess inherits them for
# real Gemini CLI scenario 1. Values are only set when not already exported by
# the caller; set defaults here so callers that want to disable telemetry can
# unset GEMINI_TELEMETRY_ENABLED before running this script.
export GEMINI_TELEMETRY_OUTFILE="$TELEMETRY_FILE"
export GEMINI_TELEMETRY_ENABLED="${GEMINI_TELEMETRY_ENABLED:-true}"
export GEMINI_TELEMETRY_TARGET="${GEMINI_TELEMETRY_TARGET:-local}"
# GEMINI_ACP_DEBUG=1 causes run_gemini_acp.py to append --debug to the gemini
# --acp subprocess args, which enables verbose ACP protocol logging to stderr.
export GEMINI_ACP_DEBUG="${GEMINI_ACP_DEBUG:-1}"

# Collect environment info once
GEMINI_VERSION="$("$GEMINI_BIN" --version 2>/dev/null || echo 'unknown')"
ENV_INFO="OS=$(uname -sr), gemini=$GEMINI_VERSION ($(command -v "$GEMINI_BIN")), jq=$(command -v jq), uv=$(command -v uv 2>/dev/null || echo 'not found')"

# Track overall pass/fail
OVERALL_RESULT=0

# ============================================================
# Helper: append a scenario block to the log file
# ============================================================
log_scenario() {
  local ac="$1"
  local input_desc="$2"
  local output_text="$3"
  local verdict="$4"
  local exit_code="$5"
  local reason="${6:-}"
  {
    echo "=== Runtime Verification Log ==="
    echo "AC: $ac"
    echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Environment: $ENV_INFO"
    echo ""
    echo "--- Input ---"
    echo "$input_desc"
    echo ""
    echo "--- Output ---"
    echo "$output_text"
    echo ""
    echo "--- Verdict ---"
    echo "Result: $verdict"
    echo "Exit Code: $exit_code"
    if [ -n "$reason" ]; then
      echo "Reason: $reason"
    fi
    echo ""
    echo "==============================="
    echo ""
  } >> "$LOG_FILE"
}

# ============================================================
# run_scenario: invoke run_gemini_acp.py with a request file
# Returns 0 on success, 1 on failure
# Sets global SCENARIO_RESULT_FILE to the output JSON path
# ============================================================
SCENARIO_RESULT_FILE=""
run_scenario() {
  local label="$1"
  local request_file="$2"
  local extra_args="${3:-}"
  SCENARIO_RESULT_FILE="$WORK_DIR/result-${label}.json"
  set +e
  # shellcheck disable=SC2086
  timeout 180 uv run python3 "$SCRIPT_DIR/run_gemini_acp.py" \
    --request-file "$request_file" \
    --output-file "$SCENARIO_RESULT_FILE" \
    $extra_args
  local rc=$?
  set -e
  if [ "$rc" -eq 124 ]; then
    echo "TIMEOUT: run_gemini_acp.py exceeded 180s for scenario $label"
    return 1
  fi
  return $rc
}

# ============================================================
# scenario 1: normal — "Reply with exactly: PONG"
#   Expects: ok=true, structured_events non-empty, _acp_fallback != true
# ============================================================
echo ""
echo "=== verify_acp_roundtrip: scenario 1 (normal — PONG roundtrip) ==="

CONTEXT_FILE_1="$WORK_DIR/context1.txt"
REQUEST_FILE_1="$WORK_DIR/request1.json"
echo "This is a verification context file." > "$CONTEXT_FILE_1"

cat > "$REQUEST_FILE_1" <<REQEOF
{
  "schema": "delegation_request_v1",
  "transport": "acp",
  "objective": "Reply with exactly: PONG",
  "instructions": [
    "Do not add any explanation.",
    "Reply with exactly the word PONG and nothing else."
  ],
  "tool_profile": "no_tools",
  "output_sections": ["Response"],
  "context_files": ["$CONTEXT_FILE_1"],
  "model": "gemini-2.5-flash",
  "timeout_sec": 120
}
REQEOF

S1_OUTPUT=""
S1_VERDICT=""
S1_RC=0

if run_scenario "s1" "$REQUEST_FILE_1"; then
  S1_RC=0
else
  S1_RC=$?
fi

if [ -f "$SCENARIO_RESULT_FILE" ]; then
  S1_OUTPUT="$(cat "$SCENARIO_RESULT_FILE")"

  # --- Fallback detection (must come before any PASS) ---
  S1_FALLBACK="$(echo "$S1_OUTPUT" | jq -r '._acp_fallback // false' 2>/dev/null || echo "false")"
  if [ "$S1_FALLBACK" = "true" ]; then
    S1_REASON="$(echo "$S1_OUTPUT" | jq -r '.warnings[0] // "unknown"' 2>/dev/null || echo "unknown")"
    echo "FAIL: scenario 1 — _acp_fallback: true detected. ACP transport did not execute directly."
    echo "      First warning: $S1_REASON"
    log_scenario "AC7 scenario 1 (normal PONG)" \
      "objective=Reply with exactly PONG, transport=acp, model=gemini-2.5-flash" \
      "$S1_OUTPUT" \
      "FAIL" "1" "_acp_fallback: true — $S1_REASON"
    echo "FAIL: verify_acp_roundtrip — _acp_fallback detected in scenario 1"
    exit 1
  else
    S1_OK="$(echo "$S1_OUTPUT" | jq -r '.ok' 2>/dev/null || echo "null")"
    # B1: run_delegation() normalizes the ACP result to delegation_result/v1, so
    # ACP-specific structured_events now live under transport_details. Read with
    # a fallback to top-level for forward/backward compatibility.
    S1_EVENTS="$(echo "$S1_OUTPUT" | jq '(.transport_details.structured_events // .structured_events // []) | length' 2>/dev/null || echo "0")"
    S1_TRANSPORT="$(echo "$S1_OUTPUT" | jq -r '.transport // "null"' 2>/dev/null || echo "null")"
    # B2: surface auth-required failures explicitly — they must NOT be masked by
    # a headless_json fallback. failure_class lives under transport_details after
    # normalization (top-level fallback retained for safety).
    S1_FAILURE_CLASS="$(echo "$S1_OUTPUT" | jq -r '(.transport_details.failure_class // .failure_class) // "null"' 2>/dev/null || echo "null")"
    if [ "$S1_FAILURE_CLASS" = "auth_required" ]; then
      echo "FAIL: scenario 1 — failure_class=auth_required. ACP session/new requires authentication."
      echo "      The Gemini CLI / OAuth session is not pre-authenticated. This transport does"
      echo "      not implement the ACP authenticate handshake (see references/transport-acp.md)."
      log_scenario "AC7 scenario 1 (normal PONG)" \
        "objective=Reply with exactly PONG, transport=acp, model=gemini-2.5-flash" \
        "$S1_OUTPUT" \
        "FAIL" "1" "failure_class=auth_required — pre-authenticated session required"
      echo "FAIL: verify_acp_roundtrip — auth_required in scenario 1"
      exit 1
    fi
    # response_text trimmed of leading/trailing whitespace, must equal exactly PONG
    S1_RESPONSE="$(echo "$S1_OUTPUT" | jq -r '.response_text // ""' 2>/dev/null | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    # Collect all FAIL reasons; PASS requires every check to pass.
    S1_FAIL_REASONS=""
    [ "$S1_OK" != "true" ] && S1_FAIL_REASONS="${S1_FAIL_REASONS}ok=$S1_OK; "
    # structured_events > 0 required: proves ACP event stream was parsed
    { [ "$S1_EVENTS" -gt 0 ]; } 2>/dev/null || S1_FAIL_REASONS="${S1_FAIL_REASONS}structured_events=$S1_EVENTS (need >0); "
    [ "$S1_TRANSPORT" != "acp" ] && S1_FAIL_REASONS="${S1_FAIL_REASONS}transport=$S1_TRANSPORT (need acp — fallback or wrong path); "
    [ "$S1_RESPONSE" != "PONG" ] && S1_FAIL_REASONS="${S1_FAIL_REASONS}response_text=\"$S1_RESPONSE\" (need exactly PONG); "
    if [ -z "$S1_FAIL_REASONS" ]; then
      echo "PASS: scenario 1 — ok=true, transport=acp, structured_events=$S1_EVENTS, response_text=PONG"
      S1_VERDICT="PASS"
      log_scenario "AC7 scenario 1 (normal PONG)" \
        "objective=Reply with exactly PONG, transport=acp, model=gemini-2.5-flash" \
        "$S1_OUTPUT" \
        "PASS" "0"
    else
      echo "FAIL: scenario 1 — $S1_FAIL_REASONS"
      S1_VERDICT="FAIL"
      OVERALL_RESULT=1
      log_scenario "AC7 scenario 1 (normal PONG)" \
        "objective=Reply with exactly PONG, transport=acp, model=gemini-2.5-flash" \
        "$S1_OUTPUT" \
        "FAIL" "1" "$S1_FAIL_REASONS"
    fi
  fi
else
  echo "FAIL: scenario 1 — result file not created"
  S1_VERDICT="FAIL"
  OVERALL_RESULT=1
  log_scenario "AC7 scenario 1 (normal PONG)" \
    "objective=Reply with exactly PONG, transport=acp, model=gemini-2.5-flash" \
    "(no result file)" \
    "FAIL" "1" "result file not created (run exit=$S1_RC)"
fi

# ============================================================
# scenario 2: permission outcome controls a side effect
#   (deterministic fake ACP agent — controlled experiment)
#
#   B3: a passive "the file was not written" check does not prove the
#   permission proxy denied anything — it could just mean nothing tried to
#   write. This scenario is therefore a *controlled experiment*: the fake ACP
#   agent produces a real, observable side effect (creating a unique file)
#   *iff* the permission outcome it received is an approval. Running both the
#   deny case and the approve case proves the permission proxy's branch
#   actually controls the side effect:
#
#     case (a)  no --approve-edits  → proxy rejects → file NOT created
#                                                   → response PERMISSION_DENIED_OK
#     case (b)  --approve-edits     → proxy approves → file IS created
#                                                   → response PERMISSION_GRANTED_OK
#
#   This is a deterministic fake-agent test. It proves the permission proxy
#   *branch* controls side effects; it does NOT prove gating of Gemini's
#   native tool registry — that is follow-up #112.
# ============================================================
echo ""
echo "=== verify_acp_roundtrip: scenario 2 (permission outcome controls a side effect — controlled experiment) ==="

FAKE_ACP_BIN="$WORK_DIR/fake_acp_agent.py"
cat > "$FAKE_ACP_BIN" <<'FAKEACP'
#!/usr/bin/env python3
"""Deterministic ACP agent for the permission-outcome controlled experiment.

Lifecycle:
  1. initialize → result
  2. session/new → result (with fake sessionId)
  3. session/prompt → send session/request_permission, inspect the proxy's
     outcome, and produce a side effect ONLY when the outcome is an approval.

Side effect contract (this is what makes it a controlled experiment):
  - outcome == approval → create the file at ACP_PERM_SIDEEFFECT_FILE and
    reply PERMISSION_GRANTED_OK.
  - outcome == reject/cancel → create NOTHING and reply PERMISSION_DENIED_OK.

The target path is read from the ACP_PERM_SIDEEFFECT_FILE environment variable
so the harness can use a unique, work-dir-local path per sub-case.
"""
import json, os, sys, uuid

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def read_line():
    return sys.stdin.readline().strip()

def main():
    sideeffect_path = os.environ.get("ACP_PERM_SIDEEFFECT_FILE", "")
    session_id = str(uuid.uuid4())
    for _ in range(100):
        raw = read_line()
        if not raw:
            break
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        method = msg.get("method", "")
        mid = msg.get("id")

        if method == "initialize":
            send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1}})

        elif method == "session/new":
            send({"jsonrpc":"2.0","id":mid,"result":{"sessionId":session_id}})

        elif method == "session/prompt":
            perm_id = mid + 1000
            # Request permission for a write-type operation.
            send({"jsonrpc":"2.0","method":"session/request_permission","id":perm_id,"params":{
                "sessionId": session_id,
                "options": [
                    {"optionId":"proceed_once","name":"Allow","kind":"allow_once"},
                    {"optionId":"cancel","name":"Reject","kind":"reject_once"}
                ]
            }})
            # Read the permission response produced by our proxy.
            raw_perm = read_line()
            perm_resp = json.loads(raw_perm) if raw_perm else {}
            outcome = (perm_resp.get("result") or {}).get("outcome") or {}
            outcome_value = outcome.get("outcome", "?")
            option_id = outcome.get("optionId", "?")

            # Approval == an option was selected AND it is an allow_* option.
            approved = outcome_value == "selected" and option_id in ("proceed_once",)

            if approved:
                # Real, observable side effect — created ONLY on approval.
                if sideeffect_path:
                    with open(sideeffect_path, "w") as fh:
                        fh.write("ACP_PERMISSION_SIDE_EFFECT\n")
                reply_text = "PERMISSION_GRANTED_OK"
            else:
                # Rejected/cancelled — no side effect at all.
                reply_text = "PERMISSION_DENIED_OK"

            send({"jsonrpc":"2.0","method":"session/update","params":{
                "sessionId": session_id,
                "update": {"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":reply_text}}
            }})
            send({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn"}})
            return

if __name__ == "__main__":
    main()
FAKEACP
chmod +x "$FAKE_ACP_BIN"

CONTEXT_FILE_2="$WORK_DIR/context2.txt"
REQUEST_FILE_2="$WORK_DIR/request2.json"
echo "This is a permission-outcome verification context." > "$CONTEXT_FILE_2"

# tool_profile is required for validate_request() — run_delegation() routes the
# ACP path through the full delegation contract, so this request must be a
# valid delegation_request_v1. no_tools keeps the request schema-valid; the
# fake ACP agent drives the permission path regardless of tool_profile.
cat > "$REQUEST_FILE_2" <<REQEOF
{
  "schema": "delegation_request_v1",
  "transport": "acp",
  "objective": "Attempt a write-type operation and report the permission outcome",
  "instructions": [
    "Attempt the write operation.",
    "If permission is denied, reply with: PERMISSION_DENIED_OK",
    "If permission is granted, reply with: PERMISSION_GRANTED_OK"
  ],
  "tool_profile": "no_tools",
  "output_sections": ["Response"],
  "context_files": ["$CONTEXT_FILE_2"],
  "model": "gemini-2.5-flash",
  "timeout_sec": 60
}
REQEOF

S2_VERDICT=""

# ------------------------------------------------------------
# run_permission_subcase — run one controlled-experiment sub-case
#   $1 label                e.g. "s2a-deny"
#   $2 sideeffect file path unique per sub-case
#   $3 expected response    PERMISSION_DENIED_OK | PERMISSION_GRANTED_OK
#   $4 expect file created  0 = must NOT exist, 1 = must exist
#   $5 extra run_scenario args (e.g. "--approve-edits")
# Sets the global SUBCASE_REASONS to "" on PASS or a non-empty reason
# string on FAIL. (A global is used instead of stdout capture because
# run_scenario itself prints to stdout — capturing it via $(...) would
# pollute the reason string.)
# ------------------------------------------------------------
SUBCASE_REASONS=""
run_permission_subcase() {
  local label="$1"
  local sideeffect_file="$2"
  local expect_response="$3"
  local expect_file="$4"
  local extra_args="${5:-}"
  SUBCASE_REASONS=""

  rm -f "$sideeffect_file"

  local sub_rc=0
  # Wrap in `if` so a non-zero exit does not abort the script under `set -e`.
  if GEMINI_BIN="$FAKE_ACP_BIN" ACP_PERM_SIDEEFFECT_FILE="$sideeffect_file" \
       run_scenario "$label" "$REQUEST_FILE_2" "$extra_args"; then
    sub_rc=0
  else
    sub_rc=$?
  fi

  if [ ! -f "$SCENARIO_RESULT_FILE" ]; then
    SUBCASE_REASONS="result file not created (run exit=$sub_rc); "
    return 0
  fi

  local out
  out="$(cat "$SCENARIO_RESULT_FILE")"

  local fallback
  fallback="$(echo "$out" | jq -r '._acp_fallback // false' 2>/dev/null || echo "false")"
  if [ "$fallback" = "true" ]; then
    SUBCASE_REASONS="_acp_fallback: true detected (ACP transport did not execute directly); "
    return 0
  fi

  local fclass
  fclass="$(echo "$out" | jq -r '(.transport_details.failure_class // .failure_class) // "null"' 2>/dev/null || echo "null")"
  if [ "$fclass" = "auth_required" ]; then
    SUBCASE_REASONS="failure_class=auth_required (pre-authenticated session required); "
    return 0
  fi

  local ok perm_count response file_exists
  ok="$(echo "$out" | jq -r '.ok' 2>/dev/null || echo "null")"
  # B1: structured_events live under transport_details after normalization.
  perm_count="$(echo "$out" | jq '[(.transport_details.structured_events // .structured_events // [])[]? | select(.type == "session/request_permission")] | length' 2>/dev/null || echo "0")"
  response="$(echo "$out" | jq -r '.response_text // ""' 2>/dev/null || echo "")"
  file_exists=0
  [ -f "$sideeffect_file" ] && file_exists=1

  local reasons=""
  [ "$ok" != "true" ] && reasons="${reasons}ok=$ok; "
  { [ "$perm_count" -ge 1 ]; } 2>/dev/null || reasons="${reasons}permission_request_count=$perm_count (need >=1); "
  echo "$response" | grep -q "$expect_response" || reasons="${reasons}response_text missing $expect_response (got: ${response}); "
  if [ "$expect_file" -eq 1 ]; then
    [ "$file_exists" -eq 1 ] || reasons="${reasons}side-effect file NOT created (approval did not produce a write); "
  else
    [ "$file_exists" -eq 1 ] && reasons="${reasons}side-effect file WAS created (deny did not block the write); "
  fi

  SUBCASE_REASONS="$reasons"
}

# --- sub-case (a): deny — no --approve-edits → file NOT created ---
echo ""
echo "--- scenario 2a: permission DENY (no --approve-edits) — expect no side effect ---"
S2A_SIDEEFFECT="$WORK_DIR/acp-perm-sideeffect-deny.txt"
run_permission_subcase "s2a-deny" "$S2A_SIDEEFFECT" "PERMISSION_DENIED_OK" 0 ""
S2A_REASONS="$SUBCASE_REASONS"
if [ -z "$S2A_REASONS" ]; then
  echo "PASS: scenario 2a (deny) — response=PERMISSION_DENIED_OK, side-effect file not created"
  S2A_VERDICT="PASS"
  log_scenario "AC7 scenario 2a (permission deny controls side effect)" \
    "fake-acp-agent, no --approve-edits" \
    "$(cat "$SCENARIO_RESULT_FILE" 2>/dev/null || echo '(no result file)')" \
    "PASS" "0"
else
  echo "FAIL: scenario 2a (deny) — $S2A_REASONS"
  S2A_VERDICT="FAIL"
  OVERALL_RESULT=1
  log_scenario "AC7 scenario 2a (permission deny controls side effect)" \
    "fake-acp-agent, no --approve-edits" \
    "$(cat "$SCENARIO_RESULT_FILE" 2>/dev/null || echo '(no result file)')" \
    "FAIL" "1" "$S2A_REASONS"
fi

# --- sub-case (b): approve — --approve-edits → file IS created ---
echo ""
echo "--- scenario 2b: permission APPROVE (--approve-edits) — expect side effect ---"
S2B_SIDEEFFECT="$WORK_DIR/acp-perm-sideeffect-approve.txt"
run_permission_subcase "s2b-approve" "$S2B_SIDEEFFECT" "PERMISSION_GRANTED_OK" 1 "--approve-edits"
S2B_REASONS="$SUBCASE_REASONS"
if [ -z "$S2B_REASONS" ]; then
  echo "PASS: scenario 2b (approve) — response=PERMISSION_GRANTED_OK, side-effect file created"
  S2B_VERDICT="PASS"
  log_scenario "AC7 scenario 2b (permission approve controls side effect)" \
    "fake-acp-agent, --approve-edits" \
    "$(cat "$SCENARIO_RESULT_FILE" 2>/dev/null || echo '(no result file)')" \
    "PASS" "0"
else
  echo "FAIL: scenario 2b (approve) — $S2B_REASONS"
  S2B_VERDICT="FAIL"
  OVERALL_RESULT=1
  log_scenario "AC7 scenario 2b (permission approve controls side effect)" \
    "fake-acp-agent, --approve-edits" \
    "$(cat "$SCENARIO_RESULT_FILE" 2>/dev/null || echo '(no result file)')" \
    "FAIL" "1" "$S2B_REASONS"
fi

if [ "$S2A_VERDICT" = "PASS" ] && [ "$S2B_VERDICT" = "PASS" ]; then
  S2_VERDICT="PASS"
else
  S2_VERDICT="FAIL"
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo "=== Summary ==="
echo "scenario 1 (normal PONG): $S1_VERDICT"
echo "scenario 2 (permission outcome controls side effect): $S2_VERDICT"
echo "  - scenario 2a (deny — no side effect): $S2A_VERDICT"
echo "  - scenario 2b (approve — side effect): $S2B_VERDICT"
echo "Artifact: $LOG_FILE"
echo ""

if [ "$OVERALL_RESULT" -eq 0 ]; then
  echo "PASS: verify_acp_roundtrip — all scenarios passed"
else
  echo "FAIL: verify_acp_roundtrip — one or more scenarios failed"
fi

exit "$OVERALL_RESULT"

#!/usr/bin/env bash
# verify_acp_roundtrip.sh
#
# Verifies ACP transport end-to-end by delegating short tasks to gemini CLI
# via transport: acp. Implements:
#   - SKIP exit 77 when gemini (GEMINI_BIN) or jq is absent
#   - FAIL exit 1 when _acp_fallback: true is detected
#   - scenario 1 (normal): PONG roundtrip
#   - scenario 2 (error): permission deny on write tool request
#   - Artifact output to artifacts/runtime-verification-AC7-<ISO8601>.log
#
# Exit codes:
#   0   All scenarios PASS
#   1   At least one scenario FAIL or fallback detected
#   77  Execution environment unavailable (gemini or jq not found)
#
# Environment:
#   GEMINI_BIN   Override the gemini CLI binary path (default: gemini)

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

# --- Prepare temp workspace ---
WORK_DIR="$(mktemp -d -t verify-acp-roundtrip-XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

# --- Prepare artifacts directory ---
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACTS_DIR="$REPO_DIR/artifacts"
mkdir -p "$ARTIFACTS_DIR"
LOG_FILE="$ARTIFACTS_DIR/runtime-verification-AC7-${TIMESTAMP}.log"

# Collect environment info once
ENV_INFO="OS=$(uname -sr), gemini=$(command -v "$GEMINI_BIN"), jq=$(command -v jq), uv=$(command -v uv 2>/dev/null || echo 'not found')"

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
    S1_EVENTS="$(echo "$S1_OUTPUT" | jq '.structured_events | length' 2>/dev/null || echo "0")"
    # structured_events > 0 required: proves ACP event stream (agent_message_chunk etc.) was parsed
    if [ "$S1_OK" = "true" ] && [ "$S1_EVENTS" -gt 0 ] 2>/dev/null; then
      echo "PASS: scenario 1 — ok=true, structured_events=$S1_EVENTS"
      S1_VERDICT="PASS"
      log_scenario "AC7 scenario 1 (normal PONG)" \
        "objective=Reply with exactly PONG, transport=acp, model=gemini-2.5-flash" \
        "$S1_OUTPUT" \
        "PASS" "0"
    elif [ "$S1_OK" = "true" ] && [ "$S1_EVENTS" -eq 0 ] 2>/dev/null; then
      echo "FAIL: scenario 1 — ok=true but structured_events=0 (ACP event stream not parsed)"
      S1_VERDICT="FAIL"
      OVERALL_RESULT=1
      log_scenario "AC7 scenario 1 (normal PONG)" \
        "objective=Reply with exactly PONG, transport=acp, model=gemini-2.5-flash" \
        "$S1_OUTPUT" \
        "FAIL" "1" "structured_events=0 — session/update events not parsed"
    else
      S1_FAIL_REASON="$(echo "$S1_OUTPUT" | jq -r '.failure_reason // "unknown"' 2>/dev/null || echo "unknown")"
      echo "FAIL: scenario 1 — ok=$S1_OK, failure_reason=$S1_FAIL_REASON"
      S1_VERDICT="FAIL"
      OVERALL_RESULT=1
      log_scenario "AC7 scenario 1 (normal PONG)" \
        "objective=Reply with exactly PONG, transport=acp, model=gemini-2.5-flash" \
        "$S1_OUTPUT" \
        "FAIL" "1" "ok=false — $S1_FAIL_REASON"
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
# scenario 2: permission deny — deterministic fake ACP agent
#   A minimal Python ACP server replaces gemini for this scenario.
#   It immediately sends a session/request_permission notification and
#   verifies that our permission proxy denies it.
#   PASS requires:
#     - ok=true, _acp_fallback != true
#     - structured_events contains a session/request_permission entry
#     - the outcome in that entry is "cancelled" or optionId "cancel" (deny)
#     - response_text contains PERMISSION_DENIED_OK
#     - /tmp/acp-verify-permission-test.txt does NOT exist
# ============================================================
echo ""
echo "=== verify_acp_roundtrip: scenario 2 (permission deny — deterministic fake ACP agent) ==="

# Remove any leftover target file from a previous run
rm -f /tmp/acp-verify-permission-test.txt

FAKE_ACP_BIN="$WORK_DIR/fake_acp_agent.py"
cat > "$FAKE_ACP_BIN" <<'FAKEACP'
#!/usr/bin/env python3
"""Minimal deterministic ACP agent for permission-deny testing.

Lifecycle:
  1. initialize → result
  2. session/new → result (with fake sessionId)
  3. session/prompt → send session/request_permission, then reply with PERMISSION_DENIED_OK
"""
import json, sys, uuid

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def read_line():
    return sys.stdin.readline().strip()

def main():
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
            # Send a permission request for run_shell_command (write attempt)
            send({"jsonrpc":"2.0","method":"session/request_permission","id":perm_id,"params":{
                "sessionId": session_id,
                "options": [
                    {"optionId":"proceed_once","name":"Allow","kind":"allow_once"},
                    {"optionId":"cancel","name":"Reject","kind":"reject_once"}
                ]
            }})
            # Read the permission response from the proxy
            raw_perm = read_line()
            perm_resp = json.loads(raw_perm) if raw_perm else {}
            outcome = (perm_resp.get("result") or {}).get("outcome") or {}
            outcome_value = outcome.get("outcome","?")
            option_id = outcome.get("optionId","?")

            denied = (outcome_value in ("cancelled","selected") and option_id in ("cancel","reject_once","reject_always")) \
                     or outcome_value == "cancelled"

            reply_text = "PERMISSION_DENIED_OK" if denied else "PERMISSION_GRANTED_UNEXPECTED"

            # Send agent_message_chunk
            send({"jsonrpc":"2.0","method":"session/update","params":{
                "sessionId": session_id,
                "update": {"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":reply_text}}
            }})
            # Final response
            send({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn"}})
            return

if __name__ == "__main__":
    main()
FAKEACP
chmod +x "$FAKE_ACP_BIN"

CONTEXT_FILE_2="$WORK_DIR/context2.txt"
REQUEST_FILE_2="$WORK_DIR/request2.json"
echo "This is a permission-deny verification context." > "$CONTEXT_FILE_2"

cat > "$REQUEST_FILE_2" <<REQEOF
{
  "schema": "delegation_request_v1",
  "transport": "acp",
  "objective": "Run: echo TEST > /tmp/acp-verify-permission-test.txt",
  "instructions": [
    "Execute the shell command to write the file.",
    "If permission is denied, reply with: PERMISSION_DENIED_OK",
    "If the command succeeds, reply with: WRITE_SUCCEEDED"
  ],
  "output_sections": ["Response"],
  "context_files": ["$CONTEXT_FILE_2"],
  "model": "gemini-2.5-flash",
  "timeout_sec": 60
}
REQEOF

S2_OUTPUT=""
S2_VERDICT=""
S2_RC=0

# Run WITHOUT --approve-edits using the fake ACP agent as GEMINI_BIN
GEMINI_BIN="$WORK_DIR/fake_acp_agent.py" run_scenario "s2" "$REQUEST_FILE_2"
S2_RC=$?

if [ -f "$SCENARIO_RESULT_FILE" ]; then
  S2_OUTPUT="$(cat "$SCENARIO_RESULT_FILE")"

  # --- Fallback detection (must come before any PASS) ---
  S2_FALLBACK="$(echo "$S2_OUTPUT" | jq -r '._acp_fallback // false' 2>/dev/null || echo "false")"
  if [ "$S2_FALLBACK" = "true" ]; then
    S2_REASON="$(echo "$S2_OUTPUT" | jq -r '.warnings[0] // "unknown"' 2>/dev/null || echo "unknown")"
    echo "FAIL: scenario 2 — _acp_fallback: true detected."
    echo "      First warning: $S2_REASON"
    log_scenario "AC7 scenario 2 (permission deny)" \
      "fake-acp-agent, no --approve-edits" \
      "$S2_OUTPUT" \
      "FAIL" "1" "_acp_fallback: true — $S2_REASON"
    echo "FAIL: verify_acp_roundtrip — _acp_fallback detected in scenario 2"
    exit 1
  fi

  S2_OK="$(echo "$S2_OUTPUT" | jq -r '.ok' 2>/dev/null || echo "null")"
  S2_PERM_COUNT="$(echo "$S2_OUTPUT" | jq '[.structured_events[]? | select(.type == "session/request_permission")] | length' 2>/dev/null || echo "0")"
  S2_RESPONSE_TEXT="$(echo "$S2_OUTPUT" | jq -r '.response_text // ""' 2>/dev/null || echo "")"
  S2_FILE_EXISTS=0
  [ -f /tmp/acp-verify-permission-test.txt ] && S2_FILE_EXISTS=1

  S2_FAIL_REASONS=""
  [ "$S2_OK" != "true" ] && S2_FAIL_REASONS="${S2_FAIL_REASONS}ok=$S2_OK; "
  [ "$S2_PERM_COUNT" -lt 1 ] 2>/dev/null && S2_FAIL_REASONS="${S2_FAIL_REASONS}permission_request_count=$S2_PERM_COUNT (need >=1); "
  echo "$S2_RESPONSE_TEXT" | grep -q "PERMISSION_DENIED_OK" || S2_FAIL_REASONS="${S2_FAIL_REASONS}response_text missing PERMISSION_DENIED_OK (got: ${S2_RESPONSE_TEXT}); "
  [ "$S2_FILE_EXISTS" -eq 1 ] && S2_FAIL_REASONS="${S2_FAIL_REASONS}/tmp/acp-verify-permission-test.txt exists (write was NOT denied); "

  if [ -z "$S2_FAIL_REASONS" ]; then
    echo "PASS: scenario 2 — permission_request_count=$S2_PERM_COUNT, response=PERMISSION_DENIED_OK, file not created"
    S2_VERDICT="PASS"
    log_scenario "AC7 scenario 2 (permission deny)" \
      "fake-acp-agent, no --approve-edits, permission_request_count=$S2_PERM_COUNT" \
      "$S2_OUTPUT" \
      "PASS" "0"
  else
    echo "FAIL: scenario 2 — $S2_FAIL_REASONS"
    S2_VERDICT="FAIL"
    OVERALL_RESULT=1
    log_scenario "AC7 scenario 2 (permission deny)" \
      "fake-acp-agent, no --approve-edits" \
      "$S2_OUTPUT" \
      "FAIL" "1" "$S2_FAIL_REASONS"
  fi
else
  echo "FAIL: scenario 2 — result file not created"
  S2_VERDICT="FAIL"
  OVERALL_RESULT=1
  log_scenario "AC7 scenario 2 (permission deny)" \
    "objective=write /tmp/acp-verify-permission-test.txt, no --approve-edits, model=gemini-2.5-flash" \
    "(no result file)" \
    "FAIL" "1" "result file not created (run exit=$S2_RC)"
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo "=== Summary ==="
echo "scenario 1 (normal PONG): $S1_VERDICT"
echo "scenario 2 (permission deny): $S2_VERDICT"
echo "Artifact: $LOG_FILE"
echo ""

if [ "$OVERALL_RESULT" -eq 0 ]; then
  echo "PASS: verify_acp_roundtrip — all scenarios passed"
else
  echo "FAIL: verify_acp_roundtrip — one or more scenarios failed"
fi

exit "$OVERALL_RESULT"

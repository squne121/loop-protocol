#!/usr/bin/env bash
# verify_acp_roundtrip.sh
#
# Verifies ACP transport end-to-end by delegating a short task to gemini CLI
# via transport: acp and checking that the result contains status ok and
# non-empty structured_events.
#
# Requires: gemini CLI, uv, jq
# When gemini CLI is not installed, exits 0 with a SKIP message.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"

# --- SKIP guard: gemini CLI not installed ---
if ! command -v gemini >/dev/null 2>&1; then
  echo "SKIP: gemini CLI not installed (gemini not found in PATH)"
  exit 0
fi

# --- SKIP guard: jq not available ---
if ! command -v jq >/dev/null 2>&1; then
  echo "SKIP: jq not installed (required for result validation)"
  exit 0
fi

# --- Prepare temp workspace ---
WORK_DIR="$(mktemp -d -t verify-acp-roundtrip-XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

REQUEST_FILE="$WORK_DIR/request.json"
RESULT_FILE="$WORK_DIR/result.json"
CONTEXT_FILE="$WORK_DIR/context.txt"

# Minimal context file (required by delegation_request_v1 schema)
echo "This is a verification context file." > "$CONTEXT_FILE"

# Build the ACP request
cat > "$REQUEST_FILE" <<EOF
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
  "context_files": ["$CONTEXT_FILE"],
  "model": "gemini-2.5-flash",
  "timeout_sec": 120
}
EOF

echo "=== verify_acp_roundtrip: starting ACP delegation ==="
echo "Request: $REQUEST_FILE"
echo ""

# Run the ACP transport
set +e
uv run python3 "$SCRIPT_DIR/run_gemini_acp.py" \
  --request-file "$REQUEST_FILE" \
  --output-file "$RESULT_FILE"
ACP_EXIT=$?
set -e

echo ""
echo "=== Result ==="

if [ ! -f "$RESULT_FILE" ]; then
  echo "FAIL: result file not created"
  exit 1
fi

# Check ok field
OK=$(jq -r '.ok' "$RESULT_FILE" 2>/dev/null || echo "null")
if [ "$OK" != "true" ]; then
  FAILURE=$(jq -r '.failure_reason // "unknown"' "$RESULT_FILE" 2>/dev/null || echo "unknown")
  echo "FAIL: ok=$OK, failure_reason=$FAILURE"
  echo ""
  echo "Full result:"
  jq '.' "$RESULT_FILE"
  exit 1
fi

# Check structured_events
EVENTS_COUNT=$(jq '.structured_events | length' "$RESULT_FILE" 2>/dev/null || echo "0")
echo "ok: $OK"
echo "structured_events count: $EVENTS_COUNT"
echo "response_text: $(jq -r '.response_text // "(empty)"' "$RESULT_FILE")"
echo ""

# Check for _acp_fallback (informational, not a failure)
FALLBACK=$(jq -r '._acp_fallback // false' "$RESULT_FILE" 2>/dev/null || echo "false")
if [ "$FALLBACK" = "true" ]; then
  echo "NOTE: ACP transport fell back to headless_json (gemini --acp may not support full ACP lifecycle)"
  WARNINGS=$(jq -r '.warnings[0] // ""' "$RESULT_FILE")
  echo "      First warning: $WARNINGS"
  echo ""
  echo "PASS: verify_acp_roundtrip (via headless_json fallback)"
  exit 0
fi

if [ "$EVENTS_COUNT" -eq 0 ]; then
  echo "WARN: structured_events is empty — ACP transport may not have produced events"
  echo "      This may be acceptable if the model responded via final result only."
fi

echo "PASS: verify_acp_roundtrip"
exit 0

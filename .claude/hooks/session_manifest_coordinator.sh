#!/usr/bin/env bash
# session_manifest_coordinator.sh
#
# Coordinator hook for Stop / SubagentStop events.
# Runs guard → producer in strict sequence; on guard failure, suppresses
# standard agent_session_manifest generation and exits 0 (fail-open).
#
# This is the SINGLE hook entry-point for Stop / SubagentStop.
# guard (session_recording_policy_guard.sh) and producer
# (generate_session_manifest_from_hook.mjs) MUST NOT be wired directly
# in settings.json alongside this coordinator.
#
# Design:
#   1. Read stdin once and save to a temp file.
#   2. Check stop_hook_active → short-circuit exit 0 (producer not called).
#   3. Run guard with the saved stdin.
#      - guard exit 0  → proceed to producer
#      - guard non-0   → skip producer, log reason to stderr, exit 0
#   4. Run producer with the saved stdin.
#      - producer failure → log to stderr, exit 0 (best-effort telemetry)
#
# stdin  : JSON hook context from Claude Code hook system
# stdout : empty (silent)
# stderr : diagnostic messages only
# exit   : always 0 (coordinator is best-effort telemetry; never blocks AI agent)
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve script directory (coordinator script location = hooks dir)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GUARD_SCRIPT="${SESSION_MANIFEST_GUARD:-${SCRIPT_DIR}/session_recording_policy_guard.sh}"
PRODUCER_SCRIPT="${SESSION_MANIFEST_PRODUCER:-${SCRIPT_DIR}/generate_session_manifest_from_hook.mjs}"
NODE_BIN="${SESSION_MANIFEST_NODE:-node}"

# ---------------------------------------------------------------------------
# Temporary workspace — cleaned up on exit
# ---------------------------------------------------------------------------
_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT

_STDIN_FILE="${_TMPDIR}/stdin.json"

# AC7: save stdin exactly once; both guard and producer receive the same payload
cat > "$_STDIN_FILE"

# ---------------------------------------------------------------------------
# AC6: stop_hook_active short-circuit
# If stop_hook_active is true, skip producer and exit 0 immediately.
# ---------------------------------------------------------------------------
_STOP_HOOK_ACTIVE=$(python3 -c "
import json, sys
try:
    data = json.load(open('$_STDIN_FILE'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

if [[ "$_STOP_HOOK_ACTIVE" == "true" ]]; then
    echo "[session_manifest_coordinator] info: stop_hook_active=true, short-circuit exit 0" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# AC2 / AC3 / AC4: run guard first
# ---------------------------------------------------------------------------
_GUARD_EXIT=0
# Feed the saved stdin to the guard script
"${GUARD_SCRIPT}" < "$_STDIN_FILE" >"$_TMPDIR/guard.stdout" 2>"$_TMPDIR/guard.stderr" || _GUARD_EXIT=$?
cat "$_TMPDIR/guard.stderr" >&2 2>/dev/null || true

if [[ "$_GUARD_EXIT" -ne 0 ]]; then
    # AC4: guard failure → skip producer, exit 0 (do not block Stop/SubagentStop)
    echo "[session_manifest_coordinator] info: guard exited $_GUARD_EXIT — skipping producer (best-effort, not blocking)" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# AC3 / AC5: guard passed → run producer
# ---------------------------------------------------------------------------
_PRODUCER_EXIT=0
"$NODE_BIN" "${PRODUCER_SCRIPT}" < "$_STDIN_FILE" >"$_TMPDIR/producer.stdout" 2>"$_TMPDIR/producer.stderr" || _PRODUCER_EXIT=$?
cat "$_TMPDIR/producer.stderr" >&2 2>/dev/null || true

if [[ "$_PRODUCER_EXIT" -ne 0 ]]; then
    # AC5: producer failure → exit 0 (do not block Stop/SubagentStop)
    echo "[session_manifest_coordinator] info: producer exited $_PRODUCER_EXIT — best-effort, not blocking" >&2
    exit 0
fi

exit 0

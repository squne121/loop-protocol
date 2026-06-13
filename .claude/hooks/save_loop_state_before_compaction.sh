#!/usr/bin/env bash
# save_loop_state_before_compaction.sh
#
# PreCompact hook: saves loop state artifact before Claude compacts context.
# Design:
#   - stdout: empty (Claude hooks stdout must be silent on allow path)
#   - stderr: diagnostic messages only, 10 lines or fewer
#   - exit:   always 0 (fail-open — compaction MUST NOT be blocked)
#
# stdin: PreCompact hook context JSON from Claude Code hook system
# Hook contract (AC4):
#   - Saves LOOP_STATE artifact to artifacts/ atomically (temp + rename)
#   - On save failure, logs to stderr and exits 0 (fail-open)
#   - stdout is always empty
#   - stderr output is at most 10 lines
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ARTIFACTS_DIR="${LOOP_STATE_ARTIFACTS_DIR:-${REPO_ROOT}/artifacts}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || date -u +%s)"

# ---------------------------------------------------------------------------
# Read stdin (hook context JSON); do not fail if empty
# ---------------------------------------------------------------------------
_STDIN_CONTENT=""
if read -t 0.1 -r _LINE 2>/dev/null; then
    _STDIN_CONTENT="${_LINE}"
    while IFS= read -r _LINE 2>/dev/null; do
        _STDIN_CONTENT="${_STDIN_CONTENT}
${_LINE}"
    done
fi || true

# ---------------------------------------------------------------------------
# Extract session_id for filename uniqueness
# ---------------------------------------------------------------------------
_SESSION_ID=""
if command -v python3 >/dev/null 2>&1 && [ -n "$_STDIN_CONTENT" ]; then
    _SESSION_ID=$(printf '%s' "$_STDIN_CONTENT" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    sid = data.get('session_id') or data.get('sessionId') or ''
    print(str(sid)[:16])
except Exception:
    print('')
" 2>/dev/null || echo "")
fi

_SESSION_SUFFIX="${_SESSION_ID:-nosession}"
_ARTIFACT_BASENAME="loop-state-precompact-${TIMESTAMP}-${_SESSION_SUFFIX}.json"

# ---------------------------------------------------------------------------
# Ensure artifacts directory exists (fail-open on mkdir failure)
# ---------------------------------------------------------------------------
if ! mkdir -p "${ARTIFACTS_DIR}" 2>/dev/null; then
    echo "[save_loop_state_before_compaction] warn: cannot create artifacts dir — skipping (fail-open)" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# Build loop state payload and write atomically
# ---------------------------------------------------------------------------
_TMP_FILE="${ARTIFACTS_DIR}/.tmp-precompact-$$-${TIMESTAMP}"
_FINAL_FILE="${ARTIFACTS_DIR}/${_ARTIFACT_BASENAME}"

_PAYLOAD="{\"hook_event\":\"PreCompact\",\"session_id\":\"${_SESSION_SUFFIX}\",\"saved_at\":\"${TIMESTAMP}\",\"schema\":\"loop_state_precompact_v1\"}"

if printf '%s' "${_PAYLOAD}" > "${_TMP_FILE}" 2>/dev/null && \
   mv "${_TMP_FILE}" "${_FINAL_FILE}" 2>/dev/null; then
    echo "[save_loop_state_before_compaction] info: loop state saved (artifact=${_ARTIFACT_BASENAME})" >&2
else
    echo "[save_loop_state_before_compaction] warn: artifact write failed — skipping (fail-open)" >&2
    rm -f "${_TMP_FILE}" 2>/dev/null || true
fi

# stdout is always empty (allow path stdout policy)
exit 0

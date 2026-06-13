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
#   - artifact contains: schema_version, session_id, trigger, transcript_path hash,
#     cwd, loop_state_ref, loop_state_hash, saved_at, source_hook_input_hash
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
# Ensure artifacts directory exists (fail-open on mkdir failure)
# ---------------------------------------------------------------------------
if ! mkdir -p "${ARTIFACTS_DIR}" 2>/dev/null; then
    echo "[save_loop_state_before_compaction] warn: cannot create artifacts dir — skipping (fail-open)" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# Build loop state payload using Python for proper JSON and field population
# ---------------------------------------------------------------------------
_ARTIFACT_BASENAME="loop-state-precompact-${TIMESTAMP}.json"
_TMP_FILE="${ARTIFACTS_DIR}/.tmp-precompact-$$-${TIMESTAMP}"
_FINAL_FILE="${ARTIFACTS_DIR}/${_ARTIFACT_BASENAME}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[save_loop_state_before_compaction] warn: python3 not found — skipping (fail-open)" >&2
    exit 0
fi

# Write the Python script to a temp file to avoid heredoc-in-heredoc issues
_PY_SCRIPT="${ARTIFACTS_DIR}/.tmp-py-$$-${TIMESTAMP}.py"
cat > "${_PY_SCRIPT}" << 'PYEOF'
import json
import sys
import hashlib

output_path = sys.argv[1]
timestamp = sys.argv[2]

stdin_text = sys.stdin.read()

hook_input = {}
if stdin_text.strip():
    try:
        hook_input = json.loads(stdin_text)
    except Exception:
        hook_input = {}

session_id = (
    hook_input.get('session_id')
    or hook_input.get('sessionId')
    or 'nosession'
)[:64]

trigger = hook_input.get('trigger', 'PreCompact')
cwd = hook_input.get('cwd', '')

# transcript_path is sensitive — store only a hash, not the path itself
transcript_path_raw = hook_input.get('transcript_path', '')
transcript_path_hash = hashlib.sha256(transcript_path_raw.encode()).hexdigest()[:16] if transcript_path_raw else None

# hash of the entire hook stdin for traceability
source_hook_input_hash = hashlib.sha256(stdin_text.encode()).hexdigest()[:32] if stdin_text.strip() else None

# loop_state_ref and loop_state_hash: requires session-exposed LOOP_STATE blob
# (not available in hook stdin — recorded as null for future use)
loop_state_ref = None
loop_state_hash = None

artifact = {
    'schema_version': 'loop_state_precompact_v2',
    'session_id': session_id,
    'trigger': trigger,
    'transcript_path_hash': transcript_path_hash,
    'cwd': cwd,
    'loop_state_ref': loop_state_ref,
    'loop_state_hash': loop_state_hash,
    'saved_at': timestamp,
    'source_hook_input_hash': source_hook_input_hash,
}

with open(output_path, 'w', encoding='utf-8') as f:
    json.dump(artifact, f, ensure_ascii=False)
PYEOF

# Execute Python payload builder, piping stdin content
if printf '%s' "${_STDIN_CONTENT}" | \
    uv run python3 "${_PY_SCRIPT}" "${_TMP_FILE}" "${TIMESTAMP}" 2>/dev/null; then
    rm -f "${_PY_SCRIPT}" 2>/dev/null || true
elif printf '%s' "${_STDIN_CONTENT}" | \
    python3 "${_PY_SCRIPT}" "${_TMP_FILE}" "${TIMESTAMP}" 2>/dev/null; then
    rm -f "${_PY_SCRIPT}" 2>/dev/null || true
else
    rm -f "${_PY_SCRIPT}" 2>/dev/null || true
    echo "[save_loop_state_before_compaction] warn: python3 payload build failed — skipping (fail-open)" >&2
    exit 0
fi

if [ -f "${_TMP_FILE}" ]; then
    if mv "${_TMP_FILE}" "${_FINAL_FILE}" 2>/dev/null; then
        echo "[save_loop_state_before_compaction] info: loop state saved (artifact=${_ARTIFACT_BASENAME})" >&2
    else
        echo "[save_loop_state_before_compaction] warn: artifact rename failed — skipping (fail-open)" >&2
        rm -f "${_TMP_FILE}" 2>/dev/null || true
    fi
else
    echo "[save_loop_state_before_compaction] warn: payload build produced no output — skipping (fail-open)" >&2
fi

# stdout is always empty (allow path stdout policy)
exit 0

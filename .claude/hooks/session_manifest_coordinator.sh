#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GUARD_SCRIPT="${SESSION_MANIFEST_GUARD:-${SCRIPT_DIR}/session_recording_policy_guard.sh}"
PRODUCER_SCRIPT="${SESSION_MANIFEST_PRODUCER:-${SCRIPT_DIR}/generate_session_manifest_from_hook.mjs}"
DEBOUNCE_SCRIPT="${SESSION_MANIFEST_DEBOUNCE_SCRIPT:-${SCRIPT_DIR}/session_manifest_debounce.mjs}"
NODE_BIN="${SESSION_MANIFEST_NODE:-node}"
COORDINATOR_STEP_TIMEOUT_SECONDS="${SESSION_MANIFEST_COORDINATOR_STEP_TIMEOUT_SECONDS:-15}"
SCOPE_ROLLUP_CAPTURE_SCRIPT="${SCOPE_ROLLUP_CAPTURE_SCRIPT:-${SCRIPT_DIR}/capture_scope_rollup_final_response.py}"
SCOPE_ROLLUP_CAPTURE_PYTHON="${SCOPE_ROLLUP_CAPTURE_PYTHON:-python3}"
SCOPE_ROLLUP_CAPTURE_DIR="${SCOPE_ROLLUP_CAPTURE_DIR:-/tmp}"

_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT
_STDIN_FILE="${_TMPDIR}/stdin.json"
cat > "$_STDIN_FILE"

if [[ -f "$SCOPE_ROLLUP_CAPTURE_SCRIPT" ]]; then
  if ! "$SCOPE_ROLLUP_CAPTURE_PYTHON" -c "import yaml" >/dev/null 2>&1; then
    "$SCOPE_ROLLUP_CAPTURE_PYTHON" - "$_STDIN_FILE" "$SCOPE_ROLLUP_CAPTURE_DIR" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

stdin_file = Path(sys.argv[1])
capture_dir = Path(sys.argv[2]).resolve()
payload = json.loads(stdin_file.read_text(encoding="utf-8"))
message = payload.get("last_assistant_message") or ""
match = re.search(r"^\s*invocation_id:\s*['\"]?([A-Za-z0-9._:-]+)['\"]?\s*$", message, re.MULTILINE)
invocation_id = match.group(1) if match else None
safe_invocation_id = re.sub(r"[^A-Za-z0-9._-]+", "_", invocation_id) if invocation_id else None
payload_digest = hashlib.sha256(
    json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
).hexdigest()[:12]
stem = f"scope_rollup_{safe_invocation_id}" if safe_invocation_id else f"scope_rollup_unsupported_hook_unavailable_{payload_digest}"
record_path = capture_dir / f"{stem}.capture.yaml"
if not record_path.exists():
    record_path.write_text(
        "\n".join(
            [
                "SCOPE_ROLLUP_CAPTURE_RESULT_V1:",
                "  capture_mode: unsupported",
                "  capture_status: hook_unavailable",
                "  parser_status: not_applicable",
                "  capture_routing_action: stop_human",
                "  routing_action: stop_human",
                f"  agent_type: {payload.get('agent_type')!s}",
                f"  invocation_id: {invocation_id!s}",
                "  capture_path: null",
                "  capture_sha256: null",
                "  capture_source: last_assistant_message",
                "  notes:",
                "    - hook runtime python is missing PyYAML",
                "",
            ]
        ),
        encoding="utf-8",
    )
PY
    echo "[session_manifest_coordinator] info: scope-rollup capture python lacks PyYAML; wrote hook_unavailable record" >&2
  else
    _SCOPE_ROLLUP_CAPTURE_EXIT=0
    "$SCOPE_ROLLUP_CAPTURE_PYTHON" "$SCOPE_ROLLUP_CAPTURE_SCRIPT" < "$_STDIN_FILE" \
      >"$_TMPDIR/scope_rollup_capture.stdout" 2>"$_TMPDIR/scope_rollup_capture.stderr" || _SCOPE_ROLLUP_CAPTURE_EXIT=$?
    cat "$_TMPDIR/scope_rollup_capture.stderr" >&2 2>/dev/null || true
    if [[ "$_SCOPE_ROLLUP_CAPTURE_EXIT" -ne 0 ]]; then
      echo "[session_manifest_coordinator] info: scope-rollup capture exited $_SCOPE_ROLLUP_CAPTURE_EXIT — preparation must fail closed if capture is required" >&2
    fi
  fi
fi

sanitize_stderr() {
  python3 - "$1" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore")
text = re.sub(r"[A-Za-z]:\\[^\s\"']+", "<path>", text)
text = re.sub(r"/mnt/[A-Za-z]/[^\s\"']+", "<path>", text)
text = re.sub(r"/[^\s\"']+", "<path>", text)
lines = [line.strip() for line in text.splitlines() if line.strip()]
for line in lines[:2]:
    print(line[:220])
PY
}

collect_summary() {
  local step="$1"
  local status="$2"
  local reason_code="$3"
  local stderr_file="$4"
  local line="step=${step} status=${status} reason_code=${reason_code}"
  local details
  details="$(sanitize_stderr "$stderr_file")"
  if [[ -n "$details" ]]; then
    line="${line} detail=$(echo "$details" | head -n1)"
  fi
  SUMMARY_LINES+=("$line")
}

run_step() {
  local step="$1"
  local command="$2"
  local stderr_file="${_TMPDIR}/${step}.stderr"
  local stdout_file="${_TMPDIR}/${step}.stdout"
  : > "$stderr_file"
  : > "$stdout_file"

  local exit_code=0
  timeout "${COORDINATOR_STEP_TIMEOUT_SECONDS}s" bash -lc "$command" < "$_STDIN_FILE" >"$stdout_file" 2>"$stderr_file" || exit_code=$?

  if [[ "$exit_code" -eq 124 ]]; then
    collect_summary "$step" "timeout" "${step}_timeout" "$stderr_file"
    return 124
  fi
  if [[ "$exit_code" -ne 0 ]]; then
    collect_summary "$step" "warn" "${step}_failed" "$stderr_file"
    return "$exit_code"
  fi

  collect_summary "$step" "ok" "none" "$stderr_file"
  return 0
}

STOP_HOOK_ACTIVE="$(python3 -c "
import json
try:
    data = json.load(open('${_STDIN_FILE}'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except Exception:
    print('false')
")"

declare -a SUMMARY_LINES=()
TIMEOUT_REASON=""

if [[ "$STOP_HOOK_ACTIVE" == "true" ]]; then
  SUMMARY_LINES+=("step=stop_guard status=ok reason_code=stop_hook_active")
  echo "SESSION_MANIFEST_COORDINATOR_RESULT_V1={\"status\":\"ok\",\"reason_code\":null,\"timeout_reason\":null,\"steps\":[\"stop_guard\"]}" >&2
  exit 0
fi

DEBOUNCE_COMMAND="'${NODE_BIN}' '${DEBOUNCE_SCRIPT}' --flush"
GUARD_COMMAND="'${GUARD_SCRIPT}'"
PRODUCER_COMMAND="'${NODE_BIN}' '${PRODUCER_SCRIPT}'"

run_step "debounce_flush" "$DEBOUNCE_COMMAND" || true

guard_exit=0
run_step "guard" "$GUARD_COMMAND" || guard_exit=$?
if [[ "$guard_exit" -eq 124 ]]; then
  TIMEOUT_REASON="guard_timeout"
fi
if [[ "$guard_exit" -ne 0 ]]; then
  if [[ -z "$TIMEOUT_REASON" ]]; then
    TIMEOUT_JSON="null"
  else
    TIMEOUT_JSON="\"${TIMEOUT_REASON}\""
  fi
  printf '%s\n' "${SUMMARY_LINES[@]}" | head -n 9 >&2
  echo "SESSION_MANIFEST_COORDINATOR_RESULT_V1={\"status\":\"ok\",\"reason_code\":\"guard_failed\",\"timeout_reason\":${TIMEOUT_JSON},\"steps\":[\"debounce_flush\",\"guard\"]}" >&2
  exit 0
fi

producer_exit=0
run_step "producer" "$PRODUCER_COMMAND" || producer_exit=$?
if [[ "$producer_exit" -eq 124 ]]; then
  TIMEOUT_REASON="producer_timeout"
fi

printf '%s\n' "${SUMMARY_LINES[@]}" | head -n 9 >&2
if [[ -n "$TIMEOUT_REASON" ]]; then
  TIMEOUT_JSON="\"${TIMEOUT_REASON}\""
else
  TIMEOUT_JSON="null"
fi
echo "SESSION_MANIFEST_COORDINATOR_RESULT_V1={\"status\":\"ok\",\"reason_code\":null,\"timeout_reason\":${TIMEOUT_JSON},\"steps\":[\"debounce_flush\",\"guard\",\"producer\"]}" >&2
exit 0

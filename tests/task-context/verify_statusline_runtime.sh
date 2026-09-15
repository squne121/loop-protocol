#!/usr/bin/env bash
# verify_statusline_runtime.sh -- Issue #2634 AC7.
#
# PR-time ONE-SHOT live runtime verification (never a permanent CI required
# gate) that the ACTUAL Claude Code statusLine execution path --
# `.claude/settings.json`'s `statusLine.command`
# (`python3 .claude/hooks/task_context/statusline.py`), which in turn calls
# into the existing `task-contextctl query current` live data path
# (`scripts/task-context/task_contextctl.py` via
# `.claude/hooks/task_context/ctl_client.py`) -- genuinely renders the
# Issue #2634 presentation contract (AC1-AC4) in this environment. No new
# persistent monitor / harness is introduced: this reuses the exact
# subprocess chain `statusline.py` already uses in production.
#
# Exit code / SKIP contract (docs/dev/runtime-verification-policy.md):
#   0   PASS
#   1   FAIL (includes any fallback-shaped "success")
#   77  SKIP -- Task Context DB / live session unavailable in this
#       environment. SKIP is never PASS.
#
# What is verified live (never re-derived from a unit-test mock):
#   1. no-session stdin -> `main()`'s no-session branch -> stdout == "Unbound"
#      (AC3), via a REAL subprocess invocation of statusline.py.
#   2. a synthetic, guaranteed-unbound session_id -> the REAL
#      `ctl_client.call_query_current_by_session()` -> REAL
#      `task_contextctl.py query current` subprocess -> REAL read-only DB
#      query (`get_current_projection_for_session` raising
#      `NotFoundError` -> `degraded_reason="no_binding_for_session"`) ->
#      `render()` -> stdout == "Unbound" (AC1/AC3), exercising the full live
#      round trip end to end.
#   3. opportunistic (best-effort, does not affect PASS/FAIL): if this
#      environment currently has ANY live Task Context Binding claimed by a
#      real `current_claude_session_id` (this repo instance's shared state
#      DB -- worktrees of one repo share one DB per
#      `task_context_config.repo_instance_key()`), that session_id is also
#      run through the real statusLine command and the output is logged as
#      additional live evidence (structural checks only: no legacy
#      `[Task Context]` prefix, no crash).
#
# fallback_policy (docs/dev/runtime-verification-policy.md #3): this script
# never treats a subprocess crash / non-JSON output / unexpected exception
# as a "successful" degraded/empty response -- those are FAIL, not
# reinterpreted as PASS via any fallback path.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STATUSLINE_PY="$REPO_ROOT/.claude/hooks/task_context/statusline.py"
TASK_CONTEXTCTL_PY="$REPO_ROOT/scripts/task-context/task_contextctl.py"

TESTED_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_DIR="$REPO_ROOT/artifacts"
mkdir -p "$ARTIFACT_DIR"
LOG_FILE="$ARTIFACT_DIR/runtime-verification-AC7-${TIMESTAMP}.log"

_log() {
  printf '%s\n' "$*" >>"$LOG_FILE"
}

_write_header() {
  {
    echo "=== Runtime Verification Log ==="
    echo "AC: AC7 (Issue #2634) -- statusLine presentation contract (AC1-AC4) via the real statusLine execution path"
    echo "Timestamp: $TIMESTAMP"
    echo "Environment: tested_head=$TESTED_HEAD python3=$(command -v python3 2>/dev/null || echo missing)"
    echo ""
    echo "--- Input ---"
    echo "statusline.py: $STATUSLINE_PY"
    echo "task_contextctl.py: $TASK_CONTEXTCTL_PY"
  } >>"$LOG_FILE"
}
_write_header

_finish() {
  status="$1"
  reason="$2"
  exit_code="$3"
  {
    echo ""
    echo "--- Verdict ---"
    echo "Result: $status"
    echo "Exit Code: $exit_code"
    echo "Reason: $reason"
  } >>"$LOG_FILE"
  case "$status" in
    SKIP) echo "SKIP: $reason" ;;
    FAIL) echo "FAIL: $reason" ;;
    PASS) echo "PASS: $reason" ;;
  esac
  echo "evidence log: $LOG_FILE"
  exit "$exit_code"
}

# --- skip_condition: python3 / statusline.py / task_contextctl.py missing -
if ! command -v python3 >/dev/null 2>&1; then
  _finish SKIP "python3 not found in PATH" 77
fi
if [ ! -f "$STATUSLINE_PY" ]; then
  _finish SKIP "statusline.py not found at $STATUSLINE_PY" 77
fi
if [ ! -f "$TASK_CONTEXTCTL_PY" ]; then
  _finish SKIP "task_contextctl.py not found at $TASK_CONTEXTCTL_PY -- the live data path statusline.py depends on is unavailable" 77
fi

# --- skip_condition: live Task Context DB unavailable in this environment.
# Resolved exactly the way task_context_config.resolve_state_root() /
# db_path() do (worktrees of one repo share the same DB, keyed off the
# canonical git common-dir) -- never re-derived independently here.
DB_PATH="$(
  cd "$REPO_ROOT" && python3 - <<'PYEOF' 2>>"$LOG_FILE"
import sys

sys.path.insert(0, "scripts/task-context")
import task_context_config as config  # noqa: E402

try:
    print(config.db_path())
except Exception as exc:  # noqa: BLE001 - resolution failure must not crash the shell script
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF
)"
DB_PATH_STATUS=$?
_log "resolved live DB path: $DB_PATH (resolve exit=$DB_PATH_STATUS)"

if [ "$DB_PATH_STATUS" -ne 0 ] || [ -z "$DB_PATH" ]; then
  _finish SKIP "could not resolve the live Task Context DB path (git repo / state root resolution unavailable in this environment)" 77
fi
if [ ! -f "$DB_PATH" ]; then
  _finish SKIP "no live Task Context DB found at $DB_PATH -- Task Context has never been initialized in this environment" 77
fi

# --- live check 1: no-session stdin -> "Unbound" (AC3) ---------------------
_log ""
_log "--- Output ---"
_log "[check 1] no-session stdin"
OUT_1="$(printf '{}' | python3 "$STATUSLINE_PY" 2>>"$LOG_FILE")"
EXIT_1=$?
_log "exit=$EXIT_1 stdout=$OUT_1"

if [ "$EXIT_1" -ne 0 ]; then
  _finish FAIL "statusline.py exited $EXIT_1 on no-session stdin (expected 0) -- see log" 1
fi
if [ "$OUT_1" != "Unbound" ]; then
  _finish FAIL "no-session stdin: expected stdout 'Unbound', got '$OUT_1'" 1
fi

# --- live check 2: synthetic unbound session_id -> real DB round trip ------
# a session_id this random draw guarantees is not currently claimed by any
# tab_bindings row -- exercises the REAL
# get_current_projection_for_session() NotFoundError ->
# degraded_reason="no_binding_for_session" -> render() -> "Unbound" path,
# through the real ctl_client subprocess boundary (never mocked here).
SYNTHETIC_SESSION_ID="runtime-verify-ac7-$(python3 -c 'import uuid; print(uuid.uuid4())')"
_log ""
_log "[check 2] synthetic unbound session_id=$SYNTHETIC_SESSION_ID"
OUT_2="$(printf '{"session_id": "%s"}' "$SYNTHETIC_SESSION_ID" | python3 "$STATUSLINE_PY" 2>>"$LOG_FILE")"
EXIT_2=$?
_log "exit=$EXIT_2 stdout=$OUT_2"

if [ "$EXIT_2" -ne 0 ]; then
  _finish FAIL "statusline.py exited $EXIT_2 on synthetic unbound session_id (expected 0) -- see log" 1
fi
if [ "$OUT_2" != "Unbound" ]; then
  _finish FAIL "synthetic unbound session_id: expected stdout 'Unbound' (live DB round trip via degraded_reason=no_binding_for_session), got '$OUT_2' -- fallback-shaped output is FAIL, not PASS" 1
fi

# --- live check 3 (opportunistic, best-effort, never affects PASS/FAIL) ----
# discover any session_id currently live-bound to a Task Context Binding in
# this repo instance's shared state DB, and run it through the real
# statusLine command as additional live evidence.
LIVE_SESSION_ID="$(
  cd "$REPO_ROOT" && python3 - "$DB_PATH" <<'PYEOF' 2>>"$LOG_FILE"
import sqlite3
import sys

db_path = sys.argv[1]
try:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT current_claude_session_id FROM tab_bindings "
        "WHERE current_claude_session_id IS NOT NULL LIMIT 1"
    ).fetchone()
    conn.close()
except Exception as exc:  # noqa: BLE001 - opportunistic probe must never crash the script
    print(f"probe error: {exc}", file=sys.stderr)
    sys.exit(0)
if row and row[0]:
    print(row[0])
PYEOF
)"
_log ""
_log "[check 3, opportunistic] discovered live-bound session_id: ${LIVE_SESSION_ID:-<none>}"

if [ -n "$LIVE_SESSION_ID" ]; then
  OUT_3="$(printf '{"session_id": "%s"}' "$LIVE_SESSION_ID" | python3 "$STATUSLINE_PY" 2>>"$LOG_FILE")"
  EXIT_3=$?
  _log "exit=$EXIT_3 stdout=$OUT_3"
  if [ "$EXIT_3" -eq 0 ]; then
    case "$OUT_3" in
      "[Task Context]"*)
        _finish FAIL "opportunistic live-bound session_id check: stdout still uses the removed legacy '[Task Context]' prefix ('$OUT_3') -- AC2 regression" 1
        ;;
      *)
        _log "opportunistic live-bound session_id check: presentation shape OK (no legacy prefix)"
        ;;
    esac
  else
    _log "opportunistic live-bound session_id check: statusline.py exited $EXIT_3 (non-fatal to this script's overall verdict, logged only)"
  fi
fi

if [ -n "$LIVE_SESSION_ID" ]; then
  OPPORTUNISTIC_NOTE="observed a live-bound session and confirmed its presentation shape, see log"
else
  OPPORTUNISTIC_NOTE="none observed in this environment, not required for PASS"
fi
_finish PASS "live statusLine execution path (statusline.py -> ctl_client -> task_contextctl.py query current -> DB) rendered 'Unbound' for both the no-session and synthetic-unbound-session_id cases against the live DB at $DB_PATH. Opportunistic live-bound session check: $OPPORTUNISTIC_NOTE." 0

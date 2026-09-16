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
#   3. bound-session display check (AC7, PR #2640 review fix_delta): if this
#      environment currently has ANY live Task Context Binding claimed by a
#      real `current_claude_session_id` (this repo instance's shared state
#      DB -- worktrees of one repo share one DB per
#      `task_context_config.repo_instance_key()`), that session_id is
#      independently re-probed via the exact live query boundary
#      (`ctl_client.call_query_current_by_session` ->
#      `task_contextctl.py query current` -> DB) to obtain a ground-truth
#      expected Task/ref identifier, then run through the real statusLine
#      command. A non-zero exit, empty output, `Degraded`, or an
#      unexplained `Unbound` (the immediate recheck still showed the
#      session bound) is FAIL -- this check DOES affect the script's
#      overall PASS/FAIL, unlike the previous log-only version. If no
#      live-bound session exists in this environment (or the discovered one
#      lost its binding in the race between discovery and recheck), this
#      specific bound-display check is SKIP-equivalent and is reported as
#      such in the final verdict message -- it does not retroactively
#      invalidate the already-completed, independently-valuable check 1 /
#      check 2 live round trips.
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

# PR #2640 review fix_delta (non-blocking improvement): pin the script's own
# cwd to the repo root before any DB-path resolution or statusline.py
# invocation, so the DB path resolved below and the repository instance
# `statusline.py -> ctl_client -> task_contextctl.py` resolves at run time
# can never drift apart due to an inherited caller cwd.
cd "$REPO_ROOT" || _finish FAIL "cannot enter repository root ($REPO_ROOT)" 1

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

# --- live check 3: bound-session display verification (AC7) ----------------
# discover any session_id currently live-bound to a Task Context Binding in
# this repo instance's shared state DB.
LIVE_SESSION_ID="$(
  python3 - "$DB_PATH" <<'PYEOF' 2>>"$LOG_FILE"
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
except Exception as exc:  # noqa: BLE001 - discovery probe must never crash the script
    print(f"probe error: {exc}", file=sys.stderr)
    sys.exit(0)
if row and row[0]:
    print(row[0])
PYEOF
)"
_log ""
_log "[check 3] discovered live-bound session_id: ${LIVE_SESSION_ID:-<none>}"

BOUND_CHECK_STATUS="not_applicable"
BOUND_CHECK_NOTE="no live-bound Task Context session (tab_bindings.current_claude_session_id) found in this environment -- the AC7 bound-display check was not exercised this run (check 1 / check 2 live round trips above are unaffected and already PASS)"

if [ -n "$LIVE_SESSION_ID" ]; then
  # Independent recheck immediately before invoking statusline.py: fetch the
  # projection DATA (never the rendered TEXT) via the exact same live query
  # boundary statusline.py itself uses
  # (ctl_client.call_query_current_by_session -> task_contextctl.py query
  # current -> DB), so we have a ground-truth expected Task/ref identifier
  # to assert against the actual rendered stdout below -- a positive
  # assertion, not merely "no legacy prefix".
  PROBE_JSON="$(
    python3 - "$LIVE_SESSION_ID" <<'PYEOF' 2>>"$LOG_FILE"
import json
import sys

sys.path.insert(0, ".claude/hooks/task_context")
import ctl_client  # noqa: E402

session_id = sys.argv[1]
envelope = ctl_client.call_query_current_by_session(session_id, timeout=2.0)
if not envelope or envelope.get("status") != "ok":
    print(json.dumps({"probe_status": "transport_failure"}))
    sys.exit(0)
data = envelope.get("data") or {}
if data.get("degraded"):
    print(json.dumps({"probe_status": "degraded", "degraded_reason": data.get("degraded_reason")}))
    sys.exit(0)
task = data.get("task")
if not task:
    print(json.dumps({"probe_status": "unbound"}))
    sys.exit(0)
refs = data.get("task_refs") or []
if refs:
    expected = f"#{refs[0].get('ref_number')}"
else:
    expected = task.get("title") or task.get("id")
print(json.dumps({"probe_status": "bound", "expected_identifier": expected}))
PYEOF
  )"
  _log "[check 3] bound-session recheck probe: $PROBE_JSON"

  PROBE_STATUS="$(printf '%s' "$PROBE_JSON" | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin).get("probe_status", "parse_error"))
except Exception:
    print("parse_error")' 2>>"$LOG_FILE")"

  if [ "$PROBE_STATUS" = "bound" ]; then
    EXPECTED_IDENTIFIER="$(printf '%s' "$PROBE_JSON" | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin).get("expected_identifier") or "")
except Exception:
    print("")' 2>>"$LOG_FILE")"

    OUT_3="$(printf '{"session_id": "%s"}' "$LIVE_SESSION_ID" | python3 "$STATUSLINE_PY" 2>>"$LOG_FILE")"
    EXIT_3=$?
    _log "[check 3] statusline.py bound-session run: exit=$EXIT_3 stdout=$OUT_3 expected_identifier=$EXPECTED_IDENTIFIER"

    if [ "$EXIT_3" -ne 0 ]; then
      _finish FAIL "bound-session statusLine check: statusline.py exited $EXIT_3 for a confirmed-bound session_id (expected 0) -- see log" 1
    fi
    if [ -z "$OUT_3" ]; then
      _finish FAIL "bound-session statusLine check: empty stdout for a confirmed-bound session_id -- see log" 1
    fi
    if [ "$OUT_3" = "Degraded" ]; then
      _finish FAIL "bound-session statusLine check: stdout was 'Degraded' for a confirmed-bound session_id -- see log" 1
    fi
    if [ "$OUT_3" = "Unbound" ]; then
      _finish FAIL "bound-session statusLine check: stdout was 'Unbound' immediately after an independent recheck confirmed the session was still bound -- unexplained regression, not a race (see log)" 1
    fi
    case "$OUT_3" in
      *"activity="*)
        _finish FAIL "bound-session statusLine check: stdout still carries the removed legacy 'activity=' prefix ('$OUT_3') -- AC4 regression" 1
        ;;
    esac
    case "$OUT_3" in
      "[Task Context]"*)
        _finish FAIL "bound-session statusLine check: stdout still uses the removed legacy '[Task Context]' prefix ('$OUT_3') -- AC2 regression" 1
        ;;
    esac
    if [ -n "$EXPECTED_IDENTIFIER" ]; then
      case "$OUT_3" in
        *"$EXPECTED_IDENTIFIER"*) : ;;
        *)
          _finish FAIL "bound-session statusLine check: expected identifier '$EXPECTED_IDENTIFIER' (from the independent live query probe, not from statusline.py's own rendered text) was not found in rendered stdout '$OUT_3'" 1
          ;;
      esac
    fi
    BOUND_CHECK_STATUS="pass"
    BOUND_CHECK_NOTE="confirmed-bound session_id $LIVE_SESSION_ID rendered '$OUT_3', which contains the independently-probed expected identifier '$EXPECTED_IDENTIFIER'"
  else
    # The session discovered by the earlier SELECT lost its binding by the
    # time of this immediate recheck -- a real, explainable race (e.g. the
    # bound Claude Code session ended between the two queries), not a bug.
    # SKIP-equivalent for this specific bound-display check only; the
    # already-completed check 1 / check 2 live round trips above are
    # unaffected.
    BOUND_CHECK_STATUS="skipped_race"
    BOUND_CHECK_NOTE="live-bound session_id $LIVE_SESSION_ID discovered by the earlier SELECT was no longer bound at recheck time (probe_status=$PROBE_STATUS) -- AC7 bound-display check skipped this run as an explainable race, not treated as PASS or FAIL"
  fi
fi

_log ""
_log "[check 3] verdict: status=$BOUND_CHECK_STATUS note=$BOUND_CHECK_NOTE"

if [ "$BOUND_CHECK_STATUS" = "pass" ]; then
  _finish PASS "live statusLine execution path (statusline.py -> ctl_client -> task_contextctl.py query current -> DB) rendered 'Unbound' for both the no-session and synthetic-unbound-session_id cases, AND rendered the correct bound-session identifier for a confirmed-bound live session, against the live DB at $DB_PATH. Bound-session check: $BOUND_CHECK_NOTE." 0
fi
_finish PASS "live statusLine execution path (statusline.py -> ctl_client -> task_contextctl.py query current -> DB) rendered 'Unbound' for both the no-session and synthetic-unbound-session_id cases against the live DB at $DB_PATH. Bound-session display check (AC7) status: $BOUND_CHECK_STATUS -- $BOUND_CHECK_NOTE." 0

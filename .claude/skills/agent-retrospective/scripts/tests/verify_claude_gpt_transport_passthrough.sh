#!/usr/bin/env bash
# verify_claude_gpt_transport_passthrough.sh -- Issue #2445 AC3.
#
# PR-time ONE-SHOT live verification (never a permanent CI required gate --
# Issue #2445 Out of Scope) that `run_retrospective.py`'s production
# nested-invocation adapter (`invoke_agent()` / `build_agent_invocation_argv()`
# / `DelegatedAgentPermissionPolicy.sanitize_subprocess_env()`) actually
# forwards Claude-GPT proxy transport env (`ANTHROPIC_BASE_URL` /
# `ANTHROPIC_AUTH_TOKEN` / model-alias env) to a REAL nested `claude`
# subprocess it spawns.
#
# Claim: `transport_observed` (OWNER review
# https://github.com/squne121/loop-protocol/pull/2453#issuecomment-5469097778
# P0-2), NOT exclusive attribution. Within a dedicated fresh/exclusive
# run-scoped `claude-code-proxy` instance, both the outer session's own
# traffic and the nested observer's expected Haiku-tier `/v1/messages`
# traffic are observed, and every observed request succeeded via the
# expected transport (proxy), never a fallback. Which single reqId belongs
# to the nested invocation is NOT determined here -- model-alias names
# (below) are observational evidence, not a causal/exclusive-ownership ID
# (Claude Code's own docs note the Haiku-tier alias is also used for
# background functionality, so "alias == this specific nested call" cannot
# be asserted as a proof of exclusive ownership).
#
# Design (why a dedicated ephemeral CLAUDE_GPT_HOME, not the caller's real
# one):
#   1. A fresh, per-run `CLAUDE_GPT_HOME` (a `mktemp -d` scratch directory,
#      never under the repository/worktree) guarantees the scoped transport
#      log does not exist before this run starts ("空または新規作成された
#      状態" -- AC3's literal requirement; re-using an existing/appended-to
#      log is explicitly forbidden by the Issue body).
#   2. Only the real ChatGPT subscription credential
#      (`<real_home>/proxy-config/codex/auth.json`) is copied into the
#      scratch profile -- nothing else. `codex auth status` inside the
#      scratch profile must independently confirm this seeded credential
#      actually authenticates (never assumed from file presence alone).
#   3. `scripts/claude-gpt/launch.sh` (read, never modified -- Allowed Paths
#      excludes it) is invoked with this scratch `CLAUDE_GPT_HOME`,
#      `-p "<prompt>"` telling the outer live Claude-GPT session to run,
#      via its own Bash tool, a small ephemeral driver script (NOT a
#      repository-tracked file -- written to the same scratch directory)
#      that calls `run_retrospective.py`'s real `invoke_agent()` with a
#      trivial `retrospective-runtime-observer` (Haiku-tier) request.
#   4. `transport_observed` requires seeing at least one Haiku-tier
#      (`gpt-5.6-luna`, `retrospective-runtime-observer`'s pinned
#      `model: haiku`) upstream request in the scoped log -- its absence
#      is `fallback_suspected` (Stop Condition: FAIL, not SKIP -- the
#      nested call succeeded WITHOUT the proxy, i.e. it fell back to some
#      other transport). Multiple Haiku-tier requests, and/or the outer
#      session's own `main`-alias (`gpt-5.6-terra`) requests mixed into the
#      same scoped log, are expected and allowed -- this script does not
#      attempt to reduce them to a single exclusively-attributed reqId.
#   5. `transport_log.py` (read-only use of the existing canonical
#      transport-policy validator -- never duplicated here) independently
#      confirms that every single request in the whole scoped log (both
#      outer and nested-observer traffic) is a confirmed `http` /
#      `/v1/messages` / `200` round trip -- i.e. all observed traffic used
#      the expected transport, none fell back.
#
# Exit code / SKIP contract (docs/dev/runtime-verification-policy.md):
#   0   PASS
#   1   FAIL (includes fallback-suspected "success")
#   77  SKIP -- environment unavailable (claude / claude-code-proxy missing,
#       or ChatGPT subscription auth unavailable). SKIP is never PASS.
#
# Per Issue #2445 Out of Scope: this script is a one-shot PR-time check, not
# a permanent CI required gate / harness.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../../.." && pwd)"
LIB_SH="$REPO_ROOT/scripts/claude-gpt/lib.sh"
LAUNCH_SH="$REPO_ROOT/scripts/claude-gpt/launch.sh"
TRANSPORT_LOG_PY="$REPO_ROOT/scripts/claude-gpt/transport_log.py"

TESTED_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_DIR="$SCRIPT_DIR/artifacts"
mkdir -p "$ARTIFACT_DIR"
LOG_FILE="$ARTIFACT_DIR/runtime-verification-AC3-${TIMESTAMP}.log"

_log() {
  printf '%s\n' "$*" | tee -a "$LOG_FILE"
}

_write_header() {
  {
    echo "=== Runtime Verification Log ==="
    echo "AC: AC3 (Issue #2445) -- transport_observed: nested claude subprocess Claude-GPT proxy transport routing"
    echo "Timestamp: $TIMESTAMP"
    echo "Environment: tested_head=$TESTED_HEAD"
    echo ""
    echo "--- Input ---"
    echo "script: $SCRIPT_DIR/verify_claude_gpt_transport_passthrough.sh"
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

# --- skip_condition: claude / claude-code-proxy binaries not on PATH ------
if ! command -v claude >/dev/null 2>&1; then
  _finish SKIP "claude binary not found in PATH" 77
fi
if ! command -v claude-code-proxy >/dev/null 2>&1; then
  _finish SKIP "claude-code-proxy binary not found in PATH" 77
fi

# --- skip_condition: real ChatGPT subscription credential unavailable ----
# Uses the CALLER's real (unmodified) CLAUDE_GPT_HOME solely to LOCATE the
# credential to seed the scratch profile below -- launch.sh itself is never
# invoked against this real profile.
# shellcheck source=./../../../../scripts/claude-gpt/lib.sh
. "$LIB_SH"
REAL_PROXY_CONFIG_DIR="$(claude_gpt_proxy_config_dir)"
REAL_AUTH_JSON="$REAL_PROXY_CONFIG_DIR/codex/auth.json"
if [ ! -f "$REAL_AUTH_JSON" ]; then
  _finish SKIP "no ChatGPT subscription credential found at $REAL_AUTH_JSON (real profile never used directly by this script; only its credential is copied into a scratch profile)" 77
fi

PROXY_BIN="$(claude_gpt_resolve_proxy_bin)"
REAL_AUTH_STATUS_OUTPUT="$(HOME="$(claude_gpt_proxy_home_dir)" CCP_CONFIG_DIR="$REAL_PROXY_CONFIG_DIR" "$PROXY_BIN" codex auth status 2>&1)"
case "$REAL_AUTH_STATUS_OUTPUT" in
  *Account:*) : ;;
  *)
    _finish SKIP "ChatGPT subscription auth unavailable (codex auth status did not report an Account)" 77
    ;;
esac

# --- scratch CLAUDE_GPT_HOME: run-scoped, exclusively-owned, guaranteed to
# start with NO transport log (never the caller's shared real profile) -----
SCRATCH_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/issue-2445-ac3-transport-verify.XXXXXX")"
_cleanup() {
  # some tooling launched inside the outer session (e.g. an npm cache) may
  # leave read-only files/directories behind -- widen permissions first so
  # cleanup does not silently leave scratch residue in $TMPDIR.
  chmod -R u+w "$SCRATCH_ROOT" 2>/dev/null || true
  rm -rf "$SCRATCH_ROOT"
}
trap _cleanup EXIT

export CLAUDE_GPT_HOME="$SCRATCH_ROOT/.claude-gpt"
SCRATCH_PROXY_CONFIG_DIR="$(claude_gpt_proxy_config_dir)"
SCRATCH_STATE_DIR="$(claude_gpt_proxy_state_dir)"
SCRATCH_LOG_PATH="$SCRATCH_STATE_DIR/claude-code-proxy/proxy.log"

mkdir -p "$SCRATCH_PROXY_CONFIG_DIR/codex"
cp "$REAL_AUTH_JSON" "$SCRATCH_PROXY_CONFIG_DIR/codex/auth.json"
chmod 600 "$SCRATCH_PROXY_CONFIG_DIR/codex/auth.json"

if [ -e "$SCRATCH_LOG_PATH" ]; then
  # Defensive: a brand-new mktemp scratch tree can never already contain
  # this file -- if it somehow does, refuse to proceed rather than risk
  # attributing pre-existing traffic to this run (fail-closed).
  _finish FAIL "scratch transport log unexpectedly pre-exists at $SCRATCH_LOG_PATH -- refusing to treat it as run-scoped/fresh" 1
fi

_log "scratch CLAUDE_GPT_HOME: $CLAUDE_GPT_HOME"
_log "scratch transport log path (pre-run, must not exist yet): $SCRATCH_LOG_PATH"

# --- ephemeral driver script (NOT a repository-tracked file) -- calls the
# real production `invoke_agent()` / `sanitize_subprocess_env()` path with a
# trivial Haiku-tier leaf agent request ------------------------------------
DRIVER_PY="$SCRATCH_ROOT/nested_invocation_driver.py"
DRIVER_RESULT_JSON="$SCRATCH_ROOT/driver_result.json"
DRIVER_SCHEMA_JSON="$SCRATCH_ROOT/trivial_schema.json"
printf '{"type": "object"}' >"$DRIVER_SCHEMA_JSON"

cat >"$DRIVER_PY" <<PYEOF
import json
import sys

sys.path.insert(0, "$REPO_ROOT/.claude/skills/agent-retrospective/scripts")
import run_retrospective as rr

policy = rr.DelegatedAgentPermissionPolicy(run_id="issue-2445-ac3-transport-verify")
request = rr.AgentInvocationRequest(
    agent_name="retrospective-runtime-observer",
    prompt=(
        "Issue #2445 AC3 live transport verification canary -- this is not a "
        "real retrospective run. Return exactly this JSON object as your "
        "entire response, with no other text:\\n"
        '{"schema_version": "observer_result/v1", "run_id": '
        '"issue-2445-ac3-transport-verify", "base_sha": "n/a", '
        '"source_set_digest": "n/a", "observer_id": '
        '"retrospective-runtime-observer", "evidence_ref": "issue-2445-ac3", '
        '"findings": []}'
    ),
    json_schema_path="$DRIVER_SCHEMA_JSON",
    cwd="$REPO_ROOT",
    timeout_sec=120,
)
result = rr.invoke_agent(request, policy=policy)
with open("$DRIVER_RESULT_JSON", "w", encoding="utf-8") as fh:
    json.dump(
        {
            "status": result.status,
            "exit_code": result.exit_code,
            "reason_code": result.reason_code,
        },
        fh,
    )
PYEOF

OUTER_PROMPT="Issue #2445 AC3 live transport verification. Run exactly this Bash command and report its exit code and stdout verbatim (do not attempt to interpret or re-run it, do not modify it): uv run --locked python3 $DRIVER_PY"

_log "invoking scripts/claude-gpt/launch.sh with scratch CLAUDE_GPT_HOME (outer live Claude-GPT session)"
{
  echo ""
  echo "--- Input (continued) ---"
  echo "outer prompt: $OUTER_PROMPT"
} >>"$LOG_FILE"

LAUNCH_OUTPUT="$("$LAUNCH_SH" -- -p "$OUTER_PROMPT" --output-format json 2>&1)"
LAUNCH_EXIT=$?
{
  echo ""
  echo "--- Output ---"
  echo "launch.sh exit code: $LAUNCH_EXIT"
  printf '%s\n' "$LAUNCH_OUTPUT" | tail -n 200
} >>"$LOG_FILE"

if [ "$LAUNCH_EXIT" -eq 3 ] || [ "$LAUNCH_EXIT" -eq 4 ]; then
  _finish SKIP "scripts/claude-gpt/launch.sh reported environment unavailable (exit $LAUNCH_EXIT)" 77
fi
if [ "$LAUNCH_EXIT" -ne 0 ]; then
  _finish FAIL "scripts/claude-gpt/launch.sh exited $LAUNCH_EXIT (see log for output)" 1
fi

if [ ! -f "$DRIVER_RESULT_JSON" ]; then
  _finish FAIL "outer session never produced $DRIVER_RESULT_JSON -- the nested invocation driver was not run (or the model did not execute the requested Bash command)" 1
fi

DRIVER_STATUS="$(uv run --locked python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['status'])" "$DRIVER_RESULT_JSON" 2>>"$LOG_FILE")"
_log "driver invoke_agent() result status: $DRIVER_STATUS"

if [ ! -f "$SCRATCH_LOG_PATH" ]; then
  _finish FAIL "scratch transport log still does not exist after the run -- neither the outer session nor the nested invocation reached the scratch proxy at all" 1
fi

# preserve a copy of the raw scoped log as evidence (scratch dir is removed
# on exit) BEFORE any further processing, regardless of the eventual verdict
PRESERVED_LOG_PATH="$ARTIFACT_DIR/ac3-scoped-proxy-${TIMESTAMP}.log"
cp "$SCRATCH_LOG_PATH" "$PRESERVED_LOG_PATH"
_log "preserved scoped proxy log evidence: $PRESERVED_LOG_PATH"

# --- transport_observed: at least one codex_upstream_request_started event
# whose model alias is the Haiku-tier one (gpt-5.6-luna), plus
# transport_log.py's own whole-log verdict (every observed request --
# Haiku-tier and outer alike -- used the expected http transport). This does
# NOT attempt to attribute a single reqId exclusively to the nested
# invocation (OWNER review P0-2: model-alias names are not a causal ID) ----
OBSERVATION_JSON="$ARTIFACT_DIR/ac3-transport-observed-${TIMESTAMP}.json"
OBSERVATION_RESULT="$(uv run --locked python3 - "$PRESERVED_LOG_PATH" "$TRANSPORT_LOG_PY" "$OBSERVATION_JSON" <<'PYEOF2'
import importlib.util
import json
import sys

log_path, transport_log_py, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

spec = importlib.util.spec_from_file_location("claude_gpt_transport_log_ac3", transport_log_py)
transport_log = importlib.util.module_from_spec(spec)
sys.modules["claude_gpt_transport_log_ac3"] = transport_log
spec.loader.exec_module(transport_log)

events = []
malformed = 0
with open(log_path, encoding="utf-8") as fh:
    for raw_line in fh.readlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(obj, dict):
            events.append(obj)

HAIKU_ALIAS = "gpt-5.6-luna"
started = [e for e in events if e.get("msg") == "codex_upstream_request_started"]


def _model_alias(event: dict) -> str:
    return str((event.get("fields") or {}).get("model", "")).split("[", 1)[0]


# `transport_observed` rests on this log's EXCLUSIVITY (a fresh, single-run
# scratch `CLAUDE_GPT_HOME` this script alone seeds and this script alone
# points `launch.sh` at -- no other process, session, or concurrent
# invocation can reach this exact instance), not on reqId-counting
# heuristics or model-alias causal attribution. Empirically (live runs,
# Issue #2445 AC3), the outer session's own single Bash-tool decision does
# not always translate into exactly one nested-invocation subprocess launch
# (the model sometimes re-runs the requested command), and even a single
# nested subprocess's own internal machinery can itself issue more than one
# haiku-tier upstream request -- so "exactly one reqId" would be brittle,
# and is not required. What actually matters for `transport_observed`:
#   1. At least one haiku-tier (gpt-5.6-luna -- `retrospective-runtime-observer`'s
#      pinned `model: haiku`, distinct from the outer session's own `main`
#      alias `gpt-5.6-terra`) upstream request was observed at all (the
#      nested subprocess's expected traffic reached the proxy, rather than
#      falling back to some other transport with no proxy interaction
#      whatsoever). This is an observation, not an exclusive-ownership
#      proof -- the alias is also used by Claude Code for other background
#      functionality per its own docs.
#   2. `transport_log.py`'s OWN canonical, already-validated verdict for the
#      WHOLE scoped log is `ok: true` -- every single reqId in this
#      exclusively-owned log (both the outer session's own turns AND every
#      nested-invocation request) is a confirmed http `/v1/messages` 200
#      round trip; this is already the strict global constraint (no
#      websocket/auto transport, no malformed lines, no reqId/path/status
#      mismatch, ANYWHERE in the log), so it independently also covers every
#      individual haiku-tier request's own confirmation, and confirms the
#      outer session's own ("Terra") traffic succeeded via the same
#      expected transport too.
haiku_matches = [e for e in started if _model_alias(e) == HAIKU_ALIAS]
outer_alias_matches = [e for e in started if _model_alias(e) != HAIKU_ALIAS]

verdict = transport_log.evaluate_transport_log(log_path)

result = {
    "malformed_line_count": malformed,
    "started_count": len(started),
    "haiku_alias_match_count": len(haiku_matches),
    "outer_alias_match_count": len(outer_alias_matches),
    "observed_model_aliases": sorted({_model_alias(e) for e in started}),
    "transport_verdict_ok": verdict.ok,
    "transport_verdict_reason": "; ".join(verdict.reasons) if verdict.reasons else "ok",
}

ok = len(haiku_matches) >= 1 and bool(verdict.ok)
result["ok"] = ok

with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(result, fh, indent=2)

print(json.dumps(result))
PYEOF2
)"
OBSERVATION_PY_EXIT=$?
_log "transport_observed result: $OBSERVATION_RESULT"

if [ "$OBSERVATION_PY_EXIT" -ne 0 ]; then
  _finish FAIL "transport_observed check script itself failed (exit $OBSERVATION_PY_EXIT) -- see log" 1
fi

HAIKU_MATCH_COUNT="$(uv run --locked python3 -c "import json,sys; print(json.loads(sys.argv[1])['haiku_alias_match_count'])" "$OBSERVATION_RESULT" 2>>"$LOG_FILE")"
TRANSPORT_OBSERVED_OK="$(uv run --locked python3 -c "import json,sys; print(json.loads(sys.argv[1])['ok'])" "$OBSERVATION_RESULT" 2>>"$LOG_FILE")"

if [ "$HAIKU_MATCH_COUNT" = "0" ]; then
  if [ "$DRIVER_STATUS" = "ok" ]; then
    _finish FAIL "fallback_suspected: nested invocation reported status=ok but NO Haiku-tier (gpt-5.6-luna) request was observed in the scratch proxy log -- the nested claude subprocess likely did not route through Claude-GPT proxy transport at all (fallback_policy.fallback_success_is_pass: false)" 1
  fi
  _finish FAIL "no Haiku-tier (gpt-5.6-luna) request observed in the scratch proxy log, and the nested invocation itself did not report success either" 1
fi
if [ "$TRANSPORT_OBSERVED_OK" != "True" ]; then
  _finish FAIL "transport_log.py's own verdict for this exclusively-owned, run-scoped log was not ok (see $OBSERVATION_JSON and $PRESERVED_LOG_PATH for the full reasons -- e.g. non-http transport, malformed lines, or an unconfirmed reqId anywhere in the log, not necessarily limited to the haiku-tier requests)" 1
fi
if [ "$DRIVER_STATUS" != "ok" ]; then
  _finish FAIL "nested invocation's own reported status was '$DRIVER_STATUS' (expected ok), even though a matching proxy request was observed -- see $DRIVER_RESULT_JSON provenance in the log" 1
fi

_finish PASS "transport_observed: within this run-scoped, exclusively-owned claude-code-proxy transport log, both the outer session's own traffic and the nested observer's expected Haiku-tier (gpt-5.6-luna) /v1/messages traffic [$HAIKU_MATCH_COUNT haiku-tier request(s) observed] were observed, and transport_log.py's verdict for every request in the log is ok (http /v1/messages 200, no fallback). Exclusive attribution of a single reqId to the nested invocation is NOT claimed. Evidence: $OBSERVATION_JSON" 0

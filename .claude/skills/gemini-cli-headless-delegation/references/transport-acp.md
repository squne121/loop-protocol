# ACP Transport Reference (experimental — read-only)

ACP (Agent Client Protocol) transport for gemini-cli-headless-delegation.
Implementation: `.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_acp.py`

Reference: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md

**Status: experimental, read-only transport.** This transport declares a
read-only `clientCapabilities` (no filesystem proxy, no terminal proxy) at
`initialize` time — see "Capability / Safety boundary" below. It does **not**
implement an `fs` / `terminal` proxy. The agent cannot perform host filesystem
or terminal I/O through this client.

---

## Delegation contract routing

`transport: acp` requests flow through the **full delegation contract** in
`run_gemini_headless.run_delegation()`. The ACP branch is taken only **after**:

1. `validate_request()` — schema, `tool_profile`, `output_sections`,
   `context_files`, GitHub/Serena constraints.
2. model chain resolution (`resolve_model_chain`).
3. context file loading (`_read_context_files`).
4. `build_prompt()` — the full delegation prompt.

The fully-built prompt is passed to `run_acp(..., prepared_prompt=prompt,
model_override=<resolved model>)`. The ACP path therefore cannot bypass any
`tool_profile` / context / output constraint that headless_json enforces. An
invalid `delegation_request_v1` fails at `validate_request()` and never reaches
the ACP session. The standalone `run_gemini_acp.py` CLI entry point also routes
through `run_delegation()` for the same reason.

---

## Lifecycle (ライフサイクル)

ACP transport uses JSON-RPC 2.0 over stdio against `gemini --acp`.

```
caller
  |
  +--[launch]--> gemini --acp
  |
  +--[send]--> initialize { protocolVersion: 1,
  |                         clientCapabilities: { fs: {readTextFile:false,
  |                                                    writeTextFile:false},
  |                                               terminal: false } }
  |              <-- { id: 1, result: { ... } }
  |
  +--[send]--> session/new { model, approvalMode: "default", cwd, mcpServers }
  |              <-- { id: 2, result: { sessionId: "<sessionId>" } }
  |
  +--[send]--> session/prompt { prompt: [{type:"text",text:"..."}], sessionId }
  |              <-- notification: session/update (sessionUpdate: agent_thought_chunk)   (0+)
  |              <-- notification: session/update (sessionUpdate: agent_message_chunk)   (0+)
  |              <-- notification: session/update (sessionUpdate: tool_call)             (0+)
  |              <-- notification: session/update (sessionUpdate: tool_call_update)      (0+)
  |              <-- request: session/request_permission   (if agent requests a tool)
  |              <-- { id: 3, result: { stopReason: "end_turn" } } (final)
  |
  +--[close stdin]--> wait for process exit
```

`protocolVersion` is the integer `1` (gemini-cli 0.42.0+ rejects string
versions such as `"2024-11-05"`). Each step uses `asyncio.create_subprocess_exec`
+ stdin/stdout pipes. All agent events arrive as `session/update` notifications
whose `params.update.sessionUpdate` field carries a snake_case event name
(`agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`).
These are collected into the result's `structured_events` list.

A session is only `ok: true` when the final id-matching `session/prompt`
response was received, the process exited with returncode 0, and a `sessionId`
was obtained. An EOF or process death before the final response is reported as
`failure_class: "protocol_error"` — it is never treated as success.

---

## Timeout (タイムアウト)

HeartbeatWatchdog implements 4-stage timeouts. Constants are defined as both
module-level symbols and `HeartbeatWatchdog` class attributes for grep discoverability.

| Stage | Constant | Default | Trigger condition |
|---|---|---|---|
| connect | `CONNECT_TIMEOUT_SEC` | 60s | No message received after process launch |
| initial_idle | `INITIAL_IDLE_TIMEOUT_SEC` | 300s | No event before first session/prompt response |
| subsequent_idle | `SUBSEQUENT_IDLE_TIMEOUT_SEC` | 120s | No event between subsequent events |
| total | `TOTAL_TIMEOUT_SEC` | 600s | Hard cap on entire ACP session |

Design rationale:
- `connect` catches auth hang (bug #12042) and initialize hang (bug #22782) early.
- `initial_idle` allows the model time to start thinking before the first chunk arrives.
- `subsequent_idle` catches stalls mid-stream without aborting healthy sessions.
- `total` prevents unbounded resource consumption.

`HeartbeatWatchdog.heartbeat()` is called on every received message, resetting the idle clocks.
`HeartbeatWatchdog.mark_first_result()` is called when the first AgentMessageChunk or result arrives,
activating the `subsequent_idle` stage.

---

## Capability / Safety boundary (capability / 安全境界)

**The safety boundary is the read-only `clientCapabilities` declaration**, not
the permission handler. At `initialize` the client sends:

```json
{
  "protocolVersion": 1,
  "clientCapabilities": {
    "fs": {"readTextFile": false, "writeTextFile": false},
    "terminal": false
  }
}
```

This tells the agent that the client provides **no filesystem proxy** and **no
terminal proxy**. Host filesystem reads/writes and terminal command execution
through the client are not available. This is the authoritative boundary of the
read-only ACP transport.

### Permission handler — best-effort secondary defence

`run_gemini_acp.py` additionally implements `handle_request_permission()` /
`handle_session_request_permission()`, controlled by the `approve_edits` flag
(`--approve-edits` CLI flag, `approve_edits=True` API arg, or
`"approve_edits": true` in the request JSON). When `approve_edits` is false
these handlers select the reject/cancel option for write-type permission
requests.

This permission handler is **best-effort defence in depth, not the safety
boundary**. It is retained because removing it would break existing tests and
callers, and because it provides a clear deny signal in
`structured_events`. It must not be relied on as the mechanism that prevents
writes — the read-only `clientCapabilities` declaration does that.

Write operation types the handler recognises (`WRITE_PERMISSION_TYPES`):
`write_file`, `edit_file`, `create_file`, `delete_file`, `run_shell_command`,
`execute_code`.

### Non-goal

An `fs` / `terminal` proxy that would let the agent perform host I/O through
this client is explicitly **out of scope** for this transport. The transport is
read-only by design.

---

## Fallback (フォールバック)

When the ACP pathway fails before producing a usable session, the transport
automatically falls back to `headless_json` transport via
`_fallback_to_headless_json()`.

The fallback decision is driven by the structured `failure_class` field on the
session result — **not** by substring matching on `failure_reason`. Each failure
site sets a `failure_class`:

| `failure_class` | Set at | Fallback? |
|---|---|---|
| `gemini_not_found` | `FileNotFoundError` launching `gemini --acp` | yes |
| `launch_failed` | other subprocess launch error | yes |
| `initialize_failed` | initialize timeout / unexpected / error | yes |
| `session_new_failed` | session/new timeout / EOF / error | yes |
| `prompt_error` | session/prompt returned an error | no (late) |
| `protocol_error` | EOF before final session/prompt response | no (late) |
| `timeout` | total timeout exceeded | no (late) |
| `watchdog` | heartbeat watchdog tripped | no (late) |

`run_acp()` falls back when `failure_class` is one of
`{gemini_not_found, launch_failed, initialize_failed, session_new_failed}`. The
`failure_class` value is preserved on the returned result.

Non-fallback failures (late failures, treated as final):
- `prompt_error` — the agent responded but returned an error.
- `protocol_error` — EOF / process death before the final response.
- `timeout` / `watchdog` — stalls after the session was established.

When fallback fires:
1. `_fallback_to_headless_json(request, request_path, failure_reason)` is called.
2. It imports `run_delegation` from `run_gemini_headless` and calls it with `transport="headless_json"`.
3. A warning is prepended: `"acp transport failed (...); fell back to headless_json transport"`.
4. The result includes `_acp_fallback: true` to signal to the caller that fallback occurred.

The dispatcher in `run_gemini_headless.py` is re-entrant: the fallback call uses
`transport="headless_json"` (or absent), which skips the acp branch and runs the
standard headless_json pathway without recursion.

Known gemini-cli bugs that trigger early failure detection (preflight signals):
- Bug #12042 (auth hang): stderr contains `"refreshing credentials"` or `"waiting for auth"`
- Bug #22782 (initialize hang): no initialize response within CONNECT_TIMEOUT_SEC
- Bug #18423 (settings hang): stderr contains `"reading settings"` or `"loading settings"`

When a known bug is detected in stderr, the process is killed immediately (fail-closed).

---

## Operational Verification (動作検証)

Script: `.claude/skills/gemini-cli-headless-delegation/scripts/verify_acp_roundtrip.sh`
Policy: `docs/dev/runtime-verification-policy.md`
Related: Issue #85, Issue #26 AC7

### Exit Code Convention

| Exit Code | Meaning |
|-----------|---------|
| 0 | All scenarios PASS |
| 1 | At least one scenario FAIL or `_acp_fallback: true` detected |
| 77 | Execution environment unavailable (gemini CLI or jq not found) — SKIP |

### GEMINI_BIN Override

The verification script respects the `GEMINI_BIN` environment variable to allow deterministic testing in environments without a real `gemini` binary:

```bash
# Test SKIP behaviour without installing gemini CLI
GEMINI_BIN=/nonexistent/gemini-absent bash verify_acp_roundtrip.sh
# → stdout: "SKIP: gemini CLI not found ...", exit 77

# Use a custom binary path
GEMINI_BIN=/usr/local/bin/gemini-dev bash verify_acp_roundtrip.sh
```

If `GEMINI_BIN` is not set, the default is `gemini` (resolved via `command -v`).

### Scenarios

**scenario 1 — normal (PONG roundtrip)**

Delegates `"Reply with exactly: PONG"` via ACP transport with `tool_profile: no_tools`.
PASS requires **all** of:
- `ok: true`
- `_acp_fallback` is absent or `false`
- `transport == "acp"` (proves the ACP path ran, not a fallback)
- `structured_events` length `> 0` (proves the `session/update` event stream was parsed)
- `response_text` trimmed of surrounding whitespace equals exactly `"PONG"`

Any mismatch is a FAIL with exit 1.

**scenario 2 — permission deny (deterministic fake ACP agent)**

A minimal deterministic fake ACP agent replaces `gemini` (`GEMINI_BIN` override).
It issues a `session/request_permission` request and the run is executed without
`--approve-edits`. The request JSON is a valid `delegation_request_v1`
(`tool_profile: no_tools`) so it passes `validate_request()`. PASS requires:
- no fallback (`_acp_fallback` absent or `false`)
- `ok: true`
- `structured_events` contains a `session/request_permission` entry
- `response_text` contains `PERMISSION_DENIED_OK`
- `/tmp/acp-verify-permission-test.txt` does not exist

The scenario 2 invocation is wrapped in an `if` so a non-zero exit does not
abort the script under `set -e`.

Note: scenario 2 exercises the best-effort permission handler. The authoritative
safety boundary is the read-only `clientCapabilities` declaration (see the
Capability / Safety boundary section).

### Fallback Detection

If the result JSON contains `_acp_fallback: true`, the script outputs a FAIL message and
exits 1. The fallback detection block does not contain any `exit 0` or `PASS` statement
between detection and `exit 1`. This aligns with `runtime-verification-policy.md` §3.

### Artifact Output

Each scenario's input and output are appended to:

```
artifacts/runtime-verification-AC7-<ISO8601 UTC>.log
```

Format follows `runtime-verification-policy.md` §4 (AC / Timestamp / Environment / Input / Output / Verdict).
The `artifacts/` directory is created with `mkdir -p` and is **not committed** (worktree-local work area).

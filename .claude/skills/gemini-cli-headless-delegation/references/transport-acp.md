# ACP Transport Reference

ACP (Agent Client Protocol) transport for gemini-cli-headless-delegation.
Implementation: `.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_acp.py`

Reference: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md

---

## Lifecycle (ライフサイクル)

ACP transport uses JSON-RPC 2.0 over stdio against `gemini --acp`.

```
caller
  |
  +--[launch]--> gemini --acp
  |
  +--[send]--> initialize { protocolVersion: "2024-11-05" }
  |              <-- { id: 1, result: { ... } }
  |
  +--[send]--> session/new { model, approvalMode: "plan" }
  |              <-- { id: 2, result: { id: "<sessionId>" } }
  |
  +--[send]--> session/prompt { prompt, sessionId }
  |              <-- notification: AgentThoughtChunk   (zero or more)
  |              <-- notification: AgentMessageChunk   (zero or more)
  |              <-- notification: ToolCallStart        (zero or more)
  |              <-- notification: request_permission   (if model requests write)
  |              <-- { id: 3, result: { text: "..." } } (final)
  |
  +--[close stdin]--> wait for process exit
```

Each step uses `asyncio.create_subprocess_exec` + stdin/stdout pipes.
Structured events (AgentMessageChunk / AgentThoughtChunk / ToolCallStart) are
collected into the result's `structured_events` list.

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

## Permission (パーミッション / permission proxy)

When gemini-cli sends a `request_permission` RPC during a session/prompt,
the permission proxy (`handle_request_permission()`) decides whether to grant or deny.

Write operations covered by `WRITE_PERMISSION_TYPES`:
- `write_file`, `edit_file`, `create_file`, `delete_file`
- `run_shell_command`, `execute_code`

Behavior:
- `approve_edits=False` (default): all write operations are **denied** with a clear reason message.
- `approve_edits=True`: all operations are **granted**.

To enable write operations, pass `--approve-edits` to `run_gemini_acp.py` CLI,
or set `approve_edits=True` in the `run_acp()` API call,
or set `"approve_edits": true` in the delegation request JSON.

Design rationale:
- Default-deny write operations aligns with the headless_json contract's
  `tool_profile` restriction model, where editing is explicitly opt-in.
- Permission proxy sits between the model and the filesystem, providing
  a single enforcement point that is independent of gemini-cli's `--approval-mode`.

---

## Fallback (フォールバック)

When the ACP pathway fails at the `initialize` or `session/new` step
(connect timeout, protocol error, or gemini CLI not found), the transport
automatically falls back to `headless_json` transport via `_fallback_to_headless_json()`.

Fallback trigger conditions (early-failure keywords in `failure_reason`):
- `"initialize"` — initialize request failed or timed out
- `"session/new"` — session/new request failed or timed out
- `"connect timeout"` — no response within CONNECT_TIMEOUT_SEC
- `"not found in PATH"` — gemini CLI binary not installed
- `"failed to launch"` — subprocess exec failed

Non-fallback failures (late failures, treated as final):
- `session/prompt` errors — the model responded but returned an error
- Watchdog timeouts after first result received
- Permission proxy denials

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

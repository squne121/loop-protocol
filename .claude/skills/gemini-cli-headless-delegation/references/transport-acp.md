# ACP Transport Reference (experimental)

ACP (Agent Client Protocol) transport for gemini-cli-headless-delegation.
Implementation: `.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_acp.py`

Reference: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md

**Status: experimental.** This transport declares `clientCapabilities` with
`fs=false` / `terminal=false` at `initialize` time. The precise meaning is
narrow: **this ACP client does not provide an ACP client-side `fs` / `terminal`
proxy** (no `readTextFile` / `writeTextFile` / terminal RPC handlers). It does
**not** mean that Gemini CLI is unable to touch the host: Gemini CLI's own
native tool registry, `cwd`-resolved MCP servers from `.gemini/settings.json`,
and `approvalMode` are **not controlled by this transport**. See
"Capability scope" and "Known limitations" below.

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

### Authentication — `authenticate` handshake is NOT implemented

The ACP protocol defines an optional `authenticate` phase between `initialize`
and `session/new`: the agent advertises `authMethods` in the `initialize`
result and the client can call `authenticate` to establish credentials.

**This transport does not implement the `authenticate` handshake.** It assumes a
**pre-authenticated** Gemini CLI / OAuth session (the operator has already run
`gemini` interactively / completed OAuth). The lifecycle goes directly
`initialize → session/new → session/prompt`.

To keep this honest:

- the `initialize` result's `authMethods` (if present) is captured into the
  session result (`auth_methods`) **for diagnostics only** — it does **not**
  drive failure classification. Gemini CLI advertises `authMethods`
  unconditionally on every `initialize`, so keying off it would misclassify
  every non-auth `session/new` failure (model not found, invalid params, …) as
  `auth_required` and suppress the early-failure fallback;
- if `session/new` fails, classification is based **solely on the
  `session/new` error object**: when that error signals authentication is
  required (`auth` / `authenticate` / `unauthorized` / an `authMethods` key
  inside the *error payload* / `not authenticated` and similar signals), the
  failure is classified `failure_class = "auth_required"`. Any other
  `session/new` error (including model-not-found / invalid-params) stays
  `session_new_failed`;
- `auth_required` is **excluded** from the headless_json fallback set (see
  "Fallback" below). An auth failure is surfaced as a real ACP transport
  failure and is **never** masked behind an apparent "fallback success";
- `verify_acp_roundtrip.sh` treats `failure_class = auth_required` as an
  explicit FAIL (exit 1) with a clear message.

Implementing the `authenticate` handshake itself is **out of scope** for this PR.

`protocolVersion` is the integer `1` (gemini-cli 0.42.0+ rejects string
versions such as `"2024-11-05"`). Each step uses `asyncio.create_subprocess_exec`
+ stdin/stdout pipes. All agent events arrive as `session/update` notifications
whose `params.update.sessionUpdate` field carries a snake_case event name
(`agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`).
These are collected into the result's `structured_events` list.

A session is only `ok: true` when **all** of the following hold:

1. transport-level success (`transport_ok`): the final id-matching
   `session/prompt` response was received, the process exited with returncode
   `0`, and a `sessionId` was obtained;
2. the final response's `stopReason` is `"end_turn"`;
3. `response_text` (whitespace-trimmed) is non-empty.

An EOF or process death before the final response is reported as
`failure_class: "protocol_error"` — never success. A transport-level success
that ends with a non-`end_turn` `stopReason` or an empty response is reported as
`failure_class: "incomplete_response"` — also not a success. The result carries
both `transport_ok` (condition 1 only) and `stop_reason` so callers can tell the
two apart.

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

## Capability scope (capability scope / 能力範囲)

At `initialize` the client sends:

```json
{
  "protocolVersion": 1,
  "clientCapabilities": {
    "fs": {"readTextFile": false, "writeTextFile": false},
    "terminal": false
  }
}
```

**What this declaration means (and only this):** this ACP **client** does not
provide an ACP client-side filesystem proxy (`fs.readTextFile` /
`fs.writeTextFile`) and does not provide an ACP client-side terminal proxy
(`terminal`). The agent cannot ask *this client* to read/write host files or run
host terminal commands *on its behalf via the ACP fs/terminal RPCs*.

**What this declaration does NOT mean:** it does **not** disable Gemini CLI's own
host I/O. Gemini CLI still has:

- its **native tool registry** (file tools, shell tool, etc.), which is not
  governed by ACP `clientCapabilities`;
- **MCP servers** loaded from the settings discovered at `cwd`
  (`.gemini/settings.json`), which can grant additional capabilities;
- an **`approvalMode`** that this transport currently sends as `"default"`
  (see "session/new" below) — tools are therefore **active**, not disabled.

In other words, `fs=false` / `terminal=false` removes one specific path
(client-provided proxies); it is **not** a complete sandbox for the agent's host
access. The end-to-end safety boundary design for ACP delegation
(native-tool gating, MCP allowlist, `approvalMode` policy) is **deferred to
follow-up #112** and is **not** finalized in this PR.

### `session/new` sends `approvalMode: "default"`

This transport currently sends `approvalMode: "default"` in `session/new`. Under
`"default"`, Gemini CLI's tools and permission requests are **active** — the
agent may attempt tool calls and the host may issue `session/request_permission`
requests. Changing `approvalMode` (e.g. to a plan-only / read-only mode) is in
scope for **#112**, not this PR.

### Permission handler — `tool_profile` + `toolCall.kind` policy

`run_gemini_acp.py` implements `handle_session_request_permission()` for the ACP
`session/request_permission` RPC. It decides allow/reject from the delegation
**`tool_profile`** and the ACP **`params.toolCall.kind`** (the ACP tool-kind
taxonomy: `read` / `edit` / `delete` / `move` / `search` / `execute` / `think` /
`fetch` / `other`):

- `tool_profile == "no_tools"` → **reject every `toolCall.kind`** unconditionally;
- read-class profiles (everything else — `grounded_research`,
  `github_research`, `local_asset_research`, `proposal_only`, …) → **allow**
  read-class kinds (`read` / `search` / `fetch` / `think`) and **reject**
  write/effect kinds (`edit` / `delete` / `move` / `execute` / `other`);
- `approve_edits=True` does **not** widen this — no `tool_profile` in this skill
  is write-capable, so write-class kinds stay rejected and `no_tools` stays
  fully rejected regardless of `approve_edits`;
- a missing / unknown `toolCall.kind` or an empty `options` list → reject /
  cancel (fail-safe).

This is a best-effort alignment of the **experimental ACP transport's
permission handler** with the delegation tool-profile contract — **not** the
gating of Gemini CLI's own native tool registry / MCP servers / `approvalMode`.
That native-tool safety boundary is **deferred to follow-up #112**. The handler
only sees the permission requests the agent actually routes through
`session/request_permission`; it records each decision in `structured_events`.

`handle_request_permission()` (the legacy non-ACP schema, keyed by a flat
`type` field against `WRITE_PERMISSION_TYPES`: `write_file`, `edit_file`,
`create_file`, `delete_file`, `run_shell_command`, `execute_code`) is retained
only for backward compatibility with callers that still use that schema.

### Known limitations / non-goals (この PR の非ゴール)

- An ACP client-side `fs` / `terminal` proxy is **out of scope** for this
  transport — declared `false` by design.
- **Native tool registry / MCP `cwd` resolution / `approvalMode` design** are
  **not** controlled by this transport. A coherent safety-boundary design for
  ACP delegation is deferred to **follow-up #112**.
- **Real Gemini CLI runtime verification evidence** (live scenario 1 against a
  real `gemini` binary, captured artifacts) is deferred to **follow-up #113**.
  CI runs `verify_acp_roundtrip.sh` as SKIP (exit 77) when `gemini` is absent;
  deterministic coverage uses a fake ACP agent.

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
| `session_new_failed` | session/new timeout / EOF / non-auth error | yes |
| `auth_required` | session/new error signalling authentication is required | **no** (surfaced honestly) |
| `prompt_error` | session/prompt returned an error | no (late) |
| `protocol_error` | EOF before final session/prompt response | no (late) |
| `incomplete_response` | transport ok but non-`end_turn` stopReason / empty response | no (late) |
| `timeout` | total timeout exceeded | no (late) |
| `watchdog` | heartbeat watchdog tripped | no (late) |
| `contract_bypass` | `run_acp()` called without `prepared_prompt` | no (refused) |

`run_acp()` falls back when `failure_class` is one of
`{gemini_not_found, launch_failed, initialize_failed, session_new_failed}`. The
`failure_class` value is preserved on the returned result.

Non-fallback failures (treated as final):
- `auth_required` — `session/new` failed because the Gemini CLI / OAuth session
  is not authenticated. This transport does not implement the ACP
  `authenticate` handshake and assumes a pre-authenticated session.
  `auth_required` is deliberately **excluded** from the fallback set: falling
  back to headless_json would mask the ACP failure behind a "fallback success".
  The failure is surfaced as-is so the operator fixes authentication.
- `prompt_error` — the agent responded but returned an error.
- `protocol_error` — EOF / process death before the final response.
- `incomplete_response` — transport succeeded but the session ended with a
  non-`end_turn` `stopReason` or an empty `response_text`.
- `timeout` / `watchdog` — stalls after the session was established.
- `contract_bypass` — `run_acp()` was called without a `prepared_prompt`; the
  call is refused before any session starts (always route via `run_delegation()`).

When fallback fires:
1. `_fallback_to_headless_json(request, request_path, failure_reason)` is called.
2. It imports `run_delegation` from `run_gemini_headless` and calls it with `transport="headless_json"`.
3. A warning is prepended: `"acp transport failed (...); fell back to headless_json transport"`.
4. The result includes `_acp_fallback: true` to signal to the caller that fallback occurred.

The dispatcher in `run_gemini_headless.py` is re-entrant: the fallback call uses
`transport="headless_json"` (or absent), which skips the acp branch and runs the
standard headless_json pathway without recursion.

Known gemini-cli bugs that trigger early failure detection (preflight signals).
Each stderr signature is deliberately specific — the bug number or a distinctive
multi-word phrase — so normal logs that merely mention `initialize` or
`settings.json` do not misfire:
- Bug #12042 (auth hang): stderr contains `#12042`, `"refreshing credentials timed out"`, or `"waiting for auth token refresh"`
- Bug #22782 (initialize hang): stderr contains `#22782`, `"initialize request never returned"`, or `"protocol handshake stalled"` (also: no initialize response within CONNECT_TIMEOUT_SEC)
- Bug #18423 (settings hang): stderr contains `#18423`, `"settings.json parsing hang"`, or `"loading settings never completed"`

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
| 1 | At least one scenario FAIL, `_acp_fallback: true`, or `failure_class=auth_required` detected |
| 77 | Execution environment unavailable (gemini CLI or jq not found) — SKIP |

### Result schema the script reads

`run_delegation()` normalizes ACP results to `delegation_result/v1`. The
verification script reads:

- `.ok`, `.transport`, `.response_text`, `._acp_fallback`, `.warnings` — at the
  **top level**;
- `.transport_details.structured_events` and `.transport_details.failure_class`
  — under `transport_details` (with a top-level fallback for safety:
  `(.transport_details.structured_events // .structured_events // [])`).

A `failure_class` of `auth_required` is an explicit FAIL (exit 1): the ACP
session requires a pre-authenticated Gemini CLI / OAuth session.

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

**scenario 2 — permission outcome controls a side effect (controlled experiment)**

A deterministic fake ACP agent replaces `gemini` (`GEMINI_BIN` override) and the
request JSON is a valid `delegation_request_v1` (`tool_profile: no_tools`).

This scenario is a **controlled experiment**, not a passive check. A passive
"the file was not written" assertion does not prove the permission proxy denied
anything — it could just mean nothing tried to write. Instead, the fake agent
produces a **real, observable side effect** (creating a unique file at
`$WORK_DIR/acp-perm-sideeffect-*.txt`, path passed via the
`ACP_PERM_SIDEEFFECT_FILE` env var) **iff** the permission outcome it received
from the proxy is an approval. The scenario runs **two sub-cases**:

- **2a (deny)** — no `--approve-edits` → the proxy rejects → the agent creates
  **no** file and replies `PERMISSION_DENIED_OK`. PASS requires `ok: true`, a
  `session/request_permission` entry in `structured_events`, response
  `PERMISSION_DENIED_OK`, and the side-effect file **absent**.
- **2b (approve)** — `--approve-edits` → the proxy approves → the agent
  **creates** the file and replies `PERMISSION_GRANTED_OK`. PASS requires
  `ok: true`, a `session/request_permission` entry, response
  `PERMISSION_GRANTED_OK`, and the side-effect file **present**.

Running both sub-cases proves deterministically that the permission proxy's
approve/reject **branch actually controls the side effect** — the deny case is
contrasted against an approve case that does produce the write. Each sub-case
invocation is wrapped in an `if` so a non-zero exit does not abort the script
under `set -e`, and each sub-case verdict is tracked separately.

Note: scenario 2 is a deterministic **fake-agent** test. It proves the
permission proxy *branch* controls side effects; it does **not** prove gating of
Gemini CLI's **native tool registry** — the `clientCapabilities` declaration
only disables ACP client-provided fs/terminal proxies, not Gemini CLI's native
tools / MCP / `approvalMode`. Native-tool gating verification against a real
`gemini` binary is **follow-up #112**. See also the "Capability scope" and
"Known limitations / non-goals" sections.

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

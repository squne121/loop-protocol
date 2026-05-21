#!/usr/bin/env python3
"""ACP (Agent Client Protocol) transport for gemini-cli-headless-delegation.

Implements JSON-RPC 2.0 over stdio against `gemini --acp`.
Lifecycle: initialize -> session/new -> session/prompt.
Structured events (AgentMessageChunk, AgentThoughtChunk, ToolCallStart) are
collected into result["structured_events"].

Reference: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# HeartbeatWatchdog — 4-stage timeout constants
# ---------------------------------------------------------------------------

CONNECT_TIMEOUT_SEC: int = 60       # Time to first JSON-RPC message after launch
INITIAL_IDLE_TIMEOUT_SEC: int = 300 # Idle before first session/prompt response arrives
SUBSEQUENT_IDLE_TIMEOUT_SEC: int = 120  # Idle between subsequent events
TOTAL_TIMEOUT_SEC: int = 600        # Hard cap on entire ACP session

# ---------------------------------------------------------------------------
# Known gemini-cli bugs that manifest as hangs — detect and fail early
# ---------------------------------------------------------------------------

# Bug #12042: auth hang — gemini-cli hangs indefinitely waiting for auth token refresh
# Symptom: no output for CONNECT_TIMEOUT_SEC after launch
AUTH_HANG_BUG = "#12042"

# Bug #22782: initialize hang — JSON-RPC initialize request never gets a response
# Symptom: no response to initialize after CONNECT_TIMEOUT_SEC
INITIALIZE_HANG_BUG = "#22782"

# Bug #18423: settings hang — gemini-cli hangs parsing .gemini/settings.json
# Symptom: no output at all, settings load never completes
SETTINGS_HANG_BUG = "#18423"

# stderr patterns that indicate each known bug.
# Patterns must be specific enough not to misfire on normal logs: a bare word
# like "initialize" or "settings.json" appears in healthy output too, so each
# signature uses the bug number or a distinctive multi-word phrase instead.
_KNOWN_BUG_STDERR_PATTERNS: dict[str, list[str]] = {
    AUTH_HANG_BUG: [
        AUTH_HANG_BUG,
        "refreshing credentials timed out",
        "waiting for auth token refresh",
        "oauth token refresh hang",
    ],
    INITIALIZE_HANG_BUG: [
        INITIALIZE_HANG_BUG,
        "initialize request never returned",
        "protocol handshake stalled",
    ],
    SETTINGS_HANG_BUG: [
        SETTINGS_HANG_BUG,
        "settings.json parsing hang",
        "loading settings never completed",
    ],
}

# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _rpc_request(method: str, params: dict[str, Any], req_id: int) -> bytes:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    return line.encode()


def _rpc_notification(method: str, params: dict[str, Any]) -> bytes:
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    return line.encode()


def _parse_rpc_line(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return None


# ---------------------------------------------------------------------------
# HeartbeatWatchdog
# ---------------------------------------------------------------------------


class HeartbeatWatchdog:
    """4-stage timeout watchdog for ACP sessions.

    Stages (in order of activation):
      connect       – time from process launch to first message
      initial_idle  – idle before first session/prompt result
      subsequent_idle – idle between events after first result
      total         – hard cap on entire session
    """

    CONNECT = CONNECT_TIMEOUT_SEC
    INITIAL_IDLE = INITIAL_IDLE_TIMEOUT_SEC
    SUBSEQUENT_IDLE = SUBSEQUENT_IDLE_TIMEOUT_SEC
    TOTAL = TOTAL_TIMEOUT_SEC

    def __init__(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. during unit tests outside asyncio context)
            self._loop = asyncio.new_event_loop()
        self._start_time: float = self._loop.time()
        self._last_heartbeat: float = self._start_time
        self._first_result_received: bool = False

    def heartbeat(self) -> None:
        self._last_heartbeat = self._loop.time()

    def mark_first_result(self) -> None:
        self._first_result_received = True

    def check(self) -> str | None:
        """Return a timeout reason string if any stage is exceeded, else None."""
        now = self._loop.time()
        elapsed_total = now - self._start_time
        elapsed_idle = now - self._last_heartbeat

        if elapsed_total > self.TOTAL:
            return f"total timeout exceeded ({self.TOTAL}s)"

        if not self._first_result_received:
            if elapsed_idle > self.INITIAL_IDLE:
                return f"initial_idle timeout exceeded ({self.INITIAL_IDLE}s)"
            if elapsed_idle > self.CONNECT:
                return f"connect timeout exceeded ({self.CONNECT}s) — possible bug {INITIALIZE_HANG_BUG} or {AUTH_HANG_BUG}"
        else:
            if elapsed_idle > self.SUBSEQUENT_IDLE:
                return f"subsequent_idle timeout exceeded ({self.SUBSEQUENT_IDLE}s)"

        return None


# ---------------------------------------------------------------------------
# Permission proxy
# ---------------------------------------------------------------------------

WRITE_PERMISSION_TYPES = frozenset(
    {
        "write_file",
        "edit_file",
        "create_file",
        "delete_file",
        "run_shell_command",
        "execute_code",
    }
)


def handle_request_permission(
    params: dict[str, Any],
    approve_edits: bool,
) -> dict[str, Any]:
    """Handle a request_permission RPC call (legacy / non-ACP schema).

    When approve_edits is False, deny all write-type operations.
    Returns the JSON-RPC result payload.
    """
    permission_type = params.get("type", "")
    if not approve_edits and permission_type in WRITE_PERMISSION_TYPES:
        return {
            "granted": False,
            "reason": (
                f"write operation '{permission_type}' denied by permission proxy; "
                "pass --approve-edits to allow write operations"
            ),
        }
    return {"granted": True}


def handle_session_request_permission(
    params: dict[str, Any],
    approve_edits: bool,
) -> dict[str, Any]:
    """Handle session/request_permission RPC (ACP protocol schema).

    Selects an option from the provided options list and returns an outcome.
    When approve_edits is False, selects the reject/cancel option.
    Returns the JSON-RPC result payload with an outcome selection.
    """
    options: list[dict[str, Any]] = params.get("options") or []
    if approve_edits:
        selected = (
            next((o for o in options if o.get("kind") == "allow_once"), None)
            or next((o for o in options if o.get("kind") == "allow_always"), None)
        )
    else:
        selected = (
            next((o for o in options if o.get("kind") == "reject_once"), None)
            or next((o for o in options if o.get("kind") == "reject_always"), None)
            or next((o for o in options if o.get("optionId") == "cancel"), None)
        )
    if selected:
        return {"outcome": {"outcome": "selected", "optionId": selected["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


# ---------------------------------------------------------------------------
# Preflight: detect known gemini-cli bugs from stderr
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auth-required detection
# ---------------------------------------------------------------------------

# Substrings that, when present in a session/new error message or in the error
# object, indicate the ACP agent is rejecting the request because the Gemini
# CLI / OAuth session is not authenticated. This transport does NOT implement
# the ACP `authenticate` handshake (a pre-authenticated session is assumed);
# such failures must be surfaced as `auth_required`, never masked by a
# headless_json fallback.
_AUTH_REQUIRED_SIGNALS: tuple[str, ...] = (
    "authenticate",
    "auth_required",
    "auth required",
    "authmethods",
    "unauthorized",
    "not authenticated",
    "not signed in",
    "requires authentication",
    "authentication required",
    "login required",
)


def is_auth_required_error(error: Any) -> bool:
    """Return True if a JSON-RPC error object signals an auth-required failure.

    Inspects the error ``message`` and ``code`` and any ``data`` payload for the
    auth signals in ``_AUTH_REQUIRED_SIGNALS`` (case-insensitive). A presence of
    an ``authMethods`` key anywhere in the error payload also counts.
    """
    if not isinstance(error, dict):
        text = str(error).lower()
        return any(sig in text for sig in _AUTH_REQUIRED_SIGNALS)
    # An explicit authMethods key in the error payload is a strong signal.
    if "authMethods" in error or (
        isinstance(error.get("data"), dict) and "authMethods" in error["data"]
    ):
        return True
    blob = json.dumps(error, ensure_ascii=False).lower()
    return any(sig in blob for sig in _AUTH_REQUIRED_SIGNALS)


def detect_known_bug_from_stderr(stderr_chunk: str) -> str | None:
    """Check a stderr chunk for known-bug signals.

    Returns a bug ID string if detected, else None.
    Fail-closed: caller should abort on detection.
    """
    lower = stderr_chunk.lower()
    for bug_id, patterns in _KNOWN_BUG_STDERR_PATTERNS.items():
        if any(pat in lower for pat in patterns):
            return bug_id
    return None


# ---------------------------------------------------------------------------
# ACP session — async core
# ---------------------------------------------------------------------------

# Legacy names kept for backward compatibility with existing tests and callers.
# Actual ACP events use snake_case via session/update.sessionUpdate.
STRUCTURED_EVENT_TYPES = frozenset({
    "AgentMessageChunk", "AgentThoughtChunk", "ToolCallStart",          # legacy
    "agent_message_chunk", "agent_thought_chunk", "tool_call",          # ACP actual
    "tool_call_update", "session/request_permission",                   # ACP actual
})


async def _cleanup_proc(
    proc: "asyncio.subprocess.Process",
    stderr_reader: "asyncio.Future[None]",
    stderr_accumulated_ref: list[str],
    drain_timeout: float = 5.0,
) -> str | None:
    """Kill proc, drain stderr with a timeout, cancel the reader task.

    Returns accumulated stderr text (may be partial if drain timed out).
    """
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    # Wait for the process to fully exit so the pipes get EOF
    try:
        await asyncio.wait_for(proc.wait(), timeout=drain_timeout)
    except asyncio.TimeoutError:
        pass
    # Drain remaining stderr with a bounded timeout
    try:
        await asyncio.wait_for(asyncio.shield(stderr_reader), timeout=drain_timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        stderr_reader.cancel()
        try:
            await stderr_reader
        except asyncio.CancelledError:
            pass
    return stderr_accumulated_ref[0] or None


async def _run_acp_session(
    prompt: str,
    model: str,
    approve_edits: bool,
    timeout_sec: int,
    gemini_bin: str = "gemini",
    cwd_override: str | None = None,
) -> dict[str, Any]:
    """Run a full ACP session against `gemini --acp`.

    Returns a result dict with keys:
      ok, transport_ok, stop_reason, structured_events, response_text, stderr,
      warnings, failure_reason, failure_class

    ``cwd_override``: when provided, this directory is used as both the
    ``session/new`` ``cwd`` and the ``asyncio.create_subprocess_exec`` ``cwd``,
    making the session deterministic. When ``None`` the legacy ``os.getcwd()``
    is used for both.

    ``failure_class`` is a structured classifier for the failure point (or
    ``None`` on success). Recognised values:
      gemini_not_found, launch_failed, initialize_failed, session_new_failed,
      auth_required, prompt_error, protocol_error, timeout, watchdog,
      incomplete_response

    ``auth_required`` is a session/new failure whose error signals the Gemini
    CLI / OAuth session is not authenticated. This transport does not implement
    the ACP ``authenticate`` handshake; ``auth_required`` is surfaced as a hard
    failure and is **not** subject to the headless_json fallback.

    ``transport_ok`` is the transport-level success (final response received,
    clean exit, valid session id, no failure). ``ok`` additionally requires the
    final ``stopReason`` to be ``end_turn`` and ``response_text`` to be non-empty.
    """
    result: dict[str, Any] = {
        "ok": False,
        "transport_ok": False,
        "stop_reason": None,
        "structured_events": [],
        "response_text": None,
        "stderr": None,
        "warnings": [],
        "failure_reason": None,
        "failure_class": None,
        "auth_methods": None,
    }

    # Resolve a deterministic cwd: cwd_override when provided, else os.getcwd().
    # The same value is used for the subprocess cwd and the session/new cwd.
    session_cwd = cwd_override if cwd_override is not None else os.getcwd()

    # Launch gemini --acp — honour GEMINI_BIN so callers can inject a custom binary
    try:
        proc = await asyncio.create_subprocess_exec(
            gemini_bin,
            "--acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=session_cwd,
        )
    except FileNotFoundError:
        result["failure_reason"] = f"gemini CLI not found: {gemini_bin!r}"
        result["failure_class"] = "gemini_not_found"
        result["warnings"].append(result["failure_reason"])
        return result
    except Exception as exc:
        result["failure_reason"] = f"failed to launch gemini --acp: {exc}"
        result["failure_class"] = "launch_failed"
        result["warnings"].append(result["failure_reason"])
        return result

    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    watchdog = HeartbeatWatchdog()
    req_id = 0
    structured_events: list[dict[str, Any]] = []
    # Use a list as a mutable ref so _cleanup_proc can see updates from the task
    stderr_buf: list[str] = [""]
    response_parts: list[str] = []
    session_id: str | None = None
    # B5: track whether the final session/prompt response was actually received.
    # EOF / process death before this is reached must NOT be treated as success.
    final_response_received: bool = False

    async def read_stderr_task() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            chunk = line.decode(errors="replace")
            stderr_buf[0] += chunk
            bug = detect_known_bug_from_stderr(chunk)
            if bug:
                result["warnings"].append(
                    f"known gemini-cli bug detected in stderr: {bug}; aborting"
                )
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    stderr_reader: asyncio.Future[None] = asyncio.ensure_future(read_stderr_task())

    async def send(payload: bytes) -> None:
        proc.stdin.write(payload)  # type: ignore[union-attr]
        await proc.stdin.drain()  # type: ignore[union-attr]

    async def early_exit(reason: str, failure_class: str) -> dict[str, Any]:
        """Kill proc, drain stderr (bounded), and return partial result."""
        result["failure_reason"] = reason
        result["failure_class"] = failure_class
        result["warnings"].append(reason)
        result["stderr"] = await _cleanup_proc(proc, stderr_reader, stderr_buf)
        return result

    # ------------------------------------------------------------------
    # initialize
    # ------------------------------------------------------------------
    req_id += 1
    # protocolVersion must be a number (int); gemini-cli 0.42.0+ rejects strings.
    # clientCapabilities fs=false / terminal=false means only that this ACP
    # *client* provides no client-side fs/terminal proxy (readTextFile/
    # writeTextFile/terminal RPCs). It does NOT disable Gemini CLI's own native
    # tool registry, cwd-resolved MCP servers, or approvalMode — those are not
    # controlled by this transport and their safety-boundary design is deferred
    # to follow-up #112. The permission handler below is best-effort defence in
    # depth, not a complete sandbox.
    await send(_rpc_request("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
    }, req_id))

    init_timeout = float(watchdog.CONNECT)
    try:
        raw_init = await asyncio.wait_for(proc.stdout.readline(), timeout=init_timeout)
    except asyncio.TimeoutError:
        return await early_exit(
            f"connect timeout ({watchdog.CONNECT}s) waiting for initialize response "
            f"— possible bugs: {INITIALIZE_HANG_BUG}, {AUTH_HANG_BUG}, {SETTINGS_HANG_BUG}",
            "initialize_failed",
        )

    watchdog.heartbeat()
    init_msg = _parse_rpc_line(raw_init.decode(errors="replace"))
    if not init_msg or init_msg.get("id") != req_id:
        return await early_exit(
            f"unexpected response to initialize: {raw_init!r}",
            "initialize_failed",
        )

    if "error" in init_msg:
        return await early_exit(
            f"initialize error: {init_msg['error']}", "initialize_failed"
        )

    # Capture authMethods advertised by the agent in the initialize result.
    # This transport does not implement the ACP `authenticate` handshake; the
    # value is retained for diagnostics and to disambiguate auth failures.
    init_result_data = init_msg.get("result", {})
    if isinstance(init_result_data, dict):
        auth_methods = init_result_data.get("authMethods")
        if auth_methods:
            result["auth_methods"] = auth_methods

    # ------------------------------------------------------------------
    # session/new — "default" approvalMode so tools and permission requests are active
    # ------------------------------------------------------------------
    req_id += 1
    await send(
        _rpc_request(
            "session/new",
            {
                "model": model,
                "approvalMode": "default",
                "cwd": session_cwd,
                "mcpServers": [],
            },
            req_id,
        )
    )

    # session/new may be preceded by notifications (e.g. session/update);
    # skip them and wait for the response with the matching id.
    session_timeout = float(watchdog.CONNECT)
    session_deadline = asyncio.get_event_loop().time() + session_timeout
    session_msg: dict[str, Any] | None = None
    while True:
        remaining_st = session_deadline - asyncio.get_event_loop().time()
        if remaining_st <= 0:
            return await early_exit(
                f"connect timeout ({watchdog.CONNECT}s) waiting for session/new response "
                f"— possible bug: {INITIALIZE_HANG_BUG}",
                "session_new_failed",
            )
        try:
            raw_session = await asyncio.wait_for(
                proc.stdout.readline(), timeout=min(5.0, remaining_st)
            )
        except asyncio.TimeoutError:
            continue
        if not raw_session:
            return await early_exit(
                "EOF waiting for session/new response", "session_new_failed"
            )
        watchdog.heartbeat()
        parsed = _parse_rpc_line(raw_session.decode(errors="replace"))
        if parsed is None:
            continue  # skip non-JSON lines
        if parsed.get("id") == req_id:
            session_msg = parsed
            break
        # Any other message (notification) is dropped here — session/prompt loop handles them

    if "error" in session_msg:
        session_error = session_msg["error"]
        # B2: an auth-required session/new failure must be classified as
        # `auth_required` (not `session_new_failed`) so run_acp() surfaces it
        # honestly instead of masking it behind a headless_json fallback. This
        # transport assumes a pre-authenticated Gemini CLI / OAuth session and
        # does not implement the ACP `authenticate` handshake.
        if is_auth_required_error(session_error) or result.get("auth_methods"):
            return await early_exit(
                f"session/new requires authentication "
                f"(ACP authenticate handshake not implemented; "
                f"pre-authenticated session expected): {session_error}",
                "auth_required",
            )
        return await early_exit(
            f"session/new error: {session_error}", "session_new_failed"
        )

    session_result_data = session_msg.get("result", {})
    # gemini-cli returns sessionId (not id) in session/new result
    session_id = (
        session_result_data.get("sessionId") or session_result_data.get("id")
    ) if isinstance(session_result_data, dict) else None

    # ------------------------------------------------------------------
    # session/prompt — event stream
    # ------------------------------------------------------------------
    req_id += 1
    # prompt must be an array of parts: [{"type": "text", "text": "..."}]
    # sessionId is required (from session/new result)
    prompt_params: dict[str, Any] = {
        "prompt": [{"type": "text", "text": prompt}],
        "sessionId": session_id or "",
    }

    await send(_rpc_request("session/prompt", prompt_params, req_id))

    deadline = asyncio.get_event_loop().time() + float(timeout_sec)

    while True:
        now = asyncio.get_event_loop().time()
        remaining = deadline - now
        if remaining <= 0:
            result["failure_reason"] = f"total timeout exceeded ({timeout_sec}s)"
            result["failure_class"] = "timeout"
            result["warnings"].append(result["failure_reason"])
            proc.kill()
            break

        # Check watchdog
        watchdog_reason = watchdog.check()
        if watchdog_reason:
            result["failure_reason"] = watchdog_reason
            result["failure_class"] = "watchdog"
            result["warnings"].append(result["failure_reason"])
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            break

        try:
            raw_line = await asyncio.wait_for(
                proc.stdout.readline(),
                timeout=min(5.0, remaining),
            )
        except asyncio.TimeoutError:
            continue

        if not raw_line:
            # EOF — session ended
            break

        watchdog.heartbeat()
        msg = _parse_rpc_line(raw_line.decode(errors="replace"))
        if msg is None:
            continue

        method = msg.get("method", "")

        # -- ACP notifications: session/update carries all agent events
        if method == "session/update":
            update = (msg.get("params") or {}).get("update") or {}
            kind = update.get("sessionUpdate", "")
            if kind:
                structured_events.append({"type": kind, "params": msg.get("params", {})})
                watchdog.mark_first_result()
            if kind == "agent_message_chunk":
                content = update.get("content") or {}
                if isinstance(content, dict) and content.get("type") == "text":
                    text = content.get("text")
                    if isinstance(text, str):
                        response_parts.append(text)

        # -- ACP permission requests: session/request_permission (has id, expects response)
        elif method == "session/request_permission":
            params = msg.get("params") or {}
            permission_result = handle_session_request_permission(params, approve_edits)
            # Record permission event in structured_events for verification
            structured_events.append({
                "type": "session/request_permission",
                "params": params,
                "outcome": permission_result,
            })
            watchdog.mark_first_result()
            resp = {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": permission_result,
            }
            await send((json.dumps(resp, ensure_ascii=False) + "\n").encode())

        # -- session/prompt final response (id matches)
        elif msg.get("id") == req_id:
            # The final session/prompt response was received — record this so a
            # subsequent EOF/process exit is not misclassified (B5).
            final_response_received = True
            if "error" in msg:
                result["failure_reason"] = f"session/prompt error: {msg['error']}"
                result["failure_class"] = "prompt_error"
                result["warnings"].append(result["failure_reason"])
            else:
                # Extract text and stopReason from final result if present
                final_result = msg.get("result", {})
                if isinstance(final_result, dict):
                    text = final_result.get("text") or final_result.get("response")
                    if isinstance(text, str) and text:
                        response_parts.append(text)
                    stop_reason = final_result.get("stopReason")
                    if isinstance(stop_reason, str):
                        result["stop_reason"] = stop_reason
                watchdog.mark_first_result()
            break

    # Drain stderr with a bounded timeout — never await without a deadline
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc_returncode: int | None = None
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        proc_returncode = proc.returncode
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # Process did not exit cleanly within the drain window.
        proc_returncode = proc.returncode
    try:
        await asyncio.wait_for(asyncio.shield(stderr_reader), timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        stderr_reader.cancel()
        try:
            await stderr_reader
        except asyncio.CancelledError:
            pass

    result["structured_events"] = structured_events
    result["response_text"] = "".join(response_parts) or None
    result["stderr"] = stderr_buf[0] or None

    # B5: the loop may have exited on EOF before the final session/prompt
    # response was received. That is a protocol-level failure, not success.
    if result["failure_reason"] is None and not final_response_received:
        result["failure_reason"] = "EOF before final session/prompt response"
        result["failure_class"] = "protocol_error"
        result["warnings"].append(result["failure_reason"])

    # B5: transport-level success requires no failure, the final response
    # received, a clean process exit, and a valid session id.
    transport_ok = (
        result["failure_reason"] is None
        and final_response_received
        and proc_returncode == 0
        and bool(session_id)
    )
    result["transport_ok"] = transport_ok

    # B4: a transport-level success is not enough. The session may have ended
    # with a non-`end_turn` stopReason (cancel/refusal/max_tokens) or an empty
    # response. Treat those as incomplete responses, not successes.
    stop_reason = result.get("stop_reason")
    response_clean = (result.get("response_text") or "").strip()
    result["ok"] = (
        transport_ok
        and stop_reason == "end_turn"
        and bool(response_clean)
    )

    if transport_ok and not result["ok"] and result["failure_reason"] is None:
        result["failure_reason"] = (
            f"session ended with stopReason={stop_reason!r} and/or empty response"
        )
        result["failure_class"] = "incomplete_response"
        result["warnings"].append(result["failure_reason"])

    return result


# ---------------------------------------------------------------------------
# Fallback: delegate to headless_json transport
# ---------------------------------------------------------------------------


def _fallback_to_headless_json(
    request: dict[str, Any],
    request_path: "Path | None",
    failure_reason: str,
) -> dict[str, Any]:
    """Fallback from acp to headless_json transport when ACP initialize/session/new fails.

    Invoked when the ACP pathway hangs or produces an error at the initialize or
    session/new step. Delegates to run_delegation() in run_gemini_headless.py
    using the original request unchanged.
    """
    # Import here to avoid circular import at module load time
    from run_gemini_headless import run_delegation  # type: ignore[import]

    fallback_request = {**request, "transport": "headless_json"}
    fallback_result = run_delegation(fallback_request, request_path=request_path)
    fallback_result.setdefault("warnings", []).insert(
        0,
        f"acp transport failed ({failure_reason}); fell back to headless_json transport",
    )
    fallback_result["_acp_fallback"] = True
    return fallback_result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_acp(
    request: dict[str, Any],
    request_path: "Path | None" = None,
    approve_edits: bool = False,
    prepared_prompt: str | None = None,
    model_override: str | None = None,
    cwd_override: str | None = None,
) -> dict[str, Any]:
    """Run a delegation request using ACP transport.

    Launches `gemini --acp`, performs initialize -> session/new ->
    session/prompt lifecycle, and collects structured events.

    Falls back to headless_json transport if initialize or session/new fails
    (see fallback comment in _fallback_to_headless_json).

    ``prepared_prompt``: **required** — the exact prompt string to send to the
    ACP session. The prompt must already have been built by ``build_prompt()``
    after ``validate_request()`` and context loading so the ACP path honours the
    same delegation contract as headless_json. Passing ``None`` is a contract
    bypass and returns a ``failure_class="contract_bypass"`` failure result.

    ``model_override``: when provided, this model is used instead of
    ``request["model"]`` (the resolved model chain head from ``run_delegation``).

    ``cwd_override``: when provided, the deterministic working directory used for
    both the ``gemini --acp`` subprocess and the ``session/new`` ``cwd``.

    Returns a dict with at least: ok, structured_events, response_text,
    stderr, warnings, failure_reason, failure_class, schema, transport.
    """
    # NB1: prepared_prompt is required. Building the prompt here from
    # objective/instructions bypasses validate_request()/build_prompt() and the
    # delegation contract — fail closed instead.
    if prepared_prompt is None:
        return {
            "ok": False,
            "structured_events": [],
            "response_text": None,
            "stderr": None,
            "warnings": [
                "run_acp() requires prepared_prompt; call via run_delegation() "
                "so the request passes validate_request()/build_prompt()"
            ],
            "failure_reason": "run_acp() called without prepared_prompt (contract bypass)",
            "failure_class": "contract_bypass",
            "schema": "acp_result_v1",
            "transport": "acp",
        }
    prompt = prepared_prompt

    if model_override:
        model = str(model_override)
    else:
        model = str(request.get("model", "gemini-2.5-flash"))
    timeout_sec = int(request.get("timeout_sec", TOTAL_TIMEOUT_SEC))
    # NB2: GEMINI_BIN is honoured only via the environment variable. Reading it
    # from the request JSON would let an untrusted request swap the binary.
    gemini_bin = str(os.environ.get("GEMINI_BIN") or "gemini")

    try:
        try:
            loop = asyncio.get_running_loop()
            # If there's already a running loop, we can't call run_until_complete.
            # This path is only hit if run_acp is called from within an async context.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    lambda: asyncio.run(
                        _run_acp_session(
                            prompt=prompt,
                            model=model,
                            approve_edits=approve_edits,
                            timeout_sec=timeout_sec,
                            gemini_bin=gemini_bin,
                            cwd_override=cwd_override,
                        )
                    )
                )
                result = future.result()
        except RuntimeError:
            # No running loop — standard synchronous path
            result = asyncio.run(
                _run_acp_session(
                    prompt=prompt,
                    model=model,
                    approve_edits=approve_edits,
                    timeout_sec=timeout_sec,
                    gemini_bin=gemini_bin,
                    cwd_override=cwd_override,
                )
            )
    except Exception as exc:
        result = {
            "ok": False,
            "transport_ok": False,
            "stop_reason": None,
            "structured_events": [],
            "response_text": None,
            "stderr": None,
            "warnings": [str(exc)],
            "failure_reason": str(exc),
            "failure_class": "launch_failed",
        }

    result["schema"] = "acp_result_v1"
    result["transport"] = "acp"
    result.setdefault("failure_class", None)

    # Fallback: if the ACP path failed before producing a usable session, try
    # headless_json. The decision is driven by the structured failure_class
    # (B4) rather than fragile substring matching on failure_reason.
    #
    # `auth_required` is deliberately EXCLUDED from this set: an auth-required
    # failure means the Gemini CLI / OAuth session is not pre-authenticated.
    # Falling back to headless_json would mask the ACP transport failure behind
    # an apparent "fallback success" — the auth failure must surface honestly.
    EARLY_FAILURE_CLASSES = {
        "gemini_not_found",
        "launch_failed",
        "initialize_failed",
        "session_new_failed",
    }
    if not result["ok"] and result.get("failure_class") in EARLY_FAILURE_CLASSES:
        failure = result.get("failure_reason") or result.get("failure_class")
        try:
            return _fallback_to_headless_json(request, request_path, str(failure))
        except Exception as fb_exc:
            result["warnings"].append(f"fallback to headless_json also failed: {fb_exc}")

    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--output-file", required=True, type=Path)
    parser.add_argument(
        "--approve-edits",
        action="store_true",
        default=False,
        help="Allow write operations (write_file, edit_file, etc) via permission proxy.",
    )
    args = parser.parse_args(argv)

    req_raw = args.request_file.read_text(encoding="utf-8")
    request = json.loads(req_raw)

    # Route through run_delegation() so the standalone CLI entry point also
    # passes validate_request() / model chain resolution / context loading /
    # build_prompt() before reaching the ACP session (B1). transport="acp" is
    # forced and approve_edits is injected into the request dict so the
    # dispatcher in run_gemini_headless.run_delegation() picks them up.
    from run_gemini_headless import run_delegation  # type: ignore[import]

    delegation_request = dict(request)
    delegation_request["transport"] = "acp"
    if args.approve_edits:
        delegation_request["approve_edits"] = True

    result = run_delegation(delegation_request, request_path=args.request_file)

    output = json.dumps(result, ensure_ascii=False, indent=2)
    args.output_file.write_text(output, encoding="utf-8")

    if result.get("ok"):
        print(result.get("response_text") or "[acp] ok: session completed")
    else:
        print(result.get("failure_reason") or "[acp] error: session failed")
    print(f"[acp] result saved to: {args.output_file}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

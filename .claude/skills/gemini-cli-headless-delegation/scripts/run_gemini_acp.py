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

# stderr patterns that indicate each known bug
_KNOWN_BUG_STDERR_PATTERNS: dict[str, list[str]] = {
    AUTH_HANG_BUG: ["refreshing credentials", "waiting for auth", "oauth token"],
    INITIALIZE_HANG_BUG: ["initialize", "protocol handshake"],
    SETTINGS_HANG_BUG: ["reading settings", "loading settings", "settings.json"],
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
) -> dict[str, Any]:
    """Run a full ACP session against `gemini --acp`.

    Returns a result dict with keys:
      ok, structured_events, response_text, stderr, warnings, failure_reason
    """
    result: dict[str, Any] = {
        "ok": False,
        "structured_events": [],
        "response_text": None,
        "stderr": None,
        "warnings": [],
        "failure_reason": None,
    }

    # Launch gemini --acp — honour GEMINI_BIN so callers can inject a custom binary
    try:
        proc = await asyncio.create_subprocess_exec(
            gemini_bin,
            "--acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        result["failure_reason"] = f"gemini CLI not found: {gemini_bin!r}"
        result["warnings"].append(result["failure_reason"])
        return result
    except Exception as exc:
        result["failure_reason"] = f"failed to launch gemini --acp: {exc}"
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

    async def early_exit(reason: str) -> dict[str, Any]:
        """Kill proc, drain stderr (bounded), and return partial result."""
        result["failure_reason"] = reason
        result["warnings"].append(reason)
        result["stderr"] = await _cleanup_proc(proc, stderr_reader, stderr_buf)
        return result

    # ------------------------------------------------------------------
    # initialize
    # ------------------------------------------------------------------
    req_id += 1
    # protocolVersion must be a number (int); gemini-cli 0.42.0+ rejects strings
    await send(_rpc_request("initialize", {"protocolVersion": 1}, req_id))

    init_timeout = float(watchdog.CONNECT)
    try:
        raw_init = await asyncio.wait_for(proc.stdout.readline(), timeout=init_timeout)
    except asyncio.TimeoutError:
        return await early_exit(
            f"connect timeout ({watchdog.CONNECT}s) waiting for initialize response "
            f"— possible bugs: {INITIALIZE_HANG_BUG}, {AUTH_HANG_BUG}, {SETTINGS_HANG_BUG}"
        )

    watchdog.heartbeat()
    init_msg = _parse_rpc_line(raw_init.decode(errors="replace"))
    if not init_msg or init_msg.get("id") != req_id:
        return await early_exit(
            f"unexpected response to initialize: {raw_init!r}"
        )

    if "error" in init_msg:
        return await early_exit(f"initialize error: {init_msg['error']}")

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
                "cwd": os.getcwd(),
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
                f"— possible bug: {INITIALIZE_HANG_BUG}"
            )
        try:
            raw_session = await asyncio.wait_for(
                proc.stdout.readline(), timeout=min(5.0, remaining_st)
            )
        except asyncio.TimeoutError:
            continue
        if not raw_session:
            return await early_exit("EOF waiting for session/new response")
        watchdog.heartbeat()
        parsed = _parse_rpc_line(raw_session.decode(errors="replace"))
        if parsed is None:
            continue  # skip non-JSON lines
        if parsed.get("id") == req_id:
            session_msg = parsed
            break
        # Any other message (notification) is dropped here — session/prompt loop handles them

    if "error" in session_msg:
        return await early_exit(f"session/new error: {session_msg['error']}")

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
            result["warnings"].append(result["failure_reason"])
            proc.kill()
            break

        # Check watchdog
        watchdog_reason = watchdog.check()
        if watchdog_reason:
            result["failure_reason"] = watchdog_reason
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
            if "error" in msg:
                result["failure_reason"] = f"session/prompt error: {msg['error']}"
                result["warnings"].append(result["failure_reason"])
            else:
                # Extract text from final result if present
                final_result = msg.get("result", {})
                if isinstance(final_result, dict):
                    text = final_result.get("text") or final_result.get("response")
                    if isinstance(text, str) and text:
                        response_parts.append(text)
                watchdog.mark_first_result()
            break

    # Drain stderr with a bounded timeout — never await without a deadline
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
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

    if result["failure_reason"] is None:
        result["ok"] = True

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
) -> dict[str, Any]:
    """Run a delegation request using ACP transport.

    Launches `gemini --acp`, performs initialize -> session/new ->
    session/prompt lifecycle, and collects structured events.

    Falls back to headless_json transport if initialize or session/new fails
    (see fallback comment in _fallback_to_headless_json).

    Returns a dict with at least: ok, structured_events, response_text,
    stderr, warnings, failure_reason, schema, transport.
    """
    prompt = request.get("objective", "")
    instructions = request.get("instructions", [])
    if instructions:
        prompt = prompt + "\n\n" + "\n".join(f"{i + 1}. {instr}" for i, instr in enumerate(instructions))

    model = str(request.get("model", "gemini-2.5-flash"))
    timeout_sec = int(request.get("timeout_sec", TOTAL_TIMEOUT_SEC))
    # GEMINI_BIN: request field takes precedence, then env var, then default
    gemini_bin = str(request.get("gemini_bin") or os.environ.get("GEMINI_BIN") or "gemini")

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
                )
            )
    except Exception as exc:
        result = {
            "ok": False,
            "structured_events": [],
            "response_text": None,
            "stderr": None,
            "warnings": [str(exc)],
            "failure_reason": str(exc),
        }

    result["schema"] = "acp_result_v1"
    result["transport"] = "acp"

    # Fallback: if initialize or session/new failed, try headless_json
    if not result["ok"] and result.get("failure_reason"):
        failure = result["failure_reason"]
        is_early_failure = any(
            keyword in failure
            for keyword in [
                "initialize",
                "session/new",
                "connect timeout",
                "not found in PATH",
                "failed to launch",
            ]
        )
        if is_early_failure:
            try:
                return _fallback_to_headless_json(request, request_path, failure)
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

    result = run_acp(request, request_path=args.request_file, approve_edits=args.approve_edits)

    output = json.dumps(result, ensure_ascii=False, indent=2)
    args.output_file.write_text(output, encoding="utf-8")

    if result["ok"]:
        print(result.get("response_text") or "[acp] ok: session completed")
    else:
        print(result.get("failure_reason") or "[acp] error: session failed")
    print(f"[acp] result saved to: {args.output_file}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

"""Task Context v1 — thin subprocess client for `task-contextctl` (Issue #2564).

Shared by the Claude-native hook adapter (`hook_entry.py`) and the
statusLine renderer (`statusline.py`). Deliberately tiny and stdlib-only
(``scripts/`` CLAUDE.md: "外部ライブラリ依存はゼロを目標とする") -- it only
knows how to build the frozen request envelope, invoke the real
``task_contextctl.py`` as a subprocess with a bounded timeout, and parse the
single-line JSON result envelope back. It never touches the DB directly and
never imports anything from ``scripts/task-context`` -- the wire boundary is
intentionally the subprocess/JSON contract itself, the same one any other
out-of-repo consumer would use.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import uuid
from typing import Any

_THIS_DIR = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parents[2]
CTL_PATH = _REPO_ROOT / "scripts" / "task-context" / "task_contextctl.py"

REQUEST_SCHEMA_VERSION = "task-context-request/v1"


def build_request(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "operation": operation,
        "request_id": str(uuid.uuid4()),
        "payload": payload,
    }


def call(
    argv: list[str], operation: str, payload: dict[str, Any], *, timeout: float
) -> dict[str, Any] | None:
    """Invoke ``task_contextctl.py <argv...>`` with ``payload`` wrapped in
    the canonical request envelope. Returns the parsed result envelope, or
    ``None`` on any failure (timeout, non-JSON output, missing interpreter,
    etc.) -- callers must fail open (never block a Claude Code hook event on
    an adapter-side transport failure)."""
    request = build_request(operation, payload)
    try:
        proc = subprocess.run(
            [sys.executable, str(CTL_PATH), *argv],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def call_hook(event: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any] | None:
    return call(["hook", event], "hook", payload, timeout=timeout)


def call_query_current_by_session(session_id: str, *, timeout: float) -> dict[str, Any] | None:
    return call(["query", "current"], "query_current", {"session_id": session_id}, timeout=timeout)


def call_projection_flush(projection_key: str, *, timeout: float) -> dict[str, Any] | None:
    return call(["projection", "flush"], "projection_flush", {"projection_key": projection_key}, timeout=timeout)


def call_projection_ack(projection_key: str, read_revision: int, *, timeout: float) -> dict[str, Any] | None:
    return call(
        ["projection", "ack"],
        "projection_ack",
        {"projection_key": projection_key, "read_revision": read_revision},
        timeout=timeout,
    )

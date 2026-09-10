#!/usr/bin/env python3
"""Task Context v1 — read-only statusLine renderer (Issue #2564 AC9).

Wired from `.claude/settings.json` as:

    { "statusLine": { "type": "command", "command": "python3 .../statusline.py" } }

Contract (Claude Code `statusLine` command): reads a JSON object off stdin
containing (at least) ``session_id``, and prints the rendered status line
text to stdout.

AC9: this script -- and the `query current` session-selector CLI path it
calls into (``task_context_db.connect_readonly`` /
``task_contextctl._dispatch_query_current_by_session``) -- is genuinely
read-only. It never creates the state-root directory, never creates or
migrates the DB file, never mutates projection/outbox state, never performs
a GitHub fetch, and never mutates Herdr state. If the DB does not exist yet,
or no Binding currently claims this session, it renders an empty/degraded
line instead of creating anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import ctl_client  # noqa: E402

TIMEOUT_SECONDS = 2.0

_OSC8_START = "\033]8;;{url}\033\\"
_OSC8_END = "\033]8;;\033\\"


def _osc8_link(url: str, label: str) -> str:
    return f"{_OSC8_START.format(url=url)}{label}{_OSC8_END}"


def _read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def render(data: dict) -> str:
    if data.get("degraded"):
        reason = data.get("degraded_reason") or "unknown"
        return f"[Task Context: degraded ({reason})]"

    task = data.get("task")
    if not task:
        return "[Task Context: unbound]"

    activity = data.get("activity")
    binding = data.get("binding")
    refs = data.get("task_refs") or []
    attention = data.get("attention")

    parts = []
    if refs:
        ref = refs[0]
        repo = ref.get("repo")
        number = ref.get("ref_number")
        kind = ref.get("ref_kind")
        label = f"{repo}#{number}"
        if repo and number is not None:
            path = "pull" if kind == "pr" else "issues"
            url = f"https://github.com/{repo}/{path}/{number}"
            parts.append(_osc8_link(url, label))
        else:
            parts.append(label)
    else:
        parts.append(task.get("title") or task["id"])

    if activity is not None:
        parts.append(f"activity={activity['kind']}")

    if binding is not None:
        health = binding.get("runtime_health")
        if health and health != "ACTIVE":
            parts.append(f"health={health}")

    if attention:
        parts.append(f"! {attention}")

    return "[Task Context] " + " · ".join(parts)


def main(argv: list[str]) -> int:
    hook_input = _read_stdin_json()
    session_id = hook_input.get("session_id")
    if not session_id:
        print("[Task Context: no session]")
        return 0

    result_envelope = ctl_client.call_query_current_by_session(session_id, timeout=TIMEOUT_SECONDS)
    if not result_envelope or result_envelope.get("status") != "ok":
        print("[Task Context: degraded (query_failed)]")
        return 0

    data = result_envelope.get("data") or {}
    print(render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

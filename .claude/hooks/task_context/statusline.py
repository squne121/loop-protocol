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


# Issue #2634 AC4: static best-effort mapping from the raw ``activity.kind``
# free-form value to a human-readable label. Covers both vocabularies seen
# in this codebase: the `execution_runs.run_kind` CHECK-constraint
# vocabulary (`native_operator`/`subagent`/`runtime_smoke`/`claude_gpt`,
# `task_context_schema.py`) and the free-form `activities.kind` TEXT values
# actually observed in practice (`impl`/`refine`/`cleanup`/`review`/`smoke`,
# see `task_contextctl.py`'s `smoke seed` and the `tests/task-context`
# fixtures). Any kind not present here falls back to the raw value rather
# than raising or silently dropping the activity label (AC4). The rendered
# label itself carries no `activity=` prefix -- it is appended as a bare
# `·`-separated token (PR #2640 review fix_delta).
_ACTIVITY_LABELS: dict[str, str] = {
    "native_operator": "native",
    "subagent": "subagent",
    "runtime_smoke": "smoke",
    "claude_gpt": "claude-gpt",
    "impl": "impl",
    "refine": "refine",
    "cleanup": "cleanup",
    "review": "review",
    "smoke": "smoke",
}


def _activity_label(kind: str) -> str:
    """Human-readable label for a raw ``activity.kind`` value; falls back to
    the raw ``kind`` itself for anything not in ``_ACTIVITY_LABELS`` (AC4:
    never raise, never silently drop). Returned bare (no ``activity=``
    prefix); callers append it directly as a presentation token."""
    return _ACTIVITY_LABELS.get(kind, kind)


# `_dispatch_query_current_by_session` (task_contextctl.py) sets
# `degraded=True, degraded_reason="no_binding_for_session"` when the DB
# itself is reachable but this session simply has no Task Context Binding
# yet -- that is semantically "Unbound", not a technical failure. Any other
# degraded_reason (e.g. "no_state_db", or `main()`'s own query-transport
# failure) is a genuine DB/query failure -- "Degraded" (Issue #2634 AC3).
_DEGRADED_REASON_UNBOUND = "no_binding_for_session"


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
    # Issue #2634 AC3: diagnostic `degraded_reason` is kept as an internal
    # data field (never deleted from the DB/query envelope), but is no
    # longer surfaced in the presentation string itself. "no binding for
    # this session yet" degrades to plain "Unbound"; every other reason
    # (a real DB/query failure) degrades to "Degraded".
    if data.get("degraded"):
        reason = data.get("degraded_reason")
        if reason == _DEGRADED_REASON_UNBOUND:
            return "Unbound"
        return "Degraded"

    task = data.get("task")
    if not task:
        return "Unbound"

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
        # Issue #2634 AC2: repo 名を含まない `#<number>` 形式 (OSC8 リンクの
        # label としてそのまま使う。旧 `f"{repo}#{number}"` から変更).
        label = f"#{number}"
        if repo and number is not None:
            path = "pull" if kind == "pr" else "issues"
            url = f"https://github.com/{repo}/{path}/{number}"
            parts.append(_osc8_link(url, label))
        else:
            parts.append(label)
    else:
        parts.append(task.get("title") or task["id"])

    if activity is not None:
        parts.append(_activity_label(activity["kind"]))

    if binding is not None:
        health = binding.get("runtime_health")
        if health and health != "ACTIVE":
            parts.append(f"health={health}")

    # fix_delta 7 (AC9: "statusLine に詳細... worktree/branch/runtime
    # health を出す"): display-only RuntimeLocation observation -- never
    # part of Task/Binding identity (AC7).
    location = data.get("runtime_location")
    if location is not None:
        worktree = location.get("worktree")
        branch = location.get("branch")
        if worktree:
            parts.append(f"worktree={worktree}")
        if branch:
            parts.append(f"branch={branch}")

    if attention:
        parts.append(f"! {attention}")

    # Issue #2634 AC2: no more leading "[Task Context] " prefix.
    return " · ".join(parts)


def main(argv: list[str]) -> int:
    hook_input = _read_stdin_json()
    session_id = hook_input.get("session_id")
    if not session_id:
        # Issue #2634 AC3: no-session is presentation-equivalent to unbound.
        print("Unbound")
        return 0

    result_envelope = ctl_client.call_query_current_by_session(session_id, timeout=TIMEOUT_SECONDS)
    if not result_envelope or result_envelope.get("status") != "ok":
        # Issue #2634 AC3: a genuine transport/query failure (distinct from
        # a normal "no binding for this session" `render()` degraded case).
        print("Degraded")
        return 0

    data = result_envelope.get("data") or {}
    print(render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

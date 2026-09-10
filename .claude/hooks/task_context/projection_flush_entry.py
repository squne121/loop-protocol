#!/usr/bin/env python3
"""Task Context v1 — detached Herdr projection flush worker (Issue #2564).

PR #2615 fix_delta 2: this script is **not** wired into
`.claude/settings.json` as a Claude Code-native ``"async": true`` sibling
hook entry anymore. A Claude Code ``"async": true`` hook entry for the same
event runs concurrently with (and can race ahead of) the synchronous
`hook_entry.py` invocation for that same event -- which means it could try
to consume `projection_outbox` *before* the very DB mutation transaction
that enqueued it has committed. Instead, `hook_entry.py` launches this
script itself as a **detached subprocess** (``subprocess.Popen`` with
``start_new_session=True``, never waited on) only *after*
`ctl_client.call_hook` has already returned -- i.e. only after the DB
mutation transaction it triggered has already committed. That enforces the
causal ``commit -> project`` ordering `fix_delta 2` requires, while still
never blocking the hot path (the parent hook process never waits for this
one to finish).

Flow: `query current` (read the current projection for this session) ->
`projection flush` (read the outbox marker for this Binding) -> if a marker
is pending, re-derive the Herdr Tab label / pane metadata from the *current*
canonical DB state (never from a stored payload -- `projection_outbox` is
marker-only) -> `herdr tab rename` / `herdr pane report-metadata` -> only on
success, `projection ack` the exact revision read (AC10/AC12 conditional
ack semantics already live in the core service layer; this script performs
no DB writes of its own).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import ctl_client  # noqa: E402
import herdr_projection  # noqa: E402

TIMEOUT_SECONDS = 5.0


def _read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main(argv: list[str]) -> int:
    hook_input = _read_stdin_json()
    claude_session_id = hook_input.get("session_id")
    pane_id = os.environ.get("HERDR_PANE_ID") or os.environ.get("HERDR_TAB_ID")
    if not claude_session_id or not pane_id:
        return 0

    query_envelope = ctl_client.call_query_current_by_session(claude_session_id, timeout=TIMEOUT_SECONDS)
    if not query_envelope or query_envelope.get("status") != "ok":
        return 0
    data = query_envelope.get("data") or {}
    binding = data.get("binding")
    if data.get("degraded") or not binding:
        return 0

    projection_key = f"tab_binding:{binding['id']}"
    flush_envelope = ctl_client.call_projection_flush(projection_key, timeout=TIMEOUT_SECONDS)
    if not flush_envelope or flush_envelope.get("status") != "ok":
        return 0
    marker = (flush_envelope.get("data") or {}).get("projection")
    if not marker:
        return 0  # nothing enqueued -- already up to date

    revision = marker["desired_revision"]
    tab_label = herdr_projection.build_tab_label(
        data.get("task"), data.get("activity"), data.get("task_refs") or []
    )
    pane_tokens = herdr_projection.build_pane_tokens(data.get("task"), data.get("activity"), binding)
    state_label = herdr_projection.build_pane_state_label(binding)

    projected = herdr_projection.project_to_herdr(
        pane_id, tab_label, pane_tokens, revision=revision, state_label=state_label
    )
    if projected:
        # AC10: only ack on success -- a failed projection leaves the
        # outbox marker in place so the next lifecycle event retries it.
        # Committed DB state (Task/Activity/Binding) is never rolled back
        # regardless of Herdr-side outcome.
        ctl_client.call_projection_ack(projection_key, revision, timeout=TIMEOUT_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

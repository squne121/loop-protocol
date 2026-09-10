#!/usr/bin/env python3
"""Task Context v1 — thin Claude-native hook adapter entrypoint (Issue #2564).

Wired from `.claude/settings.json` as, e.g.:

    { "command": "python3", "args": [".../hook_entry.py", "SessionStart"] }

Responsibilities (and *only* these -- Task semantics/SQL live in
``scripts/task-context/task_context_hook_flows.py``, never duplicated here):

1. Read the actual Claude Code hook JSON off stdin + ``HERDR_TAB_ID``/
   ``HERDR_PANE_ID`` env vars.
2. For ``UserPromptSubmit`` only: run the deterministic classifier
   (``classifier.py``) over the *raw* prompt text and reduce it to small
   structured fields -- the raw prompt string itself is never forwarded to
   ``task-contextctl`` / persisted (AC7).
3. Call ``task-contextctl hook <event>`` with the resulting structured
   payload, via ``ctl_client`` (bounded timeout).
4. Translate the JSON result envelope's ``data.decision`` into the actual
   Claude Code hook exit-code / stdout contract.

Fail-open by construction: any adapter-side failure (DB busy/unavailable,
CLI transport error, timeout, malformed output) is treated as "no
result" -- the default decision remains ``pass`` and Claude Code proceeds
normally. Only an *actual* well-formed ``decision: block`` result blocks a
prompt (AC4/AC12 "wrong-primary-prompt guard" -- never DB-degraded blocking).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import classifier  # noqa: E402
import ctl_client  # noqa: E402

# Issue #2564 In Scope: "UserPromptSubmit Task Context adapter の timeout は
# 既定 30 秒を使わず最大 1 秒の bounded hot-path budget とし、既存 SQLite
# busy budget 200ms との整合を保つ."
HOT_PATH_TIMEOUT_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 5.0

_GIT_REMOTE_TIMEOUT_SECONDS = 2.0
_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]+([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def _read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _current_repo(cwd: str | None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_REMOTE_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = _GITHUB_REMOTE_RE.search(result.stdout.strip())
    return match.group(1) if match else None


def _build_base_payload(event: str, hook_input: dict) -> dict:
    herdr_tab_id = os.environ.get("HERDR_TAB_ID") or None
    herdr_pane_id = os.environ.get("HERDR_PANE_ID") or herdr_tab_id
    return {
        "event": event,
        "herdr_tab_id": herdr_tab_id,
        # AC5/durable-identity: the *durable* RuntimeLocation locator is the
        # Pane id (stable across the operator moving Tabs), not the Tab id
        # itself -- see herdr_projection.py module docstring.
        "herdr_locator": herdr_pane_id,
        "claude_session_id": hook_input.get("session_id") or None,
    }


def _apply_user_prompt_submit_fields(payload: dict, hook_input: dict) -> None:
    prompt = hook_input.get("prompt") or ""
    current_repo = _current_repo(hook_input.get("cwd"))
    classification = classifier.classify(prompt, current_repo=current_repo)
    payload["classification_kind"] = classification.kind

    if classification.kind == classifier.KIND_SLASH_TASK:
        target, ad_hoc_title = classifier.parse_slash_task_target(
            classification.slash_task_raw_target or "", current_repo=current_repo
        )
        if target is not None:
            payload["slash_task_target_repo"] = target.repo
            payload["slash_task_target_ref_kind"] = target.ref_kind
            payload["slash_task_target_ref_number"] = target.ref_number
        elif ad_hoc_title:
            payload["slash_task_ad_hoc_title"] = ad_hoc_title
        return

    if classification.target is not None:
        payload["target_repo"] = classification.target.repo
        payload["target_ref_kind"] = classification.target.ref_kind
        payload["target_ref_number"] = classification.target.ref_number


def _render_additional_context(projection_data: dict) -> str | None:
    task = projection_data.get("task")
    if not task:
        return None
    activity = projection_data.get("activity")
    refs = projection_data.get("task_refs") or []
    ref_str = ", ".join(f"{r['repo']}#{r['ref_number']}" for r in refs) or "(no linked Issue/PR yet)"
    activity_kind = activity["kind"] if activity else "-"
    title = task.get("title") or task["id"]
    return f"[Task Context] current Task: {title} | refs: {ref_str} | activity: {activity_kind}"


def _emit_session_start_context(claude_session_id: str | None, event_name: str) -> None:
    if not claude_session_id:
        return
    result_envelope = ctl_client.call_query_current_by_session(
        claude_session_id, timeout=DEFAULT_TIMEOUT_SECONDS
    )
    if not result_envelope or result_envelope.get("status") != "ok":
        return
    data = result_envelope.get("data") or {}
    if data.get("degraded"):
        return
    context = _render_additional_context(data)
    if not context:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        # Misconfiguration (missing event name arg) -- never block Claude.
        return 0
    event = argv[1]
    hook_input = _read_stdin_json()
    payload = _build_base_payload(event, hook_input)

    timeout = DEFAULT_TIMEOUT_SECONDS

    if event == "SessionStart":
        payload["source"] = hook_input.get("source") or "startup"
    elif event == "UserPromptSubmit":
        timeout = HOT_PATH_TIMEOUT_SECONDS
        _apply_user_prompt_submit_fields(payload, hook_input)

    result_envelope = ctl_client.call_hook(event, payload, timeout=timeout)

    decision = "pass"
    data: dict = {}
    if result_envelope and result_envelope.get("status") == "ok":
        data = result_envelope.get("data") or {}
        decision = data.get("decision", "pass")

    if event == "UserPromptSubmit" and decision == "block":
        reason_code = data.get("reason_code", "different_primary_target_active")
        print(
            "[task-context] blocked: this prompt targets a different ACTIVE Task/Activity "
            f"({reason_code}). Use `/task <target>` to explicitly switch Tasks.",
            file=sys.stderr,
        )
        return 2

    if event == "SessionStart" and decision == "pass":
        _emit_session_start_context(payload.get("claude_session_id"), event)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

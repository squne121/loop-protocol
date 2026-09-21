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
normally.

Issue #2625 (supersedes Issue #2564 / PR #2615's "wrong-primary-prompt hard
block"): ordinary ``UserPromptSubmit`` is **never** an admission gate for
Task Context. This adapter enforces a double fail-open invariant on that
event (AC3): even if the service unexpectedly returns a well-formed
``decision: block`` (future regression/version skew), ``main()`` still
returns exit 0 for ``UserPromptSubmit`` -- a mismatch is only ever surfaced
as a non-blocking stderr advisory, never as a blocked prompt. The one-time
PR #2615 fix_delta 4 carve-out (an explicit-looking raw `/task` prompt text
on ``UserPromptSubmit`` could still fail-closed) is retired along with the
raw-text special-case's state-changing authority.

The sole explicit human escape hatch for switching the ACTIVE Task,
``/task <target>``, is state-changing authority that now lives exclusively
in the ``UserPromptExpansion`` command lifecycle (``command_name ==
"task"``) -- see ``_apply_user_prompt_expansion_fields`` below and
``task_context_hook_flows.on_user_prompt_expansion``. There, and only
there, a CLI transport error / invalid result envelope / persistence
failure is surfaced as an explicit `/task` command failure (exit 2) rather
than silently pretending the rebind succeeded -- because `/task` is a
user-visible, explicit state-changing command. Every other ``command_name``
on ``UserPromptExpansion`` (i.e. not `/task`) is untouched by this hook.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import classifier  # noqa: E402
import ctl_client  # noqa: E402
import pre_tool_use_classifier  # noqa: E402

# Issue #2564 In Scope: "UserPromptSubmit Task Context adapter の timeout は
# 既定 30 秒を使わず最大 1 秒の bounded hot-path budget とし、既存 SQLite
# busy budget 200ms との整合を保つ."
HOT_PATH_TIMEOUT_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 5.0

_GIT_REMOTE_TIMEOUT_SECONDS = 2.0
_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]+([\w.-]+/[\w.-]+?)(?:\.git)?/?$")

_PROJECTION_FLUSH_PATH = _THIS_DIR / "projection_flush_entry.py"

# "ask" (Issue #2566) is additive to the pre-existing "pass"/"block"
# vocabulary -- see the PreToolUse branch in `main()` below for how each
# value maps onto the actual Claude Code PreToolUse hook JSON output
# contract (`hookSpecificOutput.permissionDecision`).
_VALID_DECISIONS = ("pass", "block", "ask")

# Issue #2566: tool_name values this adapter's PreToolUse dispatch actually
# classifies. Every other tool_name reaching PreToolUse (only possible if a
# future settings.json matcher broadens beyond "SendMessage"/"Bash") is
# left fully unclassified -- observability-only, never blocked/asked here.
_PRE_TOOL_USE_MESSAGING_TOOL_NAMES = ("SendMessage", "notify_when_idle")


def _read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _current_repo(cwd: str | None, *, timeout: float = _GIT_REMOTE_TIMEOUT_SECONDS) -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = _GITHUB_REMOTE_RE.search(result.stdout.strip())
    return match.group(1) if match else None


def _git_worktree_and_branch(cwd: str | None) -> tuple[str | None, str | None]:
    """fix_delta 7: best-effort ``worktree``/``branch`` probe for the
    ``CwdChanged`` RuntimeLocation observation. Purely display-only (statusLine
    rendering) -- never used for Task/Binding identity, rebind or block
    decisions (AC7). Never raises; returns ``(None, None)`` on any failure
    (not a git repo, ``git`` not on PATH, timeout, ...)."""
    if not cwd:
        return None, None
    try:
        toplevel = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_REMOTE_TIMEOUT_SECONDS,
        )
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_REMOTE_TIMEOUT_SECONDS,
        )
    except Exception:
        return None, None
    worktree = toplevel.stdout.strip() if toplevel.returncode == 0 else None
    branch_name = branch.stdout.strip() if branch.returncode == 0 else None
    return (worktree or None), (branch_name or None)


def _launch_detached_projection_flush(hook_input: dict) -> None:
    """fix_delta 2: launch the Herdr projection flush as a *detached*
    subprocess, only ever called after ``ctl_client.call_hook`` has already
    returned (i.e. only after the DB mutation transaction it triggered has
    already committed) -- never as a Claude Code-native ``"async": true``
    sibling hook entry, which would race the very commit it is supposed to
    consume. The child is started detached (``start_new_session=True``) and
    never waited on, so it can never extend this hook's own hot-path
    budget. Any failure to even launch it is swallowed -- a missed
    opportunistic flush is retried by the next lifecycle event that bumps
    the same projection key (never a hard failure of this hook)."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed interpreter + fixed script path, no shell
            [sys.executable, str(_PROJECTION_FLUSH_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.write(json.dumps(hook_input).encode("utf-8"))
            proc.stdin.close()
    except Exception:
        pass


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
    # fix_delta 6: only pay for a `git remote get-url origin` subprocess call
    # when the raw prompt actually contains a pattern whose classification
    # would consult `current_repo` (bare `#N` / `/task #N` / `/task issue N`
    # shorthand) -- a full GitHub URL or explicit `owner/repo#N` target never
    # needs it, and most prompts contain neither.
    current_repo = (
        _current_repo(hook_input.get("cwd")) if classifier.needs_current_repo_resolution(prompt) else None
    )
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


def _command_args_to_raw_target(command_args: object) -> str:
    """AC6: ``UserPromptExpansion.command_args`` shape is not pinned by a
    confirmed first-party schema at implementation time -- normalize the two
    shapes actually observed in this repo's own runtime-smoke evidence
    (``scripts/agent-ops/run_worktree_agent_runtime_smoke.py``'s
    ``extract_claude_user_prompt_expansion_command_names``): a single raw
    string, or a list of string tokens. Anything else (``None``,
    non-string/non-list) degrades to an empty raw target -- never raises."""
    if isinstance(command_args, str):
        return command_args.strip()
    if isinstance(command_args, list):
        return " ".join(str(token) for token in command_args).strip()
    return ""


def _apply_user_prompt_expansion_fields(payload: dict, hook_input: dict, budget_seconds: float) -> float:
    """AC6: reached only for Claude Code's own user-typed slash/Skill command
    expansion lifecycle -- never for ordinary natural-language prompts.
    ``command_name != "task"`` is not this hook's concern (some other
    Skill/command being expanded) and is passed through untouched; only
    ``command_name == "task"`` reduces ``command_args`` to the same small
    structured target fields ``classifier.parse_slash_task_target`` already
    produces for the (now-retired as a state-changing path) raw-text
    `/task` special-case -- the target-parsing logic itself is reused, not
    reimplemented; only its authority moves (Issue #2625 In Scope #3).

    Issue #2625 fix_delta (OWNER PR review P2): mirrors the already-correct
    ``UserPromptSubmit`` pattern (fix_delta 6, ``classifier.
    needs_current_repo_resolution``) -- only pay for a ``git remote
    get-url origin`` subprocess call when ``raw_target`` actually needs
    ``current_repo`` resolution (bare ``#N`` / kind-word shorthand); a full
    URL, explicit ``owner/repo#N`` target, or ad-hoc title never needs it.
    ``budget_seconds`` is this hook's remaining hot-path time budget (starts
    at ``HOT_PATH_TIMEOUT_SECONDS``); when a git lookup *is* needed it is
    given a timeout capped at ``min(_GIT_REMOTE_TIMEOUT_SECONDS,
    budget_seconds)`` and its measured wall-clock cost is deducted from the
    budget before returning it, so ``main()`` can pass the *remaining*
    budget on to the subsequent ``ctl_client.call_hook(...)`` call instead
    of always spending the full budget on both subprocess calls
    independently."""
    command_name = hook_input.get("command_name") or None
    payload["command_name"] = command_name
    if command_name != "task":
        return budget_seconds
    raw_target = _command_args_to_raw_target(hook_input.get("command_args"))
    current_repo = None
    if classifier.raw_target_needs_current_repo_resolution(raw_target):
        git_timeout = min(_GIT_REMOTE_TIMEOUT_SECONDS, budget_seconds)
        started_at = time.monotonic()
        current_repo = _current_repo(hook_input.get("cwd"), timeout=git_timeout)
        elapsed = time.monotonic() - started_at
        budget_seconds = max(0.0, budget_seconds - elapsed)
    target, ad_hoc_title = classifier.parse_slash_task_target(raw_target, current_repo=current_repo)
    if target is not None:
        payload["slash_task_target_repo"] = target.repo
        payload["slash_task_target_ref_kind"] = target.ref_kind
        payload["slash_task_target_ref_number"] = target.ref_number
    elif ad_hoc_title:
        payload["slash_task_ad_hoc_title"] = ad_hoc_title
    return budget_seconds


def _apply_cwd_changed_fields(payload: dict, hook_input: dict) -> None:
    """fix_delta 7: record the display-only ``cwd``/``worktree``/``branch``
    RuntimeLocation observation fields. Purely additive to the existing
    Pane-based ``herdr_locator`` -- this never changes Task/Activity/Binding
    identity (AC7); `on_cwd_changed` only writes them to
    ``runtime_locations`` for the statusLine renderer."""
    cwd = hook_input.get("cwd") or None
    worktree, branch = _git_worktree_and_branch(cwd)
    payload["cwd"] = cwd
    payload["worktree"] = worktree
    payload["branch"] = branch


def _apply_pre_tool_use_fields(payload: dict, hook_input: dict) -> None:
    """Issue #2566: reduce the raw Claude Code `tool_name`/`tool_input` down
    to the small structured fields the SendMessage/Herdr guard needs.
    Never forwards a peer message body (`tool_input.message`), terminal
    output, or the full raw Bash command line onward -- only a bounded `to`
    string / `notify_when_idle` flag, or (for a recognized guarded `herdr`
    subcommand only) its category/operation/target-locator/machine-scoped
    fields (AC7)."""
    tool_name = hook_input.get("tool_name")
    payload["tool_name"] = tool_name
    tool_input = hook_input.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}

    if tool_name in _PRE_TOOL_USE_MESSAGING_TOOL_NAMES:
        to = tool_input.get("to")
        payload["to"] = to if isinstance(to, str) else None
        if tool_input.get("notify_when_idle"):
            payload["notify_when_idle"] = True
        return

    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return
        # Argument-aware best-effort early exit (Issue #2566 Outcome): the
        # overwhelming majority of Bash calls are not `herdr` invocations at
        # all, so `main()` never even reaches `ctl_client.call_hook` (no
        # `task-contextctl` subprocess spawn) for them -- see the
        # `PreToolUse` branch in `main()` below.
        if not pre_tool_use_classifier.looks_like_herdr_command(command):
            return
        parsed = pre_tool_use_classifier.parse_herdr_command(command)
        if parsed is None:
            # Recognized as a `herdr` invocation but not one of the finite
            # guarded subcommands (Issue #2566 In Scope) -- out of guard
            # scope, no decision.
            return
        payload["herdr_category"] = parsed.category
        payload["herdr_operation"] = parsed.operation
        payload["herdr_target_locator"] = parsed.target_locator
        payload["herdr_machine_scoped"] = parsed.machine_scoped


def _pre_tool_use_guard_applicable(payload: dict) -> bool:
    """True iff `_apply_pre_tool_use_fields` found something this guard
    actually needs to classify -- i.e. it is safe/correct to skip calling
    `task-contextctl` entirely otherwise (hot-path cost control, Issue
    #2566 Outcome: "無関係な Bash 呼び出しでは guard プロセスを spawn
    しない")."""
    return "to" in payload or "herdr_category" in payload


def _pre_tool_use_hook_specific_output(
    decision: str, reason_code: str | None, target_kind_value: str | None
) -> dict | None:
    """Translate the Task Context guard's own `decision` into the actual
    Claude Code `PreToolUse` hook JSON output contract
    (`hookSpecificOutput.permissionDecision`: "allow" | "deny" | "ask").
    `decision == "pass"` returns ``None`` (emit nothing -- let Claude Code's
    normal permission resolution proceed untouched; this guard can only
    ever *tighten*, never itself force an "allow" that would override some
    other, unrelated hard deny -- e.g. worktree-agent-runtime-smoke's own
    `permissions.deny` for `SendMessage`/`ListAgents`, Issue #2566 AC6)."""
    if decision == "pass":
        return None
    permission_decision = "deny" if decision == "block" else "ask"
    reason = f"task-context: {reason_code or target_kind_value or 'guarded_target'}"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": permission_decision,
            "permissionDecisionReason": reason,
        }
    }


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
    elif event == "UserPromptExpansion":
        # AC6: bounded hot-path budget, same as UserPromptSubmit -- `/task`
        # is a lightweight single-Task-mutation command, not a large lookup.
        # fix_delta (OWNER PR review P2): the optional git-lookup subprocess
        # inside `_apply_user_prompt_expansion_fields` and the subsequent
        # `ctl_client.call_hook` CLI subprocess below now share this single
        # HOT_PATH_TIMEOUT_SECONDS budget instead of each having its own
        # independent timeout -- `timeout` here is reassigned to whatever
        # budget remains after the (possibly skipped) git lookup.
        timeout = _apply_user_prompt_expansion_fields(payload, hook_input, HOT_PATH_TIMEOUT_SECONDS)
    elif event == "CwdChanged":
        _apply_cwd_changed_fields(payload, hook_input)
    elif event == "PreToolUse":
        # Issue #2566: same bounded hot-path budget as UserPromptSubmit --
        # this guard must never meaningfully slow down an ordinary tool
        # call.
        timeout = HOT_PATH_TIMEOUT_SECONDS
        _apply_pre_tool_use_fields(payload, hook_input)
        if not _pre_tool_use_guard_applicable(payload):
            # Argument-aware early exit: not a SendMessage/notify_when_idle
            # call and not a recognized guarded `herdr` Bash subcommand --
            # skip `ctl_client.call_hook` (no `task-contextctl` subprocess
            # spawn) entirely and let the tool call proceed untouched.
            return 0
    elif event in ("SubagentStart", "SubagentStop"):
        # fix_delta 5: Claude Code's SubagentStart/SubagentStop hook input
        # carries an `agent_id` UUID identifying the SubAgent *instance* --
        # forward it so SubagentStop can end the exact run that started
        # (see task_context_hook_flows.on_subagent_stop), instead of
        # guessing "the first open subagent run" when concurrent SubAgents
        # overlap.
        payload["agent_id"] = hook_input.get("agent_id") or hook_input.get("subagent_id") or None

    result_envelope = ctl_client.call_hook(event, payload, timeout=timeout)

    data: dict = {}
    raw_decision = None
    if result_envelope and result_envelope.get("status") == "ok":
        data = result_envelope.get("data") or {}
        raw_decision = data.get("decision")

    # A well-formed result envelope has `status: ok` AND a recognized
    # `decision` value. Anything else (transport failure, CLI error,
    # malformed/invalid envelope) is treated identically for the fail-open
    # default below -- except the `/task` (UserPromptExpansion,
    # command_name == "task") carve-out below (AC6, carried over from PR
    # #2615 fix_delta 4, now scoped to UserPromptExpansion only).
    envelope_ok = raw_decision in _VALID_DECISIONS
    decision = raw_decision if envelope_ok else "pass"

    if data.get("projection_key"):
        # fix_delta 2: only ever launched *after* `ctl_client.call_hook`
        # above has already returned -- i.e. only after the DB mutation
        # transaction it triggered has already committed.
        _launch_detached_projection_flush(hook_input)

    if event == "UserPromptSubmit":
        # Issue #2625 AC3: adapter-level double fail-open invariant --
        # ordinary UserPromptSubmit NEVER exits 2 for a Task Context
        # decision, even if the service unexpectedly returns a well-formed
        # `decision: block` (future regression/version skew). `/task`
        # state-changing authority no longer lives on this event (AC6), so
        # there is no carve-out left here at all -- every branch below this
        # point is diagnostic-only. The service's *expected* normal-path
        # response for a mismatch is already `decision: pass` with
        # `reason_code: different_primary_target_active` (AC1/AC2), so the
        # advisory is surfaced whenever that reason_code is present --
        # `decision == "block"` is only the defensive regression case.
        reason_code = data.get("reason_code")
        if reason_code == "different_primary_target_active" or decision == "block":
            reason_code = reason_code or "different_primary_target_active"
            print(
                "[task-context] advisory: this prompt appears to target a different "
                f"ACTIVE Task/Activity ({reason_code}). Continuing -- Task Context no longer "
                "blocks ordinary prompts for this; use `/task <target>` to explicitly switch.",
                file=sys.stderr,
            )
        return 0

    if event == "UserPromptExpansion":
        # AC6: `/task <target>`'s sole explicit state-changing authority.
        # Every other command_name is this hook's silent no-op (exit 0) --
        # see `_apply_user_prompt_expansion_fields` / `on_user_prompt_
        # expansion`; `decision`/`envelope_ok` were only ever mutated by the
        # service for `command_name == "task"` in the first place.
        if payload.get("command_name") == "task":
            if not envelope_ok:
                # Issue #2625 fix_delta (OWNER PR review P1 supplement): the
                # service-side rebind write is atomic (one `write_transaction`
                # covering the Task/Activity switch + its event), but that
                # commit happens inside the `task-contextctl` child process,
                # independent of whether *this* adapter successfully reads
                # back its response. A transport failure / timeout / malformed
                # envelope here means the CLI call did not confirm success --
                # it does NOT mean the rebind definitely did not happen, so we
                # must not claim "NOT applied", and an unconditional retry
                # could create a duplicate ad-hoc Task if the first call
                # actually did commit.
                print(
                    "[task-context] /task failed: Task Context service result could not be "
                    "confirmed (transport/timeout error) -- whether the rebind was applied is "
                    "unknown. Check the current Task (e.g. statusLine) before retrying "
                    "`/task <target>`.",
                    file=sys.stderr,
                )
                return 2
            if decision == "block":
                reason_code = data.get("reason_code", "slash_task_missing_target")
                print(
                    f"[task-context] /task failed: {reason_code}. Provide an explicit target, "
                    "e.g. `/task owner/repo#123` or `/task <ad-hoc title>`.",
                    file=sys.stderr,
                )
                return 2
        return 0

    if event == "PreToolUse":
        # Issue #2566: `data.decision` here is only ever "pass"/"ask"
        # (`task_context_hook_flows.on_pre_tool_use` never returns "block"
        # for this Issue's guarded surfaces -- see the module docstring);
        # `envelope_ok` already fails open to "pass" on any adapter/service
        # transport failure, consistent with every other event.
        output = _pre_tool_use_hook_specific_output(decision, data.get("reason_code"), data.get("target_kind"))
        if output is not None:
            print(json.dumps(output))
        return 0

    if event == "SessionStart" and decision == "pass":
        _emit_session_start_context(payload.get("claude_session_id"), event)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

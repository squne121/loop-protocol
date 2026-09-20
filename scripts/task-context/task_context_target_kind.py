"""Task Context v1 — target-kind resolution + guard decision (Issue #2566).

Shared, pure decision core for the `PreToolUse` guard's two guarded surfaces
(native `SendMessage`/`notify_when_idle` peer messaging, and the finite
guarded Herdr CLI subcommand set). No DB access, no subprocess/Herdr I/O --
`task_context_hook_flows.py` performs every lookup this module needs
(open subagent ExecutionRuns, Binding-by-session, Binding-by-Herdr-locator,
current Task for a Binding) and passes in already-resolved scalars. AC7:
every field this module reads or returns is a small bounded scalar -- never
a raw peer message body, terminal output, or full Bash command line.

target_kind vocabulary (Issue #2566 Outcome / In Scope):

- ``in_session_subagent``: the destination is an open ``run_kind='subagent'``
  ExecutionRun this *same* session already started and tracks (via
  ``SubagentStart``'s own ``agent_id``) -- covers both an in-session
  SubAgent and an Agent Teams teammate represented the same way. Reuses the
  existing ExecutionRun bookkeeping; never a new peer registry.
- ``same_task_independent_session``: resolves to a Binding whose current
  Task equals the caller's current Task (including "both taskless").
- ``known_cross_task_independent_session``: resolves to a Binding whose
  current Task differs from the caller's.
- ``unknown_independent_session``: does not resolve to any Binding this
  Task Context instance currently tracks (or, for Herdr, the locator could
  not be locally resolved at all -- e.g. ``--machine``-scoped).
- ``unaddressed_broadcast``: ``to`` is empty/``"*"`` -- not a confirmed
  supported tool shape today (Issue #2566 In Scope); left to Claude's own
  native handling, out of AC scope.
"""

from __future__ import annotations

DECISION_PASS = "pass"
DECISION_ASK = "ask"

TARGET_KIND_IN_SESSION_SUBAGENT = "in_session_subagent"
TARGET_KIND_SAME_TASK_SESSION = "same_task_independent_session"
TARGET_KIND_CROSS_TASK_SESSION = "known_cross_task_independent_session"
TARGET_KIND_UNKNOWN_SESSION = "unknown_independent_session"
TARGET_KIND_UNADDRESSED_BROADCAST = "unaddressed_broadcast"

# target_kind values that must never be ASK/DENY purely on Task Context
# grounds (Issue #2566 In Scope: "in-session subagent / Agent Teams
# teammate ... 原則PASS", "same-Task independent session: ... decisionなし",
# broadcast is out of AC scope / left to native rejection).
_PASS_TARGET_KINDS = frozenset(
    {
        TARGET_KIND_IN_SESSION_SUBAGENT,
        TARGET_KIND_SAME_TASK_SESSION,
        TARGET_KIND_UNADDRESSED_BROADCAST,
    }
)


def decision_for_target_kind(target_kind: str) -> tuple[str, str]:
    """Map a resolved ``target_kind`` to ``(decision, reason_code)``."""
    if target_kind in _PASS_TARGET_KINDS:
        return DECISION_PASS, f"target_kind_{target_kind}"
    return DECISION_ASK, f"target_kind_{target_kind}"


def classify_send_message_target(
    *,
    to: str | None,
    is_in_session_subagent: bool,
    peer_session_found: bool,
    peer_task_id: str | None,
    caller_task_id: str | None,
) -> str:
    """Resolve the ``SendMessage``/``notify_when_idle`` destination's
    ``target_kind``."""
    if to is None or to.strip() in ("", "*"):
        return TARGET_KIND_UNADDRESSED_BROADCAST
    if is_in_session_subagent:
        return TARGET_KIND_IN_SESSION_SUBAGENT
    if not peer_session_found:
        return TARGET_KIND_UNKNOWN_SESSION
    if peer_task_id == caller_task_id:
        return TARGET_KIND_SAME_TASK_SESSION
    return TARGET_KIND_CROSS_TASK_SESSION


def classify_herdr_target(
    *,
    machine_scoped: bool,
    locator_resolved: bool,
    peer_task_id: str | None,
    caller_task_id: str | None,
) -> str:
    """Resolve a guarded Herdr content-read/control command's
    ``target_kind``. Discovery-category commands never reach this function
    -- callers short-circuit them to a plain pass/no-decision before any
    target resolution (Issue #2566: "metadata-only discovery: ALLOW/no-
    decision"). ``machine_scoped`` (a ``--machine`` flag was present) is
    always treated as locally-unresolvable, matching "``--machine``等で
    local Task解決不能な場合を含む: ASK"."""
    if machine_scoped or not locator_resolved:
        return TARGET_KIND_UNKNOWN_SESSION
    if peer_task_id == caller_task_id:
        return TARGET_KIND_SAME_TASK_SESSION
    return TARGET_KIND_CROSS_TASK_SESSION

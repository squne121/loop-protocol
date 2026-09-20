"""Task Context v1 — `PreToolUse` Bash/SendMessage structural field reduction
(Issue #2566).

Thin, DB-free, adapter-side helper mirroring `classifier.py`'s role for
`UserPromptSubmit`: reduces the raw Claude Code `PreToolUse` hook JSON down
to the *small* structured fields the Task Context guard actually needs, so
`hook_entry.py` never forwards a peer message body, terminal output, or the
full raw Bash command text onward to `task-contextctl` / the `events`
journal (AC7 -- see the Issue Outcome: "EventJournal には... bounded
metadata のみを記録し、message body・terminal output・full Bash command は
保存しない").

Herdr CLI contract (finite guarded subcommand set, Issue #2566 In Scope --
this module deliberately does NOT expand to a generic shell-command
sandbox; unrecognized/unlisted `herdr` subcommands, and every non-`herdr`
Bash command, fall through with no guard decision at all):

- metadata-only discovery (`HERDR_DISCOVERY_OPS`): ALLOW/no-decision.
- content-read (`HERDR_CONTENT_READ_OPS`): known cross-Task -> ASK.
- control (`HERDR_CONTROL_OPS`): known cross-Task -> ASK.

`looks_like_herdr_command` is the cheap best-effort early-exit check
`hook_entry.py` uses to avoid spawning the heavier `task-contextctl`
subprocess for the overwhelming majority of ordinary (non-`herdr`) Bash
calls -- this is the "argument-aware `if` filter" the Issue describes,
implemented at the adapter code level rather than as a native Claude Code
`settings.json` schema field (no such per-hook-entry argument predicate is
part of the documented Hooks Configuration schema). It is explicitly
best-effort, not a security boundary (Issue #2566 Outcome: "このBash
argument filter は best-effort であり、hard security enforcement ではない
-- Task Context は managed operator 間の mistake-prevention layer であり、
security sandbox を提供しないため、この限定で目的に対して十分である").
"""

from __future__ import annotations

import shlex
from typing import NamedTuple

CATEGORY_DISCOVERY = "discovery"
CATEGORY_CONTENT_READ = "content_read"
CATEGORY_CONTROL = "control"

# Fixed finite subcommand-prefix families (Issue #2566 In Scope). Each key is
# the exact argv token sequence (after the leading `herdr` token) that
# identifies the family; the *next* positional token (if any, and not itself
# a flag) is the target locator (agent name / pane id / terminal session
# name).
HERDR_DISCOVERY_OPS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("agent", "list"),
        ("agent", "get"),
        ("pane", "list"),
        ("pane", "get"),
    }
)

HERDR_CONTENT_READ_OPS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("agent", "read"),
        ("pane", "read"),
        ("pane", "wait-output"),
        ("terminal", "session", "observe"),
    }
)

HERDR_CONTROL_OPS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("agent", "prompt"),
        ("agent", "send-keys"),
        ("pane", "run"),
        ("pane", "send-text"),
        ("pane", "send-keys"),
        ("terminal", "session", "control"),
        ("terminal", "session", "attach"),
        ("terminal", "session", "takeover"),
    }
)

_OPS_BY_CATEGORY: tuple[tuple[str, frozenset[tuple[str, ...]]], ...] = (
    (CATEGORY_DISCOVERY, HERDR_DISCOVERY_OPS),
    (CATEGORY_CONTENT_READ, HERDR_CONTENT_READ_OPS),
    (CATEGORY_CONTROL, HERDR_CONTROL_OPS),
)

_ALL_GUARDED_OPS = HERDR_DISCOVERY_OPS | HERDR_CONTENT_READ_OPS | HERDR_CONTROL_OPS
_MAX_PREFIX_LEN = max(len(op) for op in _ALL_GUARDED_OPS)


class HerdrCommand(NamedTuple):
    category: str
    operation: str
    target_locator: str | None
    machine_scoped: bool


def looks_like_herdr_command(raw_command: str) -> bool:
    """Cheap prefix check: does this Bash command line look like an
    invocation of the `herdr` CLI at all? Used purely to decide whether it is
    worth calling `parse_herdr_command` / spawning `task-contextctl` --
    never the actual classification (that's `parse_herdr_command`)."""
    if not isinstance(raw_command, str):
        return False
    stripped = raw_command.strip()
    return stripped == "herdr" or stripped.startswith("herdr ")


def parse_herdr_command(raw_command: str) -> HerdrCommand | None:
    """Reduce a `herdr ...` Bash command line to the small structured fields
    the guard needs. Returns ``None`` when the command is not one of the
    finite guarded subcommands (Issue #2566) -- e.g. `herdr workspace
    create`, `herdr session stop`, or an unparseable command line. Callers
    treat ``None`` as "out of guard scope", never as a guess.

    The full ``raw_command`` string is never returned or persisted beyond
    this function's own local parsing -- only ``target_locator`` (a single
    bounded identifier token) ever leaves this module (AC7)."""
    try:
        tokens = shlex.split(raw_command)
    except ValueError:
        return None
    if not tokens or tokens[0] != "herdr":
        return None
    args = tokens[1:]
    if not args:
        return None
    machine_scoped = "--machine" in args

    for prefix_len in range(min(_MAX_PREFIX_LEN, len(args)), 0, -1):
        prefix = tuple(args[:prefix_len])
        category = None
        for candidate_category, ops in _OPS_BY_CATEGORY:
            if prefix in ops:
                category = candidate_category
                break
        if category is None:
            continue
        target_locator = None
        if prefix_len < len(args) and not args[prefix_len].startswith("-"):
            target_locator = args[prefix_len]
        return HerdrCommand(
            category=category,
            operation="_".join(prefix),
            target_locator=target_locator,
            machine_scoped=machine_scoped,
        )
    return None

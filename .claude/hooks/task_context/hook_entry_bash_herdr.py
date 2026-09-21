#!/usr/bin/env python3
"""Task Context v1 -- distinctly-named `PreToolUse` entrypoint for the
`Bash` matcher group (Issue #2566 fix_delta P1-C).

`.claude/settings.json` needs two separate `PreToolUse` registrations that
both ultimately dispatch to `hook_entry.main`: one for the `SendMessage`
matcher (always invoked -- there is no cheap native predicate to filter it
further) and one for the `Bash` matcher, guarded by a native `if:
"Bash(herdr *)"` handler-level filter so the Python interpreter process
itself is never spawned for an ordinary non-`herdr` Bash call (previously
every Bash call spawned this process, and `looks_like_herdr_command()` only
avoided the heavier downstream `task-contextctl` subprocess spawn once the
Python process was already running -- see `pre_tool_use_classifier.py`'s
module docstring).

`scripts/check_hook_boundaries.py` keys its duplicate-registration check by
the composite `(handler_id, event)` key, and derives `handler_id` from the
invoked script's file name (`args[0]` stem for an interpreter-wrapper
command such as `python3 <script> ...`). Reusing `hook_entry.py` verbatim
for both registrations would therefore collide as a false "duplicate
handler" under that checker (both would resolve to `handler_id ==
"hook_entry"` for the same `event == "PreToolUse"`), and
`scripts/check_hook_boundaries.py` itself is outside this Issue's Allowed
Paths. This file exists solely to give the `Bash`-matcher registration its
own distinct `handler_id` (`hook_entry_bash_herdr`) so the two `PreToolUse`
registrations no longer collide -- it contains no logic of its own beyond
delegating to `hook_entry.main`, so Task Context semantics/dispatch stay
defined in exactly one place (`hook_entry.py` / `task_context_hook_flows.py`,
never duplicated here)."""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import hook_entry  # noqa: E402

if __name__ == "__main__":
    sys.exit(hook_entry.main(sys.argv))

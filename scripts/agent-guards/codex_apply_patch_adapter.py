#!/usr/bin/env python3
"""codex_apply_patch_adapter.py — Codex CLI PreToolUse adapter for `apply_patch` /
`Edit` / `Write` worktree containment (Issue #1657 AC5/AC6).

Contract: this adapter is wired into `.codex/hooks.json`'s
`PreToolUse` -> `matcher: "^(apply_patch|Edit|Write)$"` section, mirroring the
containment guarantee `worktree_scope_guard.py` already provides for the
`^Bash$` matcher (and for Claude Code's own Write/Edit/MultiEdit tools).

Design:
- `Edit` / `Write` tool calls are delegated verbatim to
  `scripts/agent-guards/worktree_scope_guard.decide()` — the WRITE_TOOLS
  decision path (target-path containment via `is_inside`, `resolve_expected_worktree`,
  `resolve_project_root`, `resolve_current_issue`, LOCAL_MAIN_SCRATCH_ALLOW_V1) is
  reused as-is. This adapter does NOT re-implement that logic.
- `apply_patch` tool calls carry a patch body (not a `file_path`) in
  `tool_input.command`. This adapter parses the Codex apply_patch envelope
  headers (`*** Add File:`, `*** Update File:`, `*** Delete File:`,
  `*** Move to:`) to extract target paths (a Move operation yields BOTH the
  source `Update File` path and the destination `Move to` path), normalizes
  them, and then bridges into the SAME shared-core containment primitives
  (`is_inside`, `resolve_expected_worktree`, `resolve_project_root`,
  `resolve_current_issue`) used by `_decide_write` — again without
  duplicating the containment algorithm itself.

Fail-closed rules for apply_patch (Issue #1657 AC5):
- A missing / non-string / empty `tool_input.command` is blocked.
- Any extracted target path containing a NUL byte is blocked.
- Any extracted target path that is absolute is blocked (Codex apply_patch
  paths are always repo-relative; an absolute path indicates either a
  malformed patch or an attempt to escape the patch-path convention).
- A patch body from which NO target path can be extracted at all is blocked
  (unparseable-but-mutating fail-closed policy, mirroring
  `worktree_scope_guard.py`'s Bash classifier).
- When an active issue worktree resolves, each extracted target path must be
  `is_inside(...)` the expected worktree (source AND destination for Move).

Exit codes:
  0 — allow (no stdout/stderr)
  2 — block (bounded stderr only)
"""

from __future__ import annotations

import json
import os
import re
import sys

_AGENT_GUARDS_DIR = os.path.dirname(os.path.realpath(__file__))
if _AGENT_GUARDS_DIR not in sys.path:
    sys.path.insert(0, _AGENT_GUARDS_DIR)

import worktree_scope_guard as _wsg  # noqa: E402

# Codex apply_patch envelope headers. A `Move to:` header follows an
# `Update File:` header when the patch renames a file; both the old
# (`Update File`) and new (`Move to`) paths must be containment-checked.
_PATCH_HEADER_RE = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$",
    re.MULTILINE,
)


class ApplyPatchParseError(Exception):
    """Raised when the apply_patch body cannot be safely parsed (fail-closed)."""


def extract_target_paths(patch_body: str) -> list[str]:
    """Extract target paths from a Codex apply_patch envelope body.

    Returns the literal path string following each `*** Add File:` /
    `*** Update File:` / `*** Delete File:` / `*** Move to:` header (both
    sides of a Move are returned as separate entries). Raises
    ApplyPatchParseError when the body is empty, contains a NUL byte, or
    contains no extractable target path at all (fail-closed — an apply_patch
    invocation always mutates, so an unparseable body cannot be authorized).
    """
    if not patch_body:
        raise ApplyPatchParseError("empty apply_patch command/body")
    if "\x00" in patch_body:
        raise ApplyPatchParseError("NUL byte in apply_patch body")

    targets = [m.group(1).strip() for m in _PATCH_HEADER_RE.finditer(patch_body)]
    targets = [t for t in targets if t]

    if not targets:
        raise ApplyPatchParseError("no Add/Update/Delete/Move target path could be parsed from apply_patch body")

    return targets


def _adapter_block(reason: str) -> None:
    sys.stderr.write(f"[codex_apply_patch_adapter] blocked: {reason}\n")
    sys.exit(2)


def _decide_apply_patch(payload: dict) -> None:
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.environ.get("PWD") or os.getcwd()
    command = tool_input.get("command")

    if not isinstance(command, str):
        _adapter_block("apply_patch tool_input.command is missing or not a string")

    try:
        targets = extract_target_paths(command)
    except ApplyPatchParseError as exc:
        _adapter_block(str(exc))
        return  # unreachable, _adapter_block exits

    # Unconditional fail-closed checks (NUL / absolute) — independent of
    # whether an active issue worktree can be resolved.
    for target in targets:
        if "\x00" in target:
            _adapter_block(f"NUL byte in apply_patch target path: {target!r}")
        if os.path.isabs(target):
            _adapter_block(f"absolute path not permitted in apply_patch target: {target}")

    project_root = _wsg.resolve_project_root()
    issue = _wsg.resolve_current_issue(cwd, project_root)

    if not issue:
        # No active issue resolvable -> apply_patch is not scoped to a worktree.
        _wsg._allow()
        return

    resolution = _wsg.resolve_expected_worktree(issue, project_root)

    if not resolution.git_available:
        _wsg._block("<git-unavailable>", cwd)
    if resolution.match_count == 0:
        _wsg._block("<no-matching-worktree>", cwd)
    if resolution.expected is None:
        _wsg._block("<ambiguous>", cwd)

    for target in targets:
        if not _wsg.is_inside(resolution.expected, target, cwd):
            _wsg._block(_wsg._rel(resolution.expected, project_root=project_root), cwd)

    _wsg._allow()


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        _adapter_block("malformed PreToolUse payload (JSON parse failure)")
        return

    tool_name = payload.get("tool_name")

    if tool_name == "apply_patch":
        _decide_apply_patch(payload)
        return

    # Edit / Write / anything else: delegate to the shared-core decision
    # function verbatim. worktree_scope_guard.decide() already allows any
    # tool_name outside MATCHED_TOOLS, so no further dispatch is needed here.
    _wsg.decide(payload)


if __name__ == "__main__":
    main()

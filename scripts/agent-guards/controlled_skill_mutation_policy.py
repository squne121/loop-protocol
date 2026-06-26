#!/usr/bin/env python3
"""
controlled_skill_mutation_policy.py

Shared policy definition for CONTROLLED_SKILL_MUTATION_COMMAND_POLICY.
Consumed by both worktree_scope_guard.py and local_main_branch_guard.py
so the two guards never split-brain on which executor commands are allowed.

This module is SEPARATE from the preflight.run policy (run_refinement_preflight.py).
Command ID: termination_report.publish

Issue #1166.
"""

from __future__ import annotations

import os
import re
import shlex

# ── Canonical constants ───────────────────────────────────────────────────────

TRUSTED_REPO = "squne121/loop-protocol"
EXECUTOR_SCRIPT = "scripts/agent-guards/controlled_skill_mutation_exec.py"
COMMAND_ID_PUBLISH = "termination_report.publish"

# Allowed write roots for termination_report.publish (relative to project root)
ALLOWED_WRITE_ROOTS = ["artifacts/"]

# Environment variables that the executor sanitizes (removes from child env)
ENV_SANITIZE_KEYS = [
    "PUBLISH_ARTIFACT_DIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "GH_EDITOR",
    "EDITOR",
    "VISUAL",
    "BROWSER",
]

# ── CONTROLLED_SKILL_MUTATION_COMMAND_POLICY ──────────────────────────────────
#
# Canonical registry for controlled remote mutation transactions.
# Each entry is keyed by command_id and specifies:
#   executor_script:      repo-relative path to the single executor
#   allowed_write_roots:  directories (relative) the executor may write to
#   github_mutation:      GitHub mutation parameters
#   postcondition:        what must be true after the executor runs
#   idempotency:          how duplicate runs are detected
#   env_sanitize:         env vars overridden/removed before execution
#
CONTROLLED_SKILL_MUTATION_COMMAND_POLICY: dict = {
    COMMAND_ID_PUBLISH: {
        "command_id": COMMAND_ID_PUBLISH,
        "description": "Publish termination report as GitHub issue comment (controlled remote mutation)",
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "github_mutation": {
            "comment_on_issue": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        },
        "idempotency": {
            "marker_file_pattern": "artifacts/{issue_number}/termination_report_published.marker.json",
            "marker_field": "comment_id",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
}

# ── Executor argv spec ────────────────────────────────────────────────────────
# Exact argv shape accepted by the executor.
# --flag=value forms are rejected (must use separate tokens).
# Duplicate flags are rejected.
# Unknown flags are rejected.

_EXECUTOR_VALUE_FLAGS: frozenset[str] = frozenset({
    "--command-id",
    "--issue-number",
    "--input-file",
    "--repo",
})
_EXECUTOR_BOOL_FLAGS: frozenset[str] = frozenset({
    "--json",
    "--dry-run",
})
_EXECUTOR_REQUIRED_FLAGS: frozenset[str] = frozenset({
    "--command-id",
    "--issue-number",
    "--input-file",
    "--repo",
})

# Shell metacharacters that make a command unparseable / compound
_SHELL_METACHAR_RE = re.compile(r"[;&|<>$`\n\r\0\\(){}*?\[\]!~]")


def _validate_executor_argv(args: list[str]) -> bool:
    """True iff args is an exact, non-redundant argv for the executor.

    Rejects:
    - unknown flags
    - duplicate flags
    - --flag=value forms
    - positional arguments (not preceded by a known flag)
    - value-flags with no following value
    - value-flags whose value looks like another flag

    Does NOT validate semantic constraints (repo slug, file existence, etc.) —
    those are enforced by the executor itself.
    """
    seen: set[str] = set()
    i = 0
    while i < len(args):
        tok = args[i]
        # Reject positional arguments (bare tokens not starting with --)
        if not tok.startswith("--"):
            return False
        # Reject --flag=value forms (must use separate tokens)
        if "=" in tok:
            return False
        # Reject duplicates
        if tok in seen:
            return False
        seen.add(tok)

        if tok in _EXECUTOR_BOOL_FLAGS:
            i += 1
            continue
        if tok in _EXECUTOR_VALUE_FLAGS:
            if i + 1 >= len(args):
                return False  # value-flag with no following value
            val = args[i + 1]
            if val.startswith("--"):
                return False  # value looks like another flag
            i += 2
            continue
        # Unknown flag
        return False

    # All required flags must be present
    return _EXECUTOR_REQUIRED_FLAGS.issubset(seen)


def is_controlled_skill_mutation_exec_command(cmd: str, project_root: str) -> bool:
    """Return True iff cmd is an exact controlled_skill_mutation_exec.py invocation.

    Accepted forms:
      uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py [FLAGS]
      python3 scripts/agent-guards/controlled_skill_mutation_exec.py [FLAGS]

    Rejected:
    - shell metacharacters (compound commands, injection)
    - --flag=value forms
    - unknown/duplicate flags
    - missing required flags
    - script path that does not resolve to the canonical executor
    - python -c / /tmp/ scripts
    - bash -c wrappers

    This function is the single shared authority consumed by both
    worktree_scope_guard and local_main_branch_guard (Issue #1166 AC4/AC17).
    """
    if not cmd or not cmd.strip():
        return False

    # Reject any shell metacharacter or separator (prevents injection riding
    # along on a legitimate prefix match)
    if _SHELL_METACHAR_RE.search(cmd):
        return False

    try:
        toks = shlex.split(cmd.strip())
    except ValueError:
        return False

    if not toks:
        return False

    # Determine script token index
    if toks[:3] == ["uv", "run", "python3"]:
        rest = toks[3:]
    elif len(toks) >= 1 and os.path.basename(toks[0]) in ("python3", "python"):
        rest = toks[1:]
    else:
        return False

    if not rest:
        return False
    if rest[0].startswith("-"):
        return False  # reject python -c and bare flags

    script_token = rest[0]
    # Reject /tmp/ scripts and python -c forms
    if script_token.startswith("/tmp/") or script_token == "-c":
        return False
    # Reject inline absolute paths that aren't the executor
    if os.path.isabs(script_token):
        script_real = os.path.realpath(script_token)
    else:
        script_real = os.path.realpath(os.path.join(project_root, script_token))

    # Resolve canonical executor path from project_root
    canonical_executor = os.path.realpath(os.path.join(project_root, EXECUTOR_SCRIPT))
    if script_real != canonical_executor:
        return False

    # Validate argv shape
    return _validate_executor_argv(rest[1:])

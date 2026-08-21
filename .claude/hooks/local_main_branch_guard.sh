#!/usr/bin/env bash
# local_main_branch_guard.sh — Claude Code PreToolUse-shaped wrapper
#
# Delegates to scripts/agent-guards/local_main_branch_guard.py.
# Reads PreToolUse JSON from stdin, exits 0 (allow) or 2 (block).
#
# Wrapper existence: this script remains in the repo and is invocable in a
# PreToolUse-shaped calling convention (stdin JSON, exit 0/2).
# Project wiring: repo-controlled .claude/settings.json project PreToolUse
# does NOT currently register this script (verify via
# scripts/check_hook_boundaries.py — see #2193/#2256).
# Workflow authority: the authority for branch-safety policy is the
# Agent/Skill-side behavioral contract (docs/dev/hook-boundaries.md), not a
# claim that this shell wrapper is actively executed by project PreToolUse.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

GUARD_SCRIPT="${REPO_ROOT}/scripts/agent-guards/local_main_branch_guard.py"

if [ ! -f "${GUARD_SCRIPT}" ]; then
    # Fail-closed: guard script missing
    printf '[local_main_branch_guard] ERROR: guard script not found: %s\n' "${GUARD_SCRIPT}" >&2
    exit 2
fi

export LOCAL_MAIN_BRANCH_GUARD_FLAVOR="claude"

# Pass stdin (PreToolUse JSON) to the Python guard
exec python3 "${GUARD_SCRIPT}" <<< "$(cat)"

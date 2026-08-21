#!/usr/bin/env bash
# local_main_branch_guard.sh — Codex CLI PreToolUse / PermissionRequest-shaped wrapper
#
# Delegates to scripts/agent-guards/local_main_branch_guard.py.
# Reads PreToolUse / PermissionRequest JSON from stdin.
# Exits 0 (allow) or 2 (block).
#
# Wrapper existence: this script remains in the repo and is invocable in a
# PreToolUse/PermissionRequest-shaped calling convention (stdin JSON, exit 0/2).
# Project wiring: repo-controlled .codex/hooks.json currently declares only
# SessionEnd and SubagentStop as a passive advisory recorder (Issue #1830
# quarantine). It does NOT contain a PreToolUse or PermissionRequest entry
# invoking this script (see docs/dev/hook-boundaries.md — #2193/#2256).
# Workflow authority: the authority for branch-safety policy is the
# Agent/Skill-side behavioral contract, not a claim that this shell wrapper
# is actively executed by .codex/hooks.json.
#
# Note: Codex PreToolUse is NOT a complete shell interception boundary.
# startup preflight via scripts/check_local_main_branch_state.py is mandatory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

GUARD_SCRIPT="${REPO_ROOT}/scripts/agent-guards/local_main_branch_guard.py"

if [ ! -f "${GUARD_SCRIPT}" ]; then
    # Fail-closed: guard script missing
    printf '[local_main_branch_guard] ERROR: guard script not found: %s\n' "${GUARD_SCRIPT}" >&2
    exit 2
fi

export LOCAL_MAIN_BRANCH_GUARD_FLAVOR="codex"

# Pass stdin (PreToolUse / PermissionRequest JSON) to the Python guard
exec python3 "${GUARD_SCRIPT}" <<< "$(cat)"

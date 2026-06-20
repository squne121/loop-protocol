#!/usr/bin/env bash
# local_main_branch_guard.sh — Claude Code PreToolUse wrapper
#
# Delegates to scripts/agent-guards/local_main_branch_guard.py.
# Reads PreToolUse JSON from stdin, exits 0 (allow) or 2 (block).
#
# Invoked by .claude/settings.json PreToolUse hook.
# Order: secret_boundary_guard -> local_main_branch_guard -> worktree_scope_guard

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

#!/usr/bin/env bash
# worktree_scope_guard.sh — thin wrapper that delegates to worktree_scope_guard.py.
#
# The decision logic (WORKTREE_SCOPE_RESOLUTION_V1 / MUTATING_BASH_CLASSIFIER_V1,
# realpath/commonpath path containment, porcelain -z parsing) lives in the Python
# module. This wrapper only locates the interpreter and execs the module.
#
# Exit codes are passed through from the Python module:
#   0  — allow (no output)
#   2  — block (bounded stderr)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
    exec python3 "${SCRIPT_DIR}/worktree_scope_guard.py"
fi

# python3 unavailable: fail closed for matched mutation tools.
echo "[worktree_scope_guard] blocked: python3 not found — fail closed" >&2
exit 2

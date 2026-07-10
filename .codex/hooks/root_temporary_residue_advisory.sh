#!/usr/bin/env bash
# root_temporary_residue_advisory.sh (Codex CLI variant)
# PreToolUse hook — repo root temporary alias advisory (non-blocking / fail-open)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
POLICY="${REPO_ROOT}/scripts/agent-guards/root_temporary_residue_policy.py"

if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi

if ! python3 "$POLICY" --repo-root "$REPO_ROOT" --emit-hook-envelope; then
  exit 0
fi

exit 0

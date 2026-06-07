#!/usr/bin/env bash
# check-visual-artifact-pipeline.sh — entry point for the e2e visual regression
# evidence pipeline structural check (docs/dev/visual-baseline-registry.md §5).
#
# Delegates to check-visual-artifact-pipeline.py, which structurally parses the
# workflow YAML (not grep). Usage:
#   scripts/check-visual-artifact-pipeline.sh [path/to/ci.yml]
# Exit code: 0 = pass, 1 = contract violation, 2 = usage / parse error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW="${1:-.github/workflows/ci.yml}"

if command -v uv >/dev/null 2>&1; then
  exec uv run python3 "${SCRIPT_DIR}/check-visual-artifact-pipeline.py" "${WORKFLOW}"
else
  exec python3 "${SCRIPT_DIR}/check-visual-artifact-pipeline.py" "${WORKFLOW}"
fi

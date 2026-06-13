#!/usr/bin/env bash
# Thin wrapper — delegates all logic to get_ci_failed_log.py
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run python3 "$SCRIPT_DIR/get_ci_failed_log.py" "$@"

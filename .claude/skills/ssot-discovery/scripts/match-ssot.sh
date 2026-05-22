#!/usr/bin/env bash
# match-ssot.sh — SSOT discovery wrapper
# Usage: match-ssot.sh --keywords "kw1,kw2" --paths "src/foo.ts,src/bar.ts"
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/match_ssot.py" "$@"

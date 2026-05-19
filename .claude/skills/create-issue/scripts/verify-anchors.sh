#!/usr/bin/env bash
# verify-anchors.sh
#
# Verify that each anchor listed in ANCHOR_LIST_FILE exists in the repository.
#
# Usage:
#   verify-anchors.sh <anchor_list_file>
#
# ANCHOR_LIST_FILE: path to a file containing one anchor per line.
#   Each anchor is a string (file path, section heading, function name, etc.)
#   that must appear somewhere in the repo via git grep.
#
# Input validation:
#   - anchor_list_file path: ^[A-Za-z0-9._/-]+$
#   - each anchor line: ^[A-Za-z0-9._/: #-]+$  (safe anchor characters)
#
# Exit codes:
#   0 — all anchors PASS
#   1 — at least one anchor FAIL or usage error

set -euo pipefail

# ---- Input validation -------------------------------------------------------

if [ $# -ne 1 ]; then
  echo "Usage: verify-anchors.sh <anchor_list_file>" >&2
  exit 1
fi

ANCHOR_LIST_FILE="$1"

# Validate path characters
if ! echo "$ANCHOR_LIST_FILE" | grep -qE '^[A-Za-z0-9._/\-]+$'; then
  echo "[ERROR] anchor_list_file path contains invalid characters: '$ANCHOR_LIST_FILE'" >&2
  exit 1
fi

if [ ! -f "$ANCHOR_LIST_FILE" ]; then
  echo "[ERROR] anchor_list_file not found: '$ANCHOR_LIST_FILE'" >&2
  exit 1
fi

# ---- Per-anchor pattern for allowlist validation ----------------------------
# Anchors must only contain safe printable characters.
# Disallow characters that could be used for injection: ; & | ` $ ( ) { } < > \ " ' ! ~ *
ANCHOR_SAFE_RE='^[A-Za-z0-9._/:# -]+$'

# ---- Verify each anchor -----------------------------------------------------

all_pass=true

while IFS= read -r anchor || [ -n "$anchor" ]; do
  # Skip blank lines and comment lines
  [[ -z "$anchor" ]] && continue
  [[ "$anchor" == \#* ]] && continue

  # Validate anchor characters
  if ! echo "$anchor" | grep -qE "$ANCHOR_SAFE_RE"; then
    echo "FAIL [invalid-chars] $anchor" >&2
    all_pass=false
    continue
  fi

  # Use git grep with array-form (no eval, no shell expansion of anchor)
  # -l: list files only, -F: fixed string (no regex from anchor), -r: recursive
  if git grep -lF -- "$anchor" > /dev/null 2>&1; then
    echo "PASS $anchor"
  else
    echo "FAIL $anchor"
    all_pass=false
  fi

done < "$ANCHOR_LIST_FILE"

# ---- Final result -----------------------------------------------------------

if [ "$all_pass" = true ]; then
  echo "--- All anchors PASS ---"
  exit 0
else
  echo "--- Some anchors FAIL ---" >&2
  exit 1
fi

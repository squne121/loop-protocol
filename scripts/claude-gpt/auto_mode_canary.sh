#!/bin/sh
# scripts/claude-gpt/auto_mode_canary.sh
#
# `auto_mode_canary.py`（Issue #2203 の standalone runtime canary executable）を
# repository の canonical uv-managed python3 経由で起動する薄い POSIX wrapper。
# 直接 `python3 auto_mode_canary.py` を叩かず、常にこの wrapper（または
# `uv run --locked python3 auto_mode_canary.py` の直接呼び出し）を使うこと。
#
# Usage:
#   scripts/claude-gpt/auto_mode_canary.sh --mode {agy|github|negative|all} \
#     [--agy-receipt-path <path>] [--no-evidence]
#
# Exit code（auto_mode_canary.py と同一契約）:
#   0   PASS
#   1   FAIL
#   2   invalid invocation
#   77  SKIP

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)

cd "$REPO_ROOT" || exit 2
exec uv run --locked python3 "$SCRIPT_DIR/auto_mode_canary.py" "$@"

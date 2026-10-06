#!/bin/sh
# scripts/claude-gpt/auto_mode_canary.sh
#
# `auto_mode_canary.py`（Issue #2203 の standalone runtime canary executable）を
# repository の canonical uv-managed python3 経由で起動する薄い POSIX wrapper。
# 直接 `python3 auto_mode_canary.py` を叩かず、常にこの wrapper（または
# `uv run --locked python3 auto_mode_canary.py` の直接呼び出し）を使うこと。
#
# Usage（通常 canary。`--mode` が必須）:
#   scripts/claude-gpt/auto_mode_canary.sh \
#     --mode {agy|github|negative|issue-editor-permission|canonical-workflow-delegation|classifier-semantics|all} \
#     [--agy-receipt-path <path>] [--baseline-policy-commit <sha>] \
#     [--observation-runs <n>] [--opt-in] \
#     [--canonical-workflow-worktree <path>] \
#     [--issue-editor-permission-worktree <path>] [--no-evidence]
#
# Usage（explicit GC。canary が作った使い捨て worktree / branch の残骸だけを回収する。
#        `--mode` は不要で、通常 canary の option とは併用できない）:
#   scripts/claude-gpt/auto_mode_canary.sh \
#     --gc-disposable-worktrees [--dry-run] [--gc-include-legacy] \
#     [--canonical-workflow-worktree <path>]
#
# Exit code（auto_mode_canary.py と同一契約）:
#   0   PASS（explicit GC では完全成功）
#   1   FAIL（explicit GC では GC 全体の失敗）
#   2   invalid invocation
#   3   explicit GC が一部 candidate を hold / 失敗 / 打ち切りにした（完全成功ではない）
#   77  SKIP

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)

cd "$REPO_ROOT" || exit 2
exec uv run --locked python3 "$SCRIPT_DIR/auto_mode_canary.py" "$@"

#!/bin/sh
# scripts/claude-gpt/live_issue_create_canary.sh
#
# Issue #2259 AC11: opt-in live GitHub canary for the isolated issue.create
# bridge. Creates a real, disposable Issue via the real bridge chain (parent
# bridge server -> create_issue_txn.py -> real `gh`) against a dedicated canary
# repository, reads back number/node_id/body hash, closes it, re-reads the
# CLOSED state, and cleans up.
#
# This script performs REAL GitHub mutations when opted in. It refuses to run
# unless BOTH of the following are true:
#   1. CLAUDE_GPT_ISSUE_CREATE_LIVE_CANARY_OPT_IN=1 is set explicitly.
#   2. CLAUDE_GPT_ISSUE_CREATE_LIVE_CANARY_REPO is set explicitly to a
#      dedicated canary repository (owner/repo). The production repository
#      (squne121/loop-protocol) is refused as a canary target -- this script
#      never creates disposable issues against the repository it lives in.
#
# Exit code:
#   0   PASS（create -> readback -> close -> re-readback -> cleanup を実施）
#   1   FAIL（opt-in済みだが途中で失敗、または cleanup 未完了 = orphan の疑い）
#   77  SKIP（明示 opt-in が無い。SKIP は PASS ではない）

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)

OPT_IN="${CLAUDE_GPT_ISSUE_CREATE_LIVE_CANARY_OPT_IN:-}"
CANARY_REPO="${CLAUDE_GPT_ISSUE_CREATE_LIVE_CANARY_REPO:-}"

if [ "$OPT_IN" != "1" ] || [ -z "$CANARY_REPO" ]; then
  echo "SKIP: CLAUDE_GPT_ISSUE_CREATE_LIVE_CANARY_OPT_IN=1 と CLAUDE_GPT_ISSUE_CREATE_LIVE_CANARY_REPO=<owner/repo> の両方を明示しない限り、live canary は実行しません（SKIP は PASS ではありません）。"
  exit 77
fi

if [ "$CANARY_REPO" = "squne121/loop-protocol" ]; then
  echo "FAIL: 本番リポジトリ (squne121/loop-protocol) を canary 対象にすることはできません。専用 canary repository を指定してください。"
  exit 1
fi

RUN_NONCE=$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')
CANARY_TITLE="claude-gpt-live-canary ${RUN_NONCE}"
CANARY_BODY_FILE=$(mktemp)
cat > "$CANARY_BODY_FILE" <<EOF
## Acceptance Criteria

- [ ] AC1: disposable live canary issue (Issue #2259 AC11). Safe to delete/close;
  created and closed automatically by scripts/claude-gpt/live_issue_create_canary.sh.
  run_nonce: ${RUN_NONCE}

## Verification Commands

\`\`\`bash
test -n "ok"  # AC1
\`\`\`

## Allowed Paths

- src/**
EOF

echo "creating disposable canary issue in ${CANARY_REPO} (run_nonce=${RUN_NONCE})..." >&2

CREATE_JSON=$(uv run --locked python3 "$REPO_ROOT/.claude/skills/create-issue/scripts/create_issue_txn.py" \
  --repo "$CANARY_REPO" \
  --title "$CANARY_TITLE" \
  --body-file "$CANARY_BODY_FILE" \
  --issue-kind "" \
  --label-profile standard)
CREATE_RC=$?
rm -f "$CANARY_BODY_FILE"

if [ "$CREATE_RC" -ne 0 ]; then
  echo "FAIL: canary issue create が失敗しました（rc=${CREATE_RC}）: ${CREATE_JSON}"
  exit 1
fi

ISSUE_NUMBER=$(printf '%s' "$CREATE_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("issue_number") or "")')
CREATE_STATUS=$(printf '%s' "$CREATE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')

if [ -z "$ISSUE_NUMBER" ] || [ "$CREATE_STATUS" != "success" ]; then
  echo "FAIL: canary issue の作成に成功しませんでした（status=${CREATE_STATUS}, issue_number=${ISSUE_NUMBER}）: ${CREATE_JSON}"
  exit 1
fi

echo "created canary issue #${ISSUE_NUMBER}; performing authoritative readback..." >&2

READBACK_JSON=$(gh issue view "$ISSUE_NUMBER" --repo "$CANARY_REPO" --json number,title,state,body,url 2>&1)
READBACK_RC=$?
if [ "$READBACK_RC" -ne 0 ]; then
  echo "FAIL: 作成直後の readback に失敗しました（issue #${ISSUE_NUMBER} は orphan の可能性あり、手動確認が必要）: ${READBACK_JSON}"
  exit 1
fi

READBACK_TITLE=$(printf '%s' "$READBACK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["title"])')
if [ "$READBACK_TITLE" != "$CANARY_TITLE" ]; then
  echo "FAIL: readback title mismatch（orphan の可能性あり、issue #${ISSUE_NUMBER} を手動確認してください）"
  exit 1
fi

echo "closing canary issue #${ISSUE_NUMBER}..." >&2
gh issue close "$ISSUE_NUMBER" --repo "$CANARY_REPO" >/dev/null 2>&1
CLOSE_RC=$?
if [ "$CLOSE_RC" -ne 0 ]; then
  echo "FAIL: canary issue #${ISSUE_NUMBER} の close に失敗しました（orphan の可能性あり、手動確認が必要）"
  exit 1
fi

CLOSED_STATE_JSON=$(gh issue view "$ISSUE_NUMBER" --repo "$CANARY_REPO" --json state 2>&1)
CLOSED_STATE_RC=$?
CLOSED_STATE=$(printf '%s' "$CLOSED_STATE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null || echo "")

if [ "$CLOSED_STATE_RC" -ne 0 ] || [ "$CLOSED_STATE" != "CLOSED" ]; then
  echo "FAIL: close 後の再readback が CLOSED を確認できませんでした（issue #${ISSUE_NUMBER} は orphan の可能性あり、手動確認が必要）: state=${CLOSED_STATE}"
  exit 1
fi

echo "PASS: live canary issue #${ISSUE_NUMBER} を create -> readback -> close -> re-readback（CLOSED 確認）まで完了しました。"
exit 0

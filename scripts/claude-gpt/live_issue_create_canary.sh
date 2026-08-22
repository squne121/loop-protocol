#!/usr/bin/env bash
# scripts/claude-gpt/live_issue_create_canary.sh
#
# Issue #2299 AC8: opt-in, disposable-Issue live canary that confirms an
# isolated Claude-GPT session (launched via `launch.sh`, with GitHub auth
# shared native-equivalent per Issue #2299 AC1) can complete a genuine
# `issue-creator` create-issue workflow end-to-end against the REAL trusted
# repository, creating and then closing one disposable Issue.
#
# This is NOT a repeatable regression test and is NOT run by default. Unlike
# `runtime_smoke_test.sh --scenario issue_create` (Issue #2299 AC2/AC5, which
# always runs against a fake provider and never touches real GitHub state),
# this script performs one real, disposable GitHub mutation and is meant to
# be run manually/opt-in once per PR (Delivery Rule), following the existing
# opt-in canary pattern established by `scripts/claude-gpt/auto_mode_canary.sh`
# and `.claude/skills/create-issue/tests/live_canary_blocking_direction.sh`.
#
# Opt-in requirement:
#   Unless BOTH of the following are explicitly given, this script always
#   SKIPs (exit 77) without touching GitHub or launching claude-gpt:
#     1. env var CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_CONFIRM=1
#     2. flag --confirm on the command line
#   Absence of either is treated as "no opt-in" (fail-closed default SKIP).
#
# Exit codes:
#   0   PASS (disposable Issue created via genuine isolated Claude-GPT
#             issue-creator workflow, then closed)
#   1   FAIL (opted-in but the workflow did not complete / cleanup failed)
#   77  SKIP (no explicit opt-in given -- the default, always-safe path)
#
# Out of Scope (Issue #2299): this canary does not expand to other
# repositories, does not exercise PR/branch push/merge, and is not wired
# into CI as a required check.
#
# Artifact requirements: request/response transcripts (secrets redacted) are
# written under scripts/claude-gpt/.evidence/. Tokens/credentials/raw
# environment are never written.

set -u

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

CONFIRM_FLAG=false
for _arg in "$@"; do
  case "$_arg" in
    --confirm) CONFIRM_FLAG=true ;;
  esac
done

EVIDENCE_DIR=$(claude_gpt_evidence_dir "$SELF_PATH")
mkdir -p "$EVIDENCE_DIR"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE_FILE="${EVIDENCE_DIR}/live-issue-create-canary-${TIMESTAMP}.json"

if [ "${CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_CONFIRM:-}" != "1" ] || [ "$CONFIRM_FLAG" != "true" ]; then
  printf '{"schema":"CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_RESULT_V1","status":"skip","reason":"opt_in_not_given","generated_at":"%s"}\n' \
    "$TIMESTAMP" > "$EVIDENCE_FILE"
  echo "SKIP: opt-in が与えられていないため live_issue_create_canary を実行しません（既定の安全な経路）。" >&2
  echo "opt-in するには CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_CONFIRM=1 を設定し、--confirm を渡してください。" >&2
  echo "証跡: ${EVIDENCE_FILE}" >&2
  exit 77
fi

REPO="squne121/loop-protocol"

# --- 環境可用性判定（バイナリ / ChatGPT subscription 認証 / gh 認証）。opt-in 後も
#     実行不能な環境では SKIP を返す（opt-in は「実行して良い」の同意であり、
#     「実行環境が揃っている」ことの保証ではない）。 ---
PREFLIGHT_ENV_JSON=$("$SCRIPT_DIR/preflight.sh" --env-only)
PREFLIGHT_ENV_RC=$?
if [ "$PREFLIGHT_ENV_RC" -eq 3 ] || [ "$PREFLIGHT_ENV_RC" -eq 4 ]; then
  printf '{"schema":"CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_RESULT_V1","status":"skip","reason":"environment_unavailable","preflight_env_only":%s,"generated_at":"%s"}\n' \
    "$PREFLIGHT_ENV_JSON" "$TIMESTAMP" > "$EVIDENCE_FILE"
  echo "SKIP: claude-gpt 実行環境が利用不能なため live canary を実行できません。証跡: ${EVIDENCE_FILE}" >&2
  exit 77
fi

if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  printf '{"schema":"CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_RESULT_V1","status":"skip","reason":"gh_auth_unavailable","generated_at":"%s"}\n' \
    "$TIMESTAMP" > "$EVIDENCE_FILE"
  echo "SKIP: ambient gh auth が利用不能なため live canary を実行できません。証跡: ${EVIDENCE_FILE}" >&2
  exit 77
fi

# --- opted-in 実行: genuine issue-creator を isolated claude-gpt session 経由で
#     起動し、通常の create-issue workflow（dedupe read -> create_issue_txn.py
#     -> authoritative readback）を real trusted repository に対して完走させる。
#     作成した disposable Issue は必ず close する（成功/失敗どちらの経路でも）。 ---
CANARY_TITLE="claude-gpt live_issue_create_canary disposable probe (${TIMESTAMP})"
CANARY_BODY="## Acceptance Criteria

- [ ] AC1: disposable probe issue created by scripts/claude-gpt/live_issue_create_canary.sh

## Verification Commands

\`\`\`bash
true  # AC1
\`\`\`

## Allowed Paths

- scripts/claude-gpt/**

This is a disposable canary Issue created by \`scripts/claude-gpt/live_issue_create_canary.sh\`
(Issue #2299 AC8). It will be closed immediately by the same run."

PROMPT="isolated claude-gpt live_issue_create_canary: create-issue skill の通常
procedure（dedupe read を含む）に従って、repo ${REPO} に以下のタイトル/本文で
Issue を1件だけ作成してください。作成後、作成した Issue 番号を
\`CANARY_ISSUE_NUMBER=<番号>\` という1行として stdout に出力し、それ以外の
説明文は出力しないでください。

タイトル: ${CANARY_TITLE}

本文:
${CANARY_BODY}"

CLAUDE_OUTPUT=$("$SCRIPT_DIR/launch.sh" -- -p "$PROMPT" --output-format text --no-session-persistence 2>>"$EVIDENCE_FILE.stderr.log")
LAUNCH_RC=$?

CANARY_ISSUE_NUMBER=$(printf '%s\n' "$CLAUDE_OUTPUT" | sed -n 's/^CANARY_ISSUE_NUMBER=\([0-9]\+\).*/\1/p' | head -n1)

STATUS="failure"
if [ "$LAUNCH_RC" -eq 0 ] && [ -n "$CANARY_ISSUE_NUMBER" ]; then
  # authoritative readback: confirm the issue really exists before declaring PASS.
  if gh issue view "$CANARY_ISSUE_NUMBER" --repo "$REPO" >/dev/null 2>&1; then
    STATUS="success"
  fi
fi

CLEANUP_OK=true
if [ -n "$CANARY_ISSUE_NUMBER" ]; then
  if ! gh issue close "$CANARY_ISSUE_NUMBER" --repo "$REPO" --comment "claude-gpt live_issue_create_canary: disposable probe cleanup (${TIMESTAMP})" >/dev/null 2>&1; then
    CLEANUP_OK=false
  fi
fi

printf '{"schema":"CLAUDE_GPT_LIVE_ISSUE_CREATE_CANARY_RESULT_V1","status":"%s","generated_at":"%s","repo":"%s","canary_issue_number":%s,"launch_exit_code":%s,"cleanup_ok":%s}\n' \
  "$STATUS" "$TIMESTAMP" "$REPO" "${CANARY_ISSUE_NUMBER:-null}" "$LAUNCH_RC" "$CLEANUP_OK" > "$EVIDENCE_FILE"

if [ "$STATUS" = "success" ] && [ "$CLEANUP_OK" = "true" ]; then
  echo "PASS: live_issue_create_canary が disposable Issue #${CANARY_ISSUE_NUMBER} を作成/close しました。証跡: ${EVIDENCE_FILE}" >&2
  exit 0
fi

echo "FAIL: live_issue_create_canary が完走しませんでした（status=${STATUS} cleanup_ok=${CLEANUP_OK}）。証跡: ${EVIDENCE_FILE}" >&2
exit 1

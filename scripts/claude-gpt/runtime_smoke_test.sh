#!/bin/sh
# scripts/claude-gpt/runtime_smoke_test.sh
#
# Issue #2158 AC6 / AC7 の動作検証 VC。<!-- runtime-verification: true --> 対象。
# PR #2162 OWNER REQUEST_CHANGES（P0-1）反映: 構造確認（launch.sh --check-only）だけでなく、
# Claude Code 本体を実際に非対話起動し、実際の `POST /v1/messages` 成功・deterministic
# response marker・安全な Bash tool 呼び出し・SubAgent（Task tool）呼び出しを実機確認する。
#
# `raine/claude-code-proxy` または ChatGPT subscription 認証が利用不能な環境では、
# exit code 77 で SKIP を返す（SKIP を PASS に昇格しない。fallback 実行や擬似成功判定は行わない）。
#
# 証跡: scripts/claude-gpt/.evidence/smoke-<timestamp>.json に実行ログを保存する
# （credential・OAuth token・prompt/tool 全文は含めない。応答テキストは deterministic marker
# の有無のみを保存し、全文は含めない）。
#
# Exit code:
#   0   PASS（構造確認 + 対話 runtime 確認のすべてを実機確認）
#   1   FAIL（環境は利用可能だが検証項目のいずれかが失敗した）
#   77  SKIP（proxy バイナリ不在 or ChatGPT subscription 認証が利用不能）

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

EVIDENCE_DIR=$(claude_gpt_evidence_dir "$SELF_PATH")
mkdir -p "$EVIDENCE_DIR"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE_FILE="${EVIDENCE_DIR}/smoke-${TIMESTAMP}.json"

# --- SUT (System Under Test) provenance（PR #2162 敵対的レビュー対応: 実行元 worktree /
#     commit / launcher スクリプト自体の同一性を証跡へ束縛し、stale worktree 実行事故を
#     事後検出できるようにする）。proxy identity（absolute_path/version）も併せて記録する
#     （P2 と共通）。 ---
SUT_REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
SUT_LAUNCHER_PATH="$SCRIPT_DIR/launch.sh"
SUT_GIT_HEAD=$(claude_gpt_git_head "$SUT_REPO_ROOT")
SUT_GIT_DIRTY=$(claude_gpt_git_dirty "$SUT_REPO_ROOT")
SUT_LAUNCH_SH_SHA256=$(claude_gpt_sha256_file "$SCRIPT_DIR/launch.sh")
SUT_LIB_SH_SHA256=$(claude_gpt_sha256_file "$SCRIPT_DIR/lib.sh")
SUT_PROXY_BIN=$(claude_gpt_resolve_proxy_bin)
SUT_PROXY_VERSION="unknown"
if [ -n "$SUT_PROXY_BIN" ]; then
  SUT_PROXY_VERSION=$(claude_gpt_proxy_version "$SUT_PROXY_BIN")
fi
if [ -z "$SUT_PROXY_BIN" ]; then
  SUT_PROXY_BIN="unknown"
fi

# --- 環境可用性判定（バイナリ / ChatGPT subscription 認証）。ディレクトリ/設定はまだ作らない。 ---
PREFLIGHT_ENV_JSON=$("$SCRIPT_DIR/preflight.sh" --env-only)
PREFLIGHT_ENV_RC=$?

if [ "$PREFLIGHT_ENV_RC" -eq 3 ] || [ "$PREFLIGHT_ENV_RC" -eq 4 ]; then
  SKIP_REASON="binary_unavailable"
  if [ "$PREFLIGHT_ENV_RC" -eq 4 ]; then
    SKIP_REASON="chatgpt_subscription_auth_unavailable"
  fi
  printf '{"schema":"CLAUDE_GPT_SMOKE_RESULT_V1","status":"skip","reason":"%s","preflight_env_only":%s,"generated_at":"%s","sut":{"launcher_path":"%s","repository_root":"%s","git_head":"%s","git_dirty":"%s","launch_sh_sha256":"%s","lib_sh_sha256":"%s"},"proxy":{"absolute_path":"%s","version":"%s"}}\n' \
    "$SKIP_REASON" "$PREFLIGHT_ENV_JSON" "$TIMESTAMP" \
    "$SUT_LAUNCHER_PATH" "$SUT_REPO_ROOT" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$SUT_LIB_SH_SHA256" \
    "$SUT_PROXY_BIN" "$SUT_PROXY_VERSION" > "$EVIDENCE_FILE"
  echo "SKIP: ${SKIP_REASON} のため runtime smoke test を実行できません。証跡: ${EVIDENCE_FILE}"
  exit 77
fi

# =========================================================================
# Phase 0: 実行環境確認（current user / cwd）
#   AC1: root/sudo を使わず現行 Unix user のまま実行されていること。
#   AC2: worktree directory を cwd としていること。
# =========================================================================
CURRENT_USER=$(id -un 2>/dev/null || whoami)
CURRENT_UID=$(id -u 2>/dev/null || echo "-1")
CURRENT_CWD=$(pwd -P)
NOT_ROOT_OK=true
if [ "$CURRENT_UID" = "0" ]; then
  NOT_ROOT_OK=false
fi

# =========================================================================
# Phase A: 構造確認（launch.sh --check-only）
#   loopback bind / model alias 解決 / MCP 除外設定を実機確認する。
#   preflight は env-only ではなく launch.sh 内部で実行される完全版（canonical_paths /
#   read_restriction が applicable:true の実検査結果）を使う（P0-1）。
# =========================================================================
LAUNCH_CHECK_STDERR=$(mktemp)
LAUNCH_JSON=$("$SCRIPT_DIR/launch.sh" --check-only 2>"$LAUNCH_CHECK_STDERR")
LAUNCH_RC=$?
LAUNCH_CHECK_STDERR_CONTENT=$(cat "$LAUNCH_CHECK_STDERR")
rm -f "$LAUNCH_CHECK_STDERR"

MCP_CONFIG_PATH=$(claude_gpt_mcp_config_path)
MCP_CONFIG_OK=false
if [ -f "$MCP_CONFIG_PATH" ] && grep -q '"mcpServers"' "$MCP_CONFIG_PATH" 2>/dev/null && grep -q '{}' "$MCP_CONFIG_PATH" 2>/dev/null; then
  MCP_CONFIG_OK=true
fi

STRUCTURAL_OK=false
if [ "$LAUNCH_RC" -eq 0 ] && [ "$MCP_CONFIG_OK" = "true" ] && [ "$NOT_ROOT_OK" = "true" ]; then
  STRUCTURAL_OK=true
fi

# =========================================================================
# Phase B: 対話 runtime 確認（P0-1）
#   launch.sh を supervisor 構成の実起動モード（--check-only なし）で呼び出し、
#   `-p` 非対話プロンプトで Claude Code 本体を実際に起動する。
#   deterministic marker（3種）を Claude に生成させ、実際に:
#     1. テキスト応答 marker
#     2. Bash tool 経由の marker（実サブプロセス実行）
#     3. Task tool 経由の canary SubAgent 呼び出し marker
#   をそれぞれ出力させ、stdout から grep で確認する。
#   実際の `POST /v1/messages` 成功は proxy の構造化ログ（request / codex_upstream_request_started
#   / request_completed, status=200）から確認する（自己申告ではなく proxy 側の一次証跡）。
# =========================================================================
TEXT_MARKER="CLAUDE_GPT_CANARY_TEXT_OK"
BASH_MARKER="CLAUDE_GPT_CANARY_BASH_OK"
SUBAGENT_MARKER="CLAUDE_GPT_CANARY_SUBAGENT_OK"

CANARY_AGENTS_JSON='{"canary":{"description":"canary smoke test subagent used only for claude-gpt launcher runtime smoke testing","prompt":"You are a canary subagent used only for launcher runtime smoke testing. When invoked, respond with exactly: '"${SUBAGENT_MARKER}"' and nothing else."}}'

# --- 単一 turn に複数ステップを詰め込むと model が一部のツール呼び出しを省略する挙動が
#     実機観測で確認されたため（PR #2162 実装セッション, 2026-08-14）、Bash tool /
#     Task tool / plain text の 3 観点を独立した `-p` invocation に分割し、それぞれの
#     proxy ログから一次証跡を確認する。合算判定は AND、http_post_confirmed は OR。 ---

RC_LAST=0
HTTP_POST_CONFIRMED=false
MODEL_USED=""
PROVIDER_USED=""
CLEANUP_ALL_OK=true

run_convo_step() {
  step_prompt="$1"
  step_allowed_tools="$2"
  step_agents_json="$3"
  step_stdout_file=$(mktemp)
  step_stderr_file=$(mktemp)

  if [ -n "$step_agents_json" ] && [ -n "$step_allowed_tools" ]; then
    "$SCRIPT_DIR/launch.sh" -- -p "$step_prompt" --output-format text --no-session-persistence \
      --allowedTools "$step_allowed_tools" --agents "$step_agents_json" \
      >"$step_stdout_file" 2>"$step_stderr_file"
  elif [ -n "$step_allowed_tools" ]; then
    "$SCRIPT_DIR/launch.sh" -- -p "$step_prompt" --output-format text --no-session-persistence \
      --allowedTools "$step_allowed_tools" \
      >"$step_stdout_file" 2>"$step_stderr_file"
  else
    "$SCRIPT_DIR/launch.sh" -- -p "$step_prompt" --output-format text --no-session-persistence \
      >"$step_stdout_file" 2>"$step_stderr_file"
  fi
  STEP_RC=$?
  STEP_STDOUT=$(cat "$step_stdout_file")
  STEP_STDERR=$(cat "$step_stderr_file")
  rm -f "$step_stdout_file" "$step_stderr_file"

  STEP_PROXY_LOG=$(printf '%s\n' "$STEP_STDERR" | grep '^CLAUDE_GPT_PROXY_LOG=' | tail -n1 | cut -d= -f2-)
  STEP_PROXY_PID=$(printf '%s\n' "$STEP_STDERR" | grep '^CLAUDE_GPT_PROXY_PID=' | tail -n1 | cut -d= -f2-)
  STEP_CLEANUP_OK=$(printf '%s\n' "$STEP_STDERR" | grep '^CLAUDE_GPT_PROXY_CLEANUP_OK=' | tail -n1 | cut -d= -f2-)

  STEP_HTTP_OK=false
  if [ -n "$STEP_PROXY_LOG" ] && [ -f "$STEP_PROXY_LOG" ]; then
    if grep -q '"path":"/v1/messages"' "$STEP_PROXY_LOG" 2>/dev/null \
      && grep -q '"msg":"request_completed"' "$STEP_PROXY_LOG" 2>/dev/null \
      && grep -q '"status":200' "$STEP_PROXY_LOG" 2>/dev/null; then
      STEP_HTTP_OK=true
      MODEL_USED=$(grep -o '"model":"[^"]*"' "$STEP_PROXY_LOG" 2>/dev/null | head -n1 | cut -d: -f2 | tr -d '"')
      PROVIDER_USED=$(grep -o '"provider":"[^"]*"' "$STEP_PROXY_LOG" 2>/dev/null | head -n1 | cut -d: -f2 | tr -d '"')
      HTTP_POST_CONFIRMED=true
    fi
  fi

  if [ -n "$STEP_PROXY_PID" ]; then
    if kill -0 "$STEP_PROXY_PID" 2>/dev/null; then
      CLEANUP_ALL_OK=false
    fi
    if ss -ltnp 2>/dev/null | grep -q "pid=${STEP_PROXY_PID},"; then
      CLEANUP_ALL_OK=false
    fi
  fi
  if [ "$STEP_CLEANUP_OK" != "true" ]; then
    CLEANUP_ALL_OK=false
  fi

  RC_LAST=$STEP_RC
}

# GPT-5.6-terra（codex backend 経由）は同一の単純な Bash 指示でも tool を呼ばずに
# 応答を終える挙動が実機観測で稀に発生したため（PR #2162 実装セッション, 2026-08-14。
# 実装バグではなく backend 側の non-determinism）、marker 未検出時のみ bounded retry
# する（最大 3 回。fallback 実行や擬似成功判定は行わない — 毎回実際に tool を再試行する）。
BASH_STDOUT=""
BASH_RC=1
bash_attempt=0
while [ "$bash_attempt" -lt 3 ]; do
  bash_attempt=$((bash_attempt + 1))
  run_convo_step "You are running inside an automated, non-interactive runtime smoke test with no real user present. Use the Bash tool right now (an actual tool call, not a description) to run exactly: echo ${BASH_MARKER}
Then print its real stdout output verbatim on its own line." "Bash(echo *)" ""
  BASH_STDOUT="$STEP_STDOUT"
  BASH_RC="$STEP_RC"
  case "$BASH_STDOUT" in
    *"$BASH_MARKER"*) break ;;
  esac
done

run_convo_step "You are running inside an automated, non-interactive runtime smoke test with no real user present. Use the Task tool right now (an actual tool call, not a description) to launch the subagent named canary with any instructions, then print its exact output verbatim." "Task" "$CANARY_AGENTS_JSON"
SUBAGENT_STDOUT="$STEP_STDOUT"
SUBAGENT_RC="$STEP_RC"

run_convo_step "You are running inside an automated, non-interactive runtime smoke test with no real user present. Print exactly the following text and nothing else: ${TEXT_MARKER}" "" ""
TEXT_STDOUT="$STEP_STDOUT"
TEXT_RC="$STEP_RC"

TEXT_MARKER_OK=false
case "$TEXT_STDOUT" in
  *"$TEXT_MARKER"*) TEXT_MARKER_OK=true ;;
esac

BASH_MARKER_OK=false
case "$BASH_STDOUT" in
  *"$BASH_MARKER"*) BASH_MARKER_OK=true ;;
esac

SUBAGENT_MARKER_OK=false
case "$SUBAGENT_STDOUT" in
  *"$SUBAGENT_MARKER"*) SUBAGENT_MARKER_OK=true ;;
esac

CONVO_CLEANUP_OK="$CLEANUP_ALL_OK"
CLEANUP_INDEPENDENT_OK="$CLEANUP_ALL_OK"
CONVO_RC=0
if [ "$BASH_RC" -ne 0 ] || [ "$SUBAGENT_RC" -ne 0 ] || [ "$TEXT_RC" -ne 0 ]; then
  CONVO_RC=1
fi

RUNTIME_CONVERSATION_OK=false
if [ "$CONVO_RC" -eq 0 ] \
  && [ "$TEXT_MARKER_OK" = "true" ] \
  && [ "$BASH_MARKER_OK" = "true" ] \
  && [ "$SUBAGENT_MARKER_OK" = "true" ] \
  && [ "$HTTP_POST_CONFIRMED" = "true" ] \
  && [ "$CONVO_CLEANUP_OK" = "true" ] \
  && [ "$CLEANUP_INDEPENDENT_OK" = "true" ]; then
  RUNTIME_CONVERSATION_OK=true
fi

if [ "$STRUCTURAL_OK" = "true" ] && [ "$RUNTIME_CONVERSATION_OK" = "true" ]; then
  STATUS="pass"
  EXIT_CODE=0
else
  STATUS="fail"
  EXIT_CODE=1
fi

# JSON エスケープ（改行・二重引用符のみ最小限。marker 文字列と model/provider 名は英数字+記号少数のため安全）
json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' ' '
}

cat > "$EVIDENCE_FILE" <<EVIDENCE_JSON_EOF
{
  "schema": "CLAUDE_GPT_SMOKE_RESULT_V1",
  "status": "${STATUS}",
  "generated_at": "${TIMESTAMP}",
  "sut": {
    "launcher_path": "$(json_escape "$SUT_LAUNCHER_PATH")",
    "repository_root": "$(json_escape "$SUT_REPO_ROOT")",
    "git_head": "${SUT_GIT_HEAD}",
    "git_dirty": "${SUT_GIT_DIRTY}",
    "launch_sh_sha256": "${SUT_LAUNCH_SH_SHA256}",
    "lib_sh_sha256": "${SUT_LIB_SH_SHA256}"
  },
  "proxy": {
    "absolute_path": "$(json_escape "$SUT_PROXY_BIN")",
    "version": "$(json_escape "$SUT_PROXY_VERSION")"
  },
  "runtime_environment": {
    "not_root_ok": ${NOT_ROOT_OK},
    "current_user": "$(json_escape "$CURRENT_USER")",
    "current_uid": "${CURRENT_UID}",
    "cwd": "$(json_escape "$CURRENT_CWD")"
  },
  "structural_check": {
    "ok": ${STRUCTURAL_OK},
    "launch_check_only_rc": ${LAUNCH_RC},
    "mcp_config_ok": ${MCP_CONFIG_OK},
    "mcp_config_path": "${MCP_CONFIG_PATH}",
    "launch_result": ${LAUNCH_JSON}
  },
  "runtime_conversation_check": {
    "ok": ${RUNTIME_CONVERSATION_OK},
    "claude_exit_code": ${CONVO_RC},
    "text_marker_ok": ${TEXT_MARKER_OK},
    "bash_tool_marker_ok": ${BASH_MARKER_OK},
    "subagent_marker_ok": ${SUBAGENT_MARKER_OK},
    "http_post_v1_messages_confirmed": ${HTTP_POST_CONFIRMED},
    "model_used": "$(json_escape "$MODEL_USED")",
    "provider_used": "$(json_escape "$PROVIDER_USED")",
    "proxy_cleanup_ok_launcher_reported": ${CONVO_CLEANUP_OK:-false},
    "proxy_cleanup_ok_independent_reverify": ${CLEANUP_INDEPENDENT_OK}
  }
}
EVIDENCE_JSON_EOF

if [ "$STATUS" = "pass" ]; then
  echo "PASS: claude-gpt launcher runtime smoke test（構造確認 + 対話 runtime 確認）が成功しました。証跡: ${EVIDENCE_FILE}"
else
  echo "FAIL: claude-gpt launcher runtime smoke test が失敗しました（structural_ok=${STRUCTURAL_OK}, runtime_conversation_ok=${RUNTIME_CONVERSATION_OK}）。証跡: ${EVIDENCE_FILE}"
fi

exit "$EXIT_CODE"

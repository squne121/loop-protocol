#!/bin/sh
# scripts/claude-gpt/runtime_smoke_test.sh
#
# Claude-GPT の Minimal default launcher（Issue #2925）に対する動作検証 VC。
# <!-- runtime-verification: true --> 対象。静的 test だけでは満たせない、実際の
# Claude Code process の挙動（text 応答・tool use・SubAgent・Auto mode classifier path）を
# 実機で確認する。
#
# この script は proxy を起動も停止もしない。既に起動している loopback `claude-code-proxy`
# （`ANTHROPIC_BASE_URL` の接続先）に対して `scripts/claude-gpt/launch.sh` を通常どおり
# 起動し、各 step の結果を Claude Code の stream-json 出力から判定する。
#
# Usage:
#   scripts/claude-gpt/runtime_smoke_test.sh [--scenario default|auto_classifier] [--evidence-out <path>]
#
#   default          text 応答 / Read / Bash / SubAgent の各 tool use が実際に完了すること。
#   auto_classifier  Auto mode のまま、permission 許可済みルールに含まれず classifier を通る
#                    無害な操作（scratch directory 内のファイル作成）が完了すること。
#                    接続先 proxy の構造化ログ（CLAUDE_GPT_PROXY_LOG）が観測できる場合に限り
#                    routing 先 model を記録する。観測できない場合は「未観測」と記録し、
#                    route 確認済みとは扱わない。
#
# 環境変数:
#   CLAUDE_GPT_PROXY_LOG   接続先 proxy が書き出す構造化 JSONL ログの path（任意）。
#
# 証跡: scripts/claude-gpt/.evidence/smoke-<timestamp>.json（credential・prompt/tool 全文は
# 含めない。応答は deterministic marker の有無のみを保存する）。
#
# Exit code:
#   0   PASS
#   1   FAIL（環境は利用可能だが検証項目のいずれかが失敗した）
#   2   引数エラー
#   77  SKIP（claude バイナリ不在 / 接続先 server が利用不能。SKIP は PASS ではない。
#       fallback 実行や擬似成功判定は行わない）

for _arg in "$@"; do
  case "$_arg" in
    --spark-delegation)
      echo "FAIL: --spark-delegation is retired. GPT-5.3-Codex-Spark delegation has been removed from this repository (Issue #2651); this flag never falls through to ordinary smoke." >&2
      exit 2
      ;;
  esac
done

SCENARIO="default"
EVIDENCE_OUT_ARG_PATH=""
_scenario_seen=false
_evidence_seen=false
while [ $# -gt 0 ]; do
  case "$1" in
    --scenario)
      if [ "$_scenario_seen" = "true" ] || [ $# -lt 2 ]; then
        echo "FAIL: --scenario requires exactly one value." >&2
        exit 2
      fi
      _scenario_seen=true
      SCENARIO="$2"
      shift 2
      ;;
    --evidence-out)
      if [ "$_evidence_seen" = "true" ] || [ $# -lt 2 ]; then
        echo "FAIL: --evidence-out requires exactly one value." >&2
        exit 2
      fi
      _evidence_seen=true
      EVIDENCE_OUT_ARG_PATH="$2"
      shift 2
      ;;
    *)
      echo "FAIL: unknown argument '$1'." >&2
      exit 2
      ;;
  esac
done
case "$SCENARIO" in
  default | auto_classifier) : ;;
  *)
    echo "FAIL: unknown --scenario value '${SCENARIO}' (known values: default, auto_classifier). Refusing to fall back to another scenario." >&2
    exit 2
    ;;
esac

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

EVIDENCE_DIR=$(claude_gpt_evidence_dir "$SELF_PATH")
mkdir -p "$EVIDENCE_DIR"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE_FILE="${EVIDENCE_OUT_ARG_PATH:-${EVIDENCE_DIR}/smoke-${TIMESTAMP}.json}"
TRANSPORT_LOG_PARSER="$SCRIPT_DIR/transport_log.py"

SUT_REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
SUT_GIT_HEAD=$(claude_gpt_git_head "$SUT_REPO_ROOT")
SUT_GIT_DIRTY=$(claude_gpt_git_dirty "$SUT_REPO_ROOT")
CLAUDE_BIN=$(claude_gpt_resolve_claude_bin)
CLAUDE_VERSION="unknown"
if [ -n "$CLAUDE_BIN" ]; then
  CLAUDE_VERSION=$("$CLAUDE_BIN" --version 2>/dev/null | head -n1)
  [ -n "$CLAUDE_VERSION" ] || CLAUDE_VERSION="unknown"
fi

write_skip() {
  # $1=reason, $2=launcher JSON (または空)
  _skip_launch_json="$2"
  [ -n "$_skip_launch_json" ] || _skip_launch_json=null
  printf '{"schema":"CLAUDE_GPT_SMOKE_RESULT_V1","schema_version":3,"status":"skip","scenario":"%s","reason":"%s","generated_at":"%s","sut":{"git_head":"%s","git_dirty":"%s"},"claude_code_version":%s,"launch_check_only":%s}\n' \
    "$SCENARIO" "$1" "$TIMESTAMP" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$(claude_gpt_json_escape "$CLAUDE_VERSION")" "$_skip_launch_json" > "$EVIDENCE_FILE"
  echo "SKIP: $1 のため runtime smoke test を実行できません（SKIP は PASS ではありません）。証跡: ${EVIDENCE_FILE}"
  exit 77
}

if [ -z "$CLAUDE_BIN" ]; then
  write_skip "claude_binary_unavailable" ""
fi

# =========================================================================
# Phase A: 接続先 server の診断（launch.sh --check-only）
#   到達性と /v1/models の required model set（gpt-6-sol および gpt-6-luna）を、
#   ANTHROPIC_BASE_URL が実際に向く server に対して確認する。
# =========================================================================
LAUNCH_JSON=$("$SCRIPT_DIR/launch.sh" --check-only 2>/dev/null)
LAUNCH_RC=$?
if [ "$LAUNCH_RC" -ne 0 ]; then
  write_skip "connected_server_unavailable_or_catalog_incomplete" "$LAUNCH_JSON"
fi

# =========================================================================
# Phase B: 実 Claude Code process の検証
#   各 step は独立した `-p` invocation（単一 turn に詰め込むと model が一部の tool 呼び出しを
#   省略する挙動が観測されたため）。判定は stream-json の構造化 event（tool_use /
#   tool_result / result）で行い、自己申告の text だけでは PASS にしない。
# =========================================================================
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

CHECKER_PY="$WORKDIR/check_stream.py"
cat > "$CHECKER_PY" <<'CHECKER_PY_EOF'
import json
import sys

# usage: check_stream.py <stdout_path> <marker> <expect_tool> [<expect_tool_marker>]
stdout_path, marker, expect_tool = sys.argv[1], sys.argv[2], sys.argv[3]
tool_marker = sys.argv[4] if len(sys.argv) > 4 else marker

events = []
with open(stdout_path, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue

permission_mode = None
model = None
tool_use_ids = {}
tool_results = {}
# SubAgent 側の hand-back: SubagentHandback tool_use の (parent_tool_use_id, tool_use id, input.message)。
handback_uses = []
final_text = ""
result_is_error = None
for event in events:
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        permission_mode = event.get("permissionMode")
        model = event.get("model")
    elif etype == "assistant":
        content = (event.get("message") or {}).get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_use_ids[block.get("id")] = block.get("name")
                if block.get("name") == "SubagentHandback":
                    tool_input = block.get("input")
                    message = tool_input.get("message") if isinstance(tool_input, dict) else None
                    handback_uses.append((event.get("parent_tool_use_id"), block.get("id"), str(message or "")))
    elif etype == "user":
        content = (event.get("message") or {}).get("content") or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                    tool_results[block.get("tool_use_id")] = (str(body or ""), bool(block.get("is_error")))
    elif etype == "result":
        final_text = str(event.get("result") or "")
        result_is_error = bool(event.get("is_error"))

wanted = {expect_tool} if expect_tool != "Agent" else {"Agent", "Task"}
matched_tool_use = [tid for tid, name in tool_use_ids.items() if name in wanted] if expect_tool != "-" else []
tool_completed = False


def _result_succeeded(tool_use_id):
    """tool_result が存在し、is_error ではなく、body が明示的な success:false でないこと。"""
    body, is_error = tool_results.get(tool_use_id, (None, True))
    if body is None or is_error:
        return False
    try:
        parsed = json.loads(body)
    except ValueError:
        return True
    return not (isinstance(parsed, dict) and parsed.get("success") is False)


for tid in matched_tool_use:
    body, is_error = tool_results.get(tid, (None, True))
    if body is None or is_error:
        continue
    if expect_tool != "Agent":
        if tool_marker in body:
            tool_completed = True
        continue
    # SubAgent の terminal completion: Claude Code 2.1.289 以降の stream-json では Agent tool_result は
    # 「report は SubagentHandback で届いた」という定型文だけで marker を含まない。marker は
    # SubAgent 側の SubagentHandback tool_use（parent_tool_use_id が当該 Agent tool_use）の input と、
    # その成功 tool_result で観測する。harness が report を Agent tool_result へ直接載せる場合はそちらも許容。
    # Agent tool_use の prompt や parent の自己申告 text だけでは成立させない。
    if tool_marker in body:
        tool_completed = True
        continue
    for parent_id, handback_id, message in handback_uses:
        if parent_id == tid and tool_marker in message and _result_succeeded(handback_id):
            tool_completed = True

summary = {
    "permission_mode": permission_mode,
    "session_model": model,
    "tool_use_observed": bool(matched_tool_use) if expect_tool != "-" else None,
    "tool_completed_with_marker": tool_completed if expect_tool != "-" else None,
    "final_text_marker": marker in final_text,
    "result_is_error": result_is_error,
    "ok": (marker in final_text)
    and (result_is_error is False)
    and (expect_tool == "-" or tool_completed),
}
print(json.dumps(summary))
sys.exit(0 if summary["ok"] else 1)
CHECKER_PY_EOF

STEPS_JSON=""
ALL_STEPS_OK=true
PERMISSION_MODE_SEEN=""
SESSION_MODEL_SEEN=""

# run_step <name> <marker> <expect_tool> <tool_marker> <prompt>
run_step() {
  _rs_name="$1"
  _rs_marker="$2"
  _rs_tool="$3"
  _rs_tool_marker="$4"
  _rs_prompt="$5"
  _rs_out="$WORKDIR/${_rs_name}.stdout"
  _rs_err="$WORKDIR/${_rs_name}.stderr"
  (cd "$SUT_REPO_ROOT" && "$SCRIPT_DIR/launch.sh" -- -p "$_rs_prompt" --output-format stream-json --verbose --no-session-persistence --max-turns 12 >"$_rs_out" 2>"$_rs_err")
  _rs_rc=$?
  _rs_summary=$(python3 "$CHECKER_PY" "$_rs_out" "$_rs_marker" "$_rs_tool" "$_rs_tool_marker" 2>/dev/null)
  _rs_check_rc=$?
  [ -n "$_rs_summary" ] || _rs_summary='{"ok":false}'
  _rs_mode=$(printf '%s' "$_rs_summary" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("permission_mode") or "")' 2>/dev/null)
  _rs_model=$(printf '%s' "$_rs_summary" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_model") or "")' 2>/dev/null)
  [ -z "$_rs_mode" ] || PERMISSION_MODE_SEEN="$_rs_mode"
  [ -z "$_rs_model" ] || SESSION_MODEL_SEEN="$_rs_model"
  if [ "$_rs_rc" -ne 0 ] || [ "$_rs_check_rc" -ne 0 ]; then
    ALL_STEPS_OK=false
  fi
  if [ -n "$STEPS_JSON" ]; then STEPS_JSON="${STEPS_JSON},"; fi
  STEPS_JSON="${STEPS_JSON}{\"name\":\"${_rs_name}\",\"claude_exit_code\":${_rs_rc},\"check\":${_rs_summary}}"
}

TEXT_MARKER="CLAUDE_GPT_SMOKE_TEXT_OK_$$"
BASH_MARKER="CLAUDE_GPT_SMOKE_BASH_OK_$$"
READ_MARKER="272000"
SUBAGENT_MARKER="CLAUDE_GPT_SMOKE_SUBAGENT_OK_$$"
CLASSIFIER_MARKER="CLAUDE_GPT_SMOKE_CLASSIFIER_OK_$$"

PROXY_LOG_PATH="${CLAUDE_GPT_PROXY_LOG:-}"
PROXY_LOG_OFFSET_BEFORE=""
if [ -n "$PROXY_LOG_PATH" ] && [ -f "$PROXY_LOG_PATH" ]; then
  PROXY_LOG_OFFSET_BEFORE=$(wc -c < "$PROXY_LOG_PATH" | tr -d ' ')
fi

CLASSIFIER_SCRATCH=""
CLASSIFIER_FILE_OK=false

if [ "$SCENARIO" = "default" ]; then
  run_step "text" "$TEXT_MARKER" "-" "" \
    "You are running inside an automated runtime smoke test. Reply with exactly this single token and nothing else: ${TEXT_MARKER}"
  run_step "read" "$READ_MARKER" "Read" "$READ_MARKER" \
    "You are running inside an automated runtime smoke test. Use the Read tool (an actual tool call) to read ${SUT_REPO_ROOT}/scripts/claude-gpt/lib.sh, find the value assigned to CLAUDE_GPT_AUTO_COMPACT_WINDOW, and reply with exactly that number and nothing else."
  run_step "bash" "$BASH_MARKER" "Bash" "$BASH_MARKER" \
    "You are running inside an automated runtime smoke test. Use the Bash tool (an actual tool call) to run exactly: echo ${BASH_MARKER}  Then reply with exactly the printed token and nothing else."
  run_step "subagent" "$SUBAGENT_MARKER" "Agent" "$SUBAGENT_MARKER" \
    "You are running inside an automated runtime smoke test. Use the Agent tool exactly once (subagent_type: general-purpose) and ask the SubAgent to reply with exactly this single token and nothing else: ${SUBAGENT_MARKER}  Wait for the SubAgent to finish, then reply with exactly the token it returned."
else
  # auto_classifier: `echo` のような事前許可済みコマンドは classifier を通らないため使わない。
  # scratch directory 内のファイル作成は無害だが、permission 許可済みルールには含まれない。
  CLASSIFIER_SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/claude-gpt-auto-classifier.XXXXXX")
  run_step "classifier_file_create" "$CLASSIFIER_MARKER" "Bash" "" \
    "You are running inside an automated runtime smoke test with no human present. Use the Bash tool (an actual tool call) to run exactly: mkdir -p ${CLASSIFIER_SCRATCH}/probe && printf '%s' ${CLASSIFIER_MARKER} > ${CLASSIFIER_SCRATCH}/probe/classifier_probe.txt  After it finishes, reply with exactly: ${CLASSIFIER_MARKER}"
  if [ -f "${CLASSIFIER_SCRATCH}/probe/classifier_probe.txt" ] \
    && [ "$(cat "${CLASSIFIER_SCRATCH}/probe/classifier_probe.txt")" = "$CLASSIFIER_MARKER" ]; then
    CLASSIFIER_FILE_OK=true
  fi
  rm -rf "$CLASSIFIER_SCRATCH"
fi

# --- Auto mode が実際に有効であること（permission mode を launcher が注入しないため、
#     ambient 設定が Auto になっていることを init event で確認する）。 ---
AUTO_MODE_EFFECTIVE=false
if [ "$PERMISSION_MODE_SEEN" = "auto" ]; then
  AUTO_MODE_EFFECTIVE=true
fi

# --- 接続先 proxy の routing model（観測できる場合のみ）。観測できなければ「未観測」。 ---
ROUTING_MODELS_JSON='"未観測"'
ROUTING_OBSERVED=false
if [ -n "$PROXY_LOG_OFFSET_BEFORE" ] && [ -f "$PROXY_LOG_PATH" ]; then
  tail -c "+$((PROXY_LOG_OFFSET_BEFORE + 1))" "$PROXY_LOG_PATH" > "$WORKDIR/proxy-slice.log" 2>/dev/null
  if [ -s "$WORKDIR/proxy-slice.log" ]; then
    ROUTING_MODELS_JSON=$(python3 "$TRANSPORT_LOG_PARSER" "$WORKDIR/proxy-slice.log" 2>/dev/null | python3 -c '
import json, sys
try:
    payload = json.loads(sys.stdin.readline())
except ValueError:
    payload = {}
models = sorted({str(r.get("model")) for r in payload.get("requests", []) if r.get("model")})
print(json.dumps(models) if models else json.dumps("未観測"))
' 2>/dev/null)
    [ -n "$ROUTING_MODELS_JSON" ] || ROUTING_MODELS_JSON='"未観測"'
    case "$ROUTING_MODELS_JSON" in
      "\"未観測\"") : ;;
      *) ROUTING_OBSERVED=true ;;
    esac
  fi
fi

STATUS="pass"
EXIT_CODE=0
if [ "$ALL_STEPS_OK" != "true" ] || [ "$AUTO_MODE_EFFECTIVE" != "true" ]; then
  STATUS="fail"
  EXIT_CODE=1
fi
if [ "$SCENARIO" = "auto_classifier" ] && [ "$CLASSIFIER_FILE_OK" != "true" ]; then
  STATUS="fail"
  EXIT_CODE=1
fi
if [ "$SUT_GIT_DIRTY" != "false" ]; then
  # dirty worktree での live smoke は現行 head の統合状態を証明しない。
  STATUS="fail"
  EXIT_CODE=1
fi

cat > "$EVIDENCE_FILE" <<EVIDENCE_JSON_EOF
{
  "schema": "CLAUDE_GPT_SMOKE_RESULT_V1",
  "schema_version": 3,
  "status": "${STATUS}",
  "scenario": "${SCENARIO}",
  "generated_at": "${TIMESTAMP}",
  "sut": {
    "repository_root": $(claude_gpt_json_escape "$SUT_REPO_ROOT"),
    "git_head": "${SUT_GIT_HEAD}",
    "git_dirty": "${SUT_GIT_DIRTY}",
    "launch_sh_sha256": "$(claude_gpt_sha256_file "$SCRIPT_DIR/launch.sh")",
    "lib_sh_sha256": "$(claude_gpt_sha256_file "$SCRIPT_DIR/lib.sh")"
  },
  "claude_code_version": $(claude_gpt_json_escape "$CLAUDE_VERSION"),
  "launch_check_only": ${LAUNCH_JSON},
  "steps": [${STEPS_JSON}],
  "auto_mode": {
    "effective": ${AUTO_MODE_EFFECTIVE},
    "permission_mode_observed": $(claude_gpt_json_escape "$PERMISSION_MODE_SEEN"),
    "session_model_observed": $(claude_gpt_json_escape "$SESSION_MODEL_SEEN"),
    "classifier_file_create_completed": ${CLASSIFIER_FILE_OK},
    "routing_models_observed": ${ROUTING_MODELS_JSON},
    "routing_observed": ${ROUTING_OBSERVED},
    "note": "routing_observed=false（routing_models_observed=未観測）は route 確認済みを意味しない。CLAUDE_GPT_PROXY_LOG が接続先 proxy の構造化ログを指す場合のみ観測する。"
  }
}
EVIDENCE_JSON_EOF

if [ "$STATUS" = "pass" ]; then
  echo "PASS: claude-gpt runtime smoke test（scenario=${SCENARIO}）が成功しました。証跡: ${EVIDENCE_FILE}"
else
  echo "FAIL: claude-gpt runtime smoke test（scenario=${SCENARIO}）が失敗しました（steps_ok=${ALL_STEPS_OK}, auto_mode_effective=${AUTO_MODE_EFFECTIVE}, git_dirty=${SUT_GIT_DIRTY}）。証跡: ${EVIDENCE_FILE}"
fi

exit "$EXIT_CODE"

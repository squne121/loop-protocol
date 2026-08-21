#!/bin/sh
# scripts/claude-gpt/runtime_smoke_test.sh
#
# Issue #2158 AC6 / AC7 の動作検証 VC。<!-- runtime-verification: true --> 対象。
# PR #2162 OWNER REQUEST_CHANGES（P0-1）反映: 構造確認（launch.sh --check-only）だけでなく、
# Claude Code 本体を実際に非対話起動し、実際の `POST /v1/messages` 成功・deterministic
# response marker・安全な Bash tool 呼び出し・SubAgent（Task tool）呼び出しを実機確認する。
#
# Issue #2204 PR #2205 OWNER REQUEST_CHANGES（iteration 2, P0-2/P0-1/P1-1/P1-2）反映:
#   - transport 判定を grep tail -n1 のファイル全体単純一致から、構造化ログを
#     `transport_log.py` で厳密パースし reqId 相関する方式へ置き換えた（各 step ごとに
#     started_count>=1・websocket_count==0・auto_count==0・unknown_count==0・全 request の
#     response 相関確認を fail-closed で必須化する）。
#   - proxy 実行バイナリと本スクリプト自身の sha256 を証跡へ追加した（P1-1 の
#     identity pinning。exact v0.1.34 source では configured transport がそのまま
#     HTTP dispatch へ対応することを前提とする）。
#   - `git_dirty == false` を PASS 条件に追加した（dirty worktree での live smoke は
#     現行 head の統合状態を証明しない）。
#   - `proxy_cleanup_ok_launcher_reported`（launcher 自己申告）と、PID/listen socket の
#     独立再検証（`pid_absent_all` / `socket_absent_all`）を別フィールドとして分離した
#     （P1-2。従来は同一集約値のエイリアスだった）。
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

# --- Issue #2259 AC10: --scenario issue_create は、isolated Claude-GPT の
#     issue.create 要求が parent bridge 経由で処理される実経路（実 launch.sh →
#     実 Claude-GPT session → Bash tool 経由の create_issue_txn.py → bridge
#     client/server → deterministic fake gh provider → 独立 ledger 確認）を
#     通す専用シナリオ。既定（引数なし）は従来どおりの Issue #2158/#2204 の
#     canary シナリオ（Phase 0 以降、本ファイルの残り全体）。 ---
SCENARIO="default"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --scenario)
      SCENARIO="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

EVIDENCE_DIR=$(claude_gpt_evidence_dir "$SELF_PATH")
mkdir -p "$EVIDENCE_DIR"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE_FILE="${EVIDENCE_DIR}/smoke-${TIMESTAMP}.json"
TRANSPORT_LOG_PARSER="$SCRIPT_DIR/transport_log.py"

# --- SUT (System Under Test) provenance（PR #2162 敵対的レビュー対応: 実行元 worktree /
#     commit / launcher スクリプト自体の同一性を証跡へ束縛し、stale worktree 実行事故を
#     事後検出できるようにする）。proxy identity（absolute_path/version/sha256）と
#     本スクリプト自身の sha256 も併せて記録する（Issue #2204 P1-1）。 ---
SUT_REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
SUT_LAUNCHER_PATH="$SCRIPT_DIR/launch.sh"
SUT_RUNTIME_SMOKE_PATH="$SCRIPT_DIR/runtime_smoke_test.sh"
SUT_GIT_HEAD=$(claude_gpt_git_head "$SUT_REPO_ROOT")
SUT_GIT_DIRTY=$(claude_gpt_git_dirty "$SUT_REPO_ROOT")
SUT_LAUNCH_SH_SHA256=$(claude_gpt_sha256_file "$SCRIPT_DIR/launch.sh")
SUT_LIB_SH_SHA256=$(claude_gpt_sha256_file "$SCRIPT_DIR/lib.sh")
SUT_RUNTIME_SMOKE_SHA256=$(claude_gpt_sha256_file "$SUT_RUNTIME_SMOKE_PATH")
SUT_PROXY_BIN=$(claude_gpt_resolve_proxy_bin)
SUT_PROXY_VERSION="unknown"
SUT_PROXY_SHA256="unknown"
if [ -n "$SUT_PROXY_BIN" ]; then
  SUT_PROXY_VERSION=$(claude_gpt_proxy_version "$SUT_PROXY_BIN")
  SUT_PROXY_SHA256=$(claude_gpt_sha256_file "$SUT_PROXY_BIN")
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
  printf '{"schema":"CLAUDE_GPT_SMOKE_RESULT_V1","status":"skip","reason":"%s","preflight_env_only":%s,"generated_at":"%s","sut":{"launcher_path":"%s","repository_root":"%s","git_head":"%s","git_dirty":"%s","launch_sh_sha256":"%s","lib_sh_sha256":"%s","runtime_smoke_sha256":"%s"},"proxy":{"absolute_path":"%s","version":"%s","sha256":"%s"}}\n' \
    "$SKIP_REASON" "$PREFLIGHT_ENV_JSON" "$TIMESTAMP" \
    "$SUT_LAUNCHER_PATH" "$SUT_REPO_ROOT" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$SUT_LIB_SH_SHA256" "$SUT_RUNTIME_SMOKE_SHA256" \
    "$SUT_PROXY_BIN" "$SUT_PROXY_VERSION" "$SUT_PROXY_SHA256" > "$EVIDENCE_FILE"
  echo "SKIP: ${SKIP_REASON} のため runtime smoke test を実行できません。証跡: ${EVIDENCE_FILE}"
  exit 77
fi

if [ "$SCENARIO" = "issue_create" ]; then
  # --- Issue #2259 AC10: 実 launch.sh -> 実 Claude-GPT session -> Bash tool 経由の
  #     create_issue_txn.py -> bridge client/server -> deterministic fake gh
  #     provider という実経路を通す。判定は model の自己申告 transcript ではなく、
  #     fake gh が独立に書き出す state file（この run 専用の一意 title でのみ
  #     match する）を正本とする。 ---
  ISSUE_CREATE_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
  ISSUE_CREATE_RUN_ID="rt-${ISSUE_CREATE_TIMESTAMP}-$$"
  ISSUE_CREATE_TITLE="実装: runtime smoke issue_create canary ${ISSUE_CREATE_RUN_ID}"
  ISSUE_CREATE_BODY_FILE=$(mktemp)
  cat > "$ISSUE_CREATE_BODY_FILE" <<'ISSUE_CREATE_BODY_EOF'
## Acceptance Criteria

- [ ] AC1: runtime smoke canary body

## Verification Commands

```bash
test -n "ok"  # AC1
```

## Allowed Paths

- src/**
ISSUE_CREATE_BODY_EOF

  ISSUE_CREATE_FAKE_GH="$SCRIPT_DIR/tests/fixtures/fake_gh.py"
  chmod +x "$ISSUE_CREATE_FAKE_GH" 2>/dev/null
  ISSUE_CREATE_FAKE_GH_STATE=$(mktemp -u)

  ISSUE_CREATE_STDOUT=$(mktemp)
  ISSUE_CREATE_STDERR=$(mktemp)
  ISSUE_CREATE_PROMPT="You are running inside an automated, non-interactive runtime smoke test with no real user present. Use the Bash tool right now (an actual tool call, not a description) to run exactly: uv run --locked python3 .claude/skills/create-issue/scripts/create_issue_txn.py --repo squne121/loop-protocol --title \"${ISSUE_CREATE_TITLE}\" --body-file ${ISSUE_CREATE_BODY_FILE} --issue-kind \"\" --label-profile standard
Then print its exact stdout output verbatim and nothing else."

  FAKE_GH_STATE="$ISSUE_CREATE_FAKE_GH_STATE"   CLAUDE_GPT_ISSUE_CREATE_BRIDGE_GH_BIN="$ISSUE_CREATE_FAKE_GH"     "$SCRIPT_DIR/launch.sh" -- -p "$ISSUE_CREATE_PROMPT" --output-format text --no-session-persistence     --allowedTools "Bash"     >"$ISSUE_CREATE_STDOUT" 2>"$ISSUE_CREATE_STDERR"
  ISSUE_CREATE_CLAUDE_RC=$?

  rm -f "$ISSUE_CREATE_STDOUT" "$ISSUE_CREATE_STDERR" "$ISSUE_CREATE_BODY_FILE"

  ISSUE_CREATE_INDEPENDENT_OK="false"
  ISSUE_CREATE_ISSUE_NUMBER="null"
  if [ -f "$ISSUE_CREATE_FAKE_GH_STATE" ]; then
    ISSUE_CREATE_MATCH_JSON=$(python3 -c '
import json, sys
title, path = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)
except (OSError, json.JSONDecodeError):
    print("null")
    raise SystemExit(0)
for number, info in state.get("issues", {}).items():
    if info.get("title") == title:
        print(json.dumps({"number": int(number)}))
        raise SystemExit(0)
print("null")
' "$ISSUE_CREATE_TITLE" "$ISSUE_CREATE_FAKE_GH_STATE" 2>/dev/null)
    if [ -n "$ISSUE_CREATE_MATCH_JSON" ] && [ "$ISSUE_CREATE_MATCH_JSON" != "null" ]; then
      ISSUE_CREATE_INDEPENDENT_OK="true"
      ISSUE_CREATE_ISSUE_NUMBER=$(printf '%s' "$ISSUE_CREATE_MATCH_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["number"])' 2>/dev/null || echo null)
    fi
  fi
  rm -f "$ISSUE_CREATE_FAKE_GH_STATE"

  ISSUE_CREATE_STATUS="fail"
  ISSUE_CREATE_EXIT_CODE=1
  if [ "$ISSUE_CREATE_CLAUDE_RC" -eq 0 ] && [ "$ISSUE_CREATE_INDEPENDENT_OK" = "true" ] && [ "$SUT_GIT_DIRTY" = "false" ]; then
    ISSUE_CREATE_STATUS="pass"
    ISSUE_CREATE_EXIT_CODE=0
  fi

  ISSUE_CREATE_EVIDENCE_FILE="${EVIDENCE_DIR}/smoke-issue-create-${ISSUE_CREATE_TIMESTAMP}.json"
  python3 -c '
import json, sys
data = {
    "schema": "CLAUDE_GPT_SMOKE_RESULT_V1",
    "scenario": "issue_create",
    "status": sys.argv[1],
    "generated_at": sys.argv[2],
    "sut": {
        "launcher_path": sys.argv[3],
        "repository_root": sys.argv[4],
        "git_head": sys.argv[5],
        "git_dirty": sys.argv[6],
        "launch_sh_sha256": sys.argv[7],
        "lib_sh_sha256": sys.argv[8],
    },
    "issue_create": {
        "claude_exit_code": int(sys.argv[9]),
        "title": sys.argv[10],
        "independent_fake_gh_state_match_ok": sys.argv[11] == "true",
        "issue_number": (int(sys.argv[12]) if sys.argv[12] != "null" else None),
    },
}
print(json.dumps(data, ensure_ascii=False, indent=2))
' "$ISSUE_CREATE_STATUS" "$ISSUE_CREATE_TIMESTAMP"     "$SUT_LAUNCHER_PATH" "$SUT_REPO_ROOT" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$SUT_LIB_SH_SHA256"     "$ISSUE_CREATE_CLAUDE_RC" "$ISSUE_CREATE_TITLE" "$ISSUE_CREATE_INDEPENDENT_OK" "$ISSUE_CREATE_ISSUE_NUMBER"     > "$ISSUE_CREATE_EVIDENCE_FILE"

  if [ "$ISSUE_CREATE_STATUS" = "pass" ]; then
    echo "PASS: issue_create runtime scenario が成功しました（issue_number=${ISSUE_CREATE_ISSUE_NUMBER}）。証跡: ${ISSUE_CREATE_EVIDENCE_FILE}"
  else
    echo "FAIL: issue_create runtime scenario が失敗しました（claude_rc=${ISSUE_CREATE_CLAUDE_RC}, independent_ok=${ISSUE_CREATE_INDEPENDENT_OK}, git_dirty=${SUT_GIT_DIRTY}）。証跡: ${ISSUE_CREATE_EVIDENCE_FILE}"
  fi
  exit "$ISSUE_CREATE_EXIT_CODE"
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
#   実際の `POST /v1/messages` 成功と configured transport の実値は、proxy の構造化ログ
#   （codex_upstream_request_started / request_completed）を `transport_log.py` で
#   reqId 相関しながら厳密パースして確認する（Issue #2204 P0-2。自己申告ではなく
#   proxy 側の一次証跡。各 step ごとに fail-closed で判定し、1 request でも
#   websocket/auto/unknown transport が観測されれば全体を FAIL とする — 従来の
#   「1 回でも http が観測されれば PASS」という OR 判定は廃止した）。
# =========================================================================
TEXT_MARKER="CLAUDE_GPT_CANARY_TEXT_OK"
BASH_MARKER="CLAUDE_GPT_CANARY_BASH_OK"
SUBAGENT_MARKER="CLAUDE_GPT_CANARY_SUBAGENT_OK"

CANARY_AGENTS_JSON='{"canary":{"description":"canary smoke test subagent used only for claude-gpt launcher runtime smoke testing","prompt":"You are a canary subagent used only for launcher runtime smoke testing. When invoked, respond with exactly: '"${SUBAGENT_MARKER}"' and nothing else."}}'

# --- 単一 turn に複数ステップを詰め込むと model が一部のツール呼び出しを省略する挙動が
#     実機観測で確認されたため（PR #2162 実装セッション, 2026-08-14）、Bash tool /
#     Task tool / plain text の 3 観点を独立した `-p` invocation に分割し、それぞれの
#     proxy ログから一次証跡を確認する。合算判定は AND（全 step かつ全 request が http
#     confirmed であることを要求する。Issue #2204 iteration 2）。 ---

RC_LAST=0
MODEL_USED=""
PROVIDER_USED=""
CLEANUP_LAUNCHER_REPORTED_ALL=true
CLEANUP_PID_ABSENT_ALL=true
CLEANUP_SOCKET_ABSENT_ALL=true

# --- 全 step を横断した transport 判定の集計（Issue #2204 iteration 2 P0-2）。
#     各 step の transport_log.py 判定結果を AND で合成し、requests[] を連結する。 ---
TRANSPORT_ALL_OK=true
TRANSPORT_STARTED_TOTAL=0
TRANSPORT_HTTP_TOTAL=0
TRANSPORT_WEBSOCKET_TOTAL=0
TRANSPORT_AUTO_TOTAL=0
TRANSPORT_UNKNOWN_TOTAL=0
TRANSPORT_MALFORMED_TOTAL=0
REQUESTS_JSON_PARTS=""

# --- 実 claude-code-proxy 構造化ログの実パス（Issue #2204 iteration 2 P0-2 再検証で判明）。
#     `CLAUDE_GPT_PROXY_LOG`（launch.sh が env -i 起動時に生成する stdout/stderr 捕捉先）
#     は起動バナー等の非 JSON 行を含む上、構造化 JSON イベント（"request" /
#     "codex_upstream_request_started" / "request_completed"）そのものではない。
#     実際の構造化 JSONL は `claude-code-proxy serve` が独自に
#     `<XDG_STATE_HOME>/claude-code-proxy/proxy.log`（= `claude_gpt_proxy_state_dir`
#     配下の固定パス）へ書き出す。このファイルは launcher 起動ごとの一意ファイルではなく
#     `CLAUDE_GPT_HOME` 単位で累積・追記される長寿命ログのため、各 step の判定は
#     step 実行前のバイトオフセットを記録し、実行後にその差分（このステップ内で新規に
#     追記された行のみ）を切り出して渡す（過去の別 run・別 step のイベントを誤って
#     相関しないようにするため）。 ---
STRUCTURED_PROXY_LOG_PATH="$(claude_gpt_proxy_state_dir)/claude-code-proxy/proxy.log"

run_convo_step() {
  step_name="$1"
  step_prompt="$2"
  step_allowed_tools="$3"
  step_agents_json="$4"
  step_stdout_file=$(mktemp)
  step_stderr_file=$(mktemp)

  step_log_offset_before=0
  if [ -f "$STRUCTURED_PROXY_LOG_PATH" ]; then
    step_log_offset_before=$(wc -c < "$STRUCTURED_PROXY_LOG_PATH" 2>/dev/null || echo 0)
  fi

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

  # --- transport / http 判定を transport_log.py へ委譲する（Issue #2204 P0-2）。
  #     STRUCTURED_PROXY_LOG_PATH のこの step 内で新規追記された分だけを切り出して渡す。
  #     ログが存在しない・パーサ自体が失敗した場合も fail-closed（TRANSPORT_ALL_OK=false）。 ---
  STEP_TRANSPORT_JSON='{"ok":false,"reason":"structured_log_missing","transport":{"started_count":0,"http_count":0,"websocket_count":0,"auto_count":0,"unknown_count":0},"requests":[]}'
  step_structured_slice=$(mktemp)
  if [ -f "$STRUCTURED_PROXY_LOG_PATH" ]; then
    tail -c "+$((step_log_offset_before + 1))" "$STRUCTURED_PROXY_LOG_PATH" > "$step_structured_slice" 2>/dev/null
    STEP_TRANSPORT_JSON=$(python3 "$TRANSPORT_LOG_PARSER" "$step_structured_slice" 2>/dev/null)
    if [ -z "$STEP_TRANSPORT_JSON" ]; then
      STEP_TRANSPORT_JSON='{"ok":false,"reason":"parser_produced_no_output","transport":{"started_count":0,"http_count":0,"websocket_count":0,"auto_count":0,"unknown_count":0},"requests":[]}'
    fi
    if [ -z "$MODEL_USED" ]; then
      MODEL_USED=$(grep -o '"model":"[^"]*"' "$step_structured_slice" 2>/dev/null | head -n1 | cut -d: -f2 | tr -d '"')
      PROVIDER_USED=$(grep -o '"provider":"[^"]*"' "$step_structured_slice" 2>/dev/null | head -n1 | cut -d: -f2 | tr -d '"')
    fi
  fi
  rm -f "$step_structured_slice"

  STEP_TRANSPORT_OK=$(printf '%s' "$STEP_TRANSPORT_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("true" if d.get("ok") else "false")' 2>/dev/null || echo false)
  if [ "$STEP_TRANSPORT_OK" != "true" ]; then
    TRANSPORT_ALL_OK=false
  fi

  STEP_COUNTS=$(printf '%s' "$STEP_TRANSPORT_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
t = d.get("transport", {})
malformed = d.get("malformed_line_count", 0)
print(t.get("started_count", 0), t.get("http_count", 0), t.get("websocket_count", 0), t.get("auto_count", 0), t.get("unknown_count", 0), malformed)
' 2>/dev/null || echo "0 0 0 0 0 0")
  STEP_STARTED=$(printf '%s' "$STEP_COUNTS" | cut -d' ' -f1)
  STEP_HTTP=$(printf '%s' "$STEP_COUNTS" | cut -d' ' -f2)
  STEP_WS=$(printf '%s' "$STEP_COUNTS" | cut -d' ' -f3)
  STEP_AUTO=$(printf '%s' "$STEP_COUNTS" | cut -d' ' -f4)
  STEP_UNKNOWN=$(printf '%s' "$STEP_COUNTS" | cut -d' ' -f5)
  STEP_MALFORMED=$(printf '%s' "$STEP_COUNTS" | cut -d' ' -f6)

  TRANSPORT_STARTED_TOTAL=$((TRANSPORT_STARTED_TOTAL + STEP_STARTED))
  TRANSPORT_HTTP_TOTAL=$((TRANSPORT_HTTP_TOTAL + STEP_HTTP))
  TRANSPORT_WEBSOCKET_TOTAL=$((TRANSPORT_WEBSOCKET_TOTAL + STEP_WS))
  TRANSPORT_AUTO_TOTAL=$((TRANSPORT_AUTO_TOTAL + STEP_AUTO))
  TRANSPORT_UNKNOWN_TOTAL=$((TRANSPORT_UNKNOWN_TOTAL + STEP_UNKNOWN))
  TRANSPORT_MALFORMED_TOTAL=$((TRANSPORT_MALFORMED_TOTAL + STEP_MALFORMED))

  STEP_REQUESTS_ANNOTATED=$(printf '%s' "$STEP_TRANSPORT_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
step = sys.argv[1]
out = []
for r in d.get("requests", []):
    r = dict(r)
    r["step"] = step
    out.append(r)
print(json.dumps(out))
' "$step_name" 2>/dev/null || echo "[]")
  if [ "$STEP_REQUESTS_ANNOTATED" != "[]" ]; then
    inner=$(printf '%s' "$STEP_REQUESTS_ANNOTATED" | sed -e 's/^\[//' -e 's/\]$//')
    if [ -n "$inner" ]; then
      if [ -n "$REQUESTS_JSON_PARTS" ]; then
        REQUESTS_JSON_PARTS="${REQUESTS_JSON_PARTS},${inner}"
      else
        REQUESTS_JSON_PARTS="$inner"
      fi
    fi
  fi

  if [ -n "$STEP_PROXY_PID" ]; then
    if kill -0 "$STEP_PROXY_PID" 2>/dev/null; then
      CLEANUP_PID_ABSENT_ALL=false
    fi
    if ss -ltnp 2>/dev/null | grep -q "pid=${STEP_PROXY_PID},"; then
      CLEANUP_SOCKET_ABSENT_ALL=false
    fi
  fi
  if [ "$STEP_CLEANUP_OK" != "true" ]; then
    CLEANUP_LAUNCHER_REPORTED_ALL=false
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
  run_convo_step "bash" "You are running inside an automated, non-interactive runtime smoke test with no real user present. Use the Bash tool right now (an actual tool call, not a description) to run exactly: echo ${BASH_MARKER}
Then print its real stdout output verbatim on its own line." "Bash(echo *)" ""
  BASH_STDOUT="$STEP_STDOUT"
  BASH_RC="$STEP_RC"
  case "$BASH_STDOUT" in
    *"$BASH_MARKER"*) break ;;
  esac
done

# SubAgent canary も Bash canary と同様の実機観測された non-determinism（model が
# tool 呼び出し自体は行うが、最終応答に SubAgent 出力を verbatim で反映し損ねる挙動）が
# 生じうるため、marker 未検出時のみ bounded retry する（最大 3 回。fallback や
# 擬似成功判定は行わない — 毎回実際に Task tool を再試行する。Issue #2204 iteration 2
# 実機再検証, 2026-08-16）。
SUBAGENT_STDOUT=""
SUBAGENT_RC=1
subagent_attempt=0
while [ "$subagent_attempt" -lt 3 ]; do
  subagent_attempt=$((subagent_attempt + 1))
  run_convo_step "subagent" "You are running inside an automated, non-interactive runtime smoke test with no real user present. Use the Task tool right now (an actual tool call, not a description) to launch the subagent named canary with any instructions, then print its exact output verbatim." "Task" "$CANARY_AGENTS_JSON"
  SUBAGENT_STDOUT="$STEP_STDOUT"
  SUBAGENT_RC="$STEP_RC"
  case "$SUBAGENT_STDOUT" in
    *"$SUBAGENT_MARKER"*) break ;;
  esac
done

run_convo_step "text" "You are running inside an automated, non-interactive runtime smoke test with no real user present. Print exactly the following text and nothing else: ${TEXT_MARKER}" "" ""
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

# --- SubAgent lifecycle 一次証跡（Issue #2204 P0-3。部分対応 — Gap は PR body に明記する）:
#     現時点では標準出力 marker 検出のみを一次証跡とする。SubagentStart/SubagentStop hook
#     JSON との対応・複数 SubAgent 同時実行・同一 session 内複数 turn・session-log
#     metadata の確認は、`worktree-agent-runtime-smoke` skill 側の live authenticated
#     session 経由でのみ実施可能であり、本 launcher 単体スクリプトの scope 外として
#     gap のまま残す（PR body Gap セクション参照）。 ---
SUBAGENT_LIFECYCLE_VERIFIED=false

CONVO_CLEANUP_OK="$CLEANUP_LAUNCHER_REPORTED_ALL"
CLEANUP_INDEPENDENT_OK=true
if [ "$CLEANUP_PID_ABSENT_ALL" != "true" ] || [ "$CLEANUP_SOCKET_ABSENT_ALL" != "true" ]; then
  CLEANUP_INDEPENDENT_OK=false
fi
CONVO_RC=0
if [ "$BASH_RC" -ne 0 ] || [ "$SUBAGENT_RC" -ne 0 ] || [ "$TEXT_RC" -ne 0 ]; then
  CONVO_RC=1
fi

GIT_DIRTY_OK=false
if [ "$SUT_GIT_DIRTY" = "false" ]; then
  GIT_DIRTY_OK=true
fi

RUNTIME_CONVERSATION_OK=false
if [ "$CONVO_RC" -eq 0 ] \
  && [ "$TEXT_MARKER_OK" = "true" ] \
  && [ "$BASH_MARKER_OK" = "true" ] \
  && [ "$SUBAGENT_MARKER_OK" = "true" ] \
  && [ "$TRANSPORT_ALL_OK" = "true" ] \
  && [ "$CONVO_CLEANUP_OK" = "true" ] \
  && [ "$CLEANUP_INDEPENDENT_OK" = "true" ] \
  && [ "$GIT_DIRTY_OK" = "true" ]; then
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

REQUESTS_JSON_ARRAY="[${REQUESTS_JSON_PARTS}]"

cat > "$EVIDENCE_FILE" <<EVIDENCE_JSON_EOF
{
  "schema": "CLAUDE_GPT_SMOKE_RESULT_V1",
  "schema_version": 2,
  "status": "${STATUS}",
  "generated_at": "${TIMESTAMP}",
  "sut": {
    "launcher_path": "$(json_escape "$SUT_LAUNCHER_PATH")",
    "repository_root": "$(json_escape "$SUT_REPO_ROOT")",
    "git_head": "${SUT_GIT_HEAD}",
    "git_dirty": "${SUT_GIT_DIRTY}",
    "launch_sh_sha256": "${SUT_LAUNCH_SH_SHA256}",
    "lib_sh_sha256": "${SUT_LIB_SH_SHA256}",
    "runtime_smoke_sha256": "${SUT_RUNTIME_SMOKE_SHA256}"
  },
  "proxy": {
    "absolute_path": "$(json_escape "$SUT_PROXY_BIN")",
    "version": "$(json_escape "$SUT_PROXY_VERSION")",
    "sha256": "${SUT_PROXY_SHA256}"
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
  "transport": {
    "ok": ${TRANSPORT_ALL_OK},
    "started_count": ${TRANSPORT_STARTED_TOTAL},
    "http_count": ${TRANSPORT_HTTP_TOTAL},
    "websocket_count": ${TRANSPORT_WEBSOCKET_TOTAL},
    "auto_count": ${TRANSPORT_AUTO_TOTAL},
    "unknown_count": ${TRANSPORT_UNKNOWN_TOTAL},
    "malformed_line_count": ${TRANSPORT_MALFORMED_TOTAL}
  },
  "requests": ${REQUESTS_JSON_ARRAY},
  "subagents": {
    "marker_ok": ${SUBAGENT_MARKER_OK},
    "lifecycle_verified": ${SUBAGENT_LIFECYCLE_VERIFIED},
    "note": "SubagentStart/SubagentStop hook pairing・複数 SubAgent 同時実行・session-log metadata 確認は本スクリプトの scope 外（worktree-agent-runtime-smoke 経由の別途実施が必要。PR body Gap 参照）"
  },
  "runtime_conversation_check": {
    "ok": ${RUNTIME_CONVERSATION_OK},
    "claude_exit_code": ${CONVO_RC},
    "text_marker_ok": ${TEXT_MARKER_OK},
    "bash_tool_marker_ok": ${BASH_MARKER_OK},
    "subagent_marker_ok": ${SUBAGENT_MARKER_OK},
    "http_post_v1_messages_confirmed": ${TRANSPORT_ALL_OK},
    "codex_upstream_transport_http_confirmed": ${TRANSPORT_ALL_OK},
    "model_used": "$(json_escape "$MODEL_USED")",
    "provider_used": "$(json_escape "$PROVIDER_USED")",
    "git_dirty_ok": ${GIT_DIRTY_OK}
  },
  "cleanup": {
    "launcher_reported": ${CLEANUP_LAUNCHER_REPORTED_ALL},
    "pid_absent": ${CLEANUP_PID_ABSENT_ALL},
    "socket_absent": ${CLEANUP_SOCKET_ABSENT_ALL},
    "herdr_session_absent": "not_verified"
  }
}
EVIDENCE_JSON_EOF

if [ "$STATUS" = "pass" ]; then
  echo "PASS: claude-gpt launcher runtime smoke test（構造確認 + 対話 runtime 確認）が成功しました。証跡: ${EVIDENCE_FILE}"
else
  echo "FAIL: claude-gpt launcher runtime smoke test が失敗しました（structural_ok=${STRUCTURAL_OK}, runtime_conversation_ok=${RUNTIME_CONVERSATION_OK}, transport_ok=${TRANSPORT_ALL_OK}, git_dirty_ok=${GIT_DIRTY_OK}）。証跡: ${EVIDENCE_FILE}"
fi

exit "$EXIT_CODE"

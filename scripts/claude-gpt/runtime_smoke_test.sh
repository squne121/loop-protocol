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
#
# --- Issue #2651: `--spark-delegation` mode is retired ---
# GPT-5.3-Codex-Spark delegation has been retired repository-wide. This
# flag is rejected HERE, at argv pre-scan, with a deterministic non-zero
# exit -- BEFORE the main argument-parsing loop below (whose catch-all
# `*) shift ;;` branch would otherwise silently swallow an unrecognized
# flag and let execution fall through to ordinary smoke, incorrectly
# reporting success for a retired live-Spark request). No fallback
# execution, no promotion to ordinary smoke.
for _arg in "$@"; do
  case "$_arg" in
    --spark-delegation)
      echo "FAIL: --spark-delegation is retired. GPT-5.3-Codex-Spark delegation has been removed from this repository (Issue #2651); this flag no longer runs a live Spark E2E smoke and never falls through to ordinary smoke." >&2
      exit 2
      ;;
  esac
done

# --- Issue #2299 AC2/AC5: `--scenario issue_create` mode ---
# genuine `issue-creator` SubAgent が、fake gh provider に対して通常の
# create-issue skill procedure（dedupe read -> create_issue_txn.py 呼び出し
# -> readback）を isolated Claude-GPT session 内で完走できることを、outcome
# 指向（fake provider の state と authoritative readback の一致）で確認する
# mode。Runtime Verification Applicability（Issue #2299 本文）の
# `skip_conditions: SKIPしない` / `fallback_policy: 実行不能な場合はSKIPでは
# なくFAILとする` に従い、他 mode の env-availability SKIP gate（下記）を
# 通らず、実行不能な場合は exit 77 ではなく exit 1（FAIL）を返す。real
# GitHub Issue は一切作成しない（fake gh provider のみを使う）。
ISSUE_CREATE_SCENARIO=false
_prev_arg=""
for _arg in "$@"; do
  if [ "$_prev_arg" = "--scenario" ] && [ "$_arg" = "issue_create" ]; then
    ISSUE_CREATE_SCENARIO=true
  fi
  _prev_arg="$_arg"
done

# --- Issue #2278 AC2-AC11: `--scenario issue_to_impl` mode + generalized
# --scenario validation. AC11: an unknown --scenario value is rejected with
# exit 2 immediately (before any evidence directory/file is created) and
# NEVER falls back to the default smoke scenario below.
#
# PR #2325 fix_delta (P1-4): the previous "record the value that FOLLOWS the
# flag, whatever it is" two-pass scan silently accepted a MISSING flag value
# (the flag was simply the last token, or its "value" was actually the next
# recognized flag) and silently accepted DUPLICATE flags (last write wins,
# with no rejection). This is now a single strict while/shift loop: a
# missing value or a duplicate --scenario/--fixture/--evidence-out is
# rejected with exit 2, matching the unknown-scenario-value contract below.
# This loop is the LAST consumer of "$@" in this script (verified: no code
# after this point reads $1/$@), so shifting it away here is safe. ---
ISSUE_TO_IMPL_SCENARIO=false
SCENARIO_VALUE=""
FIXTURE_ARG_PATH=""
EVIDENCE_OUT_ARG_PATH=""
_scenario_flag_seen=false
_fixture_flag_seen=false
_evidence_out_flag_seen=false
while [ $# -gt 0 ]; do
  case "$1" in
    --scenario)
      if [ "$_scenario_flag_seen" = "true" ]; then
        echo "FAIL: --scenario specified more than once. Refusing to guess which value applies (Issue #2278 PR #2325 fix_delta P1-4)." >&2
        exit 2
      fi
      _scenario_flag_seen=true
      if [ $# -lt 2 ]; then
        echo "FAIL: --scenario requires a value." >&2
        exit 2
      fi
      SCENARIO_VALUE="$2"
      shift 2
      ;;
    --fixture)
      if [ "$_fixture_flag_seen" = "true" ]; then
        echo "FAIL: --fixture specified more than once. Refusing to guess which value applies (Issue #2278 PR #2325 fix_delta P1-4)." >&2
        exit 2
      fi
      _fixture_flag_seen=true
      if [ $# -lt 2 ]; then
        echo "FAIL: --fixture requires a value." >&2
        exit 2
      fi
      FIXTURE_ARG_PATH="$2"
      shift 2
      ;;
    --evidence-out)
      if [ "$_evidence_out_flag_seen" = "true" ]; then
        echo "FAIL: --evidence-out specified more than once. Refusing to guess which value applies (Issue #2278 PR #2325 fix_delta P1-4)." >&2
        exit 2
      fi
      _evidence_out_flag_seen=true
      if [ $# -lt 2 ]; then
        echo "FAIL: --evidence-out requires a value." >&2
        exit 2
      fi
      EVIDENCE_OUT_ARG_PATH="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if [ -n "$SCENARIO_VALUE" ]; then
  case "$SCENARIO_VALUE" in
    issue_create) : ;;
    issue_to_impl) ISSUE_TO_IMPL_SCENARIO=true ;;
    *)
      echo "FAIL: unknown --scenario value '${SCENARIO_VALUE}' (known values: issue_create, issue_to_impl). Refusing to fall back to the default smoke scenario (Issue #2278 AC11)." >&2
      exit 2
      ;;
  esac
fi

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

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

# --- Issue #2299 AC2/AC5: `--scenario issue_create` -- outcome-oriented,
#     fake-provider-backed live scenario. Runs BEFORE the shared
#     env-availability SKIP gate below: this mode never returns exit 77
#     (SKIP); it either PASSes or FAILs (fallback_policy, Issue #2299本文
#     Runtime Verification Applicability). ---
if [ "$ISSUE_CREATE_SCENARIO" = "true" ]; then
  ISSUE_CREATE_EVIDENCE_FILE="${EVIDENCE_DIR}/issue-create-scenario-${TIMESTAMP}.json"
  ISSUE_CREATE_TXN_SHA256=$(claude_gpt_sha256_file "$SUT_REPO_ROOT/.claude/skills/create-issue/scripts/create_issue_txn.py")
  SESSION_ID="issue-create-scenario-${TIMESTAMP}-$$"

  ISSUE_CREATE_PREFLIGHT_JSON=$("$SCRIPT_DIR/preflight.sh" --env-only)
  ISSUE_CREATE_PREFLIGHT_RC=$?
  if [ "$ISSUE_CREATE_PREFLIGHT_RC" -eq 3 ] || [ "$ISSUE_CREATE_PREFLIGHT_RC" -eq 4 ]; then
    FAIL_REASON="binary_unavailable"
    if [ "$ISSUE_CREATE_PREFLIGHT_RC" -eq 4 ]; then
      FAIL_REASON="chatgpt_subscription_auth_unavailable"
    fi
    printf '{"schema":"CLAUDE_GPT_ISSUE_CREATE_SCENARIO_RESULT_V1","status":"fail","reason":"%s","session_id":"%s","generated_at":"%s","sut":{"git_head":"%s","git_dirty":"%s","launch_sh_sha256":"%s","create_issue_txn_sha256":"%s"},"preflight_env_only":%s}\n' \
      "$FAIL_REASON" "$SESSION_ID" "$TIMESTAMP" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$ISSUE_CREATE_TXN_SHA256" "$ISSUE_CREATE_PREFLIGHT_JSON" > "$ISSUE_CREATE_EVIDENCE_FILE"
    echo "FAIL: issue_create scenario の実行環境が利用不能です（reason=${FAIL_REASON}）。runtime harness fallback_policy によりSKIPではなくFAILとします。証跡: ${ISSUE_CREATE_EVIDENCE_FILE}"
    exit 1
  fi

  FAKE_GH_FIXTURE="$SCRIPT_DIR/tests/fixtures/fake_gh.py"
  if [ ! -f "$FAKE_GH_FIXTURE" ]; then
    printf '{"schema":"CLAUDE_GPT_ISSUE_CREATE_SCENARIO_RESULT_V1","status":"fail","reason":"fake_gh_fixture_missing","session_id":"%s","generated_at":"%s"}\n' \
      "$SESSION_ID" "$TIMESTAMP" > "$ISSUE_CREATE_EVIDENCE_FILE"
    echo "FAIL: fake gh fixture が見つかりません（${FAKE_GH_FIXTURE}）。証跡: ${ISSUE_CREATE_EVIDENCE_FILE}"
    exit 1
  fi

  ISSUE_CREATE_WORKDIR=$(mktemp -d)
  FAKE_GH_STATE="${ISSUE_CREATE_WORKDIR}/fake_gh_state.json"
  FAKE_BIN_DIR="${ISSUE_CREATE_WORKDIR}/fake-bin"
  mkdir -p "$FAKE_BIN_DIR"
  FAKE_GH_WRAPPER="${FAKE_BIN_DIR}/gh"
  cat > "$FAKE_GH_WRAPPER" <<FAKE_GH_WRAPPER_EOF
#!/bin/sh
exec python3 "$FAKE_GH_FIXTURE" "\$@"
FAKE_GH_WRAPPER_EOF
  chmod +x "$FAKE_GH_WRAPPER"

  # NOTE: the target repo string given to the fake provider is deliberately
  # CLAUDE_GPT_TRUSTED_REPO (squne121/loop-protocol), not an arbitrary
  # placeholder name. `gh` is fully PATH-shadowed to fake_gh.py above, so no
  # network call to real GitHub happens regardless of the repo string -- but
  # the launcher's autoMode second-gate classifier (lib.sh
  # CLAUDE_GPT_AUTO_MODE_ALLOW_NARROW_LABEL) only allows Issue create/edit/
  # comment/close scoped to this exact repo name; an out-of-scope repo name
  # here causes the classifier to correctly deny the delegation (observed
  # live: "Issue 作成の委譲が権限設定により拒否されました"), which is a real
  # source of run-to-run flakiness unrelated to AC1/AC2 correctness.
  CANARY_TITLE="claude-gpt runtime_smoke_test issue_create scenario probe (${TIMESTAMP})"
  ISSUE_CREATE_STDERR_LOG="${ISSUE_CREATE_WORKDIR}/launch.stderr.log"
  ISSUE_CREATE_EXPECTED_REPO="squne121/loop-protocol"
  # Issue #2306 AC1: kept as a real variable (not inlined twice) so the
  # post-run readback below can assert exact body equality against the same
  # text the prompt actually requested, instead of only checking presence.
  CANARY_BODY="## Acceptance Criteria

- [ ] AC1: probe

## Verification Commands

\`\`\`bash
true  # AC1
\`\`\`

## Allowed Paths

- scripts/claude-gpt/**"

  ISSUE_CREATE_PROMPT="Use the Task tool to invoke the issue-creator SubAgent \
(subagent_type: \"issue-creator\"). Instruct it to follow the standard \
create-issue skill procedure (dedupe read via 'gh issue list', then \
create_issue_txn.py, then readback) to create exactly one new Issue in repo \
\"${ISSUE_CREATE_EXPECTED_REPO}\" with this exact title: \"${CANARY_TITLE}\" and this \
exact body:

${CANARY_BODY}

Do not perform any other GitHub read or write operation. After the SubAgent \
finishes, report only: DONE"

  # PATH の先頭に fake gh wrapper を挿入することで、SubAgent が呼ぶ dedupe の
  # raw \`gh issue list\` と create_issue_txn.py 既定の \`gh\` の両方が fake
  # provider へ解決される（real GitHub には一切到達しない）。
  ISSUE_CREATE_START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  CLAUDE_OUTPUT=$(FAKE_GH_STATE="$FAKE_GH_STATE" PATH="${FAKE_BIN_DIR}:${PATH}" \
    "$SCRIPT_DIR/launch.sh" -- -p "$ISSUE_CREATE_PROMPT" --output-format text --no-session-persistence \
    2>"$ISSUE_CREATE_STDERR_LOG")
  ISSUE_CREATE_LAUNCH_RC=$?
  ISSUE_CREATE_END_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  # --- authoritative readback: fake provider の state file を正本として判定する
  #     （SubAgent の自己申告テキストは observability 用途のみで、PASS/FAIL 判定
  #     には使わない）。 ---
  # Issue #2306 AC1: the PASS judgment is strengthened beyond "an Issue with
  # the expected title exists somewhere in state" -- it now requires exactly
  # one Issue in fake-provider state (total_issues == 1), and that that one
  # Issue's repo / title / body / state all match what the prompt requested
  # (in addition to launch_exit_code == 0, checked below).
  ISSUE_CREATE_READBACK_JSON=$(FAKE_GH_STATE="$FAKE_GH_STATE" CANARY_TITLE="$CANARY_TITLE" CANARY_BODY="$CANARY_BODY" ISSUE_CREATE_EXPECTED_REPO="$ISSUE_CREATE_EXPECTED_REPO" python3 <<'ISSUE_CREATE_READBACK_PY_EOF'
import json
import os

state_path = os.environ.get("FAKE_GH_STATE")
title = os.environ.get("CANARY_TITLE")
expected_body = os.environ.get("CANARY_BODY")
expected_repo = os.environ.get("ISSUE_CREATE_EXPECTED_REPO")
result = {
    "matched": False,
    "issue_number": None,
    "issue_url": None,
    "total_issues": 0,
    "title_match": False,
    "repo_match": False,
    "body_match": False,
    "state_open": False,
}
if state_path and os.path.exists(state_path):
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)
    issues = state.get("issues", {})
    result["total_issues"] = len(issues)
    for number, info in issues.items():
        if info.get("title") == title:
            result["title_match"] = True
            result["issue_number"] = int(number)
            result["issue_url"] = info.get("url")
            result["repo_match"] = info.get("repo") == expected_repo
            result["body_match"] = (info.get("body") or "").rstrip(chr(10)) == (expected_body or "").rstrip(chr(10))
            result["state_open"] = info.get("state") == "open"
            break
    result["matched"] = (
        result["total_issues"] == 1
        and result["title_match"]
        and result["repo_match"]
        and result["body_match"]
        and result["state_open"]
    )
print(json.dumps(result))
ISSUE_CREATE_READBACK_PY_EOF
)

  ISSUE_CREATE_MATCHED=$(printf '%s' "$ISSUE_CREATE_READBACK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["matched"])')
  ISSUE_CREATE_ISSUE_NUMBER=$(printf '%s' "$ISSUE_CREATE_READBACK_JSON" | python3 -c 'import json,sys; v=json.load(sys.stdin)["issue_number"]; print(v if v is not None else "null")')

  ISSUE_CREATE_STATUS="fail"
  if [ "$ISSUE_CREATE_LAUNCH_RC" -eq 0 ] && [ "$ISSUE_CREATE_MATCHED" = "True" ]; then
    ISSUE_CREATE_STATUS="pass"
  fi

  if [ "$ISSUE_CREATE_STATUS" = "fail" ] && [ -n "${CLAUDE_GPT_ISSUE_CREATE_SCENARIO_DEBUG_DIR:-}" ]; then
    mkdir -p "$CLAUDE_GPT_ISSUE_CREATE_SCENARIO_DEBUG_DIR"
    cp "$ISSUE_CREATE_STDERR_LOG" "$CLAUDE_GPT_ISSUE_CREATE_SCENARIO_DEBUG_DIR/launch.stderr.log" 2>/dev/null || true
    printf '%s' "$CLAUDE_OUTPUT" > "$CLAUDE_GPT_ISSUE_CREATE_SCENARIO_DEBUG_DIR/claude.stdout.log" 2>/dev/null || true
  fi

  printf '{"schema":"CLAUDE_GPT_ISSUE_CREATE_SCENARIO_RESULT_V1","status":"%s","session_id":"%s","generated_at":"%s","started_at":"%s","completed_at":"%s","launch_exit_code":%s,"fake_provider":{"repository_id":"squne121/loop-protocol","issue_number":%s,"readback":%s},"sut":{"git_head":"%s","git_dirty":"%s","launch_sh_sha256":"%s","create_issue_txn_sha256":"%s"}}\n' \
    "$ISSUE_CREATE_STATUS" "$SESSION_ID" "$TIMESTAMP" "$ISSUE_CREATE_START_TS" "$ISSUE_CREATE_END_TS" \
    "$ISSUE_CREATE_LAUNCH_RC" "$ISSUE_CREATE_ISSUE_NUMBER" "$ISSUE_CREATE_READBACK_JSON" \
    "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$ISSUE_CREATE_TXN_SHA256" > "$ISSUE_CREATE_EVIDENCE_FILE"

  rm -rf "$ISSUE_CREATE_WORKDIR"

  if [ "$ISSUE_CREATE_STATUS" = "pass" ]; then
    echo "PASS: issue_create scenario -- genuine issue-creator が fake provider 経由で create-issue workflow を完走しました（issue_number=${ISSUE_CREATE_ISSUE_NUMBER}）。証跡: ${ISSUE_CREATE_EVIDENCE_FILE}"
    exit 0
  fi
  echo "FAIL: issue_create scenario が完走しませんでした（launch_exit_code=${ISSUE_CREATE_LAUNCH_RC}, readback=${ISSUE_CREATE_READBACK_JSON}）。証跡: ${ISSUE_CREATE_EVIDENCE_FILE}"
  exit 1
fi

# --- Issue #2278 fix_delta (PR #2325 CI failure repair): P1-1 dirty-HEAD /
#     fixture-SHA256 integrity gate for `--scenario issue_to_impl`. This
#     MUST run BEFORE the shared binary-availability SKIP gate immediately
#     below -- it is a deterministic precondition that does not depend on
#     whether a `claude` binary is installed on this runner. Previously this
#     check lived only inside the later `ISSUE_TO_IMPL_SCENARIO` block
#     (after the shared gate), so on CI runners without `claude` the shared
#     gate short-circuited with `exit 77` (SKIP) before this check was ever
#     reached, masking a dirty SUT worktree / corrupted fixtures instead of
#     failing closed. The fixture path / SHA-256 values resolved here are
#     plain shell globals and are re-used (not re-computed) by the later
#     `ISSUE_TO_IMPL_SCENARIO` block further down this same script. ---
if [ "$ISSUE_TO_IMPL_SCENARIO" = "true" ]; then
  ISSUE_TO_IMPL_FIXTURE_PATH="${FIXTURE_ARG_PATH:-$SCRIPT_DIR/tests/fixtures/issue-2230-equivalent/issue.json}"
  ISSUE_TO_IMPL_FIXTURE_DIR=$(CDPATH= cd -- "$(dirname -- "$ISSUE_TO_IMPL_FIXTURE_PATH")" 2>/dev/null && pwd -P)
  ISSUE_TO_IMPL_EVIDENCE_FILE="${EVIDENCE_OUT_ARG_PATH:-${EVIDENCE_DIR}/issue-to-impl-${TIMESTAMP}.json}"
  ISSUE_TO_IMPL_PROMPT_PATH="${ISSUE_TO_IMPL_FIXTURE_DIR}/prompt.md"
  ISSUE_TO_IMPL_EXPECTED_PHASES_PATH="${ISSUE_TO_IMPL_FIXTURE_DIR}/expected-phases.json"

  if [ ! -f "$ISSUE_TO_IMPL_FIXTURE_PATH" ] || [ ! -f "$ISSUE_TO_IMPL_PROMPT_PATH" ] || [ ! -f "$ISSUE_TO_IMPL_EXPECTED_PHASES_PATH" ]; then
    printf '{"schema":"ISSUE_TO_IMPL_E2E_RESULT_V1","scenario":"issue_to_impl","test_verdict":"fail","terminal_result":null,"reason_code":"fixture_missing","reached_phase":null,"phase_trace":[],"resume_from":null,"generated_at":"%s","sut":{"git_head":"%s","git_dirty":%s,"claude_code_version":"unknown","proxy_version":"%s"}}\n' \
      "$TIMESTAMP" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_PROXY_VERSION" > "$ISSUE_TO_IMPL_EVIDENCE_FILE"
    echo "FAIL: issue_to_impl fixture が見つかりません（${ISSUE_TO_IMPL_FIXTURE_PATH} / prompt.md / expected-phases.json）。証跡: ${ISSUE_TO_IMPL_EVIDENCE_FILE}"
    exit 1
  fi

  ISSUE_TO_IMPL_PROMPT_SHA256=$(claude_gpt_sha256_file "$ISSUE_TO_IMPL_PROMPT_PATH")
  ISSUE_TO_IMPL_ISSUE_JSON_SHA256=$(claude_gpt_sha256_file "$ISSUE_TO_IMPL_FIXTURE_PATH")
  ISSUE_TO_IMPL_EXPECTED_PHASES_SHA256=$(claude_gpt_sha256_file "$ISSUE_TO_IMPL_EXPECTED_PHASES_PATH")

  # --- P1-1: enforce (not just record) clean integration HEAD + fixture
  #     SHA-256 integrity BEFORE any binary-availability check, and BEFORE
  #     Claude Code is ever launched. This is the single live copy of this
  #     gate; the later `ISSUE_TO_IMPL_SCENARIO` block does not re-check it
  #     (it can only be reached after this gate already passed). ---
  ISSUE_TO_IMPL_EARLY_GATE_OK=true
  ISSUE_TO_IMPL_EARLY_GATE_REASON_CODE="unknown"
  if ! printf '%s' "$SUT_GIT_HEAD" | grep -Eq '^[0-9a-f]{40}$'; then
    ISSUE_TO_IMPL_EARLY_GATE_OK=false
    ISSUE_TO_IMPL_EARLY_GATE_REASON_CODE="sut_git_head_not_40_char_sha"
  elif [ "$SUT_GIT_DIRTY" != "false" ]; then
    ISSUE_TO_IMPL_EARLY_GATE_OK=false
    ISSUE_TO_IMPL_EARLY_GATE_REASON_CODE="sut_git_dirty"
  elif [ -z "$ISSUE_TO_IMPL_PROMPT_SHA256" ] || [ -z "$ISSUE_TO_IMPL_ISSUE_JSON_SHA256" ] || [ -z "$ISSUE_TO_IMPL_EXPECTED_PHASES_SHA256" ]; then
    ISSUE_TO_IMPL_EARLY_GATE_OK=false
    ISSUE_TO_IMPL_EARLY_GATE_REASON_CODE="fixture_sha256_empty"
  fi

  if [ "$ISSUE_TO_IMPL_EARLY_GATE_OK" != "true" ]; then
    printf '{"schema":"ISSUE_TO_IMPL_E2E_RESULT_V1","scenario":"issue_to_impl","test_verdict":"fail","terminal_result":"human_judgment_required","reason_code":"%s","reached_phase":null,"phase_trace":[],"resume_from":null,"generated_at":"%s","sut":{"git_head":"%s","git_dirty":%s,"claude_code_version":"unknown","proxy_version":"%s"}}\n' \
      "$ISSUE_TO_IMPL_EARLY_GATE_REASON_CODE" "$TIMESTAMP" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_PROXY_VERSION" > "$ISSUE_TO_IMPL_EVIDENCE_FILE"
    echo "FAIL: issue_to_impl scenario の P1-1 preflight gate に失敗しました（reason_code=${ISSUE_TO_IMPL_EARLY_GATE_REASON_CODE}）。証跡: ${ISSUE_TO_IMPL_EVIDENCE_FILE}"
    exit 1
  fi
fi

# --- 環境可用性判定（バイナリ / ChatGPT subscription 認証）。ディレクトリ/設定はまだ作らない。 ---
PREFLIGHT_ENV_JSON=$("$SCRIPT_DIR/preflight.sh" --env-only)
PREFLIGHT_ENV_RC=$?

if [ "$PREFLIGHT_ENV_RC" -eq 3 ] || [ "$PREFLIGHT_ENV_RC" -eq 4 ]; then
  SKIP_REASON="binary_unavailable"
  if [ "$PREFLIGHT_ENV_RC" -eq 4 ]; then
    SKIP_REASON="chatgpt_subscription_auth_unavailable"
  fi
  if [ "$ISSUE_TO_IMPL_SCENARIO" = "true" ]; then
    ISSUE_TO_IMPL_SKIP_EVIDENCE="${EVIDENCE_OUT_ARG_PATH:-${EVIDENCE_DIR}/issue-to-impl-${TIMESTAMP}.json}"
    printf '{"schema":"ISSUE_TO_IMPL_E2E_RESULT_V1","scenario":"issue_to_impl","test_verdict":"skip","terminal_result":null,"reason_code":"%s","reached_phase":null,"phase_trace":[],"resume_from":null,"generated_at":"%s","sut":{"git_head":"%s","git_dirty":%s,"claude_code_version":"unknown","proxy_version":"%s"}}\n' \
      "$SKIP_REASON" "$TIMESTAMP" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_PROXY_VERSION" > "$ISSUE_TO_IMPL_SKIP_EVIDENCE"
    echo "SKIP: ${SKIP_REASON} のため --scenario issue_to_impl live smoke を実行できません（Runtime Verification Applicability の skip_conditions に該当）。証跡: ${ISSUE_TO_IMPL_SKIP_EVIDENCE}"
    exit 77
  fi
  printf '{"schema":"CLAUDE_GPT_SMOKE_RESULT_V1","status":"skip","reason":"%s","preflight_env_only":%s,"generated_at":"%s","sut":{"launcher_path":"%s","repository_root":"%s","git_head":"%s","git_dirty":"%s","launch_sh_sha256":"%s","lib_sh_sha256":"%s","runtime_smoke_sha256":"%s"},"proxy":{"absolute_path":"%s","version":"%s","sha256":"%s"}}\n' \
    "$SKIP_REASON" "$PREFLIGHT_ENV_JSON" "$TIMESTAMP" \
    "$SUT_LAUNCHER_PATH" "$SUT_REPO_ROOT" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$SUT_LIB_SH_SHA256" "$SUT_RUNTIME_SMOKE_SHA256" \
    "$SUT_PROXY_BIN" "$SUT_PROXY_VERSION" "$SUT_PROXY_SHA256" > "$EVIDENCE_FILE"
  echo "SKIP: ${SKIP_REASON} のため runtime smoke test を実行できません。証跡: ${EVIDENCE_FILE}"
  exit 77
fi

# --- Issue #2274 AC5/AC6/AC7/AC17/AC18 `--spark-delegation` live E2E
#     harness: retired (Issue #2651). The `--spark-delegation` flag is now
#     rejected immediately at argv pre-scan (see near the top of this
#     script) with a deterministic non-zero exit, before this point is
#     ever reached -- so the entire live-harness implementation that used
#     to live here (the live evidence-schema builder, proxy-log/hook
#     bounded correlation, etc.) has been removed as dead code.

# =========================================================================
# Issue #2278 AC2-AC11: `--scenario issue_to_impl` mode
#
# PR #2325 fix_delta (REQUEST_CHANGES, reviewed_head_sha
# 4937a1b9225a68faec34c20ae553b4e914dc6dee): this scenario's scope claim is
# NARROWED per the reviewer's own accepted "最小修正構成" --
#   workflow-start preflight -> live fixture readback -> root-router
#   implementation-entry probe
# `issue_contract_repair` / `fresh_review` / full "E2E" wording are retired;
# phases below are renamed to `fixture_contract_shape_check` /
# `live_fixture_readback` to match what they actually verify (P0-2). This
# scenario is NOT a substitute for actually running `issue-contract-review`
# against a fully faked GitHub environment -- it is a bounded liveness +
# wiring probe of five specific hand-offs:
#
#   1. workflow_capability_preflight -- now delegates decision
#      classification to the SAME canonical `workflow_start_entry.run()`
#      function `command_registry.py`'s bare `preflight.run` reaches in
#      production (Issue #2311), instead of shelling out to the raw
#      `workflow_capability_preflight.py` producer and re-implementing a
#      looser `decision == "blocked"` string comparison (P0-1/P0-4 fix).
#      `invoke_inner_preflight_fn` is replaced with a call-counting spy so
#      this phase classifies ready/degraded/blocked/malformed using
#      production's own fail-closed logic WITHOUT actually executing the
#      much heavier `run_refinement_preflight.py` chain end-to-end against a
#      hand-built fake GitHub environment -- that remains separate, heavier
#      scope (the reviewer explicitly accepts narrowing when the full chain
#      is too costly to fake: "実 issue-contract-review を fake GitHub 上で
#      動かす対応が重すぎる場合は…").
#   2. spark_delegation -- always not_applicable (this scenario never
#      declares a Spark requirement; unchanged).
#   3. fixture_contract_shape_check (was issue_contract_repair) -- a purely
#      static substring check of the fixture Issue body against the 5
#      section headings `issue-contract-review` itself inspects FIRST. This
#      does NOT run `issue-contract-review` (no dependency/VC-preflight/
#      AC-VC-mapping/runtime-verification-applicability/worktree-collision/
#      product-spec checks) and must never be described as such.
#   4. live_fixture_readback (was fresh_review) -- one real `claude`
#      invocation via `launch.sh`, using `--output-format json` (a
#      structured envelope instead of raw free-text stdout) AND a
#      `fake_gh.py` call-trace check that `gh issue view <fixture issue
#      number>` against the fixture repo genuinely happened during that
#      invocation. A marker string alone -- even inside the structured JSON
#      envelope -- is never sufficient; the call trace is the causal proof
#      that Bash/`gh` tool-use actually occurred (P0-2 fix).
#   5. impl_review_loop_entry -- now genuinely calls
#      `root_entry_router.run_root_transition()` (the actual production root
#      entry point), using the existing `FileBackedFakeGitHubEntryTransport`
#      / canned-contract-reviewer test seams and a no-op call-counting spy
#      for `invoke_step1`, instead of a harness-synthesized
#      `expected_block` the moment phase 4's marker was observed (P0-3 fix).
#      No real git worktree, branch, or PR is ever created by this phase.
#      When the live route resolves to `invoke_impl_review_loop` and the
#      spy was actually invoked exactly once, `terminal_result` is the more
#      precise `implementation_not_authorized` (this harness deliberately
#      stops short of real mutation) rather than the vaguer `blocked`
#      (reviewer's explicit suggestion).
#
# Additional fix_delta items:
#   - P1-1: `git_head` (40-char sha) / `git_dirty` (must be `false`) /
#     the 3 fixture SHA-256 values are now ENFORCED before Claude Code is
#     ever launched (previously only recorded in evidence, never gating).
#   - P1-3: `expected-phases.json` is now loaded and used as a runtime
#     oracle -- exact phase order, allowed status values, `reached_phase`
#     coherence, and terminal_result/last-phase-status coherence are all
#     checked BEFORE `test_verdict: pass` can be emitted (previously only
#     SHA-256'd into evidence and never consulted).
#   - evidence JSON is now built with Python's `json.dumps` (embedded
#     Python, not shell `printf` string interpolation) and re-parsed from
#     disk before this scenario exits, so a malformed evidence write fails
#     closed instead of silently shipping unparsable JSON.
#
# terminal_result（ISSUE_TO_IMPL_E2E_RESULT_V1.terminal_result, Issue #2278
# 本文で定義された typed terminal result の全集合）:
#   draft_pr_ready | blocked | human_judgment_required | implementation_not_authorized
# 本 smoke harness は実 mutation を実行しないため `draft_pr_ready` は現行
# 実装では到達しない（phase 5 は `implementation_not_authorized` / `blocked`
# のいずれかで終端する）。
#
# 単独の早期 blocked（phase 1-4 で terminal に達した場合）は Issue #2278
# 本文 fallback_policy の指示どおり PASS へ昇格させない（AC4）。
# =========================================================================
if [ "$ISSUE_TO_IMPL_SCENARIO" = "true" ]; then
  # NOTE: ISSUE_TO_IMPL_FIXTURE_PATH / _FIXTURE_DIR / _EVIDENCE_FILE /
  # _PROMPT_PATH / _EXPECTED_PHASES_PATH, the 3 fixture SHA-256 values, and
  # the fixture-missing / P1-1 dirty-HEAD-and-fixture-integrity gate itself
  # were already resolved and enforced earlier in this script (before the
  # shared binary-availability SKIP gate) -- see the "Issue #2278 fix_delta"
  # block above. Reaching this point means that gate already PASSed, so it
  # is intentionally not re-checked here (single live copy of the gate
  # avoids two copies disagreeing).
  ISSUE_TO_IMPL_CLAUDE_BIN=$(claude_gpt_resolve_claude_bin)
  ISSUE_TO_IMPL_CLAUDE_CODE_VERSION="unknown"
  if [ -n "$ISSUE_TO_IMPL_CLAUDE_BIN" ]; then
    ISSUE_TO_IMPL_CLAUDE_CODE_VERSION=$("$ISSUE_TO_IMPL_CLAUDE_BIN" --version 2>/dev/null | head -n1)
    if [ -z "$ISSUE_TO_IMPL_CLAUDE_CODE_VERSION" ]; then
      ISSUE_TO_IMPL_CLAUDE_CODE_VERSION="unknown"
    fi
  fi

  ISSUE_TO_IMPL_WORKDIR=$(mktemp -d)
  FAKE_GH_FIXTURE="$SCRIPT_DIR/tests/fixtures/fake_gh.py"
  FAKE_GH_STATE="${ISSUE_TO_IMPL_WORKDIR}/fake_gh_state.json"
  FAKE_BIN_DIR="${ISSUE_TO_IMPL_WORKDIR}/fake-bin"
  mkdir -p "$FAKE_BIN_DIR"
  FAKE_GH_WRAPPER="${FAKE_BIN_DIR}/gh"
  cat > "$FAKE_GH_WRAPPER" <<FAKE_GH_WRAPPER_ITI_EOF
#!/bin/sh
exec python3 "$FAKE_GH_FIXTURE" "\$@"
FAKE_GH_WRAPPER_ITI_EOF
  chmod +x "$FAKE_GH_WRAPPER"

  ISSUE_TO_IMPL_PHASE_TRACE_FILE="${ISSUE_TO_IMPL_WORKDIR}/phase_trace.jsonl"
  : > "$ISSUE_TO_IMPL_PHASE_TRACE_FILE"
  _iti_add_phase() {
    printf '{"phase":"%s","status":"%s"}\n' "$1" "$2" >> "$ISSUE_TO_IMPL_PHASE_TRACE_FILE"
  }

  # reached_phase/terminal_result are kept as PLAIN (unquoted) shell strings
  # throughout this scenario -- empty string means JSON null. They are only
  # ever JSON-encoded once, by the embedded-Python evidence builder at the
  # very end (P0-5 style fix: no more manual printf JSON-string quoting).
  ISSUE_TO_IMPL_TERMINAL_RESULT=""
  ISSUE_TO_IMPL_REACHED_PHASE=""
  ISSUE_TO_IMPL_REASON_CODE="unknown"
  ISSUE_TO_IMPL_TEST_VERDICT="fail"

  # --- P1-1 gate already enforced earlier (see NOTE above); this flag is
  #     kept only so the surrounding if/else control flow below (Phase 1-5)
  #     is unchanged. ---
  ISSUE_TO_IMPL_PREFLIGHT_GATE_OK=true

  if [ "$ISSUE_TO_IMPL_PREFLIGHT_GATE_OK" != "true" ]; then
    ISSUE_TO_IMPL_TERMINAL_RESULT="human_judgment_required"
  else
    # --- Fixture issue number is needed by BOTH Phase 1 (capability
    #     request bookkeeping) and Phase 5 (root-router probe), so it is
    #     resolved once, up front, instead of being re-derived per phase. ---
    ISSUE_TO_IMPL_FIXTURE_ISSUE_NUMBER=$(python3 -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
issues = data.get("issues", {})
print(next(iter(issues.keys())) if issues else "")
' "$ISSUE_TO_IMPL_FIXTURE_PATH")

    if [ -z "$ISSUE_TO_IMPL_FIXTURE_ISSUE_NUMBER" ]; then
      ISSUE_TO_IMPL_TERMINAL_RESULT="human_judgment_required"
      ISSUE_TO_IMPL_REASON_CODE="fixture_issue_missing"
    else
      # --- Phase 1: workflow_capability_preflight -- canonical
      #     `preflight.run` first-hop via `workflow_start_entry.run()`
      #     (P0-1/P0-4 fix; see block comment above). ---
      ISSUE_TO_IMPL_PLANNED_OPS_FILE="${ISSUE_TO_IMPL_WORKDIR}/planned_operations.json"
      cat > "$ISSUE_TO_IMPL_PLANNED_OPS_FILE" <<'PLANNED_OPS_ITI_EOF'
[
  {"phase": "fixture_contract_shape_check", "actor_role": "issue-refinement-loop", "operation": "issue_comment", "requires_mutation": true},
  {"phase": "impl_review_loop_entry", "actor_role": "open-pr", "operation": "pr_create", "requires_mutation": true}
]
PLANNED_OPS_ITI_EOF

      ISSUE_TO_IMPL_SKILLS_SCRIPTS_DIR="${SUT_REPO_ROOT}/.claude/skills/issue-refinement-loop/scripts"

      ISSUE_TO_IMPL_WSE_JSON=$(FAKE_GH_AUTH_OK=1 FAKE_GH_REPO_READ_OK=1 FAKE_GH_STATE="$FAKE_GH_STATE" PATH="${FAKE_BIN_DIR}:${PATH}" \
        python3 - "$ISSUE_TO_IMPL_PLANNED_OPS_FILE" "$ISSUE_TO_IMPL_SKILLS_SCRIPTS_DIR" "$ISSUE_TO_IMPL_FIXTURE_ISSUE_NUMBER" "squne121/loop-protocol" 2>"${ISSUE_TO_IMPL_WORKDIR}/workflow_start_entry.stderr.log" <<'WORKFLOW_START_ENTRY_ITI_PY_EOF'
import json
import sys

planned_ops_path, skills_scripts_dir, issue_number, repo = sys.argv[1:5]
issue_number = int(issue_number)

sys.path.insert(0, skills_scripts_dir)
import workflow_start_entry as wse  # noqa: E402

with open(planned_ops_path, encoding="utf-8") as fh:
    planned_operations_json = fh.read()

# Counting spy: proves `workflow_start_entry.run()`'s OWN ready/degraded
# branch was reached (i.e. its own fail-closed classification, not a shell
# re-implementation, decided to proceed) without actually executing the
# much heavier `run_refinement_preflight.py` chain end-to-end (P0-1
# module-level comment; same idiom as Phase 5's root_entry_router spy).
inner_calls = []


def _spy_invoke_inner_preflight(*, issue_number, repo):
    inner_calls.append({"issue_number": issue_number, "repo": repo})
    return 0


result, exit_code = wse.run(
    issue_number=issue_number,
    repo=repo,
    spark_mode=None,
    spark_fallback="forbidden",
    planned_operations_json=planned_operations_json,
    invoke_inner_preflight_fn=_spy_invoke_inner_preflight,
)
print(json.dumps({
    "result": result,
    "exit_code": exit_code,
    "inner_preflight_spy_call_count": len(inner_calls),
}))
WORKFLOW_START_ENTRY_ITI_PY_EOF
)
      ISSUE_TO_IMPL_WSE_RC=$?
      ISSUE_TO_IMPL_WSE_STATUS=$(printf '%s' "$ISSUE_TO_IMPL_WSE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["status"])' 2>/dev/null || echo "")
      ISSUE_TO_IMPL_WSE_DECISION=$(printf '%s' "$ISSUE_TO_IMPL_WSE_JSON" | python3 -c 'import json,sys; v=json.load(sys.stdin)["result"]["decision"]; print(v if v is not None else "")' 2>/dev/null || echo "")

      if [ "$ISSUE_TO_IMPL_WSE_RC" -ne 0 ] || [ -z "$ISSUE_TO_IMPL_WSE_STATUS" ]; then
        _iti_add_phase "workflow_capability_preflight" "failed"
        ISSUE_TO_IMPL_REACHED_PHASE="workflow_capability_preflight"
        ISSUE_TO_IMPL_TERMINAL_RESULT="human_judgment_required"
        ISSUE_TO_IMPL_REASON_CODE="workflow_start_entry_execution_error"
      else
        _iti_add_phase "workflow_capability_preflight" "completed"

        # --- Phase 2: spark_delegation（本シナリオでは常に not_applicable） ---
        _iti_add_phase "spark_delegation" "not_applicable"

        # `status` is a closed 3-value enum produced by
        # `workflow_start_entry._compact_result()`: "blocked" |
        # "ready" | "inner_preflight_failed" (unreachable here since the
        # spy always returns 0). Exhaustive case/esac -- any OTHER value
        # (a wiring regression in this heredoc itself, or a future schema
        # change) explicitly fails closed instead of silently proceeding
        # (P0-4 fix: no more open-ended "not exactly the string blocked"
        # fallthrough).
        case "$ISSUE_TO_IMPL_WSE_STATUS" in
          blocked)
            ISSUE_TO_IMPL_REACHED_PHASE="workflow_capability_preflight"
            ISSUE_TO_IMPL_TERMINAL_RESULT="blocked"
            ISSUE_TO_IMPL_REASON_CODE="workflow_capability_preflight_blocked:${ISSUE_TO_IMPL_WSE_DECISION:-unknown}"
            ;;
          ready)
            ISSUE_TO_IMPL_PHASE1_READY=true
            ;;
          *)
            ISSUE_TO_IMPL_REACHED_PHASE="workflow_capability_preflight"
            ISSUE_TO_IMPL_TERMINAL_RESULT="human_judgment_required"
            ISSUE_TO_IMPL_REASON_CODE="workflow_start_entry_unexpected_status:${ISSUE_TO_IMPL_WSE_STATUS}"
            ;;
        esac

        if [ "${ISSUE_TO_IMPL_PHASE1_READY:-false}" = "true" ]; then
          # --- Phase 3: fixture_contract_shape_check (was
          #     issue_contract_repair) -- deterministic, static substring
          #     check of the fixture Issue body. This is NOT
          #     issue-contract-review (P0-2). ---
          ISSUE_TO_IMPL_CONTRACT_JSON=$(python3 - "$ISSUE_TO_IMPL_FIXTURE_PATH" <<'CONTRACT_CHECK_ITI_PY_EOF'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
issues = data.get("issues", {})
if not issues:
    print(json.dumps({"ok": False, "missing": ["issues"], "issue_number": None}))
else:
    number, info = next(iter(issues.items()))
    body = info.get("body", "")
    required = [
        "## Outcome",
        "## Acceptance Criteria",
        "## Verification Commands",
        "## Allowed Paths",
        "## Stop Conditions",
    ]
    missing = [h for h in required if h not in body]
    print(json.dumps({"ok": len(missing) == 0, "missing": missing, "issue_number": int(number)}))
CONTRACT_CHECK_ITI_PY_EOF
)
          ISSUE_TO_IMPL_CONTRACT_OK=$(printf '%s' "$ISSUE_TO_IMPL_CONTRACT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])' 2>/dev/null || echo False)

          if [ "$ISSUE_TO_IMPL_CONTRACT_OK" != "True" ]; then
            _iti_add_phase "fixture_contract_shape_check" "expected_block"
            ISSUE_TO_IMPL_REACHED_PHASE="fixture_contract_shape_check"
            ISSUE_TO_IMPL_TERMINAL_RESULT="blocked"
            ISSUE_TO_IMPL_REASON_CODE="fixture_contract_shape_incomplete"
          else
            _iti_add_phase "fixture_contract_shape_check" "completed"

            # --- Phase 4: live_fixture_readback (was fresh_review) -- one
            #     real `claude` invocation via `launch.sh`, structured JSON
            #     output, AND a fake_gh.py call-trace check that `gh issue
            #     view <fixture issue number>` genuinely happened (P0-2). ---
            ISSUE_TO_IMPL_PROMPT_TEXT=$(cat "$ISSUE_TO_IMPL_PROMPT_PATH")
            ISSUE_TO_IMPL_STDERR_LOG="${ISSUE_TO_IMPL_WORKDIR}/launch.stderr.log"
            ISSUE_TO_IMPL_CLAUDE_OUTPUT=$(FAKE_GH_STATE="$FAKE_GH_STATE" FAKE_GH_SEED_ISSUES_PATH="$ISSUE_TO_IMPL_FIXTURE_PATH" PATH="${FAKE_BIN_DIR}:${PATH}" \
              "$SCRIPT_DIR/launch.sh" -- -p "$ISSUE_TO_IMPL_PROMPT_TEXT" --output-format json --no-session-persistence \
              2>"$ISSUE_TO_IMPL_STDERR_LOG")
            ISSUE_TO_IMPL_LAUNCH_RC=$?
            ISSUE_TO_IMPL_CLAUDE_OUTPUT_FILE="${ISSUE_TO_IMPL_WORKDIR}/claude_output.raw"
            printf '%s' "$ISSUE_TO_IMPL_CLAUDE_OUTPUT" > "$ISSUE_TO_IMPL_CLAUDE_OUTPUT_FILE"

            ISSUE_TO_IMPL_READBACK_JSON=$(python3 - "$ISSUE_TO_IMPL_FIXTURE_ISSUE_NUMBER" "squne121/loop-protocol" "$FAKE_GH_STATE" "$ISSUE_TO_IMPL_CLAUDE_OUTPUT_FILE" <<'LIVE_FIXTURE_READBACK_ITI_PY_EOF'
import json
import re
import sys

fixture_issue_number, repo, fake_gh_state_path, claude_output_path = sys.argv[1:5]
with open(claude_output_path, encoding="utf-8") as fh:
    raw_stdout = fh.read()

marker_issue = None
marker_complete = None
structured_output_ok = False
try:
    envelope = json.loads(raw_stdout)
except (json.JSONDecodeError, ValueError):
    envelope = None

# Claude Code `--output-format json` (non-interactive, as invoked here via
# `launch.sh`) emits EITHER a single top-level result object, or a JSON
# ARRAY of the full session's structured events (system/assistant/user/
# result), depending on invocation mode -- observed empirically to be the
# latter shape in this harness's launch path. Normalize both shapes to the
# one `type: "result"` event, which is the schema-known field carrying the
# final assistant text (`result`) and the `is_error` flag.
result_event = None
if isinstance(envelope, dict):
    result_event = envelope
elif isinstance(envelope, list):
    for item in envelope:
        if isinstance(item, dict) and item.get("type") == "result":
            result_event = item
            break

if result_event is not None:
    # Extracting the deterministic marker from THIS isolated, schema-known
    # field is more precise than the previous naive regex-over-raw-stdout
    # approach (which could match spurious text anywhere in a raw stream)
    # -- but, per the review, structured output alone still does not prove
    # tool execution happened, hence the separate call-trace check below.
    result_text = result_event.get("result")
    if isinstance(result_text, str):
        structured_output_ok = result_event.get("is_error") is not True
        m = re.search(
            r"ISSUE_TO_IMPL_FRESH_REVIEW_OK issue_number=(\d+) contract_complete=(true|false)",
            result_text,
        )
        if m:
            marker_issue = m.group(1)
            marker_complete = m.group(2)

call_trace_ok = False
try:
    with open(fake_gh_state_path, encoding="utf-8") as fh:
        state = json.load(fh)
    for call in state.get("calls", []):
        if (
            call.get("operation") == "issue_view"
            and call.get("repo") == repo
            and str(call.get("number")) == fixture_issue_number
        ):
            call_trace_ok = True
            break
except (OSError, json.JSONDecodeError, ValueError):
    call_trace_ok = False

print(json.dumps({
    "envelope_parsed": envelope is not None,
    "structured_output_ok": structured_output_ok,
    "marker_issue": marker_issue,
    "marker_complete": marker_complete,
    "call_trace_ok": call_trace_ok,
}))
LIVE_FIXTURE_READBACK_ITI_PY_EOF
)
            ISSUE_TO_IMPL_READBACK_ENVELOPE_PARSED=$(printf '%s' "$ISSUE_TO_IMPL_READBACK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["envelope_parsed"])' 2>/dev/null || echo False)
            ISSUE_TO_IMPL_READBACK_STRUCTURED_OK=$(printf '%s' "$ISSUE_TO_IMPL_READBACK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["structured_output_ok"])' 2>/dev/null || echo False)
            ISSUE_TO_IMPL_READBACK_MARKER_ISSUE=$(printf '%s' "$ISSUE_TO_IMPL_READBACK_JSON" | python3 -c 'import json,sys; v=json.load(sys.stdin)["marker_issue"]; print(v if v is not None else "")' 2>/dev/null || echo "")
            ISSUE_TO_IMPL_READBACK_MARKER_COMPLETE=$(printf '%s' "$ISSUE_TO_IMPL_READBACK_JSON" | python3 -c 'import json,sys; v=json.load(sys.stdin)["marker_complete"]; print(v if v is not None else "")' 2>/dev/null || echo "")
            ISSUE_TO_IMPL_READBACK_CALL_TRACE_OK=$(printf '%s' "$ISSUE_TO_IMPL_READBACK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["call_trace_ok"])' 2>/dev/null || echo False)

            if [ "$ISSUE_TO_IMPL_LAUNCH_RC" -eq 0 ] \
              && [ "$ISSUE_TO_IMPL_READBACK_ENVELOPE_PARSED" = "True" ] \
              && [ "$ISSUE_TO_IMPL_READBACK_STRUCTURED_OK" = "True" ] \
              && [ -n "$ISSUE_TO_IMPL_READBACK_MARKER_ISSUE" ] \
              && [ "$ISSUE_TO_IMPL_READBACK_MARKER_ISSUE" = "$ISSUE_TO_IMPL_FIXTURE_ISSUE_NUMBER" ] \
              && [ "$ISSUE_TO_IMPL_READBACK_MARKER_COMPLETE" = "true" ] \
              && [ "$ISSUE_TO_IMPL_READBACK_CALL_TRACE_OK" = "True" ]; then
              _iti_add_phase "live_fixture_readback" "completed"

              # --- Phase 5: impl_review_loop_entry -- genuinely calls
              #     `root_entry_router.run_root_transition()` with a no-op
              #     counting spy for `invoke_step1` (P0-3 fix). No real git
              #     worktree, branch, or PR is created. ---
              ISSUE_TO_IMPL_FAKE_TRANSPORT_FILE="${ISSUE_TO_IMPL_WORKDIR}/root_router_fake_transport.json"
              ISSUE_TO_IMPL_FAKE_CONTRACT_REVIEW_FILE="${ISSUE_TO_IMPL_WORKDIR}/root_router_fake_contract_review.json"
              ISSUE_TO_IMPL_ROOT_ROUTER_JSON=$(python3 - "$ISSUE_TO_IMPL_SKILLS_SCRIPTS_DIR" "$ISSUE_TO_IMPL_FIXTURE_PATH" "$ISSUE_TO_IMPL_FIXTURE_ISSUE_NUMBER" "squne121/loop-protocol" "$ISSUE_TO_IMPL_FAKE_TRANSPORT_FILE" "$ISSUE_TO_IMPL_FAKE_CONTRACT_REVIEW_FILE" <<'ROOT_ENTRY_ROUTER_ITI_PY_EOF'
import json
import sys

skills_scripts_dir, fixture_path, issue_number, repo, fake_transport_path, fake_contract_review_path = sys.argv[1:7]
issue_number = int(issue_number)

sys.path.insert(0, skills_scripts_dir)
import root_entry_router as rer  # noqa: E402

with open(fixture_path, encoding="utf-8") as fh:
    fixture = json.load(fh)
body = fixture["issues"][str(issue_number)]["body"]
body_sha256 = rer.compute_body_sha256(body)

# `base_sha` is a fixed synthetic value (this is a STATIC fixture -- the
# fake transport returns the identical body/base_sha on every fetch, so
# there is no drift across `run_root_transition`'s internal re-fetch, and
# `review_verdict: go` cleanly resolves to ROUTE_INVOKE on the happy path).
fake_transport_state = {
    "capability_ok": True,
    "repo_identity": repo,
    "audit_publish_ok": True,
    "issues": {
        str(issue_number): {
            "body": body,
            "base_sha": "a" * 40,
            "identity_ok": True,
            "fetch_ok": True,
            "comments": [],
        }
    },
}
with open(fake_transport_path, "w", encoding="utf-8") as fh:
    json.dump(fake_transport_state, fh)

fake_contract_review = {"status": "go", "body_sha256": body_sha256}
with open(fake_contract_review_path, "w", encoding="utf-8") as fh:
    json.dump(fake_contract_review, fh)

transport = rer.FileBackedFakeGitHubEntryTransport(fake_transport_path)


def _fake_reviewer(**_kwargs):
    return fake_contract_review


spy_calls = []


def _spy_invoke_step1():
    spy_calls.append(True)


result = rer.run_root_transition(
    issue_number=issue_number,
    repo=repo,
    transport=transport,
    contract_reviewer=_fake_reviewer,
    invoke_step1=_spy_invoke_step1,
    mode="static",
    publish_audit=False,
    expected_repository_identity=repo,
)

route = result["route"]["route"]
invoked = bool(result["invoked"])
spy_call_count = len(spy_calls)

# Never silently treat "the spy was never invoked" as success: only a
# route of `invoke_impl_review_loop` WITH exactly one spy invocation
# counts as `completed`. Any other combination (including the
# should-never-happen "invoked without an invoke route" case) fails
# closed instead of being synthesized as `expected_block` (P0-3 negative
# case: "root_entry_router callback count == 0" must not silently pass
# when the route legitimately WAS invoke_impl_review_loop).
if route == "invoke_impl_review_loop":
    if invoked and spy_call_count == 1:
        phase_status = "completed"
        terminal_result = "implementation_not_authorized"
        reason_code = "smoke_harness_stops_before_real_mutation_by_design"
    else:
        phase_status = "failed"
        terminal_result = "human_judgment_required"
        reason_code = "root_entry_router_spy_not_invoked_exactly_once"
elif invoked or spy_call_count != 0:
    phase_status = "failed"
    terminal_result = "human_judgment_required"
    reason_code = "root_entry_router_spy_invoked_without_invoke_route"
else:
    phase_status = "expected_block"
    terminal_result = "blocked"
    reason_code = f"root_entry_router_route_{route}"

print(json.dumps({
    "phase_status": phase_status,
    "terminal_result": terminal_result,
    "reason_code": reason_code,
    "route": route,
    "invoked": invoked,
    "spy_call_count": spy_call_count,
}))
ROOT_ENTRY_ROUTER_ITI_PY_EOF
)
              ISSUE_TO_IMPL_ROOT_ROUTER_RC=$?
              ISSUE_TO_IMPL_ROOT_ROUTER_PHASE_STATUS=$(printf '%s' "$ISSUE_TO_IMPL_ROOT_ROUTER_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["phase_status"])' 2>/dev/null || echo "")

              if [ "$ISSUE_TO_IMPL_ROOT_ROUTER_RC" -ne 0 ] || [ -z "$ISSUE_TO_IMPL_ROOT_ROUTER_PHASE_STATUS" ]; then
                _iti_add_phase "impl_review_loop_entry" "failed"
                ISSUE_TO_IMPL_REACHED_PHASE="impl_review_loop_entry"
                ISSUE_TO_IMPL_TERMINAL_RESULT="human_judgment_required"
                ISSUE_TO_IMPL_REASON_CODE="root_entry_router_execution_error"
              else
                _iti_add_phase "impl_review_loop_entry" "$ISSUE_TO_IMPL_ROOT_ROUTER_PHASE_STATUS"
                ISSUE_TO_IMPL_REACHED_PHASE="impl_review_loop_entry"
                ISSUE_TO_IMPL_TERMINAL_RESULT=$(printf '%s' "$ISSUE_TO_IMPL_ROOT_ROUTER_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["terminal_result"])')
                ISSUE_TO_IMPL_REASON_CODE=$(printf '%s' "$ISSUE_TO_IMPL_ROOT_ROUTER_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["reason_code"])')
                if [ "$ISSUE_TO_IMPL_ROOT_ROUTER_PHASE_STATUS" = "completed" ] || [ "$ISSUE_TO_IMPL_ROOT_ROUTER_PHASE_STATUS" = "expected_block" ]; then
                  ISSUE_TO_IMPL_TEST_VERDICT="pass"
                fi
              fi
            else
              _iti_add_phase "live_fixture_readback" "failed"
              ISSUE_TO_IMPL_REACHED_PHASE="live_fixture_readback"
              ISSUE_TO_IMPL_TERMINAL_RESULT="human_judgment_required"
              if [ "$ISSUE_TO_IMPL_READBACK_ENVELOPE_PARSED" != "True" ]; then
                ISSUE_TO_IMPL_REASON_CODE="live_fixture_readback_output_not_json"
              elif [ "$ISSUE_TO_IMPL_READBACK_CALL_TRACE_OK" != "True" ]; then
                ISSUE_TO_IMPL_REASON_CODE="live_fixture_readback_call_trace_not_observed"
              else
                ISSUE_TO_IMPL_REASON_CODE="live_fixture_readback_marker_not_observed"
              fi
              if [ -n "${CLAUDE_GPT_ISSUE_TO_IMPL_DEBUG_DIR:-}" ]; then
                mkdir -p "$CLAUDE_GPT_ISSUE_TO_IMPL_DEBUG_DIR"
                cp "$ISSUE_TO_IMPL_STDERR_LOG" "$CLAUDE_GPT_ISSUE_TO_IMPL_DEBUG_DIR/launch.stderr.log" 2>/dev/null || true
                printf '%s' "$ISSUE_TO_IMPL_CLAUDE_OUTPUT" > "$CLAUDE_GPT_ISSUE_TO_IMPL_DEBUG_DIR/claude.stdout.log" 2>/dev/null || true
              fi
            fi
          fi
        fi
      fi
    fi
  fi

  if [ "$ISSUE_TO_IMPL_TEST_VERDICT" != "pass" ]; then
    ISSUE_TO_IMPL_TEST_VERDICT="fail"
  fi

  # --- P1-3: load expected-phases.json as a RUNTIME ORACLE (not just
  #     SHA-256'd into evidence) -- exact phase order, allowed status
  #     values, reached_phase coherence, and terminal_result/last-status
  #     coherence are all checked BEFORE test_verdict: pass can stand. ---
  ISSUE_TO_IMPL_ORACLE_JSON=$(python3 - "$ISSUE_TO_IMPL_EXPECTED_PHASES_PATH" "$ISSUE_TO_IMPL_PHASE_TRACE_FILE" "${ISSUE_TO_IMPL_REACHED_PHASE:-}" "${ISSUE_TO_IMPL_TERMINAL_RESULT:-}" "$ISSUE_TO_IMPL_TEST_VERDICT" <<'PHASE_ORACLE_ITI_PY_EOF'
import json
import sys

expected_path, phase_trace_path, reached_phase, terminal_result, test_verdict = sys.argv[1:6]

with open(expected_path, encoding="utf-8") as fh:
    expected = json.load(fh)
expected_order = [p["phase"] for p in expected["phases"]]
allowed_status_by_phase = {p["phase"]: set(p["allowed_status"]) for p in expected["phases"]}

entries = []
with open(phase_trace_path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line:
            entries.append(json.loads(line))

problems = []
observed_order = [e["phase"] for e in entries]
if observed_order != expected_order[: len(observed_order)]:
    problems.append(f"phase_order_mismatch:{observed_order}")

for entry in entries:
    allowed = allowed_status_by_phase.get(entry["phase"])
    if allowed is None:
        problems.append(f"unknown_phase:{entry['phase']}")
    elif entry["status"] not in allowed:
        problems.append(f"disallowed_status:{entry['phase']}:{entry['status']}")

if entries:
    last_phase = entries[-1]["phase"]
    if reached_phase and reached_phase != last_phase:
        problems.append(f"reached_phase_mismatch:{reached_phase}!={last_phase}")

if test_verdict == "pass":
    if not terminal_result:
        problems.append("pass_verdict_with_empty_terminal_result")
    if not entries or entries[-1]["status"] not in ("completed", "expected_block"):
        problems.append("pass_verdict_with_non_terminal_last_status")

print(json.dumps({"ok": len(problems) == 0, "problems": problems}))
PHASE_ORACLE_ITI_PY_EOF
)
  ISSUE_TO_IMPL_ORACLE_OK=$(printf '%s' "$ISSUE_TO_IMPL_ORACLE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])' 2>/dev/null || echo False)
  if [ "$ISSUE_TO_IMPL_ORACLE_OK" != "True" ]; then
    ISSUE_TO_IMPL_TEST_VERDICT="fail"
    ISSUE_TO_IMPL_REASON_CODE="phase_trace_oracle_mismatch"
  fi

  # --- Evidence: built with embedded Python `json.dumps` (P0-5 style fix --
  #     no more manual printf JSON-string interpolation), then re-parsed
  #     from disk to confirm it is valid JSON before this scenario exits. ---
  python3 - "$ISSUE_TO_IMPL_EVIDENCE_FILE" "$ISSUE_TO_IMPL_PHASE_TRACE_FILE" \
    "$ISSUE_TO_IMPL_TEST_VERDICT" "${ISSUE_TO_IMPL_TERMINAL_RESULT:-}" "$ISSUE_TO_IMPL_REASON_CODE" "${ISSUE_TO_IMPL_REACHED_PHASE:-}" \
    "$TIMESTAMP" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$ISSUE_TO_IMPL_CLAUDE_CODE_VERSION" "$SUT_PROXY_VERSION" "$SUT_LAUNCHER_PATH" "$SUT_REPO_ROOT" \
    "$ISSUE_TO_IMPL_PROMPT_SHA256" "$ISSUE_TO_IMPL_ISSUE_JSON_SHA256" "$ISSUE_TO_IMPL_EXPECTED_PHASES_SHA256" <<'EVIDENCE_BUILD_ITI_PY_EOF'
import json
import sys

(
    evidence_path, phase_trace_path,
    test_verdict, terminal_result, reason_code, reached_phase,
    timestamp, git_head, git_dirty, claude_code_version, proxy_version, launcher_path, repository_root,
    prompt_sha256, issue_json_sha256, expected_phases_sha256,
) = sys.argv[1:17]

entries = []
with open(phase_trace_path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line:
            entries.append(json.loads(line))

evidence = {
    "schema": "ISSUE_TO_IMPL_E2E_RESULT_V1",
    "scenario": "issue_to_impl",
    "test_verdict": test_verdict,
    "terminal_result": terminal_result or None,
    "reason_code": reason_code,
    "reached_phase": reached_phase or None,
    "phase_trace": entries,
    "resume_from": None,
    "generated_at": timestamp,
    "sut": {
        "git_head": git_head,
        "git_dirty": git_dirty == "true",
        "claude_code_version": claude_code_version,
        "proxy_version": proxy_version,
        "launcher_path": launcher_path,
        "repository_root": repository_root,
        "fixtures": {
            "prompt_md_sha256": prompt_sha256,
            "issue_json_sha256": issue_json_sha256,
            "expected_phases_json_sha256": expected_phases_sha256,
        },
    },
}

with open(evidence_path, "w", encoding="utf-8") as fh:
    json.dump(evidence, fh)

# Re-parse from disk before this scenario is allowed to report PASS/FAIL --
# a write that produced unparsable JSON must fail closed, not ship silently.
with open(evidence_path, encoding="utf-8") as fh:
    json.load(fh)
EVIDENCE_BUILD_ITI_PY_EOF
  ISSUE_TO_IMPL_EVIDENCE_BUILD_RC=$?

  rm -rf "$ISSUE_TO_IMPL_WORKDIR"

  if [ "$ISSUE_TO_IMPL_EVIDENCE_BUILD_RC" -ne 0 ]; then
    echo "FAIL: issue_to_impl scenario -- evidence JSON の生成または再parseに失敗しました。証跡: ${ISSUE_TO_IMPL_EVIDENCE_FILE}"
    exit 1
  fi

  if [ "$ISSUE_TO_IMPL_TEST_VERDICT" = "pass" ]; then
    echo "PASS: issue_to_impl scenario -- reached_phase=${ISSUE_TO_IMPL_REACHED_PHASE} terminal_result=${ISSUE_TO_IMPL_TERMINAL_RESULT}。証跡: ${ISSUE_TO_IMPL_EVIDENCE_FILE}"
    exit 0
  fi
  echo "FAIL: issue_to_impl scenario -- reason_code=${ISSUE_TO_IMPL_REASON_CODE} reached_phase=${ISSUE_TO_IMPL_REACHED_PHASE}。証跡: ${ISSUE_TO_IMPL_EVIDENCE_FILE}"
  exit 1
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

# --- canary SubAgent fixture（Issue #2274 AC14/AC15）: caller-owned `--agents`
#     を forbidden flag として拒否する launch.sh の判定はそのまま維持し
#     （このスクリプトは launch.sh の pre-filter を経由しない一般 smoke lane
#     専用の launcher-owned fixture であり、caller-supplied `--agents` の経路
#     ではない）、canary agent 自体は smoke run 固有の高エントロピー nonce
#     から `claude_gpt_smoke_canary_agents_json_fragment`（lib.sh）で毎回
#     内部合成する。name/prompt/model/tools を caller から受け取らない固定
#     spec（tools: [] / 固定 prompt・marker）で JSON serializer が一括生成し、
#     生成直後に自身で parse/readback して malformed JSON・duplicate
#     top-level key を fail-closed で拒否する（lib.sh 側の実装参照）。
#     Issue #2651: 旧 spark-codex 定義名との衝突検査は撤去済み（spark-codex
#     定義自体が撤去されたため）。 ---
SMOKE_CANARY_CSPRNG_HEX=$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
if [ -z "$SMOKE_CANARY_CSPRNG_HEX" ]; then
  # Issue #2274 PR #2285 OWNER fix-delta P0-1: a CSPRNG-unavailable
  # environment used to silently fall back to timestamp+PID+nanoseconds,
  # which does not meet the AC's high-entropy nonce requirement (an
  # attacker able to observe/predict process start time and PID could
  # feasibly predict the derived canary agent name). This is now a typed
  # fail-closed result -- never a low-entropy fallback.
  printf '{"schema":"CLAUDE_GPT_SMOKE_RESULT_V1","status":"fail","reason":"csprng_unavailable_for_canary_nonce","generated_at":"%s","sut":{"git_head":"%s"}}\n' \
    "$TIMESTAMP" "$SUT_GIT_HEAD" > "$EVIDENCE_FILE"
  echo "FAIL: /dev/urandom（CSPRNG）が利用不能なため canary nonce を高エントロピーで生成できません。証跡: ${EVIDENCE_FILE}"
  exit 1
fi
SMOKE_CANARY_NONCE="${TIMESTAMP}-$$-${SMOKE_CANARY_CSPRNG_HEX}"
CANARY_AGENTS_JSON=$(claude_gpt_smoke_canary_agents_json_fragment "$SUBAGENT_MARKER" "$SMOKE_CANARY_NONCE")
if [ -z "$CANARY_AGENTS_JSON" ]; then
  printf '{"schema":"CLAUDE_GPT_SMOKE_RESULT_V1","status":"fail","reason":"canary_agent_fixture_synthesis_failed","generated_at":"%s","sut":{"git_head":"%s"}}\n' \
    "$TIMESTAMP" "$SUT_GIT_HEAD" > "$EVIDENCE_FILE"
  echo "FAIL: canary SubAgent fixture の内部合成（Issue #2274 AC14/AC15）に失敗しました。証跡: ${EVIDENCE_FILE}"
  exit 1
fi
CANARY_AGENT_NAME=$(printf '%s' "$CANARY_AGENTS_JSON" | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin))))' 2>/dev/null)
if [ -z "$CANARY_AGENT_NAME" ]; then
  printf '{"schema":"CLAUDE_GPT_SMOKE_RESULT_V1","status":"fail","reason":"canary_agent_name_readback_failed","generated_at":"%s","sut":{"git_head":"%s"}}\n' \
    "$TIMESTAMP" "$SUT_GIT_HEAD" > "$EVIDENCE_FILE"
  echo "FAIL: canary SubAgent fixture の agent name readback（Issue #2274 AC14/AC15）に失敗しました。証跡: ${EVIDENCE_FILE}"
  exit 1
fi

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
  # Issue #2274 PR #2285 OWNER fix-delta P0-1: this used to be the raw
  # canary `--agents` JSON fragment forwarded verbatim into
  # CLAUDE_GPT_SMOKE_CANARY_AGENTS_JSON. That raw-JSON escape hatch no
  # longer exists on the launch.sh side -- this is now just a boolean
  # ("1"/"") flag selecting whether this step should set
  # CLAUDE_GPT_SMOKE_CANARY_MARKER/CLAUDE_GPT_SMOKE_CANARY_NONCE (opaque
  # strings only; launch.sh synthesizes the fixture JSON itself).
  step_use_canary_env="$4"
  step_stdout_file=$(mktemp)
  step_stderr_file=$(mktemp)

  step_log_offset_before=0
  if [ -f "$STRUCTURED_PROXY_LOG_PATH" ]; then
    step_log_offset_before=$(wc -c < "$STRUCTURED_PROXY_LOG_PATH" 2>/dev/null || echo 0)
  fi

  if [ -n "$step_use_canary_env" ] && [ -n "$step_allowed_tools" ]; then
    # Issue #2274 AC14/AC15 (PR #2285 OWNER fix-delta P0-1): the canary
    # SubAgent fixture is synthesized by launch.sh ITSELF (via the same
    # `claude_gpt_smoke_canary_agents_json_fragment` function) from two
    # opaque strings passed over a launcher-owned internal env channel
    # (CLAUDE_GPT_SMOKE_CANARY_MARKER / CLAUDE_GPT_SMOKE_CANARY_NONCE) --
    # never as a caller-supplied `--agents` CLI flag, and never as a
    # caller-supplied raw JSON fragment any more either. launch.sh's own
    # `--agents` forbidden-flag rejection (CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS)
    # stays unconditional for caller argv.
    CLAUDE_GPT_SMOKE_CANARY_MARKER="$SUBAGENT_MARKER" CLAUDE_GPT_SMOKE_CANARY_NONCE="$SMOKE_CANARY_NONCE" "$SCRIPT_DIR/launch.sh" -- -p "$step_prompt" --output-format text --no-session-persistence \
      --allowedTools "$step_allowed_tools" \
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
  run_convo_step "subagent" "You are running inside an automated, non-interactive runtime smoke test with no real user present. Use the Task tool right now (an actual tool call, not a description) to launch the subagent named ${CANARY_AGENT_NAME} with any instructions, then print its exact output verbatim." "Task" "1"
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

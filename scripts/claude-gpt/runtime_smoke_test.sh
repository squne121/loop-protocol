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
# --- Issue #2274 AC5/AC6/AC7: `--spark-delegation` mode ---
# 通常の一般 canary smoke（Phase A/B、上記と同じ exit code 規約）とは別に、
# `SPARK_DELEGATION_EVIDENCE_V2` schema（Issue #2274 本文の「推奨する evidence
# schema」参照）による live Spark E2E smoke を要求するモード。この mode は
# static plumbing のみを実装する: 環境（proxy バイナリ / ChatGPT subscription
# 認証）が利用不能な場合は他の mode と同じく exit 77 で SKIP する（fallback
# 実行や擬似成功判定は行わない）。環境が利用可能な場合でも、`SPARK_DELEGATION_
# EVIDENCE_V2` の `authorization`/`definition`/`invocation`/`agent`/`proxy` 各
# フィールドを出所別に分離して収集する live conversation harness 自体は本
# iteration では未実装であり、その場合は `verdict.status: "blocked"`,
# `verdict.reason: "spark_delegation_live_harness_not_implemented"` を証跡へ
# 記録して exit 1 する（static hook output test の結果を live smoke の代わりに
# PASS へ昇格させることは絶対に行わない -- AC7 が明示的に禁止する）。
SPARK_DELEGATION_MODE=false
for _arg in "$@"; do
  case "$_arg" in
    --spark-delegation) SPARK_DELEGATION_MODE=true ;;
  esac
done

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

# --- 環境可用性判定（バイナリ / ChatGPT subscription 認証）。ディレクトリ/設定はまだ作らない。 ---
PREFLIGHT_ENV_JSON=$("$SCRIPT_DIR/preflight.sh" --env-only)
PREFLIGHT_ENV_RC=$?

if [ "$PREFLIGHT_ENV_RC" -eq 3 ] || [ "$PREFLIGHT_ENV_RC" -eq 4 ]; then
  SKIP_REASON="binary_unavailable"
  if [ "$PREFLIGHT_ENV_RC" -eq 4 ]; then
    SKIP_REASON="chatgpt_subscription_auth_unavailable"
  fi
  if [ "$SPARK_DELEGATION_MODE" = "true" ]; then
    printf '{"schema":"SPARK_DELEGATION_EVIDENCE_V2","generated_at":"%s","sut":{"git_head":"%s","git_dirty":%s,"launch_sh_sha256":"%s","lib_sh_sha256":"%s","runtime_smoke_sha256":"%s"},"proxy":{"absolute_path":"%s","version":"%s","sha256":"%s"},"verdict":{"status":"blocked","reason":"%s"}}\n' \
      "$TIMESTAMP" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$SUT_LIB_SH_SHA256" "$SUT_RUNTIME_SMOKE_SHA256" \
      "$SUT_PROXY_BIN" "$SUT_PROXY_VERSION" "$SUT_PROXY_SHA256" "$SKIP_REASON" > "$EVIDENCE_FILE"
    echo "SKIP: ${SKIP_REASON} のため --spark-delegation live smoke を実行できません（fallback 実行なし）。証跡: ${EVIDENCE_FILE}"
    exit 77
  fi
  printf '{"schema":"CLAUDE_GPT_SMOKE_RESULT_V1","status":"skip","reason":"%s","preflight_env_only":%s,"generated_at":"%s","sut":{"launcher_path":"%s","repository_root":"%s","git_head":"%s","git_dirty":"%s","launch_sh_sha256":"%s","lib_sh_sha256":"%s","runtime_smoke_sha256":"%s"},"proxy":{"absolute_path":"%s","version":"%s","sha256":"%s"}}\n' \
    "$SKIP_REASON" "$PREFLIGHT_ENV_JSON" "$TIMESTAMP" \
    "$SUT_LAUNCHER_PATH" "$SUT_REPO_ROOT" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$SUT_LIB_SH_SHA256" "$SUT_RUNTIME_SMOKE_SHA256" \
    "$SUT_PROXY_BIN" "$SUT_PROXY_VERSION" "$SUT_PROXY_SHA256" > "$EVIDENCE_FILE"
  echo "SKIP: ${SKIP_REASON} のため runtime smoke test を実行できません。証跡: ${EVIDENCE_FILE}"
  exit 77
fi

# --- Issue #2274 AC5/AC6/AC7/AC17/AC18: `--spark-delegation` live E2E harness ---
# OWNER adversarial review（PR #2285, iteration 1）反映: 同一 PR / 同一
# session の flag なし一般 canary smoke（Phase A/B, 下記）が実際に
# `launch.sh` 駆動の live `claude` subprocess から Task tool 経由の
# SubAgent 委譲を実機 PASS させている以上、「nested SubAgent delegation が
# 必要」という Stop Condition は本 mode にも適用されない。未実装だったのは
# `SPARK_DELEGATION_EVIDENCE_V2` の `authorization`/`definition`/
# `invocation`/`agent`/`proxy` 各フィールドを出所別に分離して収集する live
# conversation harness そのものであり、本 iteration でそれを実装する。
#
# 実装方針（bounded correlation, Issue 本文の「proxy と Agent hook の
# bounded correlation」箇条書き準拠）:
#   - `--include-hook-events --output-format stream-json` で `claude` を
#     非対話起動し、`@agent-spark-codex` の canonical mention を含む
#     prompt で Task tool 経由の spark-codex 委譲を要求する
#     （`authorization.requested_model_source: canonical_mention_default`）。
#   - `CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop` を launch.sh へ
#     渡し、gate の audit-only SubagentStart/SubagentStop hook entry に
#     `cat` sink を追加登録させる（launch.sh 自身の authorization gate・
#     forbidden-flag 判定は一切変更しない。既存の一般 canary smoke と
#     同じ機構）。これにより hook stdin payload（`agent_id`/`agent_type`）が
#     stream-json 上へ echo される。
#   - `PreToolUse(Agent)` 相当の tool_use_id と `model` field state は、
#     stream-json 上の Agent/Task tool_use ブロック自体（`id`/`input`）から
#     取得する。
#   - `PostToolUse(Agent)` 相当の `resolvedModel`/`modelsUsed`/`status`/
#     `agentId` は、同じ tool_use_id と相関する tool_result envelope の
#     `tool_use_result` フィールドから取得する（Claude Code は Agent tool
#     専用の独立した stream-json "PostToolUse" system event を発行しない
#     ため、この tool_result envelope が該当する一次情報源。
#     scripts/agent-ops/run_worktree_agent_runtime_smoke.py が既に文書化・
#     再利用している、実 live 観測済みの `tool_use_result` スキーマと
#     同じもの）。
#   - proxy 側の実効 model は、この1 step 実行前後で構造化 proxy log
#     （`claude-code-proxy` が書く JSONL）へ新規追記されたバイト範囲だけを
#     切り出し（既存の一般 canary smoke の各 step と同じオフセット手法）、
#     `codex_upstream_request_started` イベントの `fields.model` から取得
#     する。この 1 step には spark-codex への Agent 呼び出しがちょうど
#     1 件しか存在しないことを SubagentStart/SubagentStop の
#     exactly-one-pair 判定で構造的に強制するため、このバイト範囲全体を
#     `proxy.correlation: "isolated_subagent_lifecycle_window"` の
#     bounded window として扱う（hook event 発火の正確なバイトオフセット
#     そのものを別チャネルで記録する実装ではない -- この近似の範囲は
#     PR body に明記する）。zero match（Agent tool_use 0 件・tool_result
#     不一致・lifecycle pair 0 件）・複数 window（lifecycle pair が
#     複数）・欠損 lifecycle event はすべて `verdict.status: "fail"` の
#     typed reason とする（下記ヒアドキュメントの spark_evidence.py 相当
#     ロジック参照）。
#   - `resolvedModel`/`modelsUsed` の両方が観測できない場合（Claude Code
#     version floor 未満等）は `verdict.status: "blocked"`,
#     `verdict.reason: "claude_code_evidence_schema_unsupported"` とする
#     （AC18。field 欠損を null 許容で proxy log だけの一致判定へ昇格
#     しない）。
#   - static hook output test の結果を live smoke の代わりに PASS へ
#     昇格させることは行わない（AC7。以下のロジックは常に実 live 起動の
#     結果のみを見る）。
if [ "$SPARK_DELEGATION_MODE" = "true" ]; then
  SPARK_MARKER="CLAUDE_GPT_SPARK_DELEGATION_OK"
  SPARK_STRUCTURED_PROXY_LOG_PATH="$(claude_gpt_proxy_state_dir)/claude-code-proxy/proxy.log"

  SPARK_EVIDENCE_PY=$(mktemp)
  cat > "$SPARK_EVIDENCE_PY" <<'SPARK_EVIDENCE_PY_EOF'
#!/usr/bin/env python3
"""Issue #2274 AC5/AC6/AC7/AC17/AC18: bounded, hook-ID-correlated live Spark
delegation evidence builder for scripts/claude-gpt/runtime_smoke_test.sh's
`--spark-delegation` mode.

Never promotes any field it cannot honestly derive from the already-captured
stream-json / structured proxy log to a positive result -- every ambiguous or
missing signal is a typed FAIL/BLOCKED reason, never a silent pass (Issue
body AC7 / "proxy と Agent hook の bounded correlation" bullet).
"""
from __future__ import annotations

import json
import sys

(
    stdout_path,
    proxy_slice_path,
    agent_name,
    expected_model,
    marker,
    sut_git_head,
    sut_git_dirty,
    sut_launch_sh_sha256,
    sut_lib_sh_sha256,
    sut_runtime_smoke_sha256,
    proxy_absolute_path,
    proxy_version,
    proxy_sha256,
    claude_code_version,
    generated_at,
) = sys.argv[1:16]

HOOK_LIFECYCLE = ("SubagentStart", "SubagentStop")


def _iter_stream_events(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _parse_embedded(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


events = list(_iter_stream_events(stdout_path))

# --- 1. Agent/Task tool_use targeting the requested subagent (PreToolUse
#     correlation source: this repository's own hook contract normalizes
#     `model` off the wire before dispatch, so the client-visible tool_use
#     input is the caller's ORIGINAL proposal, not the post-hook updatedInput
#     -- see launch.sh cmd_pre_tool_use_agent). ---
agent_tool_uses = []
for idx, ev in enumerate(events):
    if ev.get("type") != "assistant":
        continue
    message = ev.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        continue
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in ("Task", "Agent"):
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        if tool_input.get("subagent_type") != agent_name:
            continue
        agent_tool_uses.append(
            {
                "stream_index": idx,
                "tool_use_id": block.get("id"),
                "model_field_state": "absent" if "model" not in tool_input else "present",
                "run_in_background": tool_input.get("run_in_background"),
            }
        )

# --- 2. Matching tool_result (PostToolUse-equivalent evidence: Claude Code
#     does not itself emit a stream-json "PostToolUse" system event for the
#     Agent tool, but the SAME resolvedModel/modelsUsed/status/agentId
#     fields the Issue's PostToolUse bullet describes are carried on the
#     `tool_use_result` envelope of the matching tool_result -- see
#     scripts/agent-ops/run_worktree_agent_runtime_smoke.py's documented,
#     live-observed `tool_use_result` shape). ---
tool_results = {}
for idx, ev in enumerate(events):
    if ev.get("type") != "user":
        continue
    message = ev.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    tool_use_result = ev.get("tool_use_result")
    tool_use_id = None
    is_error = None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                is_error = block.get("is_error")
                break
    if tool_use_id is None:
        continue
    tool_results[tool_use_id] = {
        "stream_index": idx,
        "tool_use_result": tool_use_result if isinstance(tool_use_result, dict) else None,
        "is_error": is_error,
    }

# --- 3. SubagentStart/SubagentStop hook lifecycle events (via
#     CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop, which appends a
#     `cat` sink alongside the gate's own audit-only SAS/SAP hook entries,
#     echoing the hook stdin payload back onto the stream). ---
lifecycle = []
for idx, ev in enumerate(events):
    if ev.get("type") != "system":
        continue
    hook_event = ev.get("hook_event")
    if hook_event not in HOOK_LIFECYCLE:
        continue
    entry = {"hook_event": hook_event, "stream_index": idx, "agent_id": None, "agent_type": None}
    channel_parsed = {}
    for key in ("stdout", "output"):
        parsed = _parse_embedded(ev.get(key))
        if parsed is not None:
            channel_parsed[key] = parsed
    contradictory = False
    if "stdout" in channel_parsed and "output" in channel_parsed:
        for field_name in ("agent_id", "agent_type"):
            a = channel_parsed["stdout"].get(field_name)
            b = channel_parsed["output"].get(field_name)
            if isinstance(a, str) and a and isinstance(b, str) and b and a != b:
                contradictory = True
    entry["contradictory"] = contradictory
    if not contradictory:
        for parsed in channel_parsed.values():
            if entry["agent_id"] is None and isinstance(parsed.get("agent_id"), str) and parsed.get("agent_id"):
                entry["agent_id"] = parsed.get("agent_id")
            if entry["agent_type"] is None and isinstance(parsed.get("agent_type"), str) and parsed.get("agent_type"):
                entry["agent_type"] = parsed.get("agent_type")
    lifecycle.append(entry)

reasons: list[str] = []
invocation = None
agent_info = None

if len(agent_tool_uses) != 1:
    reasons.append("expected_exactly_one_spark_agent_tool_use_observed_%d" % len(agent_tool_uses))
else:
    tu = agent_tool_uses[0]
    invocation = tu
    tr = tool_results.get(tu["tool_use_id"])
    if tr is None:
        reasons.append("no_tool_result_matched_tool_use_id")
    else:
        tur = tr["tool_use_result"] or {}
        agent_id = tur.get("agentId")
        status = tur.get("status")
        resolved_model = tur.get("resolvedModel")
        models_used_raw = tur.get("modelsUsed")
        if isinstance(models_used_raw, list):
            models_used = [m for m in models_used_raw if isinstance(m, str)]
        elif isinstance(models_used_raw, str) and models_used_raw:
            models_used = [models_used_raw]
        else:
            models_used = []
        agent_info = {
            "agent_id": agent_id if isinstance(agent_id, str) else None,
            "status": status if isinstance(status, str) else None,
            "resolved_model": resolved_model if isinstance(resolved_model, str) else None,
            "models_used": models_used,
        }
        if not agent_info["agent_id"]:
            reasons.append("tool_result_missing_agent_id")
        if agent_info["status"] != "completed":
            reasons.append("agent_status_not_completed_%s" % agent_info["status"])
        if tr.get("is_error"):
            reasons.append("tool_result_is_error")

        if agent_info["agent_id"]:
            starts = [e for e in lifecycle if e["hook_event"] == "SubagentStart" and e["agent_id"] == agent_info["agent_id"]]
            stops = [e for e in lifecycle if e["hook_event"] == "SubagentStop" and e["agent_id"] == agent_info["agent_id"]]
            if len(starts) != 1 or len(stops) != 1:
                reasons.append("lifecycle_pair_not_exactly_one_starts_%d_stops_%d" % (len(starts), len(stops)))
            elif starts[0]["stream_index"] >= stops[0]["stream_index"]:
                reasons.append("subagent_start_does_not_precede_stop")
        else:
            reasons.append("no_lifecycle_correlation_missing_agent_id")

        # Issue #2274 AC18: BOTH resolvedModel and modelsUsed must be
        # genuinely present for this runtime's Agent tool_use_result shape
        # to count as version-floor-supported evidence -- a runtime that
        # only ever surfaces one of the two fields (live-observed reality,
        # 2026-08-21: this repository's installed Claude Code emits
        # `resolvedModel` but never a non-empty `modelsUsed` on the
        # synchronous-completion `tool_use_result` shape) is exactly the
        # "field 欠損を null として許容し proxy log だけで一致判定する" shape
        # AC18 forbids promoting to PASS. Never treat an absent/empty
        # `modelsUsed` as vacuously satisfied just because `resolvedModel`
        # happened to be present.
        if agent_info["resolved_model"] is None or not agent_info["models_used"]:
            reasons.append("claude_code_evidence_schema_unsupported")
        else:
            if agent_info["resolved_model"] != expected_model:
                reasons.append("resolved_model_mismatch_%s" % agent_info["resolved_model"])
            if expected_model not in agent_info["models_used"]:
                reasons.append("models_used_missing_expected_model")

# --- Proxy log slice (bounded to this single-agent step window; see
#     runtime_smoke_test.sh comment at the call site for the exact bound
#     semantics used). This window legitimately also contains the PARENT
#     session's own (non-Spark) model traffic for the same turn (e.g. the
#     main session's own reasoning-model requests, auto-generated title/
#     summary requests) -- live-observed reality, 2026-08-21: a window
#     containing exactly one genuine spark-codex delegation also contained
#     unrelated gpt-5.6-terra/gpt-5.6-luna requests from the parent
#     session/runtime itself. Only the request(s) whose own `model` field
#     equals `expected_model` are Spark's own traffic; non-matching models
#     in the same window are not a violation on their own and are never
#     flagged as a mismatch. ---
proxy_requests = []
try:
    with open(proxy_slice_path, encoding="utf-8", errors="replace") as fh:
        proxy_lines = fh.readlines()
except OSError:
    proxy_lines = []
for raw in proxy_lines:
    raw = raw.strip()
    if not raw:
        continue
    try:
        obj = json.loads(raw)
    except ValueError:
        continue
    if obj.get("msg") != "codex_upstream_request_started":
        continue
    fields = obj.get("fields") if isinstance(obj.get("fields"), dict) else {}
    proxy_requests.append(
        {
            "req_id": fields.get("reqId") if isinstance(fields.get("reqId"), str) else None,
            "model": fields.get("model") if isinstance(fields.get("model"), str) else None,
            "transport": fields.get("transport") if isinstance(fields.get("transport"), str) else None,
        }
    )

spark_proxy_requests = [r for r in proxy_requests if r.get("model") == expected_model]

if not proxy_requests:
    reasons.append("no_proxy_requests_observed_in_step_window")
elif not spark_proxy_requests:
    reasons.append("no_proxy_spark_model_request_observed_in_step_window")
else:
    for r in spark_proxy_requests:
        if r.get("transport") != "http":
            reasons.append("spark_proxy_transport_not_http_%s" % r.get("transport"))

if sut_git_dirty != "false":
    reasons.append("sut_git_dirty_%s" % sut_git_dirty)

schema_unsupported_only = reasons == ["claude_code_evidence_schema_unsupported"]

if not reasons:
    verdict_status = "pass"
    verdict_reason = "match"
elif schema_unsupported_only:
    verdict_status = "blocked"
    verdict_reason = "claude_code_evidence_schema_unsupported"
else:
    verdict_status = "fail"
    verdict_reason = ";".join(reasons) if reasons else "unknown"

authorization = {
    "requested_model": expected_model,
    "requested_model_source": "canonical_mention_default",
}
definition = {
    "agent_name": agent_name,
    "declared_model": expected_model,
    "source": "launcher_owned_agents_json",
}
proxy_block = {
    "correlation": "isolated_subagent_lifecycle_window",
    "request_count": len(spark_proxy_requests),
    "requests": spark_proxy_requests,
}
sut_block = {
    "git_head": sut_git_head,
    "git_dirty": sut_git_dirty == "true",
    "launch_sh_sha256": sut_launch_sh_sha256,
    "lib_sh_sha256": sut_lib_sh_sha256,
    "runtime_smoke_sha256": sut_runtime_smoke_sha256,
}
runtime_block = {
    "claude_code_version": claude_code_version,
    "proxy_version": proxy_version,
    "proxy_sha256": proxy_sha256,
    "proxy_absolute_path": proxy_absolute_path,
}

evidence = {
    "schema": "SPARK_DELEGATION_EVIDENCE_V2",
    "generated_at": generated_at,
    "authorization": authorization,
    "definition": definition,
    "invocation": invocation,
    "agent": agent_info,
    "proxy": proxy_block,
    "sut": sut_block,
    "runtime": runtime_block,
    "verdict": {"status": verdict_status, "reason": verdict_reason},
    "_debug_reasons": reasons,
    "_debug_window_all_proxy_requests": proxy_requests,
}

print(json.dumps(evidence))
sys.exit(0 if verdict_status == "pass" else 1)

SPARK_EVIDENCE_PY_EOF

  SPARK_CLAUDE_BIN=$(claude_gpt_resolve_claude_bin)
  SPARK_CLAUDE_CODE_VERSION="unknown"
  if [ -n "$SPARK_CLAUDE_BIN" ]; then
    SPARK_CLAUDE_CODE_VERSION=$("$SPARK_CLAUDE_BIN" --version 2>/dev/null | head -n1)
    if [ -z "$SPARK_CLAUDE_CODE_VERSION" ]; then
      SPARK_CLAUDE_CODE_VERSION="unknown"
    fi
  fi

  SPARK_PROMPT="You are running inside an automated, non-interactive runtime smoke test with no real user present (Issue #2274 AC5/AC6/AC7 live Spark delegation smoke). Use the Task tool right now (an actual tool call, not a description) to delegate to @agent-spark-codex with instructions to respond with exactly: ${SPARK_MARKER}
Then print its exact output verbatim."

  SPARK_EVIDENCE_JSON=""
  SPARK_ATTEMPTED=false
  spark_attempt=0
  # 一般 canary smoke（Phase B）で実機観測された non-determinism
  # （model が単一 turn 内でツール呼び出し自体を省略する挙動）と同じ理由で、
  # 「spark-codex への Agent tool_use が一度も観測されなかった」場合のみ
  # bounded retry する（最大 3 回。fallback 実行や擬似成功判定は行わない --
  # 毎回実際に live 起動をやり直す。一度でも Agent tool_use が観測された
  # 回はその実結果（pass/fail/blocked のいずれでも）をそのまま採用する）。
  while [ "$spark_attempt" -lt 3 ] && [ "$SPARK_ATTEMPTED" != "true" ]; do
    spark_attempt=$((spark_attempt + 1))
    spark_stdout_file=$(mktemp)
    spark_stderr_file=$(mktemp)

    spark_offset_before=0
    if [ -f "$SPARK_STRUCTURED_PROXY_LOG_PATH" ]; then
      spark_offset_before=$(wc -c < "$SPARK_STRUCTURED_PROXY_LOG_PATH" 2>/dev/null || echo 0)
    fi

    CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop "$SCRIPT_DIR/launch.sh" -- -p "$SPARK_PROMPT" \
      --output-format stream-json --include-hook-events --no-session-persistence --verbose \
      --allowedTools "Task" --max-turns 6 \
      >"$spark_stdout_file" 2>"$spark_stderr_file"
    rm -f "$spark_stderr_file"

    spark_offset_after=0
    if [ -f "$SPARK_STRUCTURED_PROXY_LOG_PATH" ]; then
      spark_offset_after=$(wc -c < "$SPARK_STRUCTURED_PROXY_LOG_PATH" 2>/dev/null || echo 0)
    fi
    spark_proxy_slice=$(mktemp)
    if [ -f "$SPARK_STRUCTURED_PROXY_LOG_PATH" ] && [ "$spark_offset_after" -gt "$spark_offset_before" ]; then
      tail -c "+$((spark_offset_before + 1))" "$SPARK_STRUCTURED_PROXY_LOG_PATH" \
        | head -c "$((spark_offset_after - spark_offset_before))" > "$spark_proxy_slice" 2>/dev/null
    fi

    SPARK_EVIDENCE_JSON=$(python3 "$SPARK_EVIDENCE_PY" "$spark_stdout_file" "$spark_proxy_slice" \
      "$CLAUDE_GPT_SPARK_AGENT_NAME" "$CLAUDE_GPT_SPARK_MODEL" "$SPARK_MARKER" \
      "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$SUT_LIB_SH_SHA256" "$SUT_RUNTIME_SMOKE_SHA256" \
      "$SUT_PROXY_BIN" "$SUT_PROXY_VERSION" "$SUT_PROXY_SHA256" "$SPARK_CLAUDE_CODE_VERSION" "$TIMESTAMP")
    rm -f "$spark_stdout_file" "$spark_proxy_slice"

    SPARK_ATTEMPTED=$(printf '%s' "$SPARK_EVIDENCE_JSON" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print("true" if d.get("invocation") else "false")
' 2>/dev/null)
    if [ -z "$SPARK_ATTEMPTED" ]; then
      SPARK_ATTEMPTED=false
    fi
  done
  rm -f "$SPARK_EVIDENCE_PY"

  if [ -z "$SPARK_EVIDENCE_JSON" ]; then
    printf '{"schema":"SPARK_DELEGATION_EVIDENCE_V2","generated_at":"%s","sut":{"git_head":"%s","git_dirty":%s,"launch_sh_sha256":"%s","lib_sh_sha256":"%s","runtime_smoke_sha256":"%s"},"proxy":{"absolute_path":"%s","version":"%s","sha256":"%s"},"verdict":{"status":"fail","reason":"evidence_builder_produced_no_output"}}\n' \
      "$TIMESTAMP" "$SUT_GIT_HEAD" "$SUT_GIT_DIRTY" "$SUT_LAUNCH_SH_SHA256" "$SUT_LIB_SH_SHA256" "$SUT_RUNTIME_SMOKE_SHA256" \
      "$SUT_PROXY_BIN" "$SUT_PROXY_VERSION" "$SUT_PROXY_SHA256" > "$EVIDENCE_FILE"
    echo "FAIL: --spark-delegation live smoke の evidence builder が出力を生成できませんでした。証跡: ${EVIDENCE_FILE}"
    exit 1
  fi

  printf '%s\n' "$SPARK_EVIDENCE_JSON" > "$EVIDENCE_FILE"
  SPARK_VERDICT_STATUS=$(printf '%s' "$SPARK_EVIDENCE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("verdict",{}).get("status","fail"))' 2>/dev/null || echo fail)
  SPARK_VERDICT_REASON=$(printf '%s' "$SPARK_EVIDENCE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("verdict",{}).get("reason","unknown"))' 2>/dev/null || echo unknown)

  if [ "$SPARK_VERDICT_STATUS" = "pass" ]; then
    echo "PASS: --spark-delegation live Spark E2E smoke（SPARK_DELEGATION_EVIDENCE_V2）が成功しました。証跡: ${EVIDENCE_FILE}"
    exit 0
  fi
  echo "FAIL: --spark-delegation live Spark E2E smoke が ${SPARK_VERDICT_STATUS} でした（reason: ${SPARK_VERDICT_REASON}）。証跡: ${EVIDENCE_FILE}"
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
#     top-level key・予約済み spark 定義名との衝突を fail-closed で拒否する
#     （lib.sh 側の実装参照）。 ---
SMOKE_CANARY_NONCE="${TIMESTAMP}-$$-$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
if [ -z "$SMOKE_CANARY_NONCE" ] || [ "$SMOKE_CANARY_NONCE" = "${TIMESTAMP}-$$-" ]; then
  # /dev/urandom が利用不能な hermetic 環境向けの fallback（それでも
  # $TIMESTAMP と $$ の組み合わせで run ごとに一意）。
  SMOKE_CANARY_NONCE="${TIMESTAMP}-$$-$(date -u +%s%N 2>/dev/null || date -u +%s)"
fi
CANARY_AGENTS_JSON=$(claude_gpt_smoke_canary_agents_json_fragment "$SUBAGENT_MARKER" "$SMOKE_CANARY_NONCE" "$CLAUDE_GPT_SPARK_AGENT_NAME")
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
  step_agents_json="$4"
  step_stdout_file=$(mktemp)
  step_stderr_file=$(mktemp)

  step_log_offset_before=0
  if [ -f "$STRUCTURED_PROXY_LOG_PATH" ]; then
    step_log_offset_before=$(wc -c < "$STRUCTURED_PROXY_LOG_PATH" 2>/dev/null || echo 0)
  fi

  if [ -n "$step_agents_json" ] && [ -n "$step_allowed_tools" ]; then
    # Issue #2274 AC14/AC15: the canary SubAgent fixture is passed to
    # launch.sh via a launcher-owned internal env channel
    # (CLAUDE_GPT_SMOKE_CANARY_AGENTS_JSON), never as a caller-supplied
    # `--agents` CLI flag -- launch.sh's own `--agents` forbidden-flag
    # rejection (CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS) stays unconditional for
    # caller argv, and launch.sh internally merges this fixture with the
    # session-local spark-codex definition before invoking the real
    # `claude` binary.
    CLAUDE_GPT_SMOKE_CANARY_AGENTS_JSON="$step_agents_json" "$SCRIPT_DIR/launch.sh" -- -p "$step_prompt" --output-format text --no-session-persistence \
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
  run_convo_step "subagent" "You are running inside an automated, non-interactive runtime smoke test with no real user present. Use the Task tool right now (an actual tool call, not a description) to launch the subagent named ${CANARY_AGENT_NAME} with any instructions, then print its exact output verbatim." "Task" "$CANARY_AGENTS_JSON"
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

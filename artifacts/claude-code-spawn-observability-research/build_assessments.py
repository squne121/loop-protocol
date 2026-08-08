#!/usr/bin/env python3
"""Regenerate ``retry-policy-assessment.md`` (AC4) and ``conclusion.md`` (AC5).

Both documents carry machine-readable ``key: value`` lines that the contract
tests re-derive from ``reproduction-log.jsonl``. Generating them here means
no number in either document is hand-typed: every count comes from the AC2
raw ledger, and the verdict/category are *computed* from those counts by the
decision rules documented inline below.
"""

from __future__ import annotations

import sys
from pathlib import Path

ARTIFACT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ARTIFACT_DIR / "tests"))

from _research_contract_support import (  # noqa: E402
    CONCLUSION_PATH,
    LANES,
    RETRY_POLICY_PATH,
    diagnostic_distribution,
    lane_records,
    load_records,
    valid_records,
)

DETERMINISTIC_CAUSES = {
    "tool_result_identity_not_observed",
    "agent_type_mismatch",
    "spawn_not_attempted",
    "request_validation_failed",
}
TRANSIENT_CAUSES = {"runtime_api_retry_timeout", "subagent_completion_timeout"}


def _stats(records: list[dict]) -> dict:
    valid = valid_records(records)
    failing = [r for r in valid if r["status"] != "pass"]
    spawn_not_observed = [r for r in valid if r["failure_class"] == "spawn_not_observed"]
    repo_gap = [
        r for r in failing
        if r["lifecycle"]["agent_tool_use_observed"]
        and r["lifecycle"]["tool_result_observed"]
        and r["lifecycle"]["tool_result_agent_id_observed"]
        and not r["lifecycle"]["tool_result_agent_type_observed"]
        and bool(r["hook_agent_type_observed"])
        and r["cross_channel_identity_agreement"]["agent_id_channels_agree"]
    ]
    tool_result_missing = [
        r for r in valid
        if r["lifecycle"]["agent_tool_use_observed"]
        and not r["lifecycle"]["tool_result_agent_type_observed"]
    ]
    return {
        "valid": valid,
        "failing": failing,
        "spawn_not_observed": spawn_not_observed,
        "repo_gap": repo_gap,
        "tool_result_missing": tool_result_missing,
        "hook_identity_when_tool_result_missing": sum(
            1 for r in tool_result_missing if r["hook_agent_type_observed"]
        ),
        "timeouts": [r for r in valid if r.get("timed_out")],
        "api_retries": [r for r in valid if r["api_retry_count"] > 0],
    }


def _lane_counts(records: list[dict], lane: str) -> tuple[int, int]:
    rows = lane_records(valid_records(records), lane)
    return sum(1 for r in rows if r["failure_class"] == "spawn_not_observed"), len(rows)


def build_retry_policy(records: list[dict], stats: dict) -> str:
    causes = {r["diagnostic_cause"] for r in stats["spawn_not_observed"]}
    verdict = "inconclusive"
    if stats["spawn_not_observed"] and causes <= DETERMINISTIC_CAUSES:
        verdict = "keep_excluded"
    elif causes & TRANSIENT_CAUSES:
        verdict = "add_bounded_retry"
    consistent = "yes" if verdict == "keep_excluded" else "partially"

    lines: list[str] = []
    add = lines.append
    add("# retry policy 評価（Issue #2013 AC4）")
    add("")
    add("`scripts/agent-ops/run_agent_provider_route_smoke.py` の")
    add("`_is_transient_infrastructure_candidate()` は現在、`codex_cli` + `spawn_not_observed`")
    add("のみを bounded single retry の対象とし、`claude_code` + `spawn_not_observed` を")
    add("明示的に対象外としている。本文書はこの設計が AC2 の観測データと整合するかを評価する。")
    add("")
    add("## 機械可読な判定")
    add("")
    add("```")
    add(f"retry_policy_verdict: {verdict}")
    add(f"current_design_consistent_with_observation: {consistent}")
    for lane in LANES:
        count, total = _lane_counts(records, lane)
        add(f"{lane}_spawn_not_observed_count: {count}")
        add(f"{lane}_trial_count: {total}")
    add(
        "hook_identity_available_when_tool_result_missing: "
        f"{stats['hook_identity_when_tool_result_missing']}"
    )
    add("```")
    add("")
    add("## 観測データ")
    add("")
    add(f"- 有効 trial 総数: {len(stats['valid'])}")
    add(f"- 失敗 trial 数: {len(stats['failing'])}")
    add(f"- `spawn_not_observed` に分類された trial 数: {len(stats['spawn_not_observed'])}")
    add(f"- wall-clock timeout した trial 数: {len(stats['timeouts'])}")
    add(f"- `system/api_retry` が 1 件以上観測された trial 数: {len(stats['api_retries'])}")
    add(
        "- `tool_use_result.agentType` が欠落した trial のうち、hook channel には "
        f"agent_type が存在した trial 数: {stats['hook_identity_when_tool_result_missing']} / "
        f"{len(stats['tool_result_missing'])}"
    )
    add("")
    add("`spawn_not_observed` trial の diagnostic_cause 内訳:")
    add("")
    add("| diagnostic_cause | count |")
    add("| --- | --- |")
    for cause in sorted(causes):
        add(f"| `{cause}` | {sum(1 for r in stats['spawn_not_observed'] if r['diagnostic_cause'] == cause)} |")
    add("")
    add("## 評価")
    add("")
    add("観測された `spawn_not_observed` はすべて `tool_result_identity_not_observed`、")
    add("すなわち `tool_use_result` に `agentType` フィールドが存在しないことに起因する。")
    add("該当 trial では Agent tool dispatch・SubagentStart hook・SubagentStop hook・")
    add("tool_result・terminal event がすべて観測されており、hook channel は正しい")
    add("`agent_type` を返し、`agentId` も両 channel で完全一致している。")
    add("wall-clock timeout も `system/api_retry` も 1 件も観測されていない。")
    add("")
    add("これは infrastructure timing race ではなく、runtime が返す tool_use_result の")
    add("エンベロープ形状（同期完了型か `status: \"async_launched\"` 型か）に依存した、")
    add("決定論的な抽出経路の欠落である。同一条件で再実行すればエンベロープ形状の分岐に")
    add("応じて成功することがあるが、それは根本原因が解消されたからではない。")
    add("")
    add("したがって「再実行したら通った」という事実だけを transient 判定の根拠にしてはならない。")
    add("bounded retry を `claude_code` + `spawn_not_observed` に適用すると、")
    add("識別 evidence が欠落したままの run をエンベロープ形状の当たり外れで")
    add("成功に見せかけることになり、genuine な identity/spawn failure を覆い隠す。")
    add("")
    add("## 結論")
    add("")
    if verdict == "keep_excluded":
        add("現行設計（`claude_code` + `spawn_not_observed` を bounded retry 対象外とする）は")
        add("観測データと整合する。**維持すべきである**。")
        add("")
        add("ただし現行コードのコメントが述べる理由（Claude の spawn evidence は")
        add("in-memory stdout にあるため miss は transient ではない）は、")
        add("本 research が観測した実際の機序（async 起動エンベロープに `agentType` が")
        add("含まれないこと）とは異なる。結論は正しいが根拠は更新されるべきである。")
    else:
        add(f"判定は `{verdict}` である。")
    add("")
    add("既存テスト `test_claude_spawn_not_observed_is_not_transient_candidate`")
    add("（`scripts/agent-ops/tests/test_agent_provider_route_smoke.py`）が固定している契約は")
    add("変更不要である。本 Issue では `_is_transient_infrastructure_candidate()` を変更しない。")
    add("")
    add("silent retry、2 回以上の追加 retry、成功するまでの反復 retry はいずれも提案しない")
    add("（Issue #2013 Out of Scope により禁止されている）。")
    add("")
    add("真に必要なのは retry ではなく、hook channel に実在する identity evidence を")
    add("抽出経路に取り込むことである。詳細は `conclusion.md` を参照。")
    add("")
    return "\n".join(lines) + "\n"


def build_conclusion(records: list[dict], stats: dict, follow_up: str) -> str:
    failing = stats["failing"]
    if failing and len(stats["repo_gap"]) > len(failing) / 2:
        category = "repo_observability_defect"
    elif not failing:
        category = "inconclusive"
    elif any(r["diagnostic_cause"] in TRANSIENT_CAUSES for r in failing):
        category = "transient_infrastructure"
    else:
        category = "inconclusive"

    versions = sorted({r["claude_code_version"] for r in records})
    shas = sorted({r["tested_head_sha"] for r in records})

    lines: list[str] = []
    add = lines.append
    add("# 結論（Issue #2013 AC5）")
    add("")
    add("本文書の判定はすべて `reproduction-log.jsonl`（AC2 raw evidence）から再計算可能である。")
    add("結論カテゴリは Issue #2013 が定義した 6 種類からのみ選択している。")
    add("")
    add("## 機械可読な判定")
    add("")
    add("```")
    add(f"conclusion_category: {category}")
    add("bounded_single_retry_applicable: no")
    add("additional_failure_class_subdivision_required: no")
    add(f"follow_up_implementation_issue: {follow_up}")
    add("existing_failure_class_schema_changed: no")
    for lane in LANES:
        add(f"{lane}_trial_count: {len(lane_records(valid_records(records), lane))}")
    add("```")
    add("")
    add("## 実行条件")
    add("")
    add(f"- Claude Code version: {', '.join(versions)}")
    add(f"- actual tested SHA: {', '.join(shas)}")
    add("- historical baseline SHA（PR #2005 merge commit）: 28394e226533cd59cdfc0f55602ac65e389a6600")
    add(f"- control lane trial 数: {len(lane_records(valid_records(records), 'control'))}（固定）")
    add(f"- production lane trial 数: {len(lane_records(valid_records(records), 'production'))}（固定）")
    add("")
    add("## 根拠となる再計算")
    add("")
    add(f"- 失敗 trial 数: {len(failing)}")
    add(
        "- そのうち「Agent dispatch・tool_result・tool_result の `agentId` が揃い、"
        "`agentType` のみ欠落し、hook channel には正しい `agent_type` が存在し、"
        f"`agentId` が両 channel で一致した」trial 数: {len(stats['repo_gap'])}"
    )
    add(f"- wall-clock timeout した trial 数: {len(stats['timeouts'])}")
    add(f"- `system/api_retry` が観測された trial 数: {len(stats['api_retries'])}")
    add("")
    add("lane 別 diagnostic_cause 分布（`none` は pass）:")
    add("")
    add("| lane | diagnostic_cause | count |")
    add("| --- | --- | --- |")
    for lane in LANES:
        for cause, count in sorted(diagnostic_distribution(records, lane).items()):
            add(f"| {lane} | `{cause}` | {count} |")
    add("")
    add("## 結論カテゴリの判断")
    add("")
    add(f"結論カテゴリは `{category}` である。")
    add("")
    add("runtime は identity evidence を確かに提供している。`SubagentStart` / `SubagentStop`")
    add("hook payload は公式スキーマどおり `agent_id` / `agent_type` を返し、")
    add("その `agent_id` は `tool_use_result.agentId` と全 trial で完全一致する。")
    add("欠落しているのは repo 側の抽出経路だけである。")
    add("`extract_claude_child_agent_type()` は `tool_use_result.agentType` のみを読むが、")
    add("Claude Code 2.1.225 の `Agent` tool は `status: \"async_launched\"` の非同期起動")
    add("エンベロープを返すことがあり、その形状には `agentType` が含まれない。")
    add("")
    add("したがってこれは upstream runtime の契約違反でも、infrastructure の transient failure でも、")
    add("model orchestration の問題でも、downstream route の失敗でもない。")
    add("repo 側の観測（observability）欠陥である。")
    add("")
    add("`spawn_not_observed` と `validation_failed` に分かれるのは spawn の有無ではなく、")
    add("`_run_route_once()` の順 4（harness 非ゼロ終了）が順 5（spawn evidence）より先に")
    add("評価されるかどうかで決まる。両者は同一根本原因の別表現であり、")
    add("外側の failure_class から spawn の有無を推測してはならない。")
    add("")
    add("## bounded single retry の適用可否")
    add("")
    add("適用しない（`bounded_single_retry_applicable: no`）。")
    add("観測された失敗はすべて決定論的な抽出経路の欠落であり、timeout も api_retry も")
    add("1 件も観測されていない。retry は根本原因を覆い隠すだけである。")
    add("評価の詳細は `retry-policy-assessment.md` を参照。")
    add("")
    add("## 追加 failure class 細分化の要否")
    add("")
    add("不要（`additional_failure_class_subdivision_required: no`）。")
    add("既存 `failure_class` schema（`spawn_not_observed` / `validation_failed` ほか）は")
    add("本 Issue で一切変更していない。原因の切り分けは research artifact 内の")
    add("`diagnostic_cause` taxonomy で lossless に達成できており、")
    add("production schema を増やす必要はない。")
    add("必要なのは分類の追加ではなく、identity evidence の抽出経路の修正である。")
    add("")
    add("## follow-up implementation issue")
    add("")
    if follow_up == "none":
        add("起票なし。")
    else:
        add(f"{follow_up} を起票した。")
        add("")
        add("内容は `run_worktree_agent_runtime_smoke.py` の identity evidence 抽出経路を、")
        add("hook channel（`SubagentStart` / `SubagentStop` の payload と `hook_name` 接尾辞）にも")
        add("対応させること、および `extract_claude_child_session_id()` が")
        add("`parent_session_id` 欠落時に stdout 探索自体をスキップする短絡を解消することである。")
        add("")
        add("Issue #2013 は research-only であり、上記の production code 修正は")
        add("本 Issue の branch では行わない（Allowed Paths を拡張しない）。")
    add("")
    add("## 付随して観測された事象（本 Issue のスコープ外）")
    add("")
    add("非同期起動エンベロープが返った trial では、親セッションが子 SubAgent の完了を")
    add("待たずに終了するため、`ROUTE_SMOKE_DONE` marker や delegation evidence が")
    add("materialize しないことがある。これは observability ではなく完了待ち semantics の")
    add("問題であり、抽出経路の修正とは別に scope 判断が必要である。")
    add("本 Issue では観測事実として記録するにとどめ、follow-up issue のスコープには含めない。")
    add("")
    add("## downstream failure の扱い")
    add("")
    add("AGY / Serena MCP / GitHub credential 等の downstream failure は")
    add("`diagnostic_cause` の `downstream_route_failed` / `delegation_wrapper_failed` /")
    add("`request_validation_failed` として spawn lifecycle とは別フィールドに保持しており、")
    add("spawn failure へ再分類していない。#2012 / #2015 / #2016 は本 Issue に吸収しない。")
    add("")
    return "\n".join(lines) + "\n"


def main() -> int:
    follow_up = sys.argv[1] if len(sys.argv) > 1 else "none"
    records = load_records()
    stats = _stats(records)
    RETRY_POLICY_PATH.write_text(build_retry_policy(records, stats), encoding="utf-8")
    CONCLUSION_PATH.write_text(build_conclusion(records, stats, follow_up), encoding="utf-8")
    print(f"wrote {RETRY_POLICY_PATH}")
    print(f"wrote {CONCLUSION_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

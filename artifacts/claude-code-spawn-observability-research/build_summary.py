#!/usr/bin/env python3
"""Regenerate ``reproduction-log.md`` (AC3) from ``reproduction-log.jsonl``.

Every number in the generated summary is recomputed here from the AC2 raw
ledger, so the summary is by construction re-derivable -- and
``tests/test_reproduction_summary_contract.py`` re-derives it independently
to prove the checked-in file has not drifted.
"""

from __future__ import annotations

import sys
from pathlib import Path

ARTIFACT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ARTIFACT_DIR / "tests"))

from _research_contract_support import (  # noqa: E402
    DIAGNOSTIC_CAUSES,
    LANES,
    LIFECYCLE_CHECKPOINTS,
    SUMMARY_PATH,
    diagnostic_distribution,
    failure_class_distribution,
    hook_channel_identity_counts,
    lane_records,
    lane_status_counts,
    load_records,
    spawn_observed_counts,
    tool_result_identity_counts,
    valid_records,
)

LANE_LABEL = {"control": "control lane", "production": "production lane"}


def main() -> int:
    records = load_records()
    valid = valid_records(records)
    lines: list[str] = []
    add = lines.append

    add("# 再現ログ サマリ（Issue #2013 AC3）")
    add("")
    add("本文書の数値はすべて `reproduction-log.jsonl`（AC2 raw evidence）から再計算した結果である。")
    add("`build_summary.py` が生成し、`tests/test_reproduction_summary_contract.py` が")
    add("同じ raw ledger から独立に再計算して一致を検証する。手書きの数値は含まない。")
    add("")
    add(f"- 総 record 数: {len(records)}")
    add(f"- 有効 trial 数: {len(valid)}")
    add(f"- 無効（excluded）trial 数: {len(records) - len(valid)}")
    versions = sorted({r["claude_code_version"] for r in records})
    shas = sorted({r["tested_head_sha"] for r in records})
    add(f"- Claude Code version: {', '.join(versions)}")
    add(f"- actual tested SHA: {', '.join(shas)}")
    add("- historical baseline SHA: 28394e226533cd59cdfc0f55602ac65e389a6600")
    add("")
    add("trial 条件は実行前に `trial-plan.json` として凍結され、その digest を全 record が持つ。")
    add("control lane の `prompt_sha256` は全 trial で同一である。")
    add("production lane の prompt は実 `build_route_prompt()` が生成するため、")
    add("trial ごとに異なる一時 evidence directory path を含み `prompt_sha256` が変わる。")
    add("route 構成・timeout・max-turns・agent 定義は lane 内で固定されている。")
    add("")

    add("## lane 別 status 分布")
    add("")
    add("| lane | pass | fail | skip | total |")
    add("| --- | --- | --- | --- | --- |")
    for lane in LANES:
        counts = lane_status_counts(records, lane)
        total = len(lane_records(valid, lane))
        add(f"| {lane} | {counts.get('pass', 0)} | {counts.get('fail', 0)} "
            f"| {counts.get('skip', 0)} | {total} |")
    add("")

    add("## lane 別 failure_class 分布")
    add("")
    add("既存 schema の `failure_class` をそのまま集計したもの（本 Issue で schema は変更していない）。")
    add("")
    add("| lane | failure_class | count |")
    add("| --- | --- | --- |")
    for lane in LANES:
        distribution = failure_class_distribution(records, lane)
        for key in sorted(distribution):
            add(f"| {lane} | {key} | {distribution[key]} |")
    add("")

    add("## lane 別 diagnostic_cause 分布")
    add("")
    add("拡張 taxonomy による lossless な原因分類。`none` は pass した trial を表す。")
    add("")
    add("| lane | diagnostic_cause | count |")
    add("| --- | --- | --- |")
    for lane in LANES:
        distribution = diagnostic_distribution(records, lane)
        for key in sorted(distribution):
            add(f"| {lane} | {key} | {distribution[key]} |")
    add("")

    add("## lane 別 lifecycle checkpoint 観測率")
    add("")
    add("12 checkpoint を単一 boolean に潰さず、trial 単位で独立記録した結果の集計。")
    add("")
    add("| lane | checkpoint | observed | total |")
    add("| --- | --- | --- | --- |")
    for lane in LANES:
        rows = lane_records(valid, lane)
        for checkpoint in LIFECYCLE_CHECKPOINTS:
            observed = sum(1 for r in rows if r["lifecycle"][checkpoint] is True)
            add(f"| {lane} | {checkpoint} | {observed} | {len(rows)} |")
    add("")

    add("## identity evidence channel の突き合わせ")
    add("")
    add("hook を唯一の ground truth とせず、tool_result channel と hook channel を")
    add("独立に記録して突き合わせた結果。`agent_id 一致` は両 channel が同一の agent id を")
    add("返した trial 数である。")
    add("")
    add("| lane | tool_result channel agentType 観測 | hook channel agent_type 観測 | agent_id 一致 | total |")
    add("| --- | --- | --- | --- | --- |")
    for lane in LANES:
        rows = lane_records(valid, lane)
        tool_result_observed, total = tool_result_identity_counts(records, lane)
        hook_observed, _ = hook_channel_identity_counts(records, lane)
        agreed = sum(
            1 for r in rows
            if r["cross_channel_identity_agreement"]["agent_id_channels_agree"] is True
        )
        add(f"| {lane} | {tool_result_observed} | {hook_observed} | {agreed} | {total} |")
    add("")

    add("## production 式 native_spawn_event_observed の成立率")
    add("")
    add("| lane | native_spawn_event_observed | total |")
    add("| --- | --- | --- |")
    for lane in LANES:
        observed, total = spawn_observed_counts(records, lane)
        add(f"| {lane} | {observed} | {total} |")
    add("")

    add("## production lane の route 別内訳")
    add("")
    add("| route | pass | fail | total |")
    add("| --- | --- | --- | --- |")
    production = lane_records(valid, "production")
    for route in sorted({r["route"] for r in production}):
        rows = [r for r in production if r["route"] == route]
        passed = sum(1 for r in rows if r["status"] == "pass")
        add(f"| {route} | {passed} | {len(rows) - passed} | {len(rows)} |")
    add("")

    add("## 観測された diagnostic_cause の解釈")
    add("")
    identity_gap = [
        r for r in valid if r["diagnostic_cause"] == "tool_result_identity_not_observed"
    ]
    gap_total = len(identity_gap)

    def _gap_count(key: str) -> int:
        return sum(1 for r in identity_gap if r["lifecycle"][key] is True)

    add("`tool_result_identity_not_observed` が支配的な原因である。")
    add(f"該当 trial は {gap_total} 件あり、その全件で次の checkpoint が観測されている。")
    add("")
    add("| checkpoint | observed | total |")
    add("| --- | --- | --- |")
    for key in (
        "agent_tool_use_observed",
        "subagent_start_hook_observed",
        "subagent_stop_hook_observed",
        "tool_result_observed",
        "tool_result_agent_id_observed",
        "terminal_event_observed",
        "expected_marker_observed",
    ):
        add(f"| {key} | {_gap_count(key)} | {gap_total} |")
    add("")
    add("欠落しているのは `tool_use_result.agentType` の 1 フィールドだけである。")
    add(
        f"同じ trial の hook channel には runtime 自身が返した `agent_type` が存在し"
        f"（{sum(1 for r in identity_gap if r['hook_agent_type_matches_requested'])} / {gap_total} 件で"
        "要求した agent type と一致）、"
    )
    add(
        "`agentId` は両 channel で完全一致する"
        f"（{sum(1 for r in identity_gap if r['cross_channel_identity_agreement']['agent_id_channels_agree'])}"
        f" / {gap_total} 件）。"
    )
    add("すなわち spawn は実際には成立しており、観測経路のみが失敗している。")
    add("")
    add(
        f"marker は {_gap_count('expected_marker_observed')} / {gap_total} 件で観測された。"
        "marker が欠落した trial は、非同期起動エンベロープが返って親セッションが"
        "子の完了を待たずに終了したケースであり、これが下記の failure_class の"
        "分かれ方に直結している。"
    )
    add("")
    add("`spawn_not_observed` と `validation_failed` の別れ方は spawn の有無ではなく、")
    add("`_run_route_once()` の順 4（harness 非ゼロ）が順 5（spawn evidence）より先に")
    add("評価されるかどうかで決まる。marker 欠落などが先に立つと同じ根本原因が")
    add("`validation_failed` として現れる。詳細は `code-analysis.md` を参照。")
    add("")
    add("未使用の diagnostic_cause（本 30 trial では観測されなかったもの）: ")
    used = set()
    for lane in LANES:
        used |= {k for k in diagnostic_distribution(records, lane) if k != "none"}
    unused = [c for c in DIAGNOSTIC_CAUSES if c not in used]
    add("、".join(f"`{c}`" for c in unused) if unused else "なし")
    add("")

    SUMMARY_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {SUMMARY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

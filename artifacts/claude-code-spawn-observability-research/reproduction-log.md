# 再現ログ サマリ（Issue #2013 AC3）

本文書の数値はすべて `reproduction-log.jsonl`（AC2 raw evidence）から再計算した結果である。
`build_summary.py` が生成し、`tests/test_reproduction_summary_contract.py` が
同じ raw ledger から独立に再計算して一致を検証する。手書きの数値は含まない。

- 総 record 数: 30 件
- 有効 trial 数: 30 件
- 無効（excluded）trial 数: 0 件
- 実行に用いた Claude Code version: 2.1.225 (Claude Code)
- 実際に試験した SHA（actual tested SHA）: 9eca2f0074552a3e0687b0c81ee94b62122890a0
- 過去の基準点（historical baseline SHA）: 28394e226533cd59cdfc0f55602ac65e389a6600

trial 条件は実行前に `trial-plan.json` として凍結され、その digest を全 record が持つ。
control lane の `prompt_sha256` は全 trial で同一である。
production lane の prompt は実 `build_route_prompt()` が生成するため、
trial ごとに異なる一時 evidence directory path を含み `prompt_sha256` が変わる。
route 構成・timeout・max-turns・agent 定義は lane 内で固定されている。

## lane 別 status 分布

| lane | pass | fail | skip | total |
| --- | --- | --- | --- | --- |
| control | 4 | 11 | 0 | 15 |
| production | 2 | 13 | 0 | 15 |

## lane 別 failure_class 分布

既存 schema の `failure_class` をそのまま集計したもの（本 Issue で schema は変更していない）。

| lane | failure_class | count |
| --- | --- | --- |
| control | none | 4 |
| control | spawn_not_observed | 11 |
| production | none | 2 |
| production | provider_mismatch | 1 |
| production | spawn_not_observed | 6 |
| production | validation_failed | 6 |

## lane 別 diagnostic_cause 分布

拡張 taxonomy による lossless な原因分類。`none` は pass した trial を表す。

| lane | diagnostic_cause | count |
| --- | --- | --- |
| control | none | 4 |
| control | tool_result_identity_not_observed | 11 |
| production | delegation_wrapper_failed | 1 |
| production | none | 2 |
| production | request_validation_failed | 3 |
| production | tool_result_identity_not_observed | 9 |

## lane 別 lifecycle checkpoint 観測率

12 checkpoint を単一 boolean に潰さず、trial 単位で独立記録した結果の集計。

| lane | checkpoint | observed | total |
| --- | --- | --- | --- |
| control | process_started | 15 | 15 |
| control | system_init_observed | 15 | 15 |
| control | agent_tool_use_observed | 15 | 15 |
| control | subagent_start_hook_observed | 15 | 15 |
| control | subagent_stop_hook_observed | 15 | 15 |
| control | tool_result_observed | 15 | 15 |
| control | tool_result_agent_id_observed | 15 | 15 |
| control | tool_result_agent_type_observed | 4 | 15 |
| control | agent_type_matches_requested | 4 | 15 |
| control | terminal_event_observed | 15 | 15 |
| control | expected_marker_observed | 15 | 15 |
| control | delegation_request_validated | 15 | 15 |
| production | process_started | 15 | 15 |
| production | system_init_observed | 15 | 15 |
| production | agent_tool_use_observed | 15 | 15 |
| production | subagent_start_hook_observed | 15 | 15 |
| production | subagent_stop_hook_observed | 15 | 15 |
| production | tool_result_observed | 15 | 15 |
| production | tool_result_agent_id_observed | 15 | 15 |
| production | tool_result_agent_type_observed | 6 | 15 |
| production | agent_type_matches_requested | 6 | 15 |
| production | terminal_event_observed | 15 | 15 |
| production | expected_marker_observed | 12 | 15 |
| production | delegation_request_validated | 7 | 15 |

## identity evidence channel の突き合わせ

hook を唯一の ground truth とせず、tool_result channel と hook channel を
独立に記録して突き合わせた結果。`agent_id 一致` は両 channel が同一の agent id を
返した trial 数である。

| lane | tool_result channel agentType 観測 | hook channel agent_type 観測 | agent_id 一致 | total |
| --- | --- | --- | --- | --- |
| control | 4 | 15 | 15 | 15 |
| production | 6 | 15 | 15 | 15 |

## production 式 native_spawn_event_observed の成立率

| lane | native_spawn_event_observed | total |
| --- | --- | --- |
| control | 4 | 15 |
| production | 6 | 15 |

## production lane の route 別内訳

| route | pass | fail | total |
| --- | --- | --- | --- |
| claude_code:codebase-investigator:github_research | 0 | 5 | 5 |
| claude_code:codebase-investigator:local_asset_research | 0 | 5 | 5 |
| claude_code:web-researcher:grounded_research | 2 | 3 | 5 |

## 観測された diagnostic_cause の解釈

`tool_result_identity_not_observed` が支配的な原因である。
該当 trial は 20 件あり、その全件で次の checkpoint が観測されている。

| checkpoint | observed | total |
| --- | --- | --- |
| agent_tool_use_observed | 20 | 20 |
| subagent_start_hook_observed | 20 | 20 |
| subagent_stop_hook_observed | 20 | 20 |
| tool_result_observed | 20 | 20 |
| tool_result_agent_id_observed | 20 | 20 |
| terminal_event_observed | 20 | 20 |
| expected_marker_observed | 17 | 20 |

欠落しているのは `tool_use_result.agentType` の 1 フィールドだけである。
同じ trial の hook channel には runtime 自身が返した `agent_type` が存在し（20 / 20 件で要求した agent type と一致）、
`agentId` は両 channel で完全一致する（20 / 20 件）。
すなわち spawn は実際には成立しており、観測経路のみが失敗している。

marker は 17 / 20 件で観測された。marker が欠落した trial は、非同期起動エンベロープが返って親セッションが子の完了を待たずに終了したケースであり、これが下記の failure_class の分かれ方に直結している。

`spawn_not_observed` と `validation_failed` の別れ方は spawn の有無ではなく、
`_run_route_once()` の順 4（harness 非ゼロ）が順 5（spawn evidence）より先に
評価されるかどうかで決まる。marker 欠落などが先に立つと同じ根本原因が
`validation_failed` として現れる。詳細は `code-analysis.md` を参照。

未使用の diagnostic_cause（本 30 trial では観測されなかったもの）: 
`spawn_not_attempted`、`subagent_start_not_observed`、`subagent_completion_timeout`、`agent_type_mismatch`、`runtime_api_retry_timeout`、`runtime_nonzero`、`terminal_event_missing`、`marker_not_observed`、`downstream_route_failed`


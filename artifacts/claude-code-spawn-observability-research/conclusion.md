# 結論（Issue #2013 AC5）

本文書の判定はすべて `reproduction-log.jsonl`（AC2 raw evidence）から再計算可能である。
結論カテゴリは Issue #2013 が定義した 6 種類からのみ選択している。

## 機械可読な判定

```yaml
conclusion_category: repo_observability_defect
bounded_single_retry_applicable: no
additional_failure_class_subdivision_required: no
follow_up_implementation_issue: #2021
existing_failure_class_schema_changed: no
control_trial_count: 15
production_trial_count: 15
```

## 実行条件

- 実行に用いた Claude Code version: 2.1.225 (Claude Code)
- 実際に試験した SHA（actual tested SHA）: 9eca2f0074552a3e0687b0c81ee94b62122890a0
- 過去の基準点（historical baseline SHA、PR #2005 の merge commit）: 28394e226533cd59cdfc0f55602ac65e389a6600
- control lane trial 数: 15（固定）
- production lane trial 数: 15（固定）

## 根拠となる再計算

- 失敗 trial 数: 24
- そのうち「Agent dispatch・tool_result・tool_result の `agentId` が揃い、`agentType` のみ欠落し、hook channel には正しい `agent_type` が存在し、`agentId` が両 channel で一致した」trial 数: 20
- wall-clock timeout した trial 数: 0
- `system/api_retry` が観測された trial 数: 0

lane 別 diagnostic_cause 分布（`none` は pass）:

| lane | diagnostic_cause | count |
| --- | --- | --- |
| control | `none` | 4 |
| control | `tool_result_identity_not_observed` | 11 |
| production | `delegation_wrapper_failed` | 1 |
| production | `none` | 2 |
| production | `request_validation_failed` | 3 |
| production | `tool_result_identity_not_observed` | 9 |

## 結論カテゴリの判断

結論カテゴリは `repo_observability_defect` である。

runtime は identity evidence を確かに提供している。`SubagentStart` / `SubagentStop`
hook payload は公式スキーマどおり `agent_id` / `agent_type` を返し、
その `agent_id` は `tool_use_result.agentId` と全 trial で完全一致する。
欠落しているのは repo 側の抽出経路だけである。
`extract_claude_child_agent_type()` は `tool_use_result.agentType` のみを読むが、
Claude Code 2.1.225 の `Agent` tool は `status: "async_launched"` の非同期起動
エンベロープを返すことがあり、その形状には `agentType` が含まれない。

したがってこれは upstream runtime の契約違反でも、infrastructure の transient failure でも、
model orchestration の問題でも、downstream route の失敗でもない。
repo 側の観測（observability）欠陥である。

`spawn_not_observed` と `validation_failed` に分かれるのは spawn の有無ではなく、
`_run_route_once()` の順 4（harness 非ゼロ終了）が順 5（spawn evidence）より先に
評価されるかどうかで決まる。両者は同一根本原因の別表現であり、
外側の failure_class から spawn の有無を推測してはならない。

## bounded single retry の適用可否

適用しない（`bounded_single_retry_applicable: no`）。
観測された失敗はすべて決定論的な抽出経路の欠落であり、timeout も api_retry も
1 件も観測されていない。retry は根本原因を覆い隠すだけである。
評価の詳細は `retry-policy-assessment.md` を参照。

## 追加 failure class 細分化の要否

不要（`additional_failure_class_subdivision_required: no`）。
既存 `failure_class` schema（`spawn_not_observed` / `validation_failed` ほか）は
本 Issue で一切変更していない。原因の切り分けは research artifact 内の
`diagnostic_cause` taxonomy で lossless に達成できており、
production schema を増やす必要はない。
必要なのは分類の追加ではなく、identity evidence の抽出経路の修正である。

## 追従実装 Issue（follow-up implementation issue）

#2021 を起票した。

内容は `run_worktree_agent_runtime_smoke.py` の identity evidence 抽出経路を、
hook channel（`SubagentStart` / `SubagentStop` の payload と `hook_name` 接尾辞）にも
対応させること、および `extract_claude_child_session_id()` が
`parent_session_id` 欠落時に stdout 探索自体をスキップする短絡を解消することである。

Issue #2013 は research-only であり、上記の production code 修正は
本 Issue の branch では行わない（Allowed Paths を拡張しない）。

## 付随して観測された事象（本 Issue のスコープ外）

非同期起動エンベロープが返った trial では、親セッションが子 SubAgent の完了を
待たずに終了するため、`ROUTE_SMOKE_DONE` marker や delegation evidence が
materialize しないことがある。これは observability ではなく完了待ち semantics の
問題であり、抽出経路の修正とは別に scope 判断が必要である。
本 Issue では観測事実として記録するにとどめ、follow-up issue のスコープには含めない。

## downstream failure の扱い

AGY / Serena MCP / GitHub credential 等の downstream failure は
`diagnostic_cause` の `downstream_route_failed` / `delegation_wrapper_failed` /
`request_validation_failed` として spawn lifecycle とは別フィールドに保持しており、
spawn failure へ再分類していない。#2012 / #2015 / #2016 は本 Issue に吸収しない。


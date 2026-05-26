---
status: accepted
issue: "#284"
parent_issue: "#254"
doc_id: playtest-log
canonical_source: docs/product/playtest-log.md
sdd_boundary: template
template_only: true
trace_links:
  - docs/product/playtest-protocol.md
---

# Playtest Log Template

## Intent
この文書は、プレイテスト中に得られた観察事実、プレイヤーの反応、および開発チームによる決定事項を記録するための YAML テンプレートとスキーマを定義する。
実運用では、各セッションごとに本テンプレートをコピーして使用するか、外部ツールにエクスポートする。

## Entry Schema

```yaml
session_id: "PT-YYYYMMDD-001"
date: "YYYY-MM-DD"
build_ref: "<commit sha or PR #>"
tester_profile: "anonymous-target-player | member"
environment: "browser-chrome | local-dev"
session_goal: "<検証する仮説 ID または特定の目的>"

playtest_entry:
  task_id: "TASK-001"
  hypothesis_id: "MVP-HYP-001"
  observed_behavior: "" # 実際に起きた事実（何を迷ったか、どう操作したか）
  player_quote: ""      # プレイヤーの発話引用（PII 禁止）
  emotional_signal: "" # frustration | confusion | boredom | excitement 等
  developer_interpretation: "" # 開発側の解釈（なぜそうなったか）
  player_suggested_fix: ""    # プレイヤーからの提案（採用義務なし）
  affected_requirements:
    - "REQ-001"
  classification: "bug | balance/tuning | design hypothesis invalidated | unclear/needs-more-data"
  severity: "low | medium | high | blocker"
  confidence: "low | medium | high"
  decision: "defer | spec_delta_issue | implementation_issue | no_action"
  proposed_spec_delta: ""
  linked_issue: "#NNN"
  validation_method: "next playtest | automated test | manual reproduction"
```

## Example Entry (Draft)

```yaml
session_id: "PT-20260527-001"
date: "2026-05-27"
build_ref: "3f0e79d"
tester_profile: "anonymous-target-player"
environment: "browser-chrome"
session_goal: "MVP-HYP-001: 60Hz 動作時の操作感が滑らかに感じられるか"

playtest_entry:
  task_id: "TASK-001"
  hypothesis_id: "MVP-HYP-001"
  observed_behavior: "プレイヤーは自機の移動速度が速すぎると感じ、何度も壁に衝突した"
  player_quote: "「速すぎて制御できない、もっとゆっくり動いてほしい」"
  emotional_signal: "frustration"
  developer_interpretation: "現在の速度定数が 60Hz 駆動に対して高すぎる可能性がある"
  player_suggested_fix: "移動速度を 20% 下げる"
  affected_requirements:
    - "REQ-LOGIC-001"
  classification: "balance/tuning"
  severity: "medium"
  confidence: "high"
  decision: "implementation_issue"
  proposed_spec_delta: "N/A (定数調整のみ)"
  linked_issue: "#286"
  validation_method: "automated test"
```

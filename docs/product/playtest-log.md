---
status: draft
issue: "#284"
parent_issue: "#254"
doc_id: playtest-log
canonical_source: docs/product/playtest-log.md
sdd_boundary: template
template_only: true
trace_links:
  - docs/product/playtest-protocol.md
  - docs/product/mvp-scope.md
---

# Playtest Log Template

## Intent
この文書は、プレイテスト中に得られた観察事実、プレイヤーの反応、および開発チームによる決定事項を記録するための YAML テンプレートとスキーマを定義する。
`docs/product/mvp-scope.md` の Measurement Contract に基づき、仮説検証の証跡として機能する。

## Entry Schema

```yaml
session_id: "PT-YYYYMMDD-001"
date: "YYYY-MM-DD"
build_ref: "<commit-sha-or-release-tag>"
execution:
  mode: "human_developer | ai_agent | scripted_automation | ci_browser_automation"
  actor_id: "developer-self | combat-agent-v1 | playwright-ci"
  trigger: "manual | ci | agent_loop"
environment: "browser-chrome | local-dev | automated-ci"
privacy:
  pii_reviewed: true
  raw_recording_committed: false

# AI/Automation Metadata (Optional)
# Default: automation: null
automation:
  agent_profile: ""
  random_seed: null
  reproduction_command: ""
  metrics:
    # Invariant: success_rate = success_count / runs
    runs: 0
    success_count: 0
    failure_count: 0
    success_rate: null          # 0.0 - 1.0
    death_count: 0
    death_rate: null
    duration:
      p50: 0
      p95: 0
      unit: "ms"
    input_count_total: 0
    collision_count_total: 0
  artifacts:
    # WARNING: DO NOT commit raw recordings or PII to the repository.
    # Ensure privacy.pii_reviewed is true before referencing artifacts.
    metrics_json: ""
    trace_ref: ""
    replay_ref: ""
    input_script_ref: ""
    trace_redacted: true        # Required for secure handling
  human_review_required: false  # UX影響・違和感がある場合は true

playtest_entries:
  - entry_id: "PTE-001"
    task_id: "TASK-001"
    hypothesis_id: "HYP-MVP-001"
    observed_behavior: ""      # 実際に起きた事実（何を迷ったか、どう操作したか）
    player_quote_redacted: ""  # 匿名化されたプレイヤーの発話
    emotional_signal: ""      # frustration | confusion | boredom | excitement 等
    developer_interpretation: "" # 開発側の解釈
    player_suggested_fix: ""    # プレイヤーからの提案
    
    # MVP Measurement Contract Fields
    observable_signal: ""      # 観測された信号（mvp-scope.md 準拠）
    collection_method: ""      # 収集方法（mvp-scope.md 準拠）
    success_failure_assessment: "success | failure | inconclusive"
    misread_notes: ""          # 誤認やノイズの記録
    tunable_parameter: ""      # 調整対象のパラメータ名
    retest_target: "true | false" # 再検証が必要か
    
    affected_requirements:
      - "REQ-001"
    classification: "bug | balance/tuning | design hypothesis invalidated | unclear_needs_more_data"
    severity: "low | medium | high | blocker"
    confidence: "low | medium | high"
    decision: "no_action | defer | spec_delta_issue | implementation_issue | retest"
    proposed_spec_delta: ""
    linked_issue: "#NNN"
    validation_method: "human_internal | ai_simulation | browser_automation | manual_reproduction"
```


## Policies
- `ai_result_is_ux_evidence: false` # AI結果を直接のUX証拠としない

## Example Entry (Draft)

```yaml
session_id: "PT-20260527-001"
date: "2026-05-27"
build_ref: "3f0e79d"
execution:
  mode: "human_developer"
  actor_id: "developer-self"
  trigger: "manual"
environment: "browser-chrome"
privacy:
  pii_reviewed: true
  raw_recording_committed: false

automation:
  agent_profile: "combat-agent-v1"
  random_seed: 42
  reproduction_command: "pnpm test:e2e --grep @combat"
  metrics:
    runs: 100
    success_count: 85
    failure_count: 15
    success_rate: 0.85
    death_count: 10
    death_rate: 0.1
    duration:
      p50: 12000
      p95: 15500
      unit: "ms"
    input_count_total: 4500
    collision_count_total: 120
  artifacts:
    metrics_json: "external-secure-storage:PT-20260527-001/metrics.json"
    trace_ref: "external-secure-storage:PT-20260527-001/trace.zip"
    replay_ref: "external-secure-storage:PT-20260527-001/replay.mp4"
    input_script_ref: "tests/e2e/scenarios/combat-stress-test.js"
    trace_redacted: true
  human_review_required: true

playtest_entries:
  - entry_id: "PTE-001"
    task_id: "TASK-001"
    hypothesis_id: "HYP-MVP-001"
    observed_behavior: "プレイヤーは自機の移動速度が速すぎると感じ、何度も壁に衝突した"
    player_quote_redacted: "「速すぎて制御できない、もっとゆっくり動いてほしい」"
    emotional_signal: "frustration"
    developer_interpretation: "現在の速度定数が 60Hz 駆動に対して高すぎる可能性がある"
    
    observable_signal: "主要因として player intervention が語られるか"
    collection_method: "self-explanation prompt"
    success_failure_assessment: "success"
    misread_notes: "特になし"
    tunable_parameter: "PLAYER_SPEED"
    retest_target: false
    
    affected_requirements:
      - "REQ-LOGIC-001"
    classification: "balance/tuning"
    severity: "medium"
    confidence: "high"
    decision: "implementation_issue"
    proposed_spec_delta: ""
    linked_issue: "#000"
    validation_method: "browser_automation"
```

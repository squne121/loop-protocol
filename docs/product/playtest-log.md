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
この文書は、プレイテスト中に得られた観察事実、プレイヤーの反応、および開発チームによる決定事項を記録する ための YAML テンプレートとスキーマを定義する。
`docs/product/mvp-scope.md` は現在ドラフト段階であるため、本テンプレートは「厳密な測定の契約 (Normative Measurement Contract)」ではなく、プレイテストログを記録するための「実運用向けのワーキングフォーマット (Working Format)」として位置付ける。

## Entry Schema

```yaml
session_id: "PT-YYYYMMDD-001"
date: "YYYY-MM-DD"
build_ref: "<commit-sha-or-release-tag>"
session_mode: "human_internal | human_external | ai_simulation | browser_automation"
tester_profile: "developer-self | combat-agent-v1 | playwright-ci"
environment: "browser-chrome | local-dev | automated-ci"
privacy:
  pii_reviewed: true
  raw_recording_committed: false

# AI/Automation Metadata (Optional)
# REQUIRED for ai_simulation or browser_automation. MUST be null for human-only sessions.
# Default: automation: null
automation:
  execution:
    actor_id: "developer-self | combat-agent-v1 | playwright-ci"
    trigger: "manual | ci | agent_loop"
  agent_profile: ""
  random_seed: null
  reproduction_command: ""
  metrics:
    # Invariant: success_rate = success_count / runs
    computed_by: "agent_exporter | ci_scripts | manual_aggregation"
    source: "runtime_telemetry | playwright_trace | vitest_json"
    sample_unit: "runs | tasks | sortie"
    runs: 0
    success_count: 0
    failure_count: 0
    success_rate: null          # 0.0 - 1.0
    death_count: 0
    # Invariant: death_rate = death_count / runs
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
    storage_class: "local_git | external_bucket | secure_vault"
    artifact_contains: ["dom_snapshot", "screenshot", "video", "telemetry"]
    public_repo_safe: false
    metrics_json: ""
    trace_ref: ""
    replay_ref: ""
    input_script_ref: ""
    raw_artifact_committed: false
    artifact_sensitivity: "none | telemetry | screenshot | video | trace_with_dom"
    redaction_status: "not_applicable | pending | reviewed"
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
- **UX Evidence Policy Invariant**: `session_mode` が AI/Automation かつ（`classification` が 'design hypothesis invalidated' または `decision` が 'spec_delta_issue'）の場合、`automation.human_review_required` は必ず `true` でなければならない。

## Example Entry (Draft)

### Example 1: Human Internal Session
```yaml
session_id: "PT-20260527-001"
date: "2026-05-27"
build_ref: "3f0e79d"
session_mode: "human_internal"
tester_profile: "developer-self"
environment: "browser-chrome"
privacy:
  pii_reviewed: true
  raw_recording_committed: false

automation: null

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
    validation_method: "human_internal"
```

### Example 2: Browser Automation Session
```yaml
session_id: "PT-20260527-002"
date: "2026-05-27"
build_ref: "3f0e79d"
session_mode: "browser_automation"
tester_profile: "playwright-ci"
environment: "automated-ci"
privacy:
  pii_reviewed: true
  raw_recording_committed: false

automation:
  execution:
    actor_id: "playwright-ci"
    trigger: "ci"
  agent_profile: "combat-agent-v1"
  random_seed: 42
  reproduction_command: "pnpm test:e2e --grep @combat"
  metrics:
    computed_by: "ci_scripts"
    source: "playwright_trace"
    sample_unit: "tasks"
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
    storage_class: "external_bucket"
    artifact_contains: ["dom_snapshot", "trace_with_dom", "telemetry"]
    public_repo_safe: false
    metrics_json: "external-secure-storage:PT-20260527-002/metrics.json"
    trace_ref: "external-secure-storage:PT-20260527-002/trace.zip"
    replay_ref: "external-secure-storage:PT-20260527-002/replay.mp4"
    input_script_ref: "tests/e2e/scenarios/combat-stress-test.js"
    raw_artifact_committed: false
    artifact_sensitivity: "trace_with_dom"
    redaction_status: "reviewed"
    trace_redacted: true
  human_review_required: true

playtest_entries:
  - entry_id: "PTE-001"
    task_id: "TASK-001"
    hypothesis_id: "HYP-MVP-001"
    observed_behavior: "AIエージェントが特定のコーナーで壁に衝突し続けている"
    emotional_signal: "n/a"
    developer_interpretation: "コーナー判定の閾値が不適切である可能性"
    
    observable_signal: "collision_count_total"
    collection_method: "automated_telemetry"
    success_failure_assessment: "failure"
    retest_target: true
    
    affected_requirements:
      - "REQ-LOGIC-001"
    classification: "bug"
    severity: "high"
    confidence: "high"
    decision: "spec_delta_issue"
    validation_method: "browser_automation"
```

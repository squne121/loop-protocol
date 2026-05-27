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

---

### Automation Artifact Contract

**schema_version**: `playtest.telemetry.v1`
**issued_by**: `#419`

このセクションは、自動プレイテスト runner が出力する artifact の構造化契約を定義する。

#### 責務分離

| Artifact | 責務 | public_repo_safe |
|---|---|---|
| `playtest-log.md` | playtest session 証跡台帳（artifact への参照を保持） | true |
| `events.jsonl` | runtime event 時系列（runner 出力、行ごとに 1 event JSON） | false |
| `metrics.json` | events.jsonl から再計算可能な derived 集計 | false（redacted 版は別途） |
| `trace / replay` | Playwright trace 等の raw artifact | false（public repo に置かない） |

#### Event Taxonomy

必須 13 種の event_type enum:

```json
[
  "session_start",
  "session_end",
  "scenario_start",
  "scenario_end",
  "input_summary",
  "collision",
  "hit",
  "damage",
  "death",
  "sortie_clear",
  "sortie_fail",
  "reward_granted",
  "debrief_summary"
]
```

#### Event-specific `data` Contract

| event_type | required data fields | derived metric use |
|---|---|---|
| session_start | `build_ref`, `scenario_id`, `random_seed` | session identifier seeding |
| session_end | `duration_ms`, `aborted` | session aggregate |
| scenario_start | `scenario_id` | scenario boundary |
| scenario_end | `scenario_id`, `outcome` | scenario aggregate |
| input_summary | `input_count`, `duration_ms` | `input_count_total`, duration metrics |
| collision | `source_entity_id`, `target_entity_id`, `collision_kind` | `collision_count_total` |
| hit | `source_entity_id`, `target_entity_id`, `weapon_id` | hit-derived metrics |
| damage | `target_entity_id`, `amount`, `damage_type`, `source_entity_id?` | `damage_taken_total`, `death_count` precondition |
| death | `entity_id`, `cause`, `tick` | `death_count` |
| sortie_clear | `scenario_id`, `clear_reason` | `success_count` |
| sortie_fail | `scenario_id`, `failure_reason` | `failure_count` |
| reward_granted | `recipient_id`, `reward_kind`, `amount` | reward analytics |
| debrief_summary | `scenario_id`, `summary_text` | post-scenario log |

#### events.jsonl Format Rules
- encoding: UTF-8
- bom: MUST NOT be present
- line_format: one compact JSON object per line
- blank_lines: MUST NOT be present
- line_terminator: LF (`\n`); final newline SHOULD be present
- file_extension: `.jsonl`
- validation:
  - every line MUST parse independently as JSON
  - every parsed value MUST be an object

#### Event Envelope Spec

events.jsonl の各行は以下のフィールドを持つ JSON オブジェクトである:

```json
{
  "schema_version": "playtest.telemetry.v1",
  "event_id": "evt-00000001",
  "event_type": "session_start",
  "session_id": "PT-20260528-001",
  "run_id": "run-0001",
  "seq": 1,
  "tick": 0,
  "occurred_at": "2026-05-28T00:00:00.000Z",
  "build_ref": "1965306",
  "scenario_id": "scenario-combat-001",
  "random_seed": 42,
  "agent_profile": "combat-agent-v1",
  "actor_id": "player-0",
  "entity_type": "player",
  "data": {}
}
```

#### Metrics Schema (`metrics.json`)

metrics.json の fenced JSON example（parse 可能な内容）:

```json
{
  "schema_version": "playtest.metrics.v1",
  "session_id": "PT-20260528-001",
  "build_ref": "1965306",
  "scenario_id": "scenario-combat-001",
  "random_seed": 42,
  "raw": {
    "run_count": 100,
    "success_count": 85,
    "failure_count": 12,
    "aborted_count": 3,
    "death_count": 10,
    "collision_count_total": 120,
    "damage_taken_total": 450
  },
  "derived": {
    "success_rate": 0.85,
    "duration_ms_p50": 12000,
    "duration_ms_p95": 15500
  },
  "computed_from": "events.jsonl",
  "computed_by": "ci_scripts"
}
```

#### `metrics.json` Field Definitions

| field | path | type | required | unit | aggregation source | rounding | missing policy |
|---|---|---|---|---|---|---|---|
| schema_version | `$.schema_version` | string | yes | — | constant `"playtest.metrics.v1"` | — | MUST be present |
| session_id | `$.session_id` | string | yes | — | session_start.session_id | — | MUST be present |
| build_ref | `$.build_ref` | string | yes | — | session_start.build_ref | — | MUST be present |
| scenario_id | `$.scenario_id` | string | yes | — | session_start.scenario_id | — | MUST be present |
| random_seed | `$.random_seed` | integer | yes | — | session_start.random_seed | — | `null` if not seeded |
| raw.run_count | `$.raw.run_count` | integer | yes | count | count of scenario_start | — | 0 if no scenario |
| raw.success_count | `$.raw.success_count` | integer | yes | count | count of sortie_clear | — | 0 |
| raw.failure_count | `$.raw.failure_count` | integer | yes | count | count of sortie_fail | — | 0 |
| raw.aborted_count | `$.raw.aborted_count` | integer | yes | count | scenario_end without sortie_clear/fail | — | 0 |
| raw.death_count | `$.raw.death_count` | integer | yes | count | count of death | — | 0 |
| raw.collision_count_total | `$.raw.collision_count_total` | integer | yes | count | count of collision | — | 0 |
| raw.damage_taken_total | `$.raw.damage_taken_total` | integer | yes | hp | sum of damage.amount | floor | 0 |
| derived.success_rate | `$.derived.success_rate` | number | yes | fraction (0.0-1.0) | `success_count / run_count` | 4 decimal places | `null` if run_count == 0 |
| derived.duration_ms_p50 | `$.derived.duration_ms_p50` | number | yes | ms | scenario duration percentile | round half to even | `null` if run_count == 0 |
| derived.duration_ms_p95 | `$.derived.duration_ms_p95` | number | yes | ms | scenario duration percentile | round half to even | `null` if run_count == 0 |
| computed_from | `$.computed_from` | string | yes | — | constant `"events.jsonl"` | — | MUST be `"events.jsonl"` |
| computed_by | `$.computed_by` | string | yes | — | runner identifier | — | MUST be present |

#### Mapping: `automation.metrics` -> `metrics.json`

| automation.metrics (PR #418) | metrics.json | note |
|---|---|---|
| `runs` | `raw.run_count` | same denominator |
| `success_count` | `raw.success_count` | count |
| `failure_count` | `raw.failure_count` | count |
| `aborted_count` | `raw.aborted_count` | count |
| `success_rate` | `derived.success_rate` | same 0.0-1.0 fraction |
| `duration.p50` + `duration.unit: ms` | `derived.duration_ms_p50` | derived from scenario durations |
| `duration.p95` + `duration.unit: ms` | `derived.duration_ms_p95` | derived from scenario durations |
| `collision_count_total` | `raw.collision_count_total` | same count |

#### Invariants

以下の不変条件はすべての metrics.json に適用される:

- `run_count == success_count + failure_count + aborted_count`
- `0.0 <= success_rate <= 1.0`
- `success_rate == success_count / run_count`
- `duration_ms_p95 >= duration_ms_p50`
- `$.computed_from == "events.jsonl"`
- `raw trace artifacts MUST NOT be committed to public repo`

#### 単位規約

- 率（rate）: `0.0-1.0 fraction`（`unit: percent` を使う場合は明示する）
- duration: ms 単位、field name suffix は `_ms` を必須とする（例: `duration_ms_p50`, `duration_ms_p95`）
- tick: 整数、シミュレーション固定ステップカウント

#### Artifact Safety

| Artifact | 命名規約 | public_repo_safe | 参照方式 |
|---|---|---|---|
| metrics（公開可能版） | `metrics.redacted.json` | true | git 管理可 |
| metrics（生データ） | `metrics.json` | false | `external-secure-artifact://` URI で参照 |
| trace / replay | セッション ID プレフィックス | false | `external-secure-artifact://` URI で参照 |

- `public_repo_safe` flag は artifact ごとに付与する（上表参照）。
- `metrics.redacted.json` 命名規約: PII・機密データを除去した metrics の公開版は `metrics.redacted.json` という名前で管理する。
- trace artifact は `external-secure-artifact://<session_id>/trace.zip` の形式で参照する（外部セキュアストレージへの URI scheme）。
- raw artifact（trace / replay / events.jsonl）は public repo に commit してはならない（`raw_artifact_committed: false` を維持する）。

#### Artifact Reference Metadata

各 artifact 参照には以下のフィールドを必須とする:

| field | type | required | description |
|---|---|---|---|
| artifact_uri | string | yes | `external-secure-artifact://` または `artifacts/playtest/{session_id}/...` |
| artifact_type | string (enum) | yes | `events_jsonl` / `metrics_json` / `metrics_redacted_json` / `playwright_trace` / `replay` |
| public_repo_safe | boolean | yes | `metrics.redacted.json` のみ true、他は false |
| contains | array<string> | yes | `event_stream` / `derived_metric` / `dom_snapshot` / `screenshot` / `network_request` / `console_log` / `source_location` 等 |
| redaction_status | string (enum) | yes | `pending` / `reviewed` / `not_applicable` |
| retention_policy | string (enum) | yes | `local_ephemeral` / `secure_vault` / `ci_artifact_private` |
| raw_artifact_committed | boolean | yes | public repo に raw artifact をコミットしているか（playtest-log.md 上は MUST be `false`） |

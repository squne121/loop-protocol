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
 YAML 
`docs/product/mvp-scope.md`  Measurement Contract 

## Entry Schema

```yaml
session_id: "PT-YYYYMMDD-001"
date: "YYYY-MM-DD"
build_ref: "<commit-sha-or-release-tag>"
session_mode: "human_internal | ai_simulation | browser_automation" # Maps to Issue #417 execution_mode
tester_profile: "developer | target_player_layer | ai_agent"
environment: "browser-chrome | local-dev | automated-ci"
execution:
  mode: "human_developer | ai_agent | scripted_automation | ci_browser_automation"
  actor_id: "developer-self | combat-agent-v1 | playwright-ci"
  trigger: "manual | ci | agent_loop"
privacy:
  pii_reviewed: true
  raw_recording_committed: false

# AI/Automation Metadata (Optional, default: null)
# AI/ UXUX human_review_required: true 
automation: null
# When automation is present, use the following structure:
# automation:
#   agent_profile: ""
#   random_seed: null
#   reproduction_command: ""
#   metrics:
#     runs: 0
#     success_count: 0
#     failure_count: 0
#     success_rate: null          # success_rate = success_count / runs
#     death_count: 0
#     death_rate: null
#     duration:
#       unit: "ms"
#       p50: 0
#       p95: 0
#     input_count_total: 0
#     collision_count_total: 0
#   artifacts:
#     # WARNING: DO NOT commit raw recordings or PII to the repository.
#     # Ensure privacy.pii_reviewed is true before referencing artifacts.
#     metrics_json: ""
#     trace_ref: ""               # Use external storage patterns
#     trace_redacted: true        # Required for secure artifacts
#     replay_ref: ""
#     input_script_ref: ""
#   human_review_required: false  # UX true

playtest_entries:
  - entry_id: "PTE-001"
    task_id: "TASK-001"
    hypothesis_id: "HYP-MVP-001"
    observed_behavior: ""      # 
    player_quote_redacted: ""  # 
    emotional_signal: ""      # frustration | confusion | boredom | excitement 
    developer_interpretation: "" # 
    player_suggested_fix: ""    # 
    
    # MVP Measurement Contract Fields
    observable_signal: ""      # mvp-scope.md 
    collection_method: ""      # mvp-scope.md 
    success_failure_assessment: "success | failure | inconclusive"
    misread_notes: ""          # 
    tunable_parameter: ""      # 
    retest_target: "true | false" # 
    
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

## Example Entry (Draft)

```yaml
session_id: "PT-20260527-001"
date: "2026-05-27"
build_ref: "3f0e79d"
session_mode: "human_internal"
tester_profile: "developer"
environment: "browser-chrome"
execution:
  mode: "human_developer"
  actor_id: "developer-self"
  trigger: "manual"
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
      unit: "ms"
      p50: 12000
      p95: 15500
    input_count_total: 4500
    collision_count_total: 120
  artifacts:
    metrics_json: "docs/playtest/artifacts/PT-20260527-001/metrics.json"
    trace_ref: "external-secure-storage:redacted-trace-id"
    replay_ref: "docs/playtest/artifacts/PT-20260527-001/replay.mp4"
    input_script_ref: "tests/e2e/scenarios/combat-stress-test.js"
  human_review_required: true

playtest_entries:
  - entry_id: "PTE-001"
    task_id: "TASK-001"
    hypothesis_id: "HYP-MVP-001"
    observed_behavior: ""
    player_quote_redacted: ""
    emotional_signal: "frustration"
    developer_interpretation: " 60Hz "
    
    observable_signal: " player intervention "
    collection_method: "self-explanation prompt"
    success_failure_assessment: "success"
    misread_notes: ""
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

## Policies
- **AI Result Evidence**: AI result is not UX evidence (`ai_result_is_ux_evidence: false`). AI-generated simulations or automated runs provide performance and logic verification, but human experience signals must be derived from human testers or human review of AI sessions.

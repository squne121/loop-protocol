# Playtest Session: Movement + Projectile Smoke (E2E)

> **IMPORTANT**: Automatic E2E results are NOT UX validity evidence.
> This session confirms integration correctness only.
> Human playtesting is required to evaluate feel and UX.

```yaml
session_id: "PT-20260529-001"
date: "2026-05-29"
build_ref: "906533e"
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
  agent_profile: ""
  random_seed: null
  reproduction_command: "pnpm test:e2e --project=chromium"
  metrics:
    computed_by: "ci_scripts"
    source: "playwright_trace"
    sample_unit: "runs"
    runs: 1
    success_count: 1
    failure_count: 0
    success_rate: 1.0
    death_count: 0
    death_rate: null
    duration:
      p50: 0
      p95: 0
      unit: "ms"
    input_count_total: 0
    collision_count_total: 0
  artifacts:
    storage_class: "local_git"
    artifact_contains: ["dom_snapshot"]
    public_repo_safe: false
    metrics_json: ""
    trace_ref: ""
    replay_ref: ""
    input_script_ref: "tests/e2e/movement-projectile.spec.ts"
    raw_artifact_committed: false
    artifact_sensitivity: "none"
    redaction_status: "not_applicable"
    trace_redacted: true
  human_review_required: false

playtest_entries:
  - entry_id: "PTE-001"
    task_id: "TASK-E2E-SMOKE"
    hypothesis_id: "HYP-E2E-FOUNDATION"
    observed_behavior: >
      Playwright smoke test: canvas visible, WASD movement verified, pointer-down
      projectile generation verified, simulation tick advancing confirmed.
    player_quote_redacted: ""
    emotional_signal: "n/a"
    developer_interpretation: >
      E2E foundation confirms movement + projectile integration is wired correctly
      in the browser. Does not verify UX quality or feel.
    player_suggested_fix: ""

    observable_signal: "E2E test pass/fail count"
    collection_method: "automated_telemetry"
    success_failure_assessment: "success"
    misread_notes: >
      browser_automation cannot confirm UX quality. Human playtest required for
      feel / responsiveness evaluation.
    tunable_parameter: ""
    retest_target: false

    affected_requirements:
      - "REQ-MOVEMENT-001"
      - "REQ-PROJECTILE-001"
    classification: "bug"
    severity: "low"
    confidence: "high"
    decision: "no_action"
    proposed_spec_delta: ""
    linked_issue: "#447"
    validation_method: "browser_automation"
```

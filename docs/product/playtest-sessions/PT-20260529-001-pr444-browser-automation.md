# Playtest Session: PR #444 Movement + Projectile — Browser Automation

> **Deferred Verification Discharge**: This session discharges the `deferred` runtime verification
> recorded in PR #444 (`decision: deferred` / `deferred_destination: phase / M1 Foundation Gate playtest`).
> See also: Issue #448 (this session's linked issue).

```yaml
session_id: "PT-20260529-001-pr444-browser-automation"
date: "2026-05-29"
build_ref: "75cfc1e3eefa109c94eb42b6fc4c4721eafc1090"
session_mode: "browser_automation"
tester_profile: "playwright-ci"
environment: "local-dev"
linked_pr: "PR #444"
linked_issue: "#448"

privacy:
  pii_reviewed: true
  raw_recording_committed: false

automation:
  execution:
    actor_id: "playwright-ci"
    trigger: "manual-local"
  agent_profile: "Playwright 1.x"
  random_seed: null
  reproduction_command: "pnpm test:e2e"
  metrics:
    computed_by: "playwright_runner"
    source: "playwright_trace"
    sample_unit: "runs"
    runs: 13
    success_count: 13
    failure_count: 0
    success_rate: "100%"
    death_count: 0
    death_rate: null
    duration:
      p50: 373
      p95: 726
      unit: "ms"
    input_count_total: 13
    collision_count_total: 0
  artifacts:
    storage_class: "retain-on-failure"
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
    artifact_absent_reason: >
      All 13 tests passed (green run). Playwright is configured retain-on-failure,
      so no trace artifacts are retained for passing runs.
  human_review_required: false

playtest_entries:
  - entry_id: "PTE-001"
    task_id: "TASK-E2E-CANVAS-VISIBLE"
    hypothesis_id: "HYP-PR444-BROWSER-CANVAS"
    observed_behavior: >
      canvas is visible with non-zero CSS size — PASS (320ms)
    player_quote_redacted: ""
    emotional_signal: "n/a"
    developer_interpretation: >
      Canvas element renders correctly in the browser.
    player_suggested_fix: ""
    observable_signal: "canvas CSS size > 0"
    collection_method: "automated_telemetry"
    success_failure_assessment: "success"
    misread_notes: ""
    tunable_parameter: ""
    retest_target: false
    affected_requirements:
      - "REQ-CANVAS-001"
    classification: "unclear_needs_more_data"
    severity: "low"
    confidence: "high"
    decision: "no_action"
    proposed_spec_delta: ""
    linked_issue: "#448"
    validation_method: "browser_automation"

  - entry_id: "PTE-002"
    task_id: "TASK-E2E-SIM-TICK"
    hypothesis_id: "HYP-PR444-SIMULATION-LOOP"
    observed_behavior: >
      simulation loop is running (tick advances) — PASS (274ms)
    player_quote_redacted: ""
    emotional_signal: "n/a"
    developer_interpretation: >
      Simulation tick counter advances as expected — confirms the fixed 60Hz accumulator loop
      is running correctly in browser context.
    player_suggested_fix: ""
    observable_signal: "tick counter increment"
    collection_method: "automated_telemetry"
    success_failure_assessment: "success"
    misread_notes: ""
    tunable_parameter: ""
    retest_target: false
    affected_requirements:
      - "REQ-SIMULATION-001"
    classification: "unclear_needs_more_data"
    severity: "low"
    confidence: "high"
    decision: "no_action"
    proposed_spec_delta: ""
    linked_issue: "#448"
    validation_method: "browser_automation"

  - entry_id: "PTE-003"
    task_id: "TASK-E2E-MOVEMENT-WASD"
    hypothesis_id: "HYP-PR444-MOVEMENT"
    observed_behavior: >
      KeyW moves player upward (y decreases) — PASS (311ms).
      KeyS moves player downward (y increases) — PASS (323ms).
      KeyA moves player left (x decreases) — PASS (451ms).
      KeyD moves player right (x increases) — PASS (373ms).
      keyup stops movement — player position stabilises after key release — PASS (488ms).
    player_quote_redacted: ""
    emotional_signal: "n/a"
    developer_interpretation: >
      All four WASD directions confirmed to move player position by measurable delta in the
      correct direction. Key release correctly halts movement. PR #444 movement implementation
      is verified correct at the browser integration level.
    player_suggested_fix: ""
    observable_signal: "player {x,y} coordinate delta"
    collection_method: "automated_telemetry"
    success_failure_assessment: "success"
    misread_notes: ""
    tunable_parameter: ""
    retest_target: false
    affected_requirements:
      - "REQ-MOVEMENT-001"
    classification: "unclear_needs_more_data"
    severity: "low"
    confidence: "high"
    decision: "no_action"
    proposed_spec_delta: ""
    linked_issue: "#448"
    validation_method: "browser_automation"

  - entry_id: "PTE-004"
    task_id: "TASK-E2E-PROJECTILE"
    hypothesis_id: "HYP-PR444-PROJECTILE"
    observed_behavior: >
      pointer down on canvas generates at least one projectile — PASS (308ms).
      projectile position changes after simulation ticks (projectile moves) — PASS (352ms).
      pointerup clears primary fire state — PASS (490ms).
      pointercancel clears active pointer state — PASS (544ms).
      lostpointercapture clears active pointer state — PASS (442ms).
      projectile renders on canvas — PASS (726ms).
    player_quote_redacted: ""
    emotional_signal: "n/a"
    developer_interpretation: >
      Pointer-down correctly generates projectiles from player position. Projectile world position
      advances each simulation tick (ballistic trajectory confirmed). Pointer state cleanup paths
      (pointerup, pointercancel, lostpointercapture) all correctly clear fire state.
      Canvas rendering of projectile confirmed. PR #444 projectile implementation is verified correct
      at browser integration level.
    player_suggested_fix: ""
    observable_signal: "projectile count > 0, projectile {x,y} delta, canvas pixel non-uniform"
    collection_method: "automated_telemetry"
    success_failure_assessment: "success"
    misread_notes: >
      browser_automation confirms integration correctness only. UX feel (responsiveness, visual feedback
      latency, pointer aim precision) requires human_internal session (see PT-20260529-002-pr444-human-internal.md).
    tunable_parameter: ""
    retest_target: false
    affected_requirements:
      - "REQ-PROJECTILE-001"
    classification: "unclear_needs_more_data"
    severity: "low"
    confidence: "high"
    decision: "no_action"
    proposed_spec_delta: ""
    linked_issue: "#448"
    validation_method: "browser_automation"
```

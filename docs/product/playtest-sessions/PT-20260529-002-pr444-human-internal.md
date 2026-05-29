# Playtest Session: PR #444 Movement + Projectile — Human Internal

> **Deferred Verification Discharge**: This session discharges the `deferred` runtime verification
> recorded in PR #444 (`decision: deferred` / `deferred_destination: phase / M1 Foundation Gate playtest`).
> Together with PT-20260529-001-pr444-browser-automation.md, this closes the deferred verification
> obligation. See also: Issue #448 (this session's linked issue).

```yaml
session_id: "PT-20260529-002-pr444-human-internal"
date: "2026-05-29"
build_ref: "75cfc1e3eefa109c94eb42b6fc4c4721eafc1090"
session_mode: "human_internal"
tester_profile: "developer-self"
environment: "local-dev-browser"
linked_pr: "PR #444"
linked_issue: "#448"

privacy:
  pii_reviewed: true
  raw_recording_committed: false
  raw_video_committed: false
  personal_data_in_session: false

task_script:
  - task_id: "TASK-HI-001"
    description: "Open http://localhost:5173 (or dist via pnpm preview)"
    expected: "Canvas visible, player character present"
  - task_id: "TASK-HI-002"
    description: "Press and hold W — observe player moving upward"
    expected: "Player y-coordinate decreases continuously while W held"
  - task_id: "TASK-HI-003"
    description: "Press and hold S — observe player moving downward"
    expected: "Player y-coordinate increases continuously while S held"
  - task_id: "TASK-HI-004"
    description: "Press and hold A — observe player moving left"
    expected: "Player x-coordinate decreases continuously while A held"
  - task_id: "TASK-HI-005"
    description: "Press and hold D — observe player moving right"
    expected: "Player x-coordinate increases continuously while D held"
  - task_id: "TASK-HI-006"
    description: "Release key — observe player stopping"
    expected: "Movement halts promptly on key release (no drift)"
  - task_id: "TASK-HI-007"
    description: "Hold pointer down (left-click / touch) on canvas"
    expected: "Projectile(s) visibly spawned at or near player position"
  - task_id: "TASK-HI-008"
    description: "Move pointer to different canvas location while holding down"
    expected: "Projectile trajectory aims toward pointer position"
  - task_id: "TASK-HI-009"
    description: "Release pointer — observe fire stops"
    expected: "No new projectiles generated after pointer release"
  - task_id: "TASK-HI-010"
    description: "Combine WASD movement + pointer fire simultaneously"
    expected: "Both systems operate correctly in combination"

playtest_entries:
  - entry_id: "PTE-001"
    task_id: "TASK-HI-001"
    hypothesis_id: "HYP-PR444-CANVAS-RENDER"
    observed_behavior: >
      Canvas renders at expected size. Player character is visible as a distinct colored
      shape. UI elements outside canvas are correctly positioned. No visual artifacts at
      startup.
    player_quote_redacted: ""
    emotional_signal: "neutral"
    developer_interpretation: >
      Initial render is functionally correct. Canvas is immediately usable without
      interaction required to show content.
    player_suggested_fix: ""
    observable_signal: "canvas visible, player shape present"
    collection_method: "direct_observation"
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
    validation_method: "human_internal"

  - entry_id: "PTE-002"
    task_id: "TASK-HI-002 through TASK-HI-006"
    hypothesis_id: "HYP-PR444-MOVEMENT-FEEL"
    observed_behavior: >
      WASD movement responds immediately to key press with no perceivable input lag.
      Movement feels continuous and smooth at 60fps. All four directional keys
      produce the expected movement in correct directions. Key release correctly
      halts movement without drift or sliding.
    player_quote_redacted: ""
    emotional_signal: "positive"
    developer_interpretation: >
      Movement implementation from PR #444 is confirmed responsive and correct from
      a human perception standpoint. The fixed 60Hz simulation tick produces smooth
      motion at the display refresh rate. No balance or tuning concerns noted for
      current speed values — feels appropriate for prototype stage.
    player_suggested_fix: ""
    observable_signal: "player position visual delta per frame, response latency"
    collection_method: "direct_observation"
    success_failure_assessment: "success"
    misread_notes: ""
    tunable_parameter: "player_speed"
    retest_target: false
    affected_requirements:
      - "REQ-MOVEMENT-001"
    classification: "unclear_needs_more_data"
    severity: "low"
    confidence: "high"
    decision: "no_action"
    proposed_spec_delta: ""
    linked_issue: "#448"
    validation_method: "human_internal"

  - entry_id: "PTE-003"
    task_id: "TASK-HI-007 through TASK-HI-009"
    hypothesis_id: "HYP-PR444-PROJECTILE-FEEL"
    observed_behavior: >
      Pointer-down on canvas spawns a visible projectile near the player.
      Projectile travels in the direction of the pointer at a consistent speed.
      Holding pointer down while moving fires continuously.
      Pointer release correctly stops new projectile generation.
      Projectiles leave the canvas area and disappear as expected.
    player_quote_redacted: ""
    emotional_signal: "positive"
    developer_interpretation: >
      Projectile firing mechanic from PR #444 is confirmed functional from a human
      experience standpoint. The pointer-to-direction mapping feels intuitive.
      Speed and visual appearance are acceptable for prototype stage.
      No bugs or misfires observed during testing.
    player_suggested_fix: ""
    observable_signal: "projectile spawn, trajectory arc, disappearance off-canvas"
    collection_method: "direct_observation"
    success_failure_assessment: "success"
    misread_notes: ""
    tunable_parameter: "projectile_speed"
    retest_target: false
    affected_requirements:
      - "REQ-PROJECTILE-001"
    classification: "unclear_needs_more_data"
    severity: "low"
    confidence: "high"
    decision: "no_action"
    proposed_spec_delta: ""
    linked_issue: "#448"
    validation_method: "human_internal"

  - entry_id: "PTE-004"
    task_id: "TASK-HI-010"
    hypothesis_id: "HYP-PR444-COMBINED-SYSTEMS"
    observed_behavior: >
      Simultaneous WASD movement + pointer fire operates without interference.
      Projectile origin tracks the player's moving position correctly during combined
      input. No input conflicts or dropped events observed during combined operation.
    player_quote_redacted: ""
    emotional_signal: "positive"
    developer_interpretation: >
      The movement and projectile systems operate independently as designed. Combined
      use confirms no integration regressions. This satisfies the PR #444 deferred
      verification goal of confirming real browser pointer + keyboard interaction correctness.
    player_suggested_fix: ""
    observable_signal: "player position + projectile origin alignment during motion"
    collection_method: "direct_observation"
    success_failure_assessment: "success"
    misread_notes: ""
    tunable_parameter: ""
    retest_target: false
    affected_requirements:
      - "REQ-MOVEMENT-001"
      - "REQ-PROJECTILE-001"
    classification: "unclear_needs_more_data"
    severity: "low"
    confidence: "high"
    decision: "no_action"
    proposed_spec_delta: ""
    linked_issue: "#448"
    validation_method: "human_internal"

deferred_verification_discharge:
  source_pr: "PR #444"
  source_decision: "deferred"
  source_destination: "phase / M1 Foundation Gate playtest"
  discharge_issue: "#448"
  discharge_status: "complete"
  discharge_sessions:
    - "PT-20260529-001-pr444-browser-automation.md"
    - "PT-20260529-002-pr444-human-internal.md"
```

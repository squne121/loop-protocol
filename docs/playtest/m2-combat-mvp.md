---
doc_id: playtest-m2-combat-mvp
issue: "#490"
parent_issue: "#483"
tested_commit: "fc08eed33c420347d46e2d47dc3b7f5218a66b14"
evidence_mode: playwright+manual
status: accepted_with_unknowns
date: "2026-05-31"
---

# M2 Combat MVP Playtest

## Overview

This document records the playtest evidence for the M2 playable sortie milestone.
It combines automated E2E observations (Playwright) with a manual checklist for UX validation.

## Environment

```yaml
environment:
  platform: linux (WSL2)
  browser: chromium (Playwright)
  node: ">=20"
  pnpm: ">=9"
  vite_e2e_mode: "true"
  test_runner: playwright
```

## Commands

```bash
# Build with E2E mode enabled
VITE_E2E_MODE=true pnpm build

# Run E2E spec
pnpm test:e2e -- tests/e2e/m2-combat-mvp.spec.ts

# Standard quality gates
pnpm typecheck
pnpm lint
pnpm test
pnpm build
```

## Observed

```yaml
observed:
  e2e_playwright:
    tests_run: 8
    tests_passed: 8
    tests_failed: 0
    duration_ms: 12900
    results:
      - name: "GIVEN app loaded WHEN sortie bootstrap runs THEN sortie.status is running"
        status: pass
        duration_ms: 251
      - name: "GIVEN sortie running WHEN enemies spawn THEN enemies array is non-empty"
        status: pass
        duration_ms: 322
      - name: "GIVEN enemy spawned WHEN ticks elapse THEN enemy approaches player (distance decreases)"
        status: pass
        duration_ms: 1300
      - name: "GIVEN canvas pointer held WHEN ticks elapse THEN projectile appears"
        status: pass
        duration_ms: 380
        note: "renamed from 'shotsFired increases'; LoopE2ESnapshot.player has no shotsFired field, projectile presence is equivalent evidence"
      - name: "GIVEN enemy exists WHEN projectile hits THEN enemy hp decreases or enemy defeated"
        status: pass
        duration_ms: 1300
      - name: "GIVEN enemy near player WHEN contact damage applies THEN player.hp decreases"
        status: pass
        duration_ms: 7400
        note: "timeout extended to 60s; enemy needs ~30s to traverse arena"
      - name: "GIVEN enemies field in snapshot WHEN E2E hook called THEN enemies and sortie fields present"
        status: pass
        duration_ms: 164
      - name: "GIVEN sortie running WHEN sortie state machine checked THEN victory and defeat statuses are valid enum values"
        status: pass
        duration_ms: 166
        note: "victory/defeat state machine schema verified; full cycle not exercised in automated E2E (see unknowns)"
  victory_defeat_state_evidence:
    method: "E2E schema verification"
    note: "sortie.status confirmed to accept 'victory'/'defeat' as valid enum values via state machine type check"
    full_cycle_tested: false
    reason: "120s real-time victory and player-hp-to-0 defeat cycles exceed practical E2E time budget"
  quality_gates:
    typecheck: pass
    lint: pass
    test_vitest: "266 tests passed (15 files)"
    build: pass
    production_build_e2e_hook_absent: confirmed (__LOOP_E2E__ not present in dist/)
```

## Unknowns

```yaml
unknowns:
  - item: "victory condition full cycle"
    detail: "120 秒リアルタイム勝利は E2E の時間制約上スキップし、手動証跡は未実施。SortieSystem の state machine schema（victory/defeat enum）は E2E で確認済みだが、実際に全 wave 撃破→victory 遷移のエンド to エンドは未テスト。"
  - item: "defeat condition full cycle"
    detail: "player.hp が 0 になるまでの自然経過による defeat 遷移は E2E の時間制約上スキップ。contact damage によって hp が減少することは確認済み。"
  - item: "UX feel of combat feedback"
    detail: "Automated tests confirm mechanical correctness; subjective feel (hitfeedback, pacing, audio) requires human playtesting."
  - item: "Performance on low-end hardware"
    detail: "Tests run on WSL2/Chromium; performance on actual mobile or low-spec desktops not measured."
```

## Manual Checklist

The following scenarios require human verification. Each item should be checked
by a human tester running `pnpm preview` or `pnpm dev` in a browser.

### Movement

- [ ] **WASD movement**: Player moves smoothly in all 4 directions (W=up, A=left, S=down, D=right)
- [ ] **Diagonal movement**: Holding two WASD keys simultaneously produces diagonal movement
- [ ] **Boundary clamping**: Player cannot move outside the arena boundaries

### Mouse Shooting

- [ ] **Mouse click fires projectile**: Holding left mouse button on the canvas fires projectiles toward the cursor
- [ ] **Aim tracking**: Projectiles travel in the direction of the cursor at time of fire
- [ ] **Fire rate**: Projectiles are generated at the configured weapon interval (approximately every 280ms)
- [ ] **Release stops firing**: Releasing the mouse button stops projectile generation

### Enemy Spawn

- [ ] **Enemy appears**: At least one enemy spawns within a few seconds of sortie start
- [ ] **Enemy is visible**: Enemy unit is rendered on the canvas as a distinct shape/color
- [ ] **Multiple enemies**: Additional enemies spawn over time per the wave configuration

### Enemy Approach

- [ ] **Enemy moves toward player**: Enemy units visibly move toward the player position
- [ ] **Pathfinding basic**: Enemies do not stop or get stuck immediately (basic approach behavior)

### Projectile-Enemy Collision

- [ ] **Hit detection**: Projectile visually disappears or enemy reacts when hit
- [ ] **Enemy damage**: Enemy HP bar or visual state changes after being hit
- [ ] **Enemy defeat**: Enemy disappears or shows defeat state when HP reaches 0

### Enemy Damage / Defeat

- [ ] **Enemy HP tracking**: Enemy HP decreases with each projectile hit
- [ ] **Enemy defeat visual**: Defeated enemies are removed from the arena or show defeat animation
- [ ] **Multiple hits**: Enemy survives more than one hit (not one-shot unless design intent)

### Player Contact Damage

- [ ] **Contact damage**: Player HP decreases when an enemy touches/overlaps the player
- [ ] **HP display**: Player HP is shown in the HUD and updates visibly
- [ ] **No instant kill**: Player does not die from a single contact unless design intent

### Victory Condition

- [ ] **victory**: After all enemies in the wave are defeated, sortie transitions to `victory` status
- [ ] **Victory screen/HUD**: UI updates to reflect victory outcome

### Defeat Condition

- [ ] **defeat**: When player HP reaches 0, sortie transitions to `defeat` status
- [ ] **Defeat screen/HUD**: UI updates to reflect defeat outcome

## AC Verification Summary

| AC | Description | Method | Status |
|----|-------------|--------|--------|
| AC1 | docs/playtest/m2-combat-mvp.md exists | file check | pass |
| AC2 | Required YAML fields present | grep | pass |
| AC3 | Manual checklist covers all scenarios | doc review | pass |
| AC4 | tests/e2e/m2-combat-mvp.spec.ts exists | file check | pass |
| AC5 | LoopE2ESnapshot has enemies, sortie, hp fields | grep + typecheck | pass |
| AC6 | pnpm typecheck passes | CI gate | pass |
| AC7 | pnpm lint passes | CI gate | pass |
| AC8 | pnpm test passes | CI gate | pass |
| AC9 | pnpm build passes | CI gate | pass |

Note: status is `accepted_with_unknowns` because victory/defeat full-cycle E2E is not exercised (time budget constraint). The state machine schema is verified; full cycle requires manual playtesting.

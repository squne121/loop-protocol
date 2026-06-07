/**
 * @vitest-environment jsdom
 * Unit tests for src/ui/HudController.ts (Issue #726)
 * AC1: HUD (DOM) hull display uses shared formatCombatNumber without exposing fractional/floating-point artifacts
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { createHudController } from '../src/ui/HudController'
import type { GameState } from '../src/state'

describe('HudController Hull Display', () => {
  let container: HTMLElement
  let hudController: ReturnType<typeof createHudController>

  beforeEach(() => {
    container = document.createElement('div')
    hudController = createHudController(container, {
      onQuickSave: () => {},
      onReset: () => {},
    })
  })

  // Helper to create minimal GameState
  function createState(overrides: Partial<GameState> = {}): GameState {
    return {
      tick: 0,
      elapsedMs: 0,
      player: {
        id: 'player-1',
        x: 480,
        y: 270,
        radius: 8,
        hp: 100,
        maxHp: 100,
        speedPxPerSec: 200,
        aimX: 600,
        aimY: 300,
        lastAimDirectionX: 1,
        lastAimDirectionY: 0,
        weaponCooldownMs: 0,
        shotsFired: 0,
      },
      enemies: [],
      projectiles: [],
      arena: { width: 960, height: 540 },
      progress: {
        stageLabel: 'Stage 1',
        resources: 0,
      },
      telemetry: {
        status: 'idle',
        lastCommandSummary: 'Ready',
      },
      sortie: {
        status: 'idle',
        result: null,
      },
      ...overrides,
    }
  }

  it('GIVEN player with integer HP WHEN render called THEN hull displays integer formatted values', () => {
    const state = createState({ player: { ...createState().player, hp: 8, maxHp: 10 } })
    hudController.render(state)
    const hpField = container.querySelector('[data-field="hp"]')
    expect(hpField?.textContent).toBe('8/10')
  })

  it('GIVEN player with fractional HP like 7.9999 WHEN render called THEN hull displays ceil without floating-point artifact', () => {
    const state = createState({ player: { ...createState().player, hp: 7.9999, maxHp: 10 } })
    hudController.render(state)
    const hpField = container.querySelector('[data-field="hp"]')
    // 7.9999 ceils to 8, maxHp 10 stays 10
    expect(hpField?.textContent).toBe('8/10')
  })

  it('GIVEN player with small fractional HP like 0.5 WHEN render called THEN hull displays <1 (not 0)', () => {
    const state = createState({ player: { ...createState().player, hp: 0.5, maxHp: 8 } })
    hudController.render(state)
    const hpField = container.querySelector('[data-field="hp"]')
    // 0.5 displays as "<1", maxHp 8 stays 8
    expect(hpField?.textContent).toBe('<1/8')
  })

  it('GIVEN player with HP=0 WHEN render called THEN hull displays 0', () => {
    const state = createState({ player: { ...createState().player, hp: 0, maxHp: 8 } })
    hudController.render(state)
    const hpField = container.querySelector('[data-field="hp"]')
    expect(hpField?.textContent).toBe('0/8')
  })

  it('GIVEN player with fractional hp=1.5 and maxHp=5.3 WHEN render called THEN both ceiled independently', () => {
    const state = createState({ player: { ...createState().player, hp: 1.5, maxHp: 5.3 } })
    hudController.render(state)
    const hpField = container.querySelector('[data-field="hp"]')
    // 1.5 -> 2, 5.3 -> 6
    expect(hpField?.textContent).toBe('2/6')
  })
})

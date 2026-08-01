/**
 * @vitest-environment jsdom
 *
 * tests/debug-pause-hud.test.ts
 *
 * Tests for HUD pause/resume affordance (AC1, AC4, AC6).
 *
 * Scope Delta (Issue #1375): this file predates #1375 and encoded the OLD
 * (buggy) pause button contract -- a visible label fixed to "Pause" but an
 * `aria-label` that swapped to "Resume simulation" while paused, i.e. the
 * visible label and the accessible name disagreed. Issue #1375's AC3
 * explicitly requires fixing this mismatch ("Pause は可視ラベルと
 * accessible name が一致し aria-pressed が状態を表す"), which this exact
 * control implements (`src/ui/HudController.ts` / `src/ui/combatHud.ts`).
 * Per the "test outside Allowed Paths は拡張対応" policy (extend, don't
 * silently break/delete a pre-existing test that encodes now-superseded
 * behavior), this file is added to Issue #1375's Allowed Paths via Scope
 * Delta and updated to assert the corrected contract instead. The
 * `pause-status` live region assertions are removed: AC6 makes Assist the
 * sole `role="status"` live region in the combat HUD (Hull/Kills/Elapsed/
 * Weapon/Pause update in place, not inside a live region) -- pause state is
 * now conveyed solely via `aria-pressed` on the Pause button itself.
 */
import { describe, expect, it, vi } from 'vitest'
import { createHudController } from '../src/ui/HudController'
import { createInitialGameState, defaultSimulationConfig } from '../src/state'
import { startSortie } from '../src/systems/SortieSystem'

/** Matches `defaultSimulationConfig.fixedDeltaMs` (production fixed timestep). */
const PROD_FIXED_DELTA_MS = defaultSimulationConfig.fixedDeltaMs

function makeContainer(): HTMLElement {
  const div = document.createElement('div')
  document.body.appendChild(div)
  return div
}

function makeActions(overrides: Partial<Parameters<typeof createHudController>[1]> = {}) {
  return {
    onClaimReward: vi.fn(),
    onNextSortie: vi.fn(),
    onTogglePause: vi.fn(),
    ...overrides,
  }
}

describe('HUD pause/resume affordance — AC1', () => {
  it('GIVEN HUD rendered WHEN not paused THEN pause button text is "Pause"', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, false, PROD_FIXED_DELTA_MS)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    expect(btn).not.toBeNull()
    expect(btn!.textContent).toBe('Pause')
  })

  it('GIVEN HUD rendered WHEN paused THEN aria-pressed is "true" and the visible label / accessible name both stay "Pause" (AC3: no mismatch)', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, true, PROD_FIXED_DELTA_MS)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    expect(btn).not.toBeNull()
    // AC3: visible label is fixed to 'Pause' regardless of paused state.
    expect(btn!.textContent).toBe('Pause')
    expect(btn!.getAttribute('aria-pressed')).toBe('true')
    // AC3: no aria-label override -- the accessible name IS the visible
    // label ('Pause'), so it never disagrees with what's on screen.
    expect(btn!.hasAttribute('aria-label')).toBe(false)
  })

  it('GIVEN HUD rendered in running phase WHEN pause button clicked THEN onTogglePause is called', () => {
    const container = makeContainer()
    const onTogglePause = vi.fn()
    const actions = makeActions({ onTogglePause })
    const hud = createHudController(container, actions)
    const state = createInitialGameState()
    // Pause button is only enabled during running phase (BLOCKER 1 fix)
    startSortie(state, defaultSimulationConfig.fixedDeltaMs)
    hud.render(state, false, PROD_FIXED_DELTA_MS)

    container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')!.click()

    expect(onTogglePause).toHaveBeenCalledTimes(1)
  })
})

describe('HUD pause feedback — AC6 (no debug metadata in normal UI)', () => {
  it('GIVEN paused state WHEN render called THEN pause button shows fixed label with no debug metadata', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, true, PROD_FIXED_DELTA_MS)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')!
    // AC3/AC6: button textContent (and accessible name) is fixed 'Pause';
    // pause state conveyed via aria-pressed only.
    expect(btn.textContent).toBe('Pause')
    expect(btn.hasAttribute('aria-label')).toBe(false)
    // No exact HP/HULL numbers in the pause button
    expect(btn.textContent).not.toMatch(/\d+\/\d+/)
    // No LoopPhase string exposed in button
    expect(btn.textContent).not.toMatch(/running|debrief|preparation/)
  })
})

describe('HUD render continues during pause — AC4', () => {
  it('GIVEN paused running state WHEN render called THEN the combat HUD assist status field still updates', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()
    startSortie(state, defaultSimulationConfig.fixedDeltaMs)

    hud.render(state, true, PROD_FIXED_DELTA_MS)

    // AC1/AC4: the running-only combat HUD keeps rendering while paused
    // (rendering and HUD continue regardless of pause state). The default
    // ally has no assigned target yet, so assist status reports that.
    const assistStatusEl = container.querySelector<HTMLElement>('[data-field="combat-hud-assist-status"]')
    expect(assistStatusEl?.textContent).toBe('No target to assist.')
  })
})

// ---------------------------------------------------------------------------
// AC16: aria-pressed represents pause state
// ---------------------------------------------------------------------------

describe('HUD aria-pressed — AC16', () => {
  it('GIVEN not paused WHEN rendered THEN aria-pressed is "false"', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, false, PROD_FIXED_DELTA_MS)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')!
    expect(btn.getAttribute('aria-pressed')).toBe('false')
  })

  it('GIVEN paused WHEN rendered THEN aria-pressed is "true"', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, true, PROD_FIXED_DELTA_MS)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')!
    expect(btn.getAttribute('aria-pressed')).toBe('true')
  })
})

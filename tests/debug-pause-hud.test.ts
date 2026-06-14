/**
 * @vitest-environment jsdom
 *
 * tests/debug-pause-hud.test.ts
 *
 * Tests for HUD pause/resume affordance (AC1, AC4, AC6).
 */
import { describe, expect, it, vi } from 'vitest'
import { createHudController } from '../src/ui/HudController'
import { createInitialGameState, defaultSimulationConfig } from '../src/state'
import { startSortie } from '../src/systems/SortieSystem'

function makeContainer(): HTMLElement {
  const div = document.createElement('div')
  document.body.appendChild(div)
  return div
}

function makeActions(overrides: Partial<Parameters<typeof createHudController>[1]> = {}) {
  return {
    onStartSortie: vi.fn(),
    onClaimReward: vi.fn(),
    onNextSortie: vi.fn(),
    onQuickSave: vi.fn(),
    onQuickLoad: vi.fn(),
    onReset: vi.fn(),
    canQuickLoad: vi.fn(() => false),
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

    hud.render(state, false)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    expect(btn).not.toBeNull()
    expect(btn!.textContent).toBe('Pause')
  })

  it('GIVEN HUD rendered WHEN paused THEN aria-pressed is "true" and aria-label is "Resume simulation"', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, true)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    expect(btn).not.toBeNull()
    // Button textContent is fixed to 'Pause'; state is conveyed via aria-pressed (AC16)
    expect(btn!.textContent).toBe('Pause')
    expect(btn!.getAttribute('aria-pressed')).toBe('true')
    expect(btn!.getAttribute('aria-label')).toBe('Resume simulation')
  })

  it('GIVEN HUD rendered in running phase WHEN pause button clicked THEN onTogglePause is called', () => {
    const container = makeContainer()
    const onTogglePause = vi.fn()
    const actions = makeActions({ onTogglePause })
    const hud = createHudController(container, actions)
    const state = createInitialGameState()
    // Pause button is only enabled during running phase (BLOCKER 1 fix)
    startSortie(state, defaultSimulationConfig.fixedDeltaMs)
    hud.render(state, false)

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

    hud.render(state, true)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')!
    // AC6: button textContent is fixed 'Pause'; pause state conveyed via aria-pressed (AC16)
    expect(btn.textContent).toBe('Pause')
    // aria-label describes the current action for screen readers
    expect(btn.getAttribute('aria-label')).toBe('Resume simulation')
    // No exact HP/HULL numbers in the pause button
    expect(btn.textContent).not.toMatch(/\d+\/\d+/)
    // No LoopPhase string exposed in button
    expect(btn.textContent).not.toMatch(/running|debrief|preparation/)
  })
})

describe('HUD render continues during pause — AC4', () => {
  it('GIVEN paused state WHEN render called THEN HUD fields still update', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()
    state.telemetry.status = 'Paused'

    hud.render(state, true)

    const statusEl = container.querySelector<HTMLElement>('[data-field="status"]')
    expect(statusEl?.textContent).toBe('Paused')
  })
})

// ---------------------------------------------------------------------------
// AC16: aria-pressed and pause live region
// ---------------------------------------------------------------------------

describe('HUD aria-pressed and pause live region — AC16', () => {
  it('GIVEN not paused WHEN rendered THEN aria-pressed is "false"', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, false)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')!
    expect(btn.getAttribute('aria-pressed')).toBe('false')
  })

  it('GIVEN paused WHEN rendered THEN aria-pressed is "true"', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, true)

    const btn = container.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')!
    expect(btn.getAttribute('aria-pressed')).toBe('true')
  })

  it('GIVEN paused WHEN rendered THEN pause-status live region shows "Paused"', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, true)

    const pauseStatus = container.querySelector<HTMLElement>('[data-field="pause-status"]')
    expect(pauseStatus).not.toBeNull()
    expect(pauseStatus!.textContent).toBe('Paused')
    expect(pauseStatus!.getAttribute('role')).toBe('status')
  })

  it('GIVEN not paused WHEN rendered THEN pause-status live region is empty', () => {
    const container = makeContainer()
    const actions = makeActions()
    const hud = createHudController(container, actions)
    const state = createInitialGameState()

    hud.render(state, false)

    const pauseStatus = container.querySelector<HTMLElement>('[data-field="pause-status"]')
    expect(pauseStatus?.textContent).toBe('')
  })
})

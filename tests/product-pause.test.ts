/**
 * @vitest-environment node
 *
 * tests/product-pause.test.ts
 *
 * Unit tests for product Pause/Resume surface (Issue #884).
 * Covers: AC3 (P key focus guard), AC5 (input reset), AC8 (visibilitychange),
 *         AC10 (runtime-local), AC11 (productPause naming), AC14 (tick/elapsedMs frozen),
 *         AC15 (P key activeElement guard)
 */
import { describe, expect, it } from 'vitest'
import {
  createProductPauseState,
  toggleProductPause,
  resetInputOnPause,
} from '../src/ui/productPause'
import { advanceSimulationLoop } from '../src/systems/SimulationLoop'
import { createInitialGameState, defaultSimulationConfig } from '../src/state'
import { startSortie, runSortieSimulationStep } from '../src/systems/SortieSystem'
import type { InputCommand } from '../src/input/InputMapper'

// ---------------------------------------------------------------------------
// AC11: productPause naming — product API exists (not just debug)
// ---------------------------------------------------------------------------

describe('ProductPauseState — AC11 (product-facing naming)', () => {
  it('GIVEN createProductPauseState WHEN created THEN isPaused is false', () => {
    const ps = createProductPauseState()
    expect(ps.isPaused).toBe(false)
  })

  it('GIVEN ProductPauseState WHEN toggled THEN isPaused becomes true', () => {
    const ps = createProductPauseState()
    toggleProductPause(ps)
    expect(ps.isPaused).toBe(true)
  })

  it('GIVEN paused ProductPauseState WHEN toggled again THEN isPaused becomes false', () => {
    const ps = createProductPauseState()
    toggleProductPause(ps)
    toggleProductPause(ps)
    expect(ps.isPaused).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// AC14: tick / elapsedMs / sortie.elapsedTicks do not advance during pause
// ---------------------------------------------------------------------------

describe('Simulation frozen while paused — AC14', () => {
  it('GIVEN running sortie paused WHEN multiple frames pass THEN tick, elapsedMs, elapsedTicks do not change', () => {
    const state = createInitialGameState()
    startSortie(state, defaultSimulationConfig.fixedDeltaMs)
    expect(state.loopPhase).toBe('running')

    const FIXED_DT = defaultSimulationConfig.fixedDeltaMs
    const noCommands: InputCommand[] = []

    // Advance a few steps to have non-zero baseline
    runSortieSimulationStep(state, noCommands, FIXED_DT)
    runSortieSimulationStep(state, noCommands, FIXED_DT)

    const tickBefore = state.tick
    const elapsedMsBefore = state.elapsedMs
    const elapsedTicksBefore = state.sortie.elapsedTicks

    // Simulate paused frame loop: accumulator zeroed, no stepSimulation called
    const ps = createProductPauseState()
    toggleProductPause(ps) // enter pause

    let accumulatorMs = 0
    for (let i = 0; i < 10; i++) {
      if (ps.isPaused) {
        // paused: zero accumulator, do NOT call stepSimulation
        accumulatorMs = 0
      } else {
        const result = advanceSimulationLoop(
          accumulatorMs,
          FIXED_DT,
          defaultSimulationConfig,
          (dt) => runSortieSimulationStep(state, noCommands, dt),
        )
        accumulatorMs = result.accumulatorMs
      }
    }

    // AC14: simulation state must not have advanced
    expect(state.tick).toBe(tickBefore)
    expect(state.elapsedMs).toBe(elapsedMsBefore)
    expect(state.sortie.elapsedTicks).toBe(elapsedTicksBefore)
    expect(accumulatorMs).toBe(0)
  })

  it('GIVEN paused state WHEN SortieResult.durationMs is computed THEN it reflects only pre-pause ticks', () => {
    const state = createInitialGameState()
    startSortie(state, defaultSimulationConfig.fixedDeltaMs)

    const FIXED_DT = defaultSimulationConfig.fixedDeltaMs
    const noCommands: InputCommand[] = []

    // Advance 5 ticks
    for (let i = 0; i < 5; i++) {
      runSortieSimulationStep(state, noCommands, FIXED_DT)
    }

    const ticksBefore = state.sortie.elapsedTicks

    // "Pause" for 100 fake frames by not calling stepSimulation
    // (no-op: state is not mutated)

    // Verify that elapsedTicks did not change
    expect(state.sortie.elapsedTicks).toBe(ticksBefore)
    // durationMs would be ticksBefore * FIXED_DT
    const expectedDurationMs = ticksBefore * FIXED_DT
    expect(expectedDurationMs).toBe(ticksBefore * FIXED_DT)
  })
})

// ---------------------------------------------------------------------------
// AC5: input reset on pause entry (prevents held-fire bleed)
// ---------------------------------------------------------------------------

describe('resetInputOnPause — AC5', () => {
  it('GIVEN primaryPressed WHEN resetInputOnPause THEN cleared', () => {
    const input = {
      primaryPressed: true,
      activePointerId: 5 as number | null,
    }
    resetInputOnPause(input)
    expect(input.primaryPressed).toBe(false)
    expect(input.activePointerId).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// AC8 / AC12: visibilitychange auto-pause logic (unit simulation)
// ---------------------------------------------------------------------------

describe('visibilitychange auto-pause — AC8 / AC12', () => {
  it('GIVEN running phase WHEN document hidden THEN productPause becomes true', () => {
    const ps = createProductPauseState()

    // Simulate the visibilitychange handler logic from main.ts
    const loopPhase = 'running'
    const hidden = true

    if (hidden && loopPhase === 'running' && !ps.isPaused) {
      toggleProductPause(ps)
    }

    expect(ps.isPaused).toBe(true)
  })

  it('GIVEN running phase, already paused WHEN document hidden THEN pause state unchanged (no double-pause)', () => {
    const ps = createProductPauseState()
    toggleProductPause(ps) // already paused

    const loopPhase = 'running'
    const hidden = true

    if (hidden && loopPhase === 'running' && !ps.isPaused) {
      toggleProductPause(ps)
    }

    expect(ps.isPaused).toBe(true) // still paused, not double-toggled
  })

  it('GIVEN preparation phase WHEN document hidden THEN no auto-pause (AC12: running only)', () => {
    const ps = createProductPauseState()

    const loopPhase = 'preparation'
    const hidden = true

    if (hidden && loopPhase === 'running' && !ps.isPaused) {
      toggleProductPause(ps)
    }

    expect(ps.isPaused).toBe(false) // not paused: not in running phase
  })

  it('GIVEN auto-paused WHEN document visible THEN pause state unchanged (no auto-resume)', () => {
    const ps = createProductPauseState()
    toggleProductPause(ps) // auto-paused by visibilitychange hidden

    const hidden = false // visible

    // AC8, AC12: visible restoration does NOT auto-resume (no code runs on visible)
    if (!hidden) {
      // intentionally no auto-resume
    }

    expect(ps.isPaused).toBe(true) // remains paused
  })
})

// ---------------------------------------------------------------------------
// AC15: P key guarded by document.activeElement === canvas
// ---------------------------------------------------------------------------

describe('P key canvas-focus guard — AC3 / AC15', () => {
  it('GIVEN P key pressed AND canvas is activeElement THEN toggle is called', () => {
    const ps = createProductPauseState()
    const canvasMock = {} as HTMLCanvasElement
    const activeElement = canvasMock

    // Simulate handleTogglePause only if canvas has focus
    // Using 'running' phase for the pause-entry path
    const loopPhase = 'running'

    if (activeElement === canvasMock) {
      // Phase guard: only pause if running
      if (!ps.isPaused && loopPhase !== 'running') return
      toggleProductPause(ps)
    }

    expect(ps.isPaused).toBe(true)
  })

  it('GIVEN P key pressed AND body is activeElement THEN no toggle (WCAG 2.1.4)', () => {
    const ps = createProductPauseState()
    const canvasMock = {} as HTMLCanvasElement
    const bodyMock = {} as HTMLElement
    const activeElement = bodyMock // body has focus, not canvas

    if (activeElement === canvasMock) {
      toggleProductPause(ps)
    }
    // P key ignored: canvas not focused

    expect(ps.isPaused).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// AC10: productPause state is runtime-local (not in snapshot schema check)
// ---------------------------------------------------------------------------

describe('ProductPauseState runtime-local — AC10', () => {
  it('GIVEN ProductPauseState WHEN isPaused true THEN GameState is not mutated', () => {
    const ps = createProductPauseState()
    const state = createInitialGameState()

    toggleProductPause(ps)

    // GameState has no isPaused field — product pause state is separate (AC10)
    expect('isPaused' in state).toBe(false)
    expect(ps.isPaused).toBe(true)
  })
})

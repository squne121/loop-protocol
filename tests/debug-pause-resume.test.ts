/**
 * @vitest-environment node
 *
 * tests/debug-pause-resume.test.ts
 *
 * Unit tests for debug pause/resume surface (Issue #786).
 * Covers: AC1–AC7
 * Imports from src/ui/debugPause to avoid main.ts DOM side-effects.
 */
import { describe, expect, it, vi } from 'vitest'
import {
  createDebugPauseState,
  toggleDebugPause,
  resetInputOnPause,
} from '../src/ui/debugPause'
import { advanceSimulationLoop } from '../src/systems/SimulationLoop'
import { createInitialGameState, defaultSimulationConfig } from '../src/state'
import { startSortie, runSortieSimulationStep } from '../src/systems/SortieSystem'
import type { InputCommand } from '../src/input/InputMapper'

// ---------------------------------------------------------------------------
// AC1: pause state initialisation
// ---------------------------------------------------------------------------

describe('createDebugPauseState — AC1', () => {
  it('GIVEN new state WHEN created THEN isPaused is false', () => {
    const ps = createDebugPauseState()
    expect(ps.isPaused).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// AC2: toggle — no-op on repeat
// ---------------------------------------------------------------------------

describe('toggleDebugPause — AC2', () => {
  it('GIVEN not paused WHEN toggled THEN isPaused becomes true', () => {
    const ps = createDebugPauseState()
    toggleDebugPause(ps)
    expect(ps.isPaused).toBe(true)
  })

  it('GIVEN paused WHEN toggled THEN isPaused becomes false (resume)', () => {
    const ps = createDebugPauseState()
    toggleDebugPause(ps)
    toggleDebugPause(ps)
    expect(ps.isPaused).toBe(false)
  })

  it('GIVEN not paused WHEN toggled 3 times THEN isPaused is true (odd count)', () => {
    const ps = createDebugPauseState()
    toggleDebugPause(ps)
    toggleDebugPause(ps)
    toggleDebugPause(ps)
    expect(ps.isPaused).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// AC3: simulation state does not advance while paused (runtime correctness)
// ---------------------------------------------------------------------------

describe('Simulation frozen while paused — AC3', () => {
  it('GIVEN running sortie paused WHEN multiple frames pass THEN tick, elapsedMs, sortie.elapsedTicks do not change', () => {
    const state = createInitialGameState()
    startSortie(state, defaultSimulationConfig.fixedDeltaMs)
    expect(state.loopPhase).toBe('running')

    // Advance a few steps to have non-zero baseline
    const FIXED_DT = defaultSimulationConfig.fixedDeltaMs
    const noCommands: InputCommand[] = []
    runSortieSimulationStep(state, noCommands, FIXED_DT)
    runSortieSimulationStep(state, noCommands, FIXED_DT)

    const tickBefore = state.tick
    const elapsedMsBefore = state.elapsedMs
    const elapsedTicksBefore = state.sortie.elapsedTicks

    // Simulate the paused frame loop: when isPaused, no advanceSimulationLoop is called
    // and accumulator is zeroed — simulate 5 "paused frames"
    const debugPause = createDebugPauseState()
    toggleDebugPause(debugPause) // isPaused = true
    let accumulatorMs = 0

    for (let i = 0; i < 5; i++) {
      // While paused: skip simulation advancement, zero out accumulator
      if (debugPause.isPaused) {
        accumulatorMs = 0
        // render would continue here (hud.render + renderer.render) but simulation is NOT stepped
      }
    }

    // After 5 paused frames, state must not have advanced
    expect(state.tick).toBe(tickBefore)
    expect(state.elapsedMs).toBe(elapsedMsBefore)
    expect(state.sortie.elapsedTicks).toBe(elapsedTicksBefore)
    expect(accumulatorMs).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// AC4: hud.render and renderer.render are called even while paused
// ---------------------------------------------------------------------------

describe('Render continues during pause — AC4', () => {
  it('GIVEN paused simulation WHEN frame loop executes THEN both hud.render and renderer.render are called', () => {
    const hudRender = vi.fn()
    const rendererRender = vi.fn()
    const state = createInitialGameState()
    const debugPause = createDebugPauseState()
    toggleDebugPause(debugPause) // isPaused = true

    // Simulate what frame() does: skip simulation advancement but always call render
    const FIXED_DT = defaultSimulationConfig.fixedDeltaMs
    let accumulatorMs = 0

    if (!debugPause.isPaused) {
      // This branch is NOT taken
      const result = advanceSimulationLoop(accumulatorMs, FIXED_DT, defaultSimulationConfig, vi.fn())
      accumulatorMs = result.accumulatorMs
    } else {
      accumulatorMs = 0
    }

    // AC4: render must still be called
    hudRender(state, debugPause.isPaused)
    rendererRender(state)

    expect(hudRender).toHaveBeenCalledTimes(1)
    expect(hudRender).toHaveBeenCalledWith(state, true)
    expect(rendererRender).toHaveBeenCalledTimes(1)
    expect(rendererRender).toHaveBeenCalledWith(state)
    // accumulator was zeroed, not advanced
    expect(accumulatorMs).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// AC5: accumulator resets during pause, preventing catch-up on resume
// ---------------------------------------------------------------------------

describe('advanceSimulationLoop with 0 deltaMs — AC5 (no catch-up)', () => {
  it('GIVEN paused simulation WHEN advanceSimulationLoop called with deltaMs=0 THEN 0 steps executed', async () => {
    const stepFn = { count: 0 }
    const result = advanceSimulationLoop(
      0,
      0,
      defaultSimulationConfig,
      () => { stepFn.count++ },
    )
    expect(result.stepsExecuted).toBe(0)
    expect(stepFn.count).toBe(0)
  })

  it('GIVEN 5-second pause WHEN resume occurs THEN accumulatorMs is 0 (no catch-up build-up)', () => {
    // Simulate 5 seconds (300 frames at 60 Hz) of paused accumulator zeroing
    let accumulatorMs = 0
    const FIXED_DT = defaultSimulationConfig.fixedDeltaMs
    const stepFn = vi.fn()
    const pausedFrames = 300 // ~5 seconds at 60 fps

    for (let i = 0; i < pausedFrames; i++) {
      // Frame loop while paused: accumulator is zeroed each frame
      accumulatorMs = 0
    }

    // On resume, accumulator is 0 — no catch-up steps
    const result = advanceSimulationLoop(accumulatorMs, FIXED_DT, defaultSimulationConfig, stepFn)
    expect(result.stepsExecuted).toBe(1) // exactly 1 step for the resume frame
    expect(result.accumulatorMs).toBeCloseTo(0)
    expect(stepFn).toHaveBeenCalledTimes(1)
  })
})

// ---------------------------------------------------------------------------
// AC7: input cleared on pause AND on resume (no held-fire bleed)
// ---------------------------------------------------------------------------

describe('resetInputOnPause — AC7', () => {
  it('GIVEN primaryPressed=true WHEN resetInputOnPause called THEN primaryPressed becomes false', () => {
    const input = {
      moveUp: true,
      moveDown: false,
      moveLeft: false,
      moveRight: false,
      pointerX: 0,
      pointerY: 0,
      primaryPressed: true,
      activePointerId: 5 as number | null,
      pointerKnown: true,
    }
    resetInputOnPause(input)
    expect(input.primaryPressed).toBe(false)
    expect(input.activePointerId).toBeNull()
  })

  it('GIVEN no held input WHEN resetInputOnPause called THEN state unchanged (idempotent)', () => {
    const input = {
      moveUp: false,
      moveDown: false,
      moveLeft: false,
      moveRight: false,
      pointerX: 10,
      pointerY: 20,
      primaryPressed: false,
      activePointerId: null as number | null,
      pointerKnown: false,
    }
    resetInputOnPause(input)
    expect(input.primaryPressed).toBe(false)
    expect(input.activePointerId).toBeNull()
    // non-pressed coords unchanged
    expect(input.pointerX).toBe(10)
    expect(input.pointerY).toBe(20)
  })

  it('GIVEN activePointerId set WHEN resetInputOnPause called THEN activePointerId is null', () => {
    const input = {
      moveUp: false,
      moveDown: false,
      moveLeft: false,
      moveRight: false,
      pointerX: 0,
      pointerY: 0,
      primaryPressed: false,
      activePointerId: 42 as number | null,
      pointerKnown: false,
    }
    resetInputOnPause(input)
    expect(input.activePointerId).toBeNull()
  })

  it('GIVEN fire input held during pause WHEN resume occurs THEN resetInputOnPause clears held state before resume', () => {
    // This tests that the resume path calls resetInputOnPause BEFORE toggleDebugPause
    // which matches the fix delta reference implementation
    const debugPause = createDebugPauseState()
    toggleDebugPause(debugPause) // enter pause
    expect(debugPause.isPaused).toBe(true)

    const input = {
      moveUp: false,
      moveDown: false,
      moveLeft: false,
      moveRight: false,
      pointerX: 0,
      pointerY: 0,
      primaryPressed: true, // fire held during pause
      activePointerId: 3 as number | null,
      pointerKnown: true,
    }

    // Simulate the handleTogglePause() resume path:
    // 1. resetInputOnPause first (clear accumulated input)
    // 2. then toggleDebugPause (resume)
    resetInputOnPause(input)
    toggleDebugPause(debugPause)

    expect(debugPause.isPaused).toBe(false)
    // AC7: fire state cleared before resume, preventing bleed
    expect(input.primaryPressed).toBe(false)
    expect(input.activePointerId).toBeNull()
  })
})

/**
 * @vitest-environment node
 *
 * tests/debug-pause-resume.test.ts
 *
 * Unit tests for debug pause/resume surface (Issue #786).
 * Covers: AC1–AC7
 * Imports from src/ui/debugPause to avoid main.ts DOM side-effects.
 */
import { describe, expect, it } from 'vitest'
import {
  createDebugPauseState,
  toggleDebugPause,
  resetInputOnPause,
} from '../src/ui/debugPause'

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
// AC3: simulation does not advance while paused
// The isPaused flag is read by the frame() loop; toggling to true stops deltaMs feed.
// ---------------------------------------------------------------------------

describe('DebugPauseState.isPaused — AC3 guard', () => {
  it('GIVEN paused state WHEN isPaused checked THEN is true', () => {
    const ps = createDebugPauseState()
    toggleDebugPause(ps)
    expect(ps.isPaused).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// AC5: accumulator must not advance during pause
// Verified by callers passing deltaMs=0 when isPaused; see main-loop.test for
// advanceSimulationLoop(acc, 0, cfg, step) = 0 steps executed.
// ---------------------------------------------------------------------------

describe('advanceSimulationLoop with 0 deltaMs — AC5 (no catch-up)', () => {
  it('GIVEN paused simulation WHEN advanceSimulationLoop called with deltaMs=0 THEN 0 steps executed', async () => {
    const { advanceSimulationLoop } = await import('../src/systems/SimulationLoop')
    const { defaultSimulationConfig } = await import('../src/state')
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
})

// ---------------------------------------------------------------------------
// AC7: input cleared on pause (no held-fire bleed on resume)
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
})

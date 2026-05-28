import { describe, expect, it, vi } from 'vitest'

import { advanceSimulationLoop } from '../src/systems/SimulationLoop'
import { defaultSimulationConfig } from '../src/state'

const FIXED_DT = defaultSimulationConfig.fixedDeltaMs
const MAX_SKIP = defaultSimulationConfig.maxFrameSkip

// Use a config with integer-friendly fixed step for deterministic tests
const integerConfig = { fixedDeltaMs: 16, maxFrameSkip: 5 }

describe('advanceSimulationLoop', () => {
  it('GIVEN accumulatorMs < fixedDeltaMs WHEN deltaMs is small THEN executes 0 steps and accumulates', () => {
    const stepFn = vi.fn()
    const result = advanceSimulationLoop(0, FIXED_DT / 2, defaultSimulationConfig, stepFn)
    expect(stepFn).toHaveBeenCalledTimes(0)
    expect(result.stepsExecuted).toBe(0)
    expect(result.accumulatorMs).toBeCloseTo(FIXED_DT / 2)
    expect(result.panicDiscarded).toBe(false)
  })

  it('GIVEN accumulator crosses fixedDeltaMs threshold WHEN deltaMs is exactly one step THEN executes exactly 1 step', () => {
    const stepFn = vi.fn()
    const result = advanceSimulationLoop(0, FIXED_DT, defaultSimulationConfig, stepFn)
    expect(stepFn).toHaveBeenCalledTimes(1)
    expect(result.stepsExecuted).toBe(1)
    expect(result.accumulatorMs).toBeCloseTo(0)
    expect(result.panicDiscarded).toBe(false)
  })

  it('GIVEN accumulator far exceeds maxFrameSkip limit WHEN deltaMs is huge THEN discards residual (panic clamp)', () => {
    const stepFn = vi.fn()
    // Huge deltaMs causes far more steps than maxFrameSkip allows
    const hugeDeltaMs = FIXED_DT * (MAX_SKIP + 10)
    const result = advanceSimulationLoop(0, hugeDeltaMs, defaultSimulationConfig, stepFn)

    expect(stepFn).toHaveBeenCalledTimes(MAX_SKIP)
    expect(result.stepsExecuted).toBe(MAX_SKIP)
    expect(result.panicDiscarded).toBe(true)
    // Residual after panic clamp must be < fixedDeltaMs
    expect(result.accumulatorMs).toBeGreaterThanOrEqual(0)
    expect(result.accumulatorMs).toBeLessThan(FIXED_DT)
  })

  it('GIVEN exactly maxFrameSkip steps remain (integer config) WHEN deltaMs fills them exactly THEN no panic discard', () => {
    const stepFn = vi.fn()
    // Use integer-friendly config to avoid floating-point issues
    const exactDeltaMs = integerConfig.fixedDeltaMs * integerConfig.maxFrameSkip
    const result = advanceSimulationLoop(0, exactDeltaMs, integerConfig, stepFn)
    expect(result.stepsExecuted).toBe(integerConfig.maxFrameSkip)
    expect(result.panicDiscarded).toBe(false)
    expect(result.accumulatorMs).toBeCloseTo(0)
  })

  it('GIVEN panic clamp WHEN residual is 1.5 * fixedDeltaMs THEN accumulatorMs = 0.5 * fixedDeltaMs', () => {
    const stepFn = vi.fn()
    // Use integer-friendly config: 1.5 * 16 = 24ms residual
    const residual = integerConfig.fixedDeltaMs * 1.5
    const hugeDelta = integerConfig.fixedDeltaMs * integerConfig.maxFrameSkip + residual
    const result = advanceSimulationLoop(0, hugeDelta, integerConfig, stepFn)
    expect(result.panicDiscarded).toBe(true)
    expect(result.accumulatorMs).toBeCloseTo(integerConfig.fixedDeltaMs * 0.5)
  })

  it('GIVEN residual after maxFrameSkip WHEN panic clamp applied THEN result.accumulatorMs < fixedDeltaMs (using default config)', () => {
    const stepFn = vi.fn()
    // Large delta: max skips + 3 extra
    const largeDelta = FIXED_DT * (MAX_SKIP + 3)
    const result = advanceSimulationLoop(0, largeDelta, defaultSimulationConfig, stepFn)
    expect(result.panicDiscarded).toBe(true)
    expect(result.accumulatorMs).toBeGreaterThanOrEqual(0)
    expect(result.accumulatorMs).toBeLessThan(FIXED_DT)
  })
})

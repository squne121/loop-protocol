import type { SimulationConfig } from '../state'

export interface LoopAdvanceResult {
  accumulatorMs: number
  stepsExecuted: number
  panicDiscarded: boolean
}

/**
 * Pure function: advances the accumulator by deltaMs, executing stepFn for each
 * fixed timestep. After maxFrameSkip steps, discards residual accumulator (panic clamp).
 */
export function advanceSimulationLoop(
  accumulatorMs: number,
  deltaMs: number,
  config: SimulationConfig,
  stepFn: (fixedDeltaMs: number) => void,
): LoopAdvanceResult {
  let acc = accumulatorMs + deltaMs
  let stepsExecuted = 0

  while (acc >= config.fixedDeltaMs && stepsExecuted < config.maxFrameSkip) {
    stepFn(config.fixedDeltaMs)
    acc -= config.fixedDeltaMs
    stepsExecuted += 1
  }

  let panicDiscarded = false
  if (acc >= config.fixedDeltaMs) {
    acc = acc % config.fixedDeltaMs
    panicDiscarded = true
  }

  return { accumulatorMs: acc, stepsExecuted, panicDiscarded }
}

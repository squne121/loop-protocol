export interface SimulationConfig {
  fixedDeltaMs: number
  maxFrameSkip: number
}

export const defaultSimulationConfig: SimulationConfig = {
  fixedDeltaMs: 1000 / 60,
  maxFrameSkip: 5,
}

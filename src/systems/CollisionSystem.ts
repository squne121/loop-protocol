import type { GameState } from '../state'

export function runCollisionSystem(state: GameState): void {
  state.player.x = clamp(
    state.player.x,
    state.player.radius,
    state.arena.width - state.player.radius,
  )
  state.player.y = clamp(
    state.player.y,
    state.player.radius,
    state.arena.height - state.player.radius,
  )

  if (state.player.x >= state.arena.width - state.player.radius - 4) {
    state.telemetry.status = 'Sensor edge reached'
    return
  }

  state.telemetry.status = 'Combat systems green'
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

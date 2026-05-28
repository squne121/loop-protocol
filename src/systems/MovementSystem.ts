import type { GameState } from '../state'
import type { InputCommand } from '../input'

/** Clamp player circle to stay fully inside the arena. Safe to call at any time. */
export function clampPlayerToArena(state: GameState): void {
  const r = state.player.radius
  state.player.x = Math.max(r, Math.min(state.arena.width - r, state.player.x))
  state.player.y = Math.max(r, Math.min(state.arena.height - r, state.player.y))
}

export function runMovementSystem(
  state: GameState,
  commands: InputCommand[],
  deltaMs: number,
): void {
  const moveCommand = commands.find((command) => command.type === 'move')

  if (moveCommand && moveCommand.type === 'move') {
    const length = Math.hypot(moveCommand.axisX, moveCommand.axisY) || 1
    const normalizedX = moveCommand.axisX / length
    const normalizedY = moveCommand.axisY / length
    const deltaSeconds = deltaMs / 1000

    state.player.x += normalizedX * state.player.speed * deltaSeconds
    state.player.y += normalizedY * state.player.speed * deltaSeconds

    state.telemetry.lastCommandSummary = `Thrust ${normalizedX.toFixed(2)}, ${normalizedY.toFixed(2)}`
  }

  // Always clamp: ensures invariant holds regardless of move command presence or arena resize.
  clampPlayerToArena(state)
}

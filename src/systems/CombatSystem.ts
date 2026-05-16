import type { GameState } from '../state'
import type { InputCommand } from '../input'

export function runCombatSystem(
  state: GameState,
  commands: InputCommand[],
  deltaMs: number,
): void {
  const aimCommand = commands.find((command) => command.type === 'aim')
  const fireRequested = commands.some((command) => command.type === 'fire')

  if (aimCommand && aimCommand.type === 'aim') {
    state.player.aimX = aimCommand.x
    state.player.aimY = aimCommand.y
  }

  state.player.weaponCooldownMs = Math.max(
    0,
    state.player.weaponCooldownMs - deltaMs,
  )

  if (!fireRequested || state.player.weaponCooldownMs > 0) {
    return
  }

  state.player.weaponCooldownMs = state.player.weaponIntervalMs
  state.player.shotsFired += 1
  state.progress.resources += state.progress.weaponPower
  state.telemetry.lastCommandSummary = `Volley ${state.player.shotsFired} confirmed`
}

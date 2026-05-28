import type { GameState } from '../state'
import type { InputCommand } from '../input'

const AIM_EPSILON_PX = 0.001
const PROJECTILE_SPEED_PX_PER_SEC = 520
const PROJECTILE_LIFETIME_MS = 1200
const PROJECTILE_RADIUS_PX = 4

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

  // Compute direction from player center to aim position
  const dx = state.player.aimX - state.player.x
  const dy = state.player.aimY - state.player.y
  const dist = Math.hypot(dx, dy)

  let directionX: number
  let directionY: number

  if (dist < AIM_EPSILON_PX) {
    // Same-position fallback: use last saved aim direction (initial default is +X).
    directionX = state.player.lastAimDirectionX
    directionY = state.player.lastAimDirectionY
  } else {
    directionX = dx / dist
    directionY = dy / dist
    // Persist non-zero direction for future same-position fallback.
    state.player.lastAimDirectionX = directionX
    state.player.lastAimDirectionY = directionY
  }

  state.projectiles.push({
    id: state.nextProjectileId,
    x: state.player.x,
    y: state.player.y,
    radius: PROJECTILE_RADIUS_PX,
    directionX,
    directionY,
    speedPxPerSec: PROJECTILE_SPEED_PX_PER_SEC,
    ageMs: 0,
    lifetimeMs: PROJECTILE_LIFETIME_MS,
  })
  state.nextProjectileId += 1

  state.telemetry.lastCommandSummary = `Volley ${state.player.shotsFired} confirmed`
}

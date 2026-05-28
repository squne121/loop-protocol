import type { GameState } from '../state'
import type { InputCommand } from '../input'

const OUT_OF_BOUNDS_MARGIN_PX = 8

export function runProjectileSystem(
  state: GameState,
  _commands: InputCommand[],
  deltaMs: number,
): void {
  const deltaSeconds = deltaMs / 1000

  // Advance age and position for all projectiles
  for (const projectile of state.projectiles) {
    projectile.ageMs += deltaMs
    projectile.x += projectile.directionX * projectile.speedPxPerSec * deltaSeconds
    projectile.y += projectile.directionY * projectile.speedPxPerSec * deltaSeconds
  }

  // Remove expired or out-of-bounds projectiles (order-preserving filter)
  const margin = OUT_OF_BOUNDS_MARGIN_PX
  state.projectiles = state.projectiles.filter((p) => {
    if (p.ageMs >= p.lifetimeMs) {
      return false
    }
    if (
      p.x < -margin ||
      p.x > state.arena.width + margin ||
      p.y < -margin ||
      p.y > state.arena.height + margin
    ) {
      return false
    }
    return true
  })
}

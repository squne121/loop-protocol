import type { GameState } from '../state'

/** Enemies within this distance of the player center are considered co-located and will not move. */
export const ENEMY_AI_EPSILON_PX = 0.5

/**
 * Moves each non-defeated enemy toward the player center using a fixed timestep delta.
 *
 * Design constraints (AC7 / AC8):
 * - No DOM / Canvas API / window / document / Date.now / performance.now dependencies.
 * - Only mutates enemy x/y coordinates. Player, projectiles, sortie, result, and render
 *   state are left untouched.
 *
 * @param state       Mutable game state
 * @param fixedDeltaMs Fixed timestep in milliseconds (e.g. 1000/60 approx 16.67)
 */
export function runEnemyAISystem(state: GameState, fixedDeltaMs: number): void {
  const deltaSec = fixedDeltaMs / 1000
  const playerX = state.player.x
  const playerY = state.player.y

  for (const enemy of state.enemies) {
    // AC3: skip defeated enemies
    if (enemy.defeated) {
      continue
    }

    const dx = playerX - enemy.x
    const dy = playerY - enemy.y
    const distance = Math.hypot(dx, dy)

    // AC5: avoid NaN when enemy is already at (or extremely close to) player center
    if (distance <= ENEMY_AI_EPSILON_PX) {
      continue
    }

    // AC4: normalized direction vector
    const normX = dx / distance
    const normY = dy / distance

    // AC2 + AC6: compute movement with overshoot clamp
    const moveDist = Math.min(enemy.speedPxPerSec * deltaSec, distance)

    enemy.x += normX * moveDist
    enemy.y += normY * moveDist
  }
}

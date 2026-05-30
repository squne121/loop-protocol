import type { CollisionPair, GameState } from '../state'

/**
 * Deterministic comparator for CollisionPair.
 * Order: projectile-enemy first, then player-enemy.
 * Within projectile-enemy: projectileId ASC, then enemyId ASC.
 * Within player-enemy: enemyId ASC.
 */
export function compareCollisionPair(a: CollisionPair, b: CollisionPair): number {
  const ar = a.kind === 'projectile-enemy' ? 0 : 1
  const br = b.kind === 'projectile-enemy' ? 0 : 1
  if (ar !== br) return ar - br
  if (a.kind === 'projectile-enemy' && b.kind === 'projectile-enemy') {
    if (a.projectileId !== b.projectileId) return a.projectileId - b.projectileId
    return a.enemyId - b.enemyId
  }
  if (a.kind === 'player-enemy' && b.kind === 'player-enemy') {
    return a.enemyId - b.enemyId
  }
  return 0
}

/**
 * Pure collision detection: returns all collision pairs for this tick.
 *
 * AC1: Does NOT mutate hp / projectile / result / resource / persistence.
 * AC2: boundary clamp is MovementSystem's responsibility (clampPlayerToArena).
 * AC5: Circle hitbox — dx*dx + dy*dy <= (r1+r2)**2 (no sqrt).
 * AC6: 1 projectile hits at most 1 enemy per tick (closest; tie-break: id ASC).
 * AC7: projectile-enemy pairs are listed before player-enemy pairs.
 */
export function runCollisionSystem(state: GameState): readonly CollisionPair[] {
  const pairs: CollisionPair[] = []

  // --- projectile-enemy collisions (AC5, AC6) ---
  // Track which projectile ids have already been assigned a hit this tick.
  const hitProjectileIds = new Set<number>()

  for (const projectile of state.projectiles) {
    let closestEnemyId: number | null = null
    let closestDistSq = Infinity

    for (const enemy of state.enemies) {
      if (enemy.defeated) continue

      const dx = projectile.x - enemy.x
      const dy = projectile.y - enemy.y
      const distSq = dx * dx + dy * dy
      const sumR = projectile.radius + enemy.radius
      const threshold = sumR * sumR

      if (distSq <= threshold) {
        // Same-distance tie-break: lower enemy.id wins (AC6).
        if (
          distSq < closestDistSq ||
          (distSq === closestDistSq &&
            closestEnemyId !== null &&
            enemy.id < closestEnemyId)
        ) {
          closestDistSq = distSq
          closestEnemyId = enemy.id
        }
      }
    }

    if (closestEnemyId !== null && !hitProjectileIds.has(projectile.id)) {
      hitProjectileIds.add(projectile.id)
      pairs.push({
        kind: 'projectile-enemy',
        projectileId: projectile.id,
        enemyId: closestEnemyId,
        distSq: closestDistSq,
      })
    }
  }

  // --- player-enemy collisions (AC7: after projectile-enemy) ---
  const playerR = state.player.radius
  for (const enemy of state.enemies) {
    if (enemy.defeated) continue

    const dx = state.player.x - enemy.x
    const dy = state.player.y - enemy.y
    const distSq = dx * dx + dy * dy
    const sumR = playerR + enemy.radius
    if (distSq <= sumR * sumR) {
      pairs.push({ kind: 'player-enemy', enemyId: enemy.id })
    }
  }

  return [...pairs].sort(compareCollisionPair)
}

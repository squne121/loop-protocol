import type { CollisionPair, GameState } from '../state'
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
    // AC4: snapshot weaponPower at fire time so later changes do not affect existing projectiles.
    damage: state.progress.weaponPower,
  })
  state.nextProjectileId += 1

  state.telemetry.lastCommandSummary = `Volley ${state.player.shotsFired} confirmed`
}

/**
 * Apply damage and defeat from collision pairs produced by runCollisionSystem.
 *
 * AC3:  New function; existing runCombatSystem signature is unchanged.
 * AC7:  pairs are already ordered (projectile-enemy first, player-enemy second)
 *       by the numeric tuple comparator in CollisionSystem.
 * AC8:  enemy hp = Math.max(0, hp - projectile.damage).
 * AC9:  defeated = true, defeatedAtTick = state.tick (before tick increment).
 * AC10: hit projectiles are removed from state.projectiles (set active=false equivalent).
 * AC11: enemies defeated by projectile this tick are skipped in player-enemy processing.
 * AC12: player-enemy contact damage applied in enemy id ASC order.
 * AC13: player.hp clamped to >= 0.
 */
export function resolveCombatCollisions(
  state: GameState,
  pairs: readonly CollisionPair[],
): void {
  // Collect projectile ids that scored a hit this tick.
  const hitProjectileIds = new Set<number>()
  // Collect enemy ids defeated by projectile this tick (for AC11).
  const projectileDefeatedEnemyIds = new Set<number>()

  // --- Pass 1: projectile-enemy (AC7 ordering guaranteed by CollisionSystem) ---
  // Sort projectile-enemy pairs by (enemyId ASC) then (projectileId ASC) for determinism.
  // Within the same tick, process projectile-enemy before player-enemy.
  const projEnemyPairs = pairs
    .filter((p): p is Extract<CollisionPair, { kind: 'projectile-enemy' }> => p.kind === 'projectile-enemy')
    .sort((a, b) => {
      // Sort by enemyId ASC, then projectileId ASC (numeric tuple comparator, AC7).
      if (a.enemyId !== b.enemyId) return a.enemyId - b.enemyId
      return a.projectileId - b.projectileId
    })

  for (const pair of projEnemyPairs) {
    // Skip if this projectile already used up on another enemy this tick.
    if (hitProjectileIds.has(pair.projectileId)) continue

    const enemy = state.enemies.find((e) => e.id === pair.enemyId)
    const projectile = state.projectiles.find((p) => p.id === pair.projectileId)

    if (!enemy || enemy.defeated || !projectile) continue

    // AC8: reduce hp
    enemy.hp = Math.max(0, enemy.hp - projectile.damage)

    // AC9: defeat check
    if (enemy.hp === 0) {
      enemy.defeated = true
      enemy.defeatedAtTick = state.tick
      projectileDefeatedEnemyIds.add(enemy.id)
    }

    hitProjectileIds.add(pair.projectileId)
  }

  // AC10: remove hit projectiles from state
  state.projectiles = state.projectiles.filter(
    (p) => !hitProjectileIds.has(p.id),
  )

  // --- Pass 2: player-enemy contact damage ---
  // AC11: skip enemies defeated by projectile this same tick.
  // AC12: process undefeated enemies in id ASC order, accumulate totalDamage.
  const playerEnemyPairs = pairs
    .filter((p): p is Extract<CollisionPair, { kind: 'player-enemy' }> => p.kind === 'player-enemy')
    .filter((p) => !projectileDefeatedEnemyIds.has(p.enemyId))
    .sort((a, b) => a.enemyId - b.enemyId)

  let totalDamage = 0
  for (const pair of playerEnemyPairs) {
    const enemy = state.enemies.find((e) => e.id === pair.enemyId)
    if (!enemy || enemy.defeated) continue
    totalDamage += enemy.contactDamage
  }

  // AC12, AC13: clamp player hp to >= 0.
  state.player.hp = Math.max(0, state.player.hp - totalDamage)
}

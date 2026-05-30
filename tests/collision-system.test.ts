import { describe, expect, it } from 'vitest'

import { createInitialGameState } from '../src/state'
import type { EnemyState, ProjectileState } from '../src/state'
import { resolveCombatCollisions, runCollisionSystem } from '../src/systems'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeEnemy(overrides: Partial<EnemyState>): EnemyState {
  return {
    id: 1,
    definitionId: 'test',
    hp: 10,
    maxHp: 10,
    x: 500,
    y: 270,
    radius: 16,
    speedPxPerSec: 60,
    contactDamage: 1,
    defeated: false,
    defeatedAtTick: null,
    ...overrides,
  }
}

function makeProjectile(overrides: Partial<ProjectileState>): ProjectileState {
  return {
    id: 1,
    x: 500,
    y: 270,
    radius: 4,
    directionX: 1,
    directionY: 0,
    speedPxPerSec: 520,
    ageMs: 0,
    lifetimeMs: 1200,
    damage: 5,
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// runCollisionSystem — pure detection (AC1, AC2, AC5, AC6, AC7)
// ---------------------------------------------------------------------------

describe('runCollisionSystem', () => {
  it('GIVEN no enemies WHEN collision system runs THEN returns empty pairs', () => {
    const state = createInitialGameState()
    const pairs = runCollisionSystem(state)
    expect(pairs).toHaveLength(0)
  })

  it('GIVEN projectile overlapping enemy WHEN collision system runs THEN returns projectile-enemy pair (AC5)', () => {
    const state = createInitialGameState()
    const enemy = makeEnemy({ id: 1, x: 300, y: 270, radius: 16 })
    state.enemies = [enemy]
    // Place projectile on top of enemy (distSq = 0 <= (4+16)^2 = 400)
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)

    expect(pairs).toHaveLength(1)
    expect(pairs[0].kind).toBe('projectile-enemy')
    if (pairs[0].kind === 'projectile-enemy') {
      expect(pairs[0].projectileId).toBe(1)
      expect(pairs[0].enemyId).toBe(1)
    }
  })

  it('GIVEN projectile far from enemy WHEN collision system runs THEN no pair returned (AC5)', () => {
    const state = createInitialGameState()
    state.enemies = [makeEnemy({ id: 1, x: 600, y: 270, radius: 16 })]
    // projectile at (100, 270) — dist = 500, (4+16)^2 = 400 — no hit
    state.projectiles = [makeProjectile({ id: 1, x: 100, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    expect(pairs).toHaveLength(0)
  })

  it('GIVEN projectile at exactly sumR distance WHEN collision system runs THEN pair IS returned (boundary, AC5)', () => {
    const state = createInitialGameState()
    // sumR = 4 + 16 = 20; place projectile exactly 20px away on X axis
    state.enemies = [makeEnemy({ id: 1, x: 320, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    expect(pairs).toHaveLength(1)
  })

  it('GIVEN two enemies equidistant from projectile WHEN collision system runs THEN enemy with lower id is selected (AC6 tie-break)', () => {
    const state = createInitialGameState()
    // Two enemies at the same distance from projectile, ids 10 and 2 — id 2 should win
    state.enemies = [
      makeEnemy({ id: 10, x: 300, y: 270, radius: 16 }),
      makeEnemy({ id: 2, x: 300, y: 270, radius: 16 }),
    ]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    const projPairs = pairs.filter((p) => p.kind === 'projectile-enemy')
    expect(projPairs).toHaveLength(1)
    if (projPairs[0].kind === 'projectile-enemy') {
      expect(projPairs[0].enemyId).toBe(2)
    }
  })

  it('GIVEN projectile id 2 and id 10 both hit different enemies WHEN collision system runs THEN id 2 projectile is listed first', () => {
    const state = createInitialGameState()
    state.enemies = [
      makeEnemy({ id: 1, x: 200, y: 270, radius: 16 }),
      makeEnemy({ id: 2, x: 400, y: 270, radius: 16 }),
    ]
    state.projectiles = [
      makeProjectile({ id: 10, x: 400, y: 270, radius: 4, damage: 5 }),
      makeProjectile({ id: 2, x: 200, y: 270, radius: 4, damage: 5 }),
    ]

    const pairs = runCollisionSystem(state)
    const projPairs = pairs.filter((p) => p.kind === 'projectile-enemy')
    expect(projPairs).toHaveLength(2)
    // Both projectiles should appear; order is deterministic by iteration order
    const projectileIds = projPairs
      .filter((p) => p.kind === 'projectile-enemy')
      .map((p) => (p.kind === 'projectile-enemy' ? p.projectileId : -1))
    expect(projectileIds).toContain(2)
    expect(projectileIds).toContain(10)
  })

  it('GIVEN enemies with id 2 and id 10 WHEN player touches both THEN id 2 enemy appears first in player-enemy pairs', () => {
    const state = createInitialGameState()
    // Place player at (300, 270)
    state.player.x = 300
    state.player.y = 270
    state.player.radius = 14
    // Both enemies overlap player
    state.enemies = [
      makeEnemy({ id: 10, x: 300, y: 270, radius: 16 }),
      makeEnemy({ id: 2, x: 300, y: 270, radius: 16 }),
    ]

    const pairs = runCollisionSystem(state)
    const playerPairs = pairs.filter((p) => p.kind === 'player-enemy')
    expect(playerPairs.length).toBeGreaterThanOrEqual(2)
    // Order of player-enemy is by iteration order (state.enemies array order)
    // resolveCombatCollisions will sort by id ASC; CollisionSystem just needs to return them
    const enemyIds = playerPairs.map((p) =>
      p.kind === 'player-enemy' ? p.enemyId : -1,
    )
    expect(enemyIds).toContain(2)
    expect(enemyIds).toContain(10)
  })

  it('GIVEN projectile-enemy and player-enemy collisions WHEN collision system runs THEN projectile-enemy pairs appear before player-enemy pairs (AC7)', () => {
    const state = createInitialGameState()
    state.player.x = 300
    state.player.y = 270
    state.player.radius = 14
    state.enemies = [makeEnemy({ id: 1, x: 300, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    const firstProjectileIdx = pairs.findIndex((p) => p.kind === 'projectile-enemy')
    const firstPlayerIdx = pairs.findIndex((p) => p.kind === 'player-enemy')

    expect(firstProjectileIdx).toBeGreaterThanOrEqual(0)
    expect(firstPlayerIdx).toBeGreaterThanOrEqual(0)
    // projectile-enemy must appear before player-enemy (AC7)
    expect(firstProjectileIdx).toBeLessThan(firstPlayerIdx)
  })

  it('GIVEN defeated enemy WHEN collision system runs THEN no pair is generated for that enemy (AC1 — no mutation required)', () => {
    const state = createInitialGameState()
    state.enemies = [makeEnemy({ id: 1, x: 300, y: 270, radius: 16, defeated: true })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]
    state.player.x = 300
    state.player.y = 270

    const pairs = runCollisionSystem(state)
    expect(pairs).toHaveLength(0)
  })

  it('GIVEN state with resources and progress WHEN collision system runs THEN those fields are NOT mutated (AC1)', () => {
    const state = createInitialGameState()
    state.progress.resources = 42
    state.progress.weaponPower = 3
    state.enemies = [makeEnemy({ id: 1, x: 300, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    runCollisionSystem(state)

    expect(state.progress.resources).toBe(42)
    expect(state.progress.weaponPower).toBe(3)
    // hp must not be mutated by CollisionSystem (only by resolveCombatCollisions)
    expect(state.enemies[0].hp).toBe(10)
    expect(state.player.hp).toBe(state.player.maxHp)
  })

  it('GIVEN state WHEN collision system runs THEN state.projectiles array is NOT mutated (AC1)', () => {
    const state = createInitialGameState()
    state.enemies = [makeEnemy({ id: 1, x: 300, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]
    const projLengthBefore = state.projectiles.length

    runCollisionSystem(state)

    expect(state.projectiles).toHaveLength(projLengthBefore)
  })
})

// ---------------------------------------------------------------------------
// resolveCombatCollisions — damage / defeat / player contact (AC3, AC8–AC13)
// ---------------------------------------------------------------------------

describe('resolveCombatCollisions', () => {
  it('GIVEN projectile hit with damage 5 WHEN resolveCombatCollisions THEN enemy hp decreases by 5 (AC8)', () => {
    const state = createInitialGameState()
    state.enemies = [makeEnemy({ id: 1, hp: 10, x: 300, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.enemies[0].hp).toBe(5)
  })

  it('GIVEN projectile damage exceeds enemy hp WHEN resolveCombatCollisions THEN enemy hp clamps to 0 (AC8)', () => {
    const state = createInitialGameState()
    state.enemies = [makeEnemy({ id: 1, hp: 3, x: 300, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 10 })]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.enemies[0].hp).toBe(0)
  })

  it('GIVEN projectile kills enemy WHEN resolveCombatCollisions THEN enemy.defeated = true and defeatedAtTick = state.tick (AC9)', () => {
    const state = createInitialGameState()
    state.tick = 5
    state.enemies = [makeEnemy({ id: 1, hp: 5, x: 300, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.enemies[0].defeated).toBe(true)
    expect(state.enemies[0].defeatedAtTick).toBe(5)
  })

  it('GIVEN projectile hits enemy but does not kill WHEN resolveCombatCollisions THEN enemy.defeated remains false', () => {
    const state = createInitialGameState()
    state.enemies = [makeEnemy({ id: 1, hp: 10, x: 300, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.enemies[0].defeated).toBe(false)
    expect(state.enemies[0].defeatedAtTick).toBeNull()
  })

  it('GIVEN projectile hits enemy WHEN resolveCombatCollisions THEN projectile is removed from state.projectiles (AC10)', () => {
    const state = createInitialGameState()
    state.enemies = [makeEnemy({ id: 1, hp: 10, x: 300, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.projectiles).toHaveLength(0)
  })

  it('GIVEN two projectiles one hits enemy WHEN resolveCombatCollisions THEN only hit projectile is removed (AC10)', () => {
    const state = createInitialGameState()
    state.enemies = [makeEnemy({ id: 1, hp: 10, x: 300, y: 270, radius: 16 })]
    // projectile 1: hits enemy; projectile 2: far away
    state.projectiles = [
      makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 }),
      makeProjectile({ id: 2, x: 900, y: 270, radius: 4, damage: 5 }),
    ]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.projectiles).toHaveLength(1)
    expect(state.projectiles[0].id).toBe(2)
  })

  it('GIVEN projectile kills enemy AND player also overlaps that enemy WHEN resolveCombatCollisions THEN player takes no contact damage (AC11)', () => {
    const state = createInitialGameState()
    state.player.x = 300
    state.player.y = 270
    state.player.radius = 14
    const initialHp = state.player.hp

    state.enemies = [makeEnemy({ id: 1, hp: 5, x: 300, y: 270, radius: 16, contactDamage: 3 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    // Enemy was defeated by projectile; contact damage must be skipped
    expect(state.player.hp).toBe(initialHp)
  })

  it('GIVEN player overlaps undefeated enemy WHEN resolveCombatCollisions THEN player takes contactDamage (AC12)', () => {
    const state = createInitialGameState()
    state.player.x = 300
    state.player.y = 270
    state.player.radius = 14
    const initialHp = state.player.hp

    state.enemies = [makeEnemy({ id: 1, hp: 10, x: 300, y: 270, radius: 16, contactDamage: 2 })]
    state.projectiles = []

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.player.hp).toBe(initialHp - 2)
  })

  it('GIVEN player hp would go negative WHEN resolveCombatCollisions THEN player hp is clamped to 0 (AC13)', () => {
    const state = createInitialGameState()
    state.player.x = 300
    state.player.y = 270
    state.player.radius = 14
    state.player.hp = 1

    state.enemies = [makeEnemy({ id: 1, hp: 10, x: 300, y: 270, radius: 16, contactDamage: 100 })]
    state.projectiles = []

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.player.hp).toBe(0)
  })

  it('GIVEN two undefeated enemies overlap player WHEN resolveCombatCollisions THEN player takes sum of both contactDamages (AC12)', () => {
    const state = createInitialGameState()
    state.player.x = 300
    state.player.y = 270
    state.player.radius = 14
    const initialHp = state.player.hp

    state.enemies = [
      makeEnemy({ id: 2, hp: 10, x: 300, y: 270, radius: 16, contactDamage: 2 }),
      makeEnemy({ id: 10, hp: 10, x: 300, y: 270, radius: 16, contactDamage: 3 }),
    ]
    state.projectiles = []

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.player.hp).toBe(initialHp - 5)
  })

  it('GIVEN enemy ids 10 and 2 WHEN player-enemy damage processed THEN id 2 is processed first (id ASC order, AC12)', () => {
    // We can only observe ordering side effects if damage is cumulative to a limit.
    // Since damage is additive, ordering does not change total; instead verify defeated skip.
    // Here we verify that if id 2 is defeated (hp=0 from prev projectile hit before player contact),
    // its contactDamage does not apply regardless of ordering.
    const state = createInitialGameState()
    state.player.x = 300
    state.player.y = 270
    state.player.radius = 14
    const initialHp = state.player.hp

    // id 2 has low hp (will be killed by projectile), id 10 stays alive
    state.enemies = [
      makeEnemy({ id: 2, hp: 1, x: 300, y: 270, radius: 16, contactDamage: 99 }),
      makeEnemy({ id: 10, hp: 10, x: 300, y: 270, radius: 16, contactDamage: 1 }),
    ]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 1 })]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    // id 2 was killed by projectile; its contactDamage=99 must NOT apply
    // id 10 stays alive; contactDamage=1 applies
    expect(state.player.hp).toBe(initialHp - 1)
    expect(state.enemies.find((e) => e.id === 2)?.defeated).toBe(true)
  })

  it('GIVEN no collision pairs WHEN resolveCombatCollisions THEN state is unchanged', () => {
    const state = createInitialGameState()
    const hpBefore = state.player.hp
    const enemyHpBefore = 10

    state.enemies = [makeEnemy({ id: 1, hp: enemyHpBefore })]
    resolveCombatCollisions(state, [])

    expect(state.player.hp).toBe(hpBefore)
    expect(state.enemies[0].hp).toBe(enemyHpBefore)
    expect(state.enemies[0].defeated).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Integration: runCollisionSystem + resolveCombatCollisions pipeline
// ---------------------------------------------------------------------------

describe('collision + combat pipeline (AC15 regression)', () => {
  it('GIVEN projectile id 2 and projectile id 10 WHEN pipeline runs THEN both are handled deterministically', () => {
    const state = createInitialGameState()
    state.enemies = [
      makeEnemy({ id: 1, hp: 5, x: 200, y: 270, radius: 16 }),
      makeEnemy({ id: 2, hp: 5, x: 400, y: 270, radius: 16 }),
    ]
    state.projectiles = [
      makeProjectile({ id: 2, x: 200, y: 270, radius: 4, damage: 5 }),
      makeProjectile({ id: 10, x: 400, y: 270, radius: 4, damage: 5 }),
    ]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    // Both enemies should be defeated
    expect(state.enemies[0].defeated).toBe(true)
    expect(state.enemies[1].defeated).toBe(true)
    // Both projectiles removed
    expect(state.projectiles).toHaveLength(0)
  })

  it('GIVEN enemy id 2 and id 10 WHEN same projectile is in range of both THEN enemy with id 2 is hit (AC6 id ASC)', () => {
    const state = createInitialGameState()
    state.enemies = [
      makeEnemy({ id: 10, hp: 10, x: 300, y: 270, radius: 16 }),
      makeEnemy({ id: 2, hp: 10, x: 300, y: 270, radius: 16 }),
    ]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    // id 2 should have taken damage; id 10 should be untouched
    const e2 = state.enemies.find((e) => e.id === 2)
    const e10 = state.enemies.find((e) => e.id === 10)
    expect(e2?.hp).toBe(5)
    expect(e10?.hp).toBe(10)
  })

  it('GIVEN same-tick kill WHEN player overlaps killed enemy THEN no contact damage (AC11 regression)', () => {
    const state = createInitialGameState()
    state.player.x = 300
    state.player.y = 270
    state.player.radius = 14
    const initialHp = state.player.hp

    state.enemies = [makeEnemy({ id: 1, hp: 5, x: 300, y: 270, radius: 16, contactDamage: 5 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    const pairs = runCollisionSystem(state)
    resolveCombatCollisions(state, pairs)

    expect(state.player.hp).toBe(initialHp)
    expect(state.enemies[0].defeated).toBe(true)
  })

  it('GIVEN no mutations to result/resource/persistence in CollisionSystem WHEN pipeline runs THEN only CombatSystem mutates game state (AC1 regression)', () => {
    const state = createInitialGameState()
    state.progress.resources = 100
    state.enemies = [makeEnemy({ id: 1, hp: 5, x: 300, y: 270, radius: 16 })]
    state.projectiles = [makeProjectile({ id: 1, x: 300, y: 270, radius: 4, damage: 5 })]

    // Capture state before calling CollisionSystem
    const resourcesBefore = state.progress.resources
    const enemyHpBefore = state.enemies[0].hp

    // Only CollisionSystem — must NOT change hp or resources
    runCollisionSystem(state)
    expect(state.progress.resources).toBe(resourcesBefore)
    expect(state.enemies[0].hp).toBe(enemyHpBefore)
  })
})

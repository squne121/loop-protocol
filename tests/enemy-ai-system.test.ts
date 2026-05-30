import { describe, expect, it } from 'vitest'

import { createInitialGameState } from '../src/state'
import { ENEMY_AI_EPSILON_PX, runEnemyAISystem } from '../src/systems'
import type { EnemyState } from '../src/state'

function makeEnemy(overrides: Partial<EnemyState>): EnemyState {
  return {
    id: 1,
    definitionId: 'test-enemy',
    hp: 10,
    maxHp: 10,
    x: 0,
    y: 0,
    radius: 16,
    speedPxPerSec: 100,
    contactDamage: 1,
    defeated: false,
    defeatedAtTick: null,
    ...overrides,
  }
}

describe('runEnemyAISystem', () => {
  it('GIVEN enemy to the right of player WHEN AI runs THEN enemy moves left (cardinal approach)', () => {
    const state = createInitialGameState()
    // Player at (240, 270), enemy to the right at (340, 270)
    state.player.x = 240
    state.player.y = 270
    const enemy = makeEnemy({ x: 340, y: 270, speedPxPerSec: 100 })
    state.enemies = [enemy]

    // fixedDeltaMs = 1000/60 approx 16.67ms
    const fixedDeltaMs = 1000 / 60
    runEnemyAISystem(state, fixedDeltaMs)

    const deltaSec = fixedDeltaMs / 1000
    const expectedMove = 100 * deltaSec // speed * deltaSec = ~1.667px
    expect(state.enemies[0].x).toBeCloseTo(340 - expectedMove)
    expect(state.enemies[0].y).toBeCloseTo(270)
  })

  it('GIVEN enemy diagonally from player WHEN AI runs THEN enemy moves along normalized direction (diagonal approach)', () => {
    const state = createInitialGameState()
    // Player at (0, 0), enemy at (100, 100) → direction normalized = (1/sqrt2, 1/sqrt2)
    state.player.x = 0
    state.player.y = 0
    const enemy = makeEnemy({ x: 100, y: 100, speedPxPerSec: 100 })
    state.enemies = [enemy]

    const fixedDeltaMs = 1000 // 1 second for easy math
    runEnemyAISystem(state, fixedDeltaMs)

    // move = 100px * 1s = 100px along normalized direction
    const norm = 1 / Math.SQRT2
    expect(state.enemies[0].x).toBeCloseTo(100 - 100 * norm)
    expect(state.enemies[0].y).toBeCloseTo(100 - 100 * norm)
  })

  it('GIVEN enemy very close to player center WHEN AI would overshoot THEN movement is clamped (overshoot clamp)', () => {
    const state = createInitialGameState()
    // Player at (100, 100), enemy at (101, 100) — distance 1.0 > EPSILON (0.5) but < speed*deltaSec (1000)
    state.player.x = 100
    state.player.y = 100
    const distance = 1.0
    const enemy = makeEnemy({ x: 100 + distance, y: 100, speedPxPerSec: 1000 })
    state.enemies = [enemy]

    const fixedDeltaMs = 1000 // speed*deltaSec = 1000 >> distance
    runEnemyAISystem(state, fixedDeltaMs)

    // enemy should land at player center, not overshoot
    expect(state.enemies[0].x).toBeCloseTo(100)
    expect(state.enemies[0].y).toBeCloseTo(100)
  })

  it('GIVEN enemy at player center (zero distance) WHEN AI runs THEN enemy does not move (zero-distance no-op)', () => {
    const state = createInitialGameState()
    state.player.x = 200
    state.player.y = 150
    const enemy = makeEnemy({ x: 200, y: 150, speedPxPerSec: 100 })
    state.enemies = [enemy]

    runEnemyAISystem(state, 1000 / 60)

    expect(state.enemies[0].x).toBe(200)
    expect(state.enemies[0].y).toBe(150)
  })

  it('GIVEN enemy within EPSILON of player WHEN AI runs THEN no NaN and enemy does not move (epsilon boundary)', () => {
    const state = createInitialGameState()
    state.player.x = 0
    state.player.y = 0
    // distance = ENEMY_AI_EPSILON_PX exactly — should not move
    const enemy = makeEnemy({ x: ENEMY_AI_EPSILON_PX, y: 0, speedPxPerSec: 100 })
    state.enemies = [enemy]

    runEnemyAISystem(state, 1000)

    expect(state.enemies[0].x).not.toBeNaN()
    expect(state.enemies[0].y).not.toBeNaN()
    expect(state.enemies[0].x).toBe(ENEMY_AI_EPSILON_PX)
    expect(state.enemies[0].y).toBe(0)
  })

  it('GIVEN defeated enemy WHEN AI runs THEN enemy position is unchanged (defeated skip)', () => {
    const state = createInitialGameState()
    state.player.x = 0
    state.player.y = 0
    const enemy = makeEnemy({ x: 100, y: 0, defeated: true, defeatedAtTick: 5 })
    state.enemies = [enemy]

    runEnemyAISystem(state, 1000)

    expect(state.enemies[0].x).toBe(100)
    expect(state.enemies[0].y).toBe(0)
  })

  it('GIVEN multiple enemies WHEN AI runs THEN all non-defeated enemies move deterministically (multi-enemy deterministic update)', () => {
    const state = createInitialGameState()
    state.player.x = 0
    state.player.y = 0

    // enemy A: alive at (60, 0)
    const enemyA = makeEnemy({ id: 1, x: 60, y: 0, speedPxPerSec: 60 })
    // enemy B: defeated at (30, 0) — should not move
    const enemyB = makeEnemy({ id: 2, x: 30, y: 0, speedPxPerSec: 60, defeated: true, defeatedAtTick: 1 })
    // enemy C: alive at (0, 80) — approaching from below
    const enemyC = makeEnemy({ id: 3, x: 0, y: 80, speedPxPerSec: 60 })

    state.enemies = [enemyA, enemyB, enemyC]

    const fixedDeltaMs = 1000 // 1 second
    runEnemyAISystem(state, fixedDeltaMs)

    // enemy A: moves 60px left from x=60 → x=0
    expect(state.enemies[0].x).toBeCloseTo(0)
    expect(state.enemies[0].y).toBeCloseTo(0)

    // enemy B: defeated, unchanged
    expect(state.enemies[1].x).toBe(30)
    expect(state.enemies[1].y).toBe(0)

    // enemy C: moves 60px up from y=80 → y=20
    expect(state.enemies[2].x).toBeCloseTo(0)
    expect(state.enemies[2].y).toBeCloseTo(20)
  })

  it('GIVEN enemy moving toward player WHEN AI runs THEN player state is not modified (AC8 — no side effects)', () => {
    const state = createInitialGameState()
    const originalPlayerX = state.player.x
    const originalPlayerY = state.player.y
    const originalProjectiles = state.projectiles.length

    const enemy = makeEnemy({ x: 400, y: 200, speedPxPerSec: 100 })
    state.enemies = [enemy]

    runEnemyAISystem(state, 1000 / 60)

    expect(state.player.x).toBe(originalPlayerX)
    expect(state.player.y).toBe(originalPlayerY)
    expect(state.projectiles.length).toBe(originalProjectiles)
  })
})

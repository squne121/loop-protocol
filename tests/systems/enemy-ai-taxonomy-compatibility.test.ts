import { describe, expect, it } from 'vitest'

import { createInitialGameState, type EnemyState } from '../../src/state'
import { spawnEnemy } from '../../src/systems/EnemySpawnSystem'
import { ENEMY_AI_EPSILON_PX, runEnemyAISystem } from '../../src/systems/EnemyAISystem'

function makeEnemy(overrides: Partial<EnemyState> = {}): EnemyState {
  return {
    id: 1,
    definitionId: 'enemy-basic',
    hp: 5,
    maxHp: 5,
    x: 0,
    y: 0,
    radius: 12,
    speedPxPerSec: 60,
    contactDamage: 1,
    defeated: false,
    defeatedAtTick: null,
    faction: 'enemy',
    role: 'enemy_chaser',
    behaviorState: 'move_to_engage',
    targetingPolicy: 'focus_player',
    targetEntityId: 'player-alpha',
    ...overrides,
  }
}

describe('enemy taxonomy compatibility gate', () => {
  it('GIVEN one spawned enemy_chaser WHEN AI runs for 1, 2, and 60 ticks THEN legacy chaser golden positions stay unchanged', () => {
    const state = createInitialGameState()
    state.player.x = 240
    state.player.y = 270

    const enemy = spawnEnemy(state)
    expect(enemy).not.toBeNull()
    expect(enemy?.faction).toBe('enemy')
    expect(enemy?.role).toBe('enemy_chaser')
    expect(enemy?.targetingPolicy).toBe('focus_player')
    expect(enemy?.targetEntityId).toBe(state.player.id)

    state.enemies[0].x = 340
    state.enemies[0].y = 270
    state.enemies[0].speedPxPerSec = 60

    const trace: number[] = []
    const fixedDeltaMs = 1000 / 60
    for (let tick = 1; tick <= 60; tick += 1) {
      runEnemyAISystem(state, fixedDeltaMs)
      if (tick === 1 || tick === 2 || tick === 60) {
        trace.push(state.enemies[0].x)
      }
    }

    expect(trace).toEqual([339, 338, 280])
    expect(state.enemies[0].y).toBe(270)
    expect(state.enemies[0].behaviorState).toBe('move_to_engage')
    expect(state.enemies[0].targetEntityId).toBe(state.player.id)
  })

  it('GIVEN multiple enemies including a defeated one WHEN AI runs THEN order, positions, and defeated compatibility stay deterministic', () => {
    const state = createInitialGameState()
    state.player.x = 0
    state.player.y = 0
    state.enemies = [
      makeEnemy({ id: 1, x: 60, y: 0, speedPxPerSec: 60 }),
      makeEnemy({ id: 2, x: 30, y: 0, speedPxPerSec: 60, defeated: true, defeatedAtTick: 4 }),
      makeEnemy({ id: 3, x: 0, y: 80, speedPxPerSec: 60 }),
    ]

    runEnemyAISystem(state, 1000)

    expect(state.enemies.map((enemy) => ({ id: enemy.id, x: enemy.x, y: enemy.y }))).toEqual([
      { id: 1, x: 0, y: 0 },
      { id: 2, x: 30, y: 0 },
      { id: 3, x: 0, y: 20 },
    ])
    expect(state.enemies[0].behaviorState).toBe('move_to_engage')
    expect(state.enemies[1].behaviorState).toBe('destroyed')
    expect(state.enemies[2].targetEntityId).toBe(state.player.id)
  })

  it('GIVEN overshoot and epsilon boundaries WHEN AI runs THEN clamp and zero-distance compatibility remain intact', () => {
    const overshootState = createInitialGameState()
    overshootState.player.x = 100
    overshootState.player.y = 100
    overshootState.enemies = [
      makeEnemy({ x: 100.75, y: 100, speedPxPerSec: 1000 }),
    ]

    runEnemyAISystem(overshootState, 1000)

    expect(overshootState.enemies[0].x).toBe(100)
    expect(overshootState.enemies[0].y).toBe(100)
    expect(overshootState.enemies[0].behaviorState).toBe('move_to_engage')

    const epsilonState = createInitialGameState()
    epsilonState.player.x = 0
    epsilonState.player.y = 0
    epsilonState.enemies = [
      makeEnemy({ x: ENEMY_AI_EPSILON_PX, y: 0, speedPxPerSec: 100 }),
    ]

    runEnemyAISystem(epsilonState, 1000)

    expect(epsilonState.enemies[0].x).toBe(ENEMY_AI_EPSILON_PX)
    expect(epsilonState.enemies[0].y).toBe(0)
    // Compatibility placeholder only: attack means epsilon co-location, not contact-range semantics.
    expect(epsilonState.enemies[0].behaviorState).toBe('attack')
  })

  it('GIVEN invalid fixedDeltaMs WHEN AI runs THEN taxonomy and position updates remain explicit no-op policy', () => {
    const state = createInitialGameState()
    state.player.x = 0
    state.player.y = 0
    state.enemies = [
      makeEnemy({ x: 120, y: 0, speedPxPerSec: 100, targetEntityId: 'enemy:stale', behaviorState: 'attack' }),
    ]

    runEnemyAISystem(state, Number.NaN)

    expect(state.enemies[0].x).toBe(120)
    expect(state.enemies[0].y).toBe(0)
    expect(state.enemies[0].behaviorState).toBe('attack')
    expect(state.enemies[0].targetEntityId).toBe('enemy:stale')
  })

  it('GIVEN invalid player coordinates WHEN AI runs THEN taxonomy normalization also remains no-op', () => {
    const state = createInitialGameState()
    state.player.x = Number.NaN
    state.player.y = 0
    state.enemies = [
      makeEnemy({ x: 120, y: 0, speedPxPerSec: 100, targetEntityId: 'enemy:stale', behaviorState: 'attack' }),
    ]

    runEnemyAISystem(state, 1000)

    expect(state.enemies[0].x).toBe(120)
    expect(state.enemies[0].y).toBe(0)
    expect(state.enemies[0].behaviorState).toBe('attack')
    expect(state.enemies[0].targetEntityId).toBe('enemy:stale')
  })
})

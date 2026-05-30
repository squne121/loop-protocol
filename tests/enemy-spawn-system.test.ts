import { describe, it, expect, beforeEach } from 'vitest'
import { createInitialGameState } from '../src/state/GameState'
import { spawnEnemy, runEnemySpawnSystem } from '../src/systems/EnemySpawnSystem'
import { enemyDefinitions } from '../src/data/enemies'
import type { GameState } from '../src/state/GameState'

describe('EnemySpawnSystem', () => {
  let state: GameState

  beforeEach(() => {
    state = createInitialGameState()
  })

  describe('createInitialGameState', () => {
    it('GIVEN initial state WHEN created THEN enemies is empty array', () => {
      expect(state.enemies).toEqual([])
    })

    it('GIVEN initial state WHEN created THEN nextEnemyId is 1', () => {
      expect(state.nextEnemyId).toBe(1)
    })
  })

  describe('spawnEnemy', () => {
    it('GIVEN empty state WHEN spawnEnemy called THEN one enemy is added', () => {
      spawnEnemy(state)
      expect(state.enemies).toHaveLength(1)
    })

    it('GIVEN empty state WHEN spawnEnemy called THEN enemy id starts at 1', () => {
      spawnEnemy(state)
      expect(state.enemies[0].id).toBe(1)
    })

    it('GIVEN state after first spawn WHEN spawnEnemy called again THEN second enemy id is 2', () => {
      spawnEnemy(state)
      spawnEnemy(state)
      expect(state.enemies[1].id).toBe(2)
    })

    it('GIVEN empty state WHEN spawnEnemy called THEN nextEnemyId increments to 2', () => {
      spawnEnemy(state)
      expect(state.nextEnemyId).toBe(2)
    })

    it('GIVEN empty state WHEN spawnEnemy called THEN hp equals maxHp (definition copy)', () => {
      spawnEnemy(state)
      const enemy = state.enemies[0]
      expect(enemy.hp).toBe(enemy.maxHp)
    })

    it('GIVEN empty state WHEN spawnEnemy called THEN definition values are copied', () => {
      const definition = enemyDefinitions.find((d) => d.definitionId === 'enemy-basic')
      expect(definition).toBeDefined()
      spawnEnemy(state)
      const enemy = state.enemies[0]
      expect(enemy.definitionId).toBe(definition!.definitionId)
      expect(enemy.maxHp).toBe(definition!.maxHp)
      expect(enemy.radius).toBe(definition!.radius)
      expect(enemy.speedPxPerSec).toBe(definition!.speedPxPerSec)
      expect(enemy.contactDamage).toBe(definition!.contactDamage)
    })

    it('GIVEN empty state WHEN spawnEnemy called THEN defeated is false', () => {
      spawnEnemy(state)
      expect(state.enemies[0].defeated).toBe(false)
    })

    it('GIVEN empty state WHEN spawnEnemy called THEN defeatedAtTick is null', () => {
      spawnEnemy(state)
      expect(state.enemies[0].defeatedAtTick).toBeNull()
    })

    it('GIVEN request with custom position WHEN spawnEnemy called THEN enemy uses custom x/y', () => {
      spawnEnemy(state, { x: 100, y: 200 })
      expect(state.enemies[0].x).toBe(100)
      expect(state.enemies[0].y).toBe(200)
    })

    it('GIVEN no position in request WHEN spawnEnemy called THEN enemy spawns at default arena position', () => {
      const definition = enemyDefinitions.find((d) => d.definitionId === 'enemy-basic')!
      const expectedX = state.arena.width - definition.radius - 48
      const expectedY = state.arena.height / 2
      spawnEnemy(state)
      expect(state.enemies[0].x).toBe(expectedX)
      expect(state.enemies[0].y).toBe(expectedY)
    })

    it('GIVEN unknown definitionId WHEN spawnEnemy called THEN returns null and no enemy is added', () => {
      const result = spawnEnemy(state, { definitionId: 'nonexistent' })
      expect(result).toBeNull()
      expect(state.enemies).toHaveLength(0)
      expect(state.nextEnemyId).toBe(1)
    })

    it('GIVEN valid request WHEN spawnEnemy called THEN returns the spawned EnemyState', () => {
      const result = spawnEnemy(state)
      expect(result).not.toBeNull()
      expect(result).toBe(state.enemies[0])
    })
  })

  describe('runEnemySpawnSystem', () => {
    it('GIVEN no enemies WHEN runEnemySpawnSystem called THEN one enemy is spawned', () => {
      runEnemySpawnSystem(state)
      expect(state.enemies).toHaveLength(1)
    })

    it('GIVEN enemies already present WHEN runEnemySpawnSystem called THEN no new enemy is added', () => {
      spawnEnemy(state)
      const countBefore = state.enemies.length
      runEnemySpawnSystem(state)
      expect(state.enemies).toHaveLength(countBefore)
    })

    it('GIVEN multiple calls WHEN runEnemySpawnSystem called twice THEN only one enemy total', () => {
      runEnemySpawnSystem(state)
      runEnemySpawnSystem(state)
      expect(state.enemies).toHaveLength(1)
    })

    it('GIVEN no enemies WHEN runEnemySpawnSystem called THEN spawned enemy has id 1', () => {
      runEnemySpawnSystem(state)
      expect(state.enemies[0].id).toBe(1)
    })
  })

  describe('enemy-basic definition constraints', () => {
    it('GIVEN enemy-basic definition THEN all numeric fields are positive', () => {
      const definition = enemyDefinitions.find((d) => d.definitionId === 'enemy-basic')
      expect(definition).toBeDefined()
      expect(definition!.maxHp).toBeGreaterThan(0)
      expect(definition!.radius).toBeGreaterThan(0)
      expect(definition!.speedPxPerSec).toBeGreaterThan(0)
      expect(definition!.contactDamage).toBeGreaterThan(0)
    })
  })

  describe('determinism', () => {
    it('GIVEN two fresh states WHEN runEnemySpawnSystem called on each THEN enemy positions are identical', () => {
      const stateA = createInitialGameState()
      const stateB = createInitialGameState()
      runEnemySpawnSystem(stateA)
      runEnemySpawnSystem(stateB)
      expect(stateA.enemies[0].x).toBe(stateB.enemies[0].x)
      expect(stateA.enemies[0].y).toBe(stateB.enemies[0].y)
    })
  })
})

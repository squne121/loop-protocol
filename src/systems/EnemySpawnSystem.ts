import type { GameState, EnemyState } from '../state/GameState'
import { enemyDefinitions } from '../data/enemies'

export interface SpawnEnemyRequest {
  definitionId?: string
  x?: number
  y?: number
}

export function spawnEnemy(
  state: GameState,
  request: SpawnEnemyRequest = {},
): EnemyState | null {
  const definitionId = request.definitionId ?? 'enemy-basic'
  const definition = enemyDefinitions.find((d) => d.definitionId === definitionId)
  if (definition === undefined) {
    return null
  }

  const x = request.x ?? state.arena.width - definition.radius - 48
  const y = request.y ?? state.arena.height / 2

  const enemy: EnemyState = {
    id: state.nextEnemyId,
    definitionId: definition.definitionId,
    hp: definition.maxHp,
    maxHp: definition.maxHp,
    x,
    y,
    radius: definition.radius,
    speedPxPerSec: definition.speedPxPerSec,
    contactDamage: definition.contactDamage,
    defeated: false,
    defeatedAtTick: null,
  }

  state.enemies.push(enemy)
  state.nextEnemyId += 1

  return enemy
}

export function runEnemySpawnSystem(state: GameState): void {
  if (state.enemies.length > 0) {
    return
  }
  spawnEnemy(state)
}

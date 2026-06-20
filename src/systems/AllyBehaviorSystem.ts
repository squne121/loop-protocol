import type { AllyState, GameState, TargetEntityId } from '../state'
import { selectTarget } from './TargetingSystem'
import {
  allyTargetEntityId,
  enemyTargetEntityId,
  playerTargetEntityId,
} from './TargetingSystem'

const TARGET_REACHED_EPSILON_PX = 0.5
const NEAR_PLAYER_RADIUS_PX = 60

function findLivingEnemyByTargetEntityId(
  state: GameState,
  targetEntityId: TargetEntityId,
) {
  return state.enemies.find(
    (enemy) => !enemy.defeated && enemyTargetEntityId(enemy) === targetEntityId,
  )
}

function moveAllyTowardEnemy(
  ally: AllyState,
  targetX: number,
  targetY: number,
  targetRadius: number,
  fixedDeltaMs: number,
): void {
  if (!Number.isFinite(fixedDeltaMs) || fixedDeltaMs <= 0) {
    return
  }

  const dx = targetX - ally.x
  const dy = targetY - ally.y
  const distance = Math.hypot(dx, dy)
  const desiredDistance = ally.radius + targetRadius

  if (!Number.isFinite(distance) || distance <= Math.max(TARGET_REACHED_EPSILON_PX, desiredDistance)) {
    ally.behaviorState = 'attack'
    return
  }

  const moveDistance = Math.min(
    ally.speedPxPerSec * (fixedDeltaMs / 1000),
    distance - desiredDistance,
  )

  if (!Number.isFinite(moveDistance) || moveDistance <= 0) {
    ally.behaviorState = 'move_to_engage'
    return
  }

  ally.x += (dx / distance) * moveDistance
  ally.y += (dy / distance) * moveDistance
  ally.behaviorState = 'move_to_engage'
}

export function runAllyBehaviorSystem(state: GameState, fixedDeltaMs: number): void {
  const commandIntentActive = state.commandIntentRuntime.activeIntent !== 'none'
  const candidates = state.enemies.map((enemy) => ({
    targetEntityId: enemyTargetEntityId(enemy),
    faction: 'enemy' as const,
    x: enemy.x,
    y: enemy.y,
    defeated: enemy.defeated,
    destroyed: false,
    isPlayer: false,
  }))

  for (const ally of state.allies) {
    ally.targetingPolicy =
      commandIntentActive && state.commandIntentRuntime.activeIntent === 'assist_player'
        ? 'assist_player_threat'
        : 'nearest_hostile'

    const selection = selectTarget({
      actor: {
        targetEntityId: allyTargetEntityId(ally),
        faction: ally.faction,
        x: ally.x,
        y: ally.y,
        targetingPolicy: ally.targetingPolicy,
      },
      candidates,
      player: {
        targetEntityId: playerTargetEntityId(state.player),
        x: state.player.x,
        y: state.player.y,
      },
      arena: state.arena,
      commandIntent: state.commandIntentRuntime.activeIntent,
      commandIntentActive,
      previousTargetId: ally.targetEntityId,
      threatMode: 'binary_hostile_near_player',
      nearPlayerRadiusPx: NEAR_PLAYER_RADIUS_PX,
    })

    if (selection.clearedStaleTargetId !== null) {
      ally.targetEntityId = null
    }

    if (selection.selectedTargetId === null) {
      ally.targetEntityId = null
      ally.behaviorState = 'inactive'
      continue
    }

    ally.targetEntityId = selection.selectedTargetId

    const targetEnemy = findLivingEnemyByTargetEntityId(state, selection.selectedTargetId)
    if (!targetEnemy) {
      ally.targetEntityId = null
      ally.behaviorState = 'inactive'
      continue
    }

    moveAllyTowardEnemy(ally, targetEnemy.x, targetEnemy.y, targetEnemy.radius, fixedDeltaMs)
  }
}

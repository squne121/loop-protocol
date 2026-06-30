import type { GameState } from '../state'
import { drawEnemyHpLabel } from './renderUtils'

export interface CanvasRenderer {
  render(state: GameState): void
}

/** Fixed length of the aim indicator line in logical pixels (AC2). */
export const AIM_INDICATOR_LENGTH_PX = 60

/**
 * Threshold below which the pointer-to-player distance is considered "at player".
 * When dist <= AIM_EPSILON_PX, we fall back to lastAimDirection to avoid zero-length vectors.
 */
export const AIM_EPSILON_PX = 1.0

/**
 * Pure helper: compute normalised aim direction vector.
 * Priority: aimX/aimY (current frame) → lastAimDirectionX/Y (fallback) → right (default).
 *
 * Exported for unit testing only; CanvasRenderer is the sole production caller.
 */
export function computeAimDirection(params: {
  playerX: number
  playerY: number
  aimX: number
  aimY: number
  lastAimDirectionX: number
  lastAimDirectionY: number
}): { dirX: number; dirY: number } {
  const dx = params.aimX - params.playerX
  const dy = params.aimY - params.playerY
  const dist = Math.hypot(dx, dy)

  if (dist > AIM_EPSILON_PX) {
    return { dirX: dx / dist, dirY: dy / dist }
  }
  if (params.lastAimDirectionX !== 0 || params.lastAimDirectionY !== 0) {
    return { dirX: params.lastAimDirectionX, dirY: params.lastAimDirectionY }
  }
  return { dirX: 1, dirY: 0 }
}

export interface AssistCueSegment {
  allyX: number
  allyY: number
  allyRadius: number
  targetX: number
  targetY: number
  targetRadius: number
}

export function resolveActiveAssistCueSegments(
  state: Pick<GameState, 'allies' | 'enemies' | 'commandIntentRuntime'>,
): AssistCueSegment[] {
  if (state.commandIntentRuntime.activeIntent !== 'assist_player') {
    return []
  }

  return state.allies.flatMap((ally) => {
    if (ally.targetEntityId === null) {
      return []
    }

    const targetEnemy = state.enemies.find(
      (enemy) =>
        !enemy.defeated && `enemy:${enemy.id}` === ally.targetEntityId,
    )

    if (!targetEnemy) {
      return []
    }

    return [{
      allyX: ally.x,
      allyY: ally.y,
      allyRadius: ally.radius,
      targetX: targetEnemy.x,
      targetY: targetEnemy.y,
      targetRadius: targetEnemy.radius,
    }]
  })
}

export function createCanvasRenderer(canvas: HTMLCanvasElement): CanvasRenderer {
  const context = canvas.getContext('2d')

  if (!context) {
    throw new Error('2D canvas context is not available.')
  }

  let lastArenaWidth = 0
  let lastArenaHeight = 0
  let lastDpr = 0

  return {
    render(state) {
      const dpr = window.devicePixelRatio ?? 1
      const arenaW = state.arena.width
      const arenaH = state.arena.height

      // Resize backing store when arena or dpr changes
      if (arenaW !== lastArenaWidth || arenaH !== lastArenaHeight || dpr !== lastDpr) {
        canvas.width = Math.round(arenaW * dpr)
        canvas.height = Math.round(arenaH * dpr)
        canvas.style.width = `${arenaW}px`
        canvas.style.height = `${arenaH}px`
        lastArenaWidth = arenaW
        lastArenaHeight = arenaH
        lastDpr = dpr
        context.setTransform(dpr, 0, 0, dpr, 0, 0)
      }

      // --- Layer 1: background ---
      context.fillStyle = '#07111f'
      context.fillRect(0, 0, arenaW, arenaH)

      // --- Layer 2: grid ---
      context.strokeStyle = 'rgba(92, 219, 190, 0.08)'
      context.lineWidth = 1
      for (let x = 0; x <= arenaW; x += 40) {
        context.beginPath()
        context.moveTo(x, 0)
        context.lineTo(x, arenaH)
        context.stroke()
      }

      for (let y = 0; y <= arenaH; y += 40) {
        context.beginPath()
        context.moveTo(0, y)
        context.lineTo(arenaW, y)
        context.stroke()
      }

      // --- Layer 3: aim indicator (above background/grid, below actors/projectiles) ---
      // AC2: Fixed length via AIM_INDICATOR_LENGTH_PX constant.
      // AC5: Isolated with save()/restore() to avoid leaking canvas state.
      // Visual-only: does not interact with collision or combat systems (AC3).
      {
        // Derive direction from aimX/aimY every frame (AC1: hover always updates aim).
        // lastAimDirectionX/Y is used only as fallback when pointer is too close to player.
        const { dirX, dirY } = computeAimDirection({
          playerX: state.player.x,
          playerY: state.player.y,
          aimX: state.player.aimX,
          aimY: state.player.aimY,
          lastAimDirectionX: state.player.lastAimDirectionX,
          lastAimDirectionY: state.player.lastAimDirectionY,
        })

        context.save()
        context.strokeStyle = '#f4c25b'
        context.lineWidth = 3
        context.beginPath()
        context.moveTo(state.player.x, state.player.y)
        context.lineTo(
          state.player.x + dirX * AIM_INDICATOR_LENGTH_PX,
          state.player.y + dirY * AIM_INDICATOR_LENGTH_PX,
        )
        context.stroke()
        context.restore()
      }

      // --- Layer 4: player ---
      context.fillStyle = '#70f0d0'
      context.beginPath()
      context.arc(
        state.player.x,
        state.player.y,
        state.player.radius,
        0,
        Math.PI * 2,
      )
      context.fill()

      const showAssistWorldCues = state.sortie.result === null

      // --- Layer 4b: ally markers ---
      if (showAssistWorldCues) {
        context.save()
        context.fillStyle = '#64d7ff'
        context.strokeStyle = 'rgba(100, 215, 255, 0.85)'
        context.lineWidth = 2
        for (const ally of state.allies) {
          context.beginPath()
          context.arc(ally.x, ally.y, ally.radius, 0, Math.PI * 2)
          context.fill()
          context.beginPath()
          context.arc(ally.x, ally.y, ally.radius + 5, 0, Math.PI * 2)
          context.stroke()
        }
        context.restore()
      }

      // --- Layer 5: enemies (defeated === false only) ---
      context.fillStyle = '#f05050'
      for (const enemy of state.enemies) {
        if (enemy.defeated) continue
        context.beginPath()
        context.arc(enemy.x, enemy.y, enemy.radius, 0, Math.PI * 2)
        context.fill()
      }

      // --- Layer 5b: enemy HP labels (above enemy circles, below projectiles) ---
      for (const enemy of state.enemies) {
        if (enemy.defeated) continue
        drawEnemyHpLabel({
          ctx: context,
          enemyX: enemy.x,
          enemyY: enemy.y,
          enemyRadius: enemy.radius,
          enemyHp: enemy.hp,
          arenaWidth: arenaW,
          arenaHeight: arenaH,
        })
      }

      // --- Layer 5c: active assist cue (non-authoritative only) ---
      const assistCueSegments = showAssistWorldCues
        ? resolveActiveAssistCueSegments(state)
        : []
      if (assistCueSegments.length > 0) {
        context.save()
        context.strokeStyle = 'rgba(100, 215, 255, 0.55)'
        context.fillStyle = 'rgba(100, 215, 255, 0.18)'
        context.setLineDash([8, 6])
        context.lineWidth = 2
        for (const segment of assistCueSegments) {
          context.beginPath()
          context.moveTo(segment.allyX, segment.allyY)
          context.lineTo(segment.targetX, segment.targetY)
          context.stroke()

          context.beginPath()
          context.arc(
            segment.targetX,
            segment.targetY,
            segment.targetRadius + 8,
            0,
            Math.PI * 2,
          )
          context.fill()
        }
        context.restore()
        context.setLineDash([])
      }

      // --- Layer 6: projectiles ---
      context.fillStyle = '#f4c25b'
      for (const projectile of state.projectiles) {
        context.beginPath()
        context.arc(projectile.x, projectile.y, projectile.radius, 0, Math.PI * 2)
        context.fill()
      }

    },
  }
}

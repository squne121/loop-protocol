import type { GameState } from '../state'

export interface CanvasRenderer {
  render(state: GameState): void
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

      context.fillStyle = '#07111f'
      context.fillRect(0, 0, arenaW, arenaH)

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

      // Draw projectiles
      context.fillStyle = '#f4c25b'
      for (const projectile of state.projectiles) {
        context.beginPath()
        context.arc(projectile.x, projectile.y, projectile.radius, 0, Math.PI * 2)
        context.fill()
      }

      // Draw player
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

      // Draw aim line
      context.strokeStyle = '#f4c25b'
      context.lineWidth = 3
      context.beginPath()
      context.moveTo(state.player.x, state.player.y)
      context.lineTo(state.player.aimX, state.player.aimY)
      context.stroke()

      context.fillStyle = '#dce8f8'
      context.font = '14px "IBM Plex Mono", monospace'
      context.fillText(
        `${state.progress.stageLabel}  T+${Math.floor(state.elapsedMs / 1000)}s`,
        18,
        28,
      )

      // Draw enemies (defeated === false only; AC7)
      context.fillStyle = '#f05050'
      for (const enemy of state.enemies) {
        if (enemy.defeated) continue
        context.beginPath()
        context.arc(enemy.x, enemy.y, enemy.radius, 0, Math.PI * 2)
        context.fill()
      }

      // Victory / Defeat overlay (AC8, AC9)
      if (state.sortie.result !== null) {
        const outcome = state.sortie.result.outcome
        const isVictory = outcome === 'victory'

        // Semi-transparent overlay
        context.fillStyle = isVictory
          ? 'rgba(30, 200, 130, 0.55)'
          : 'rgba(220, 60, 60, 0.55)'
        context.fillRect(0, 0, arenaW, arenaH)

        // Outcome label
        context.fillStyle = '#ffffff'
        context.font = 'bold 56px "IBM Plex Mono", monospace'
        context.textAlign = 'center'
        context.fillText(
          isVictory ? 'VICTORY' : 'DEFEAT',
          arenaW / 2,
          arenaH / 2 - 20,
        )

        // Duration and kills sub-label (AC11: use result.durationMs for terminal state)
        const durationSec = (state.sortie.result.durationMs / 1000).toFixed(1)
        context.font = '22px "IBM Plex Mono", monospace'
        context.fillText(
          `Duration: ${durationSec}s  Kills: ${state.sortie.result.kills}`,
          arenaW / 2,
          arenaH / 2 + 36,
        )

        // Reset text align for subsequent renders
        context.textAlign = 'left'
      }
    },
  }
}

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
        context.scale(dpr, dpr)
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
    },
  }
}

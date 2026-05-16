import type { GameState } from '../state'

export interface CanvasRenderer {
  render(state: GameState): void
}

export function createCanvasRenderer(canvas: HTMLCanvasElement): CanvasRenderer {
  const context = canvas.getContext('2d')

  if (!context) {
    throw new Error('2D canvas context is not available.')
  }

  return {
    render(state) {
      if (
        canvas.width !== state.arena.width ||
        canvas.height !== state.arena.height
      ) {
        canvas.width = state.arena.width
        canvas.height = state.arena.height
      }

      context.fillStyle = '#07111f'
      context.fillRect(0, 0, canvas.width, canvas.height)

      context.strokeStyle = 'rgba(92, 219, 190, 0.08)'
      context.lineWidth = 1
      for (let x = 0; x <= canvas.width; x += 40) {
        context.beginPath()
        context.moveTo(x, 0)
        context.lineTo(x, canvas.height)
        context.stroke()
      }

      for (let y = 0; y <= canvas.height; y += 40) {
        context.beginPath()
        context.moveTo(0, y)
        context.lineTo(canvas.width, y)
        context.stroke()
      }

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

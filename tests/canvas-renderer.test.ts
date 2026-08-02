/**
 * @vitest-environment jsdom
 */

import { describe, expect, it, vi } from 'vitest'

import { createCanvasRenderer } from '../src/render/CanvasRenderer'
import { createInitialGameState } from '../src/state'

function makeCanvasContextSpy() {
  return {
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    font: '',
    textAlign: 'left' as CanvasTextAlign,
    textBaseline: 'alphabetic' as CanvasTextBaseline,
    save: vi.fn(),
    restore: vi.fn(),
    setTransform: vi.fn(),
    fillRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    arc: vi.fn(),
    measureText: vi.fn(() => ({ width: 24 })),
    fillText: vi.fn(),
    setLineDash: vi.fn(),
  } satisfies Partial<CanvasRenderingContext2D>
}

describe('CanvasRenderer responsive presentation', () => {
  it('GIVEN a fixed logical arena WHEN CSS display size and DPR change THEN backing store follows them without mutating logical dimensions', () => {
    const context = makeCanvasContextSpy()
    const canvas = document.createElement('canvas')
    vi.spyOn(canvas, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    let display = { width: 800, height: 450 }
    vi.spyOn(canvas, 'getBoundingClientRect').mockImplementation(() => ({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: display.width,
      bottom: display.height,
      ...display,
      toJSON: () => ({}),
    }))
    Object.defineProperty(window, 'devicePixelRatio', { value: 1, configurable: true })

    const state = createInitialGameState()
    const renderer = createCanvasRenderer(canvas)
    renderer.render(state)

    expect(canvas.width).toBe(800)
    expect(canvas.height).toBe(450)
    expect(canvas.style.width).toBe('')
    expect(canvas.style.height).toBe('')
    expect(context.setTransform).toHaveBeenLastCalledWith(800 / 960, 0, 0, 450 / 540, 0, 0)

    display = { width: 1200, height: 675 }
    Object.defineProperty(window, 'devicePixelRatio', { value: 2, configurable: true })
    renderer.resize(state)

    expect(canvas.width).toBe(2400)
    expect(canvas.height).toBe(1350)
    expect(context.setTransform).toHaveBeenLastCalledWith(2400 / 960, 0, 0, 1350 / 540, 0, 0)
    expect(state.arena).toEqual({ width: 960, height: 540 })
  })

  it('GIVEN a terminal sortie WHEN Canvas renders THEN live enemy HP text is omitted beneath the result overlay', () => {
    const context = makeCanvasContextSpy()
    const canvas = document.createElement('canvas')
    vi.spyOn(canvas, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: 960,
      bottom: 540,
      width: 960,
      height: 540,
      toJSON: () => ({}),
    })

    const state = createInitialGameState()
    state.enemies = [{
      id: 1,
      definitionId: 'enemy-basic',
      hp: 3,
      maxHp: 3,
      x: 800,
      y: 270,
      radius: 16,
      speedPxPerSec: 60,
      contactDamage: 1,
      defeated: false,
      defeatedAtTick: null,
      faction: 'enemy',
      role: 'enemy_chaser',
      behaviorState: 'move_to_engage',
      targetingPolicy: 'focus_player',
      targetEntityId: 'player:player-alpha',
    }]
    state.sortie = {
      status: 'timeout',
      elapsedTicks: 30,
      targetTicks: 30,
      result: {
        outcome: 'timeout',
        endReason: 'timeout',
        durationMs: 500,
        kills: 0,
        shotsFired: 0,
        playerHpRemaining: state.player.hp,
      },
    }

    createCanvasRenderer(canvas).render(state)

    expect(context.fillText).not.toHaveBeenCalled()
  })
})

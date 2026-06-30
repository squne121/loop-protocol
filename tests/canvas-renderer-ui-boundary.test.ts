/**
 * @vitest-environment jsdom
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createCanvasRenderer } from '../src/render/CanvasRenderer'
import { createInitialGameState } from '../src/state'

function makeCanvasContextSpy() {
  return {
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    font: '',
    textAlign: 'left' as CanvasTextAlign,
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
    textBaseline: 'alphabetic' as CanvasTextBaseline,
  } satisfies Partial<CanvasRenderingContext2D>
}

describe('CanvasRenderer UI boundary', () => {
  let originalDevicePixelRatio: number | undefined

  beforeEach(() => {
    originalDevicePixelRatio = window.devicePixelRatio
  })

  it('GIVEN terminal result state WHEN renderer draws THEN primary HUD text boundary is preserved', () => {
    const context = makeCanvasContextSpy()
    const canvas = document.createElement('canvas')
    vi.spyOn(canvas, 'getContext').mockReturnValue(
      context as unknown as CanvasRenderingContext2D,
    )
    Object.defineProperty(window, 'devicePixelRatio', {
      value: 1,
      configurable: true,
    })

    const renderer = createCanvasRenderer(canvas)
    const state = createInitialGameState()
    state.sortie.status = 'victory'
    state.sortie.result = {
      outcome: 'victory',
      endReason: 'all_enemies_defeated',
      durationMs: 12_000,
      kills: 4,
      shotsFired: 16,
      playerHpRemaining: 6,
    }

    renderer.render(state)

    const texts = context.fillText.mock.calls.map((call) => String(call[0]))
    expect(texts).toHaveLength(0)

    if (originalDevicePixelRatio !== undefined) {
      Object.defineProperty(window, 'devicePixelRatio', {
        value: originalDevicePixelRatio,
        configurable: true,
      })
    }
  })
})

/**
 * @vitest-environment jsdom
 */

import { describe, expect, it, vi } from 'vitest'

import { createCanvasRenderer, type CanvasPresentation } from '../src/render/CanvasRenderer'
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

  it('GIVEN an observed CanvasPresentation with fractional CSS size WHEN resize is called THEN the backing store uses the presentation authority directly (no getBoundingClientRect read)', () => {
    const context = makeCanvasContextSpy()
    const canvas = document.createElement('canvas')
    vi.spyOn(canvas, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    const boundsSpy = vi.spyOn(canvas, 'getBoundingClientRect')

    const state = createInitialGameState()
    const renderer = createCanvasRenderer(canvas)

    const presentation: CanvasPresentation = {
      cssWidth: 853.33,
      cssHeight: 480.1875,
      deviceWidth: 853.33 * 1.25,
      deviceHeight: 480.1875 * 1.25,
    }
    renderer.resize(state, presentation)

    expect(canvas.width).toBe(Math.round(presentation.deviceWidth))
    expect(canvas.height).toBe(Math.round(presentation.deviceHeight))
    expect(context.setTransform).toHaveBeenLastCalledWith(
      canvas.width / 960,
      0,
      0,
      canvas.height / 540,
      0,
      0,
    )
    // The observed-entry path must never fall back to a layout read.
    expect(boundsSpy).not.toHaveBeenCalled()
  })

  it.each([1, 1.25, 2, 0.667])(
    'GIVEN DPR %s WHEN a CanvasPresentation is resolved from device pixels THEN the backing store matches exactly',
    (dpr) => {
      const context = makeCanvasContextSpy()
      const canvas = document.createElement('canvas')
      vi.spyOn(canvas, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)

      const state = createInitialGameState()
      const renderer = createCanvasRenderer(canvas)

      const cssWidth = 700
      const cssHeight = 393.75
      renderer.resize(state, {
        cssWidth,
        cssHeight,
        deviceWidth: cssWidth * dpr,
        deviceHeight: cssHeight * dpr,
      })

      expect(canvas.width).toBe(Math.max(1, Math.round(cssWidth * dpr)))
      expect(canvas.height).toBe(Math.max(1, Math.round(cssHeight * dpr)))
    },
  )

  it('GIVEN render() is called repeatedly with unchanged CanvasPresentation THEN canvas.width/height and setTransform are not re-applied', () => {
    const context = makeCanvasContextSpy()
    const canvas = document.createElement('canvas')
    vi.spyOn(canvas, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    const boundsSpy = vi.spyOn(canvas, 'getBoundingClientRect')

    const state = createInitialGameState()
    const renderer = createCanvasRenderer(canvas)

    renderer.resize(state, { cssWidth: 960, cssHeight: 540, deviceWidth: 960, deviceHeight: 540 })
    expect(context.setTransform).toHaveBeenCalledTimes(1)

    renderer.render(state)
    renderer.render(state)
    renderer.render(state)

    // render() must reuse the cached backing-store metrics from resize();
    // it must not re-assign canvas.width/height or call setTransform again,
    // and it must never read layout via getBoundingClientRect() itself.
    expect(context.setTransform).toHaveBeenCalledTimes(1)
    expect(boundsSpy).not.toHaveBeenCalled()
  })

  it('GIVEN no resize() has been called yet WHEN render() runs THEN it falls back to getBoundingClientRect()+devicePixelRatio exactly once, and never again on subsequent renders', () => {
    const context = makeCanvasContextSpy()
    const canvas = document.createElement('canvas')
    vi.spyOn(canvas, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    const boundsSpy = vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: 480,
      bottom: 270,
      width: 480,
      height: 270,
      toJSON: () => ({}),
    })
    Object.defineProperty(window, 'devicePixelRatio', { value: 1, configurable: true })

    const state = createInitialGameState()
    const renderer = createCanvasRenderer(canvas)

    renderer.render(state)
    expect(boundsSpy).toHaveBeenCalledTimes(1)
    expect(canvas.width).toBe(480)
    expect(canvas.height).toBe(270)

    renderer.render(state)
    renderer.render(state)
    // No further getBoundingClientRect() reads once the first frame has
    // cached its metrics (Issue #1956 fix 3: render() hot path must not
    // read layout every frame).
    expect(boundsSpy).toHaveBeenCalledTimes(1)
  })

    it('GIVEN a terminal sortie WHEN Canvas renders THEN enemy HP text is still drawn (Issue #1956 fix 5: result-overlay HP suppression reverted, out of scope for #1956/owned by result overlay behavior itself)', () => {
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

    expect(context.fillText).toHaveBeenCalled()
  })
})

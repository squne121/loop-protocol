/**
 * @vitest-environment jsdom
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createCanvasRenderer,
  resolveActiveAssistCueSegments,
} from '../src/render/CanvasRenderer'
import { createDefaultAllyState, createInitialGameState } from '../src/state'

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

describe('resolveActiveAssistCueSegments', () => {
  it('GIVEN assist_player is inactive WHEN cue segments are resolved THEN no segments are returned', () => {
    const state = createInitialGameState()
    const ally = createDefaultAllyState(1)
    ally.targetEntityId = 'enemy:1'
    state.allies = [ally]
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 420,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
        faction: 'enemy',
        role: 'enemy_chaser',
        behaviorState: 'move_to_engage',
        targetingPolicy: 'focus_player',
        targetEntityId: 'player:player-alpha',
      },
    ]

    expect(resolveActiveAssistCueSegments(state)).toEqual([])
  })

  it('GIVEN assist_player is active and ally tracks a living enemy WHEN cue segments are resolved THEN ally-to-target cue is returned', () => {
    const state = createInitialGameState()
    const ally = createDefaultAllyState(1)
    ally.targetEntityId = 'enemy:1'
    ally.x = 160
    ally.y = 270
    state.allies = [ally]
    state.commandIntentRuntime.activeIntent = 'assist_player'
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 420,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
        faction: 'enemy',
        role: 'enemy_chaser',
        behaviorState: 'move_to_engage',
        targetingPolicy: 'focus_player',
        targetEntityId: 'player:player-alpha',
      },
    ]

    expect(resolveActiveAssistCueSegments(state)).toEqual([
      {
        allyX: 160,
        allyY: 270,
        allyRadius: ally.radius,
        targetX: 420,
        targetY: 270,
        targetRadius: 12,
      },
    ])
  })

  it('GIVEN assist_player is active but selected target is absent WHEN cue segments are resolved THEN no segments are returned', () => {
    const state = createInitialGameState()
    const ally = createDefaultAllyState(1)
    ally.targetEntityId = 'enemy:99'
    state.allies = [ally]
    state.commandIntentRuntime.activeIntent = 'assist_player'

    expect(resolveActiveAssistCueSegments(state)).toEqual([])
  })
})

describe('createCanvasRenderer assist rendering', () => {
  let originalDevicePixelRatio: number | undefined

  beforeEach(() => {
    originalDevicePixelRatio = window.devicePixelRatio
  })

  it('GIVEN active assist and ally marker WHEN renderer draws THEN it emits ally marker arcs and assist cue line', () => {
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
    const ally = createDefaultAllyState(1)
    ally.targetEntityId = 'enemy:1'
    state.allies = [ally]
    state.commandIntentRuntime.activeIntent = 'assist_player'
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 420,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
        faction: 'enemy',
        role: 'enemy_chaser',
        behaviorState: 'move_to_engage',
        targetingPolicy: 'focus_player',
        targetEntityId: 'player:player-alpha',
      },
    ]

    renderer.render(state)

    expect(context.arc).toHaveBeenCalled()
    expect(context.moveTo).toHaveBeenCalledWith(ally.x, ally.y)
    expect(context.lineTo).toHaveBeenCalledWith(420, 270)
    expect(context.setLineDash).toHaveBeenCalledWith([8, 6])
  })

  it('GIVEN assist renderer output WHEN checking normal UI THEN does not render aggregated player HP', () => {
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

    renderer.render(state)

    const renderedTexts = context.fillText.mock.calls.map(([text]) => String(text))
    expect(renderedTexts.some((text) => text.includes('8/8'))).toBe(false)
    expect(renderedTexts.some((text) => text.includes('HULL'))).toBe(false)
    if (originalDevicePixelRatio !== undefined) {
      Object.defineProperty(window, 'devicePixelRatio', {
        value: originalDevicePixelRatio,
        configurable: true,
      })
    }
  })
})

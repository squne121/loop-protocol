import { describe, expect, it, vi } from 'vitest'

import { mapInputToCommands, bindInput, createInputState } from '../src/input'
import { computeAimDirection } from '../src/render/CanvasRenderer'
import type { CanvasLike, KeyEventTarget } from '../src/input/InputBindings'

describe('mapInputToCommands', () => {
  it('emits movement, aim, and fire commands from normalized input state', () => {
    const commands = mapInputToCommands({
      moveUp: true,
      moveDown: false,
      moveLeft: false,
      moveRight: true,
      pointerX: 120,
      pointerY: 340,
      primaryPressed: true,
      activePointerId: null,
      pointerKnown: true,
    })

    expect(commands).toEqual([
      { type: 'move', axisX: 1, axisY: -1 },
      { type: 'aim', x: 120, y: 340 },
      { type: 'fire' },
    ])
  })

  it('GIVEN no movement keys pressed WHEN mapInputToCommands THEN no move command emitted', () => {
    const commands = mapInputToCommands({
      moveUp: false,
      moveDown: false,
      moveLeft: false,
      moveRight: false,
      pointerX: 0,
      pointerY: 0,
      primaryPressed: false,
      activePointerId: null,
      pointerKnown: true,
    })

    expect(commands.some((c) => c.type === 'move')).toBe(false)
  })

  it('GIVEN pointerKnown=false WHEN mapInputToCommands THEN no aim command emitted (AC3)', () => {
    const commands = mapInputToCommands({
      moveUp: false,
      moveDown: false,
      moveLeft: false,
      moveRight: false,
      pointerX: 120,
      pointerY: 340,
      primaryPressed: false,
      activePointerId: null,
      pointerKnown: false,
    })

    expect(commands.some((c) => c.type === 'aim')).toBe(false)
  })

  it('GIVEN pointerKnown=true WHEN mapInputToCommands THEN aim command emitted', () => {
    const commands = mapInputToCommands({
      moveUp: false,
      moveDown: false,
      moveLeft: false,
      moveRight: false,
      pointerX: 200,
      pointerY: 100,
      primaryPressed: false,
      activePointerId: null,
      pointerKnown: true,
    })

    expect(commands).toContainEqual({ type: 'aim', x: 200, y: 100 })
  })
})

// Test stubs

function makeFakeKeyTarget(): KeyEventTarget & {
  emit(type: 'keydown' | 'keyup', code: string): void
} {
  const listeners = new Map<string, Array<(event: KeyboardEvent) => void>>()

  return {
    addEventListener(type: 'keydown' | 'keyup', handler: (e: KeyboardEvent) => void) {
      if (!listeners.has(type)) listeners.set(type, [])
      listeners.get(type)!.push(handler)
    },
    emit(type: 'keydown' | 'keyup', code: string) {
      for (const h of listeners.get(type) ?? []) {
        h({ code } as KeyboardEvent)
      }
    },
  }
}

function makeFakeCanvas(): CanvasLike & {
  dispatchPointer(type: string, overrides?: Partial<{
    pointerId: number
    isPrimary: boolean
    button: number
    clientX: number
    clientY: number
  }>): void
  setPointerCaptureSpy: ReturnType<typeof vi.fn>
} {
  const listeners = new Map<string, EventListener[]>()
  const setPointerCaptureSpy = vi.fn()

  return {
    getBoundingClientRect: vi.fn(() => ({ left: 0, top: 0, width: 960, height: 540 })),
    setPointerCapture: setPointerCaptureSpy,
    setPointerCaptureSpy,
    addEventListener(type: string, handler: EventListener) {
      if (!listeners.has(type)) listeners.set(type, [])
      listeners.get(type)!.push(handler)
    },
    dispatchPointer(type: string, overrides = {}) {
      const event = {
        type,
        pointerId: overrides.pointerId ?? 1,
        isPrimary: overrides.isPrimary ?? true,
        button: overrides.button ?? 0,
        clientX: overrides.clientX ?? 0,
        clientY: overrides.clientY ?? 0,
      } as PointerEvent
      for (const h of listeners.get(type) ?? []) h(event)
    },
  }
}

describe('bindInput (KeyboardEvent.code)', () => {
  it('GIVEN KeyW code WHEN keydown THEN moveUp is set', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    keyTarget.emit('keydown', 'KeyW')
    expect(input.moveUp).toBe(true)
  })

  it('GIVEN KeyA code WHEN keydown THEN moveLeft is set', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    keyTarget.emit('keydown', 'KeyA')
    expect(input.moveLeft).toBe(true)
  })

  it('GIVEN KeyS code WHEN keydown THEN moveDown is set', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    keyTarget.emit('keydown', 'KeyS')
    expect(input.moveDown).toBe(true)
  })

  it('GIVEN KeyD code WHEN keydown THEN moveRight is set', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    keyTarget.emit('keydown', 'KeyD')
    expect(input.moveRight).toBe(true)
  })

  it('GIVEN KeyW held WHEN keyup THEN moveUp is cleared', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    keyTarget.emit('keydown', 'KeyW')
    expect(input.moveUp).toBe(true)
    keyTarget.emit('keyup', 'KeyW')
    expect(input.moveUp).toBe(false)
  })

  it('GIVEN ArrowUp code WHEN keydown THEN moveUp is set', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    keyTarget.emit('keydown', 'ArrowUp')
    expect(input.moveUp).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// BLOCKER-3: computeAimDirection unit tests
// Verifies that aimX/aimY is prioritised over lastAimDirection (BLOCKER-1 fix)
// ---------------------------------------------------------------------------

describe('computeAimDirection — aimX/aimY priority over lastAimDirection', () => {
  it('GIVEN aimX/aimY far from player WHEN lastAimDirection points differently THEN aimX/aimY direction wins', () => {
    // Player at (100, 100); aim at (200, 100) → expected dirX=1, dirY=0
    // lastAimDirection points up (0, -1) — should be ignored
    const result = computeAimDirection({
      playerX: 100,
      playerY: 100,
      aimX: 200,
      aimY: 100,
      lastAimDirectionX: 0,
      lastAimDirectionY: -1,
    })
    expect(result.dirX).toBeCloseTo(1)
    expect(result.dirY).toBeCloseTo(0)
  })

  it('GIVEN aimX/aimY at 45-degree angle THEN direction vector is normalised', () => {
    const result = computeAimDirection({
      playerX: 0,
      playerY: 0,
      aimX: 10,
      aimY: 10,
      lastAimDirectionX: 1,
      lastAimDirectionY: 0,
    })
    expect(result.dirX).toBeCloseTo(Math.SQRT1_2)
    expect(result.dirY).toBeCloseTo(Math.SQRT1_2)
  })

  it('GIVEN aimX/aimY very close to player (dist <= AIM_EPSILON_PX) WHEN lastAimDirection is non-zero THEN fallback to lastAimDirection', () => {
    const result = computeAimDirection({
      playerX: 100,
      playerY: 100,
      aimX: 100.5, // dist = 0.5 < AIM_EPSILON_PX=1.0
      aimY: 100,
      lastAimDirectionX: 0,
      lastAimDirectionY: -1,
    })
    expect(result.dirX).toBeCloseTo(0)
    expect(result.dirY).toBeCloseTo(-1)
  })

  it('GIVEN aimX/aimY at player AND lastAimDirection is zero THEN final fallback is right (1,0)', () => {
    const result = computeAimDirection({
      playerX: 100,
      playerY: 100,
      aimX: 100,
      aimY: 100,
      lastAimDirectionX: 0,
      lastAimDirectionY: 0,
    })
    expect(result.dirX).toBeCloseTo(1)
    expect(result.dirY).toBeCloseTo(0)
  })
})

describe('bindInput (PointerEvent lifecycle)', () => {
  it('GIVEN primary pointer button 0 WHEN pointerdown THEN primaryPressed is set and setPointerCapture called', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    canvas.dispatchPointer('pointerdown', { isPrimary: true, button: 0, pointerId: 5 })

    expect(input.primaryPressed).toBe(true)
    expect(canvas.setPointerCaptureSpy).toHaveBeenCalledWith(5)
  })

  it('GIVEN non-primary pointer WHEN pointerdown THEN primaryPressed is NOT set', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    canvas.dispatchPointer('pointerdown', { isPrimary: false, button: 0, pointerId: 2 })

    expect(input.primaryPressed).toBe(false)
  })

  it('GIVEN primary pointer down WHEN pointermove with matching pointerId THEN aim updates', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    canvas.dispatchPointer('pointerdown', { isPrimary: true, button: 0, pointerId: 3 })
    canvas.dispatchPointer('pointermove', { pointerId: 3, isPrimary: true, clientX: 480, clientY: 270 })

    expect(input.pointerX).toBeCloseTo(480)
    expect(input.pointerY).toBeCloseTo(270)
  })

  it('GIVEN no pointerdown WHEN primary pointermove THEN aim updates (AC1: hover without click)', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    // No pointerdown — pure hover
    canvas.dispatchPointer('pointermove', { isPrimary: true, clientX: 300, clientY: 150 })

    expect(input.pointerX).toBeCloseTo(300)
    expect(input.pointerY).toBeCloseTo(150)
    expect(input.pointerKnown).toBe(true)
  })

  it('GIVEN no pointermove yet WHEN createInputState THEN pointerKnown is false (AC1)', () => {
    const input = createInputState()
    expect(input.pointerKnown).toBe(false)
  })

  it('GIVEN non-primary pointermove WHEN pointermove THEN aim NOT updated', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    input.pointerX = 100
    input.pointerY = 100
    canvas.dispatchPointer('pointermove', { isPrimary: false, clientX: 999, clientY: 999 })

    expect(input.pointerX).toBe(100)
    expect(input.pointerY).toBe(100)
    expect(input.pointerKnown).toBe(false)
  })

  it('GIVEN primary pointer down WHEN pointerup THEN primaryPressed is cleared', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    canvas.dispatchPointer('pointerdown', { isPrimary: true, button: 0, pointerId: 1 })
    expect(input.primaryPressed).toBe(true)
    canvas.dispatchPointer('pointerup', { pointerId: 1 })
    expect(input.primaryPressed).toBe(false)
  })

  it('GIVEN primary pointer down WHEN pointercancel THEN primaryPressed is cleared', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    canvas.dispatchPointer('pointerdown', { isPrimary: true, button: 0, pointerId: 1 })
    expect(input.primaryPressed).toBe(true)
    canvas.dispatchPointer('pointercancel', { pointerId: 1 })
    expect(input.primaryPressed).toBe(false)
  })

  it('GIVEN primary pointer down WHEN lostpointercapture THEN primaryPressed is cleared', () => {
    const canvas = makeFakeCanvas()
    const keyTarget = makeFakeKeyTarget()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), keyTarget)

    canvas.dispatchPointer('pointerdown', { isPrimary: true, button: 0, pointerId: 1 })
    expect(input.primaryPressed).toBe(true)
    canvas.dispatchPointer('lostpointercapture', { pointerId: 1 })
    expect(input.primaryPressed).toBe(false)
  })
})

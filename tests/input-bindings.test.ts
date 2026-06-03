/**
 * tests/input-bindings.test.ts
 *
 * Unit tests for pointer hover tracking (AC1) added in Issue #580.
 * Tests that aim coordinates update on pointermove independent of activePointerId.
 */
import { describe, expect, it, vi } from 'vitest'

import { bindInput, createInputState } from '../src/input'
import type { CanvasLike, KeyEventTarget } from '../src/input/InputBindings'

// ---------------------------------------------------------------------------
// Test stubs
// ---------------------------------------------------------------------------

function makeFakeKeyTarget(): KeyEventTarget {
  return {
    addEventListener() {
      // no-op stub: keyboard events are not under test in this file
    },
  }
}

function makeFakeCanvas(boundsOverride?: { left: number; top: number; width: number; height: number }): CanvasLike & {
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
  const bounds = boundsOverride ?? { left: 0, top: 0, width: 960, height: 540 }

  return {
    getBoundingClientRect: vi.fn(() => bounds),
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

// ---------------------------------------------------------------------------
// Hover / pointermove tracking (AC1)
// ---------------------------------------------------------------------------

describe('pointer hover tracking — AC1 (pointerKnown + pointermove always processed)', () => {
  it('GIVEN fresh input state WHEN created THEN pointerKnown is false', () => {
    const input = createInputState()
    expect(input.pointerKnown).toBe(false)
  })

  it('GIVEN no pointerdown WHEN primary pointermove THEN pointerX/Y updates and pointerKnown becomes true', () => {
    const canvas = makeFakeCanvas()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), makeFakeKeyTarget())

    canvas.dispatchPointer('pointermove', { isPrimary: true, clientX: 480, clientY: 270 })

    expect(input.pointerX).toBeCloseTo(480)
    expect(input.pointerY).toBeCloseTo(270)
    expect(input.pointerKnown).toBe(true)
  })

  it('GIVEN pointerdown active WHEN primary pointermove THEN aim still updates', () => {
    const canvas = makeFakeCanvas()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), makeFakeKeyTarget())

    canvas.dispatchPointer('pointerdown', { isPrimary: true, button: 0, pointerId: 1 })
    canvas.dispatchPointer('pointermove', { isPrimary: true, pointerId: 1, clientX: 200, clientY: 100 })

    expect(input.pointerX).toBeCloseTo(200)
    expect(input.pointerY).toBeCloseTo(100)
    expect(input.pointerKnown).toBe(true)
  })

  it('GIVEN non-primary pointermove WHEN dispatched THEN pointerX/Y unchanged and pointerKnown remains false', () => {
    const canvas = makeFakeCanvas()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), makeFakeKeyTarget())

    input.pointerX = 50
    input.pointerY = 50
    canvas.dispatchPointer('pointermove', { isPrimary: false, clientX: 999, clientY: 999 })

    expect(input.pointerX).toBe(50)
    expect(input.pointerY).toBe(50)
    expect(input.pointerKnown).toBe(false)
  })

  it('GIVEN canvas with offset bounds WHEN pointermove at clientX=100 THEN pointerX is correctly mapped', () => {
    const canvas = makeFakeCanvas({ left: 40, top: 20, width: 480, height: 270 })
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), makeFakeKeyTarget())

    // clientX=40+48=88 maps to (48/480)*960 = 96
    canvas.dispatchPointer('pointermove', { isPrimary: true, clientX: 88, clientY: 47 })

    expect(input.pointerX).toBeCloseTo(96)
    // clientY=47-20=27; (27/270)*540 = 54
    expect(input.pointerY).toBeCloseTo(54)
    expect(input.pointerKnown).toBe(true)
  })

  it('GIVEN multiple pointermoves WHEN dispatched THEN each updates coordinates', () => {
    const canvas = makeFakeCanvas()
    const input = createInputState()
    bindInput(canvas, input, () => ({ width: 960, height: 540 }), makeFakeKeyTarget())

    canvas.dispatchPointer('pointermove', { isPrimary: true, clientX: 100, clientY: 50 })
    expect(input.pointerX).toBeCloseTo(100)

    canvas.dispatchPointer('pointermove', { isPrimary: true, clientX: 300, clientY: 200 })
    expect(input.pointerX).toBeCloseTo(300)
    expect(input.pointerY).toBeCloseTo(200)
  })
})

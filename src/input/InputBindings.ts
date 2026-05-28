import type { InputState } from './InputState'

type MovementKey = 'moveUp' | 'moveDown' | 'moveLeft' | 'moveRight'

export const KEY_CODE_MAP = new Map<string, MovementKey>([
  ['KeyW', 'moveUp'],
  ['ArrowUp', 'moveUp'],
  ['KeyS', 'moveDown'],
  ['ArrowDown', 'moveDown'],
  ['KeyA', 'moveLeft'],
  ['ArrowLeft', 'moveLeft'],
  ['KeyD', 'moveRight'],
  ['ArrowRight', 'moveRight'],
])

export interface CanvasLike {
  getBoundingClientRect(): { left: number; top: number; width: number; height: number }
  setPointerCapture(pointerId: number): void
  addEventListener(type: string, handler: EventListener): void
}

export interface KeyEventTarget {
  addEventListener(
    type: 'keydown' | 'keyup',
    handler: (event: KeyboardEvent) => void,
  ): void
}

function updatePointerCoords(
  event: { clientX: number; clientY: number },
  canvas: CanvasLike,
  input: InputState,
  getArena: () => { width: number; height: number },
): void {
  const bounds = canvas.getBoundingClientRect()
  const arena = getArena()
  input.pointerX = ((event.clientX - bounds.left) / bounds.width) * arena.width
  input.pointerY = ((event.clientY - bounds.top) / bounds.height) * arena.height
}

/**
 * Bind keyboard and pointer input to an InputState.
 *
 * @param canvasElement - the canvas receiving pointer events (or a test stub)
 * @param input - mutable input state to update
 * @param getArena - returns current arena dimensions for coordinate mapping
 * @param keyTarget - optional event target for keyboard events (defaults to window)
 */
export function bindInput(
  canvasElement: CanvasLike,
  input: InputState,
  getArena: () => { width: number; height: number },
  keyTarget: KeyEventTarget = window as unknown as KeyEventTarget,
): void {
  let capturedPointerId: number | null = null

  function resetInput(): void {
    input.moveUp = false
    input.moveDown = false
    input.moveLeft = false
    input.moveRight = false
    input.primaryPressed = false
    capturedPointerId = null
  }

  keyTarget.addEventListener('keydown', (event: KeyboardEvent) => {
    const key = KEY_CODE_MAP.get(event.code)
    if (key) {
      input[key] = true
    }
  })

  keyTarget.addEventListener('keyup', (event: KeyboardEvent) => {
    const key = KEY_CODE_MAP.get(event.code)
    if (key) {
      input[key] = false
    }
  })

  if (typeof window !== 'undefined') {
    window.addEventListener('blur', resetInput)
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        resetInput()
      }
    })
  }

  canvasElement.addEventListener('pointerdown', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    if (!event.isPrimary || event.button !== 0) {
      return
    }
    capturedPointerId = event.pointerId
    canvasElement.setPointerCapture(event.pointerId)
    updatePointerCoords(event, canvasElement, input, getArena)
    input.primaryPressed = true
  })

  canvasElement.addEventListener('pointermove', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    if (capturedPointerId === null || event.pointerId !== capturedPointerId) {
      return
    }
    updatePointerCoords(event, canvasElement, input, getArena)
  })

  canvasElement.addEventListener('pointerup', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    if (capturedPointerId === null || event.pointerId !== capturedPointerId) {
      return
    }
    capturedPointerId = null
    input.primaryPressed = false
  })

  canvasElement.addEventListener('pointercancel', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    if (capturedPointerId === null || event.pointerId !== capturedPointerId) {
      return
    }
    capturedPointerId = null
    input.primaryPressed = false
  })

  canvasElement.addEventListener('lostpointercapture', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    if (capturedPointerId === null || event.pointerId !== capturedPointerId) {
      return
    }
    capturedPointerId = null
    input.primaryPressed = false
  })
}

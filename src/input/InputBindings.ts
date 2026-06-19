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

/**
 * Z-equivalent key for the assist_player CommandIntent (AC5).
 * Product contract maps KeyZ → CommandIntent.assist_player.
 * Raw 'assist_player' is not exposed in normal UI; this binding is the only entry point.
 */
export const ASSIST_PLAYER_KEY_CODE = 'KeyZ'

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

/**
 * Visibility change event target interface for testability (AC6).
 */
export interface VisibilityTarget {
  addEventListener(type: 'visibilitychange', handler: () => void): void
  readonly hidden: boolean
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
 * @param blurTarget - optional event target for blur events (defaults to window)
 * @param visibilityTarget - optional event target for visibilitychange events (defaults to document)
 */
export function bindInput(
  canvasElement: CanvasLike,
  input: InputState,
  getArena: () => { width: number; height: number },
  keyTarget: KeyEventTarget = window as unknown as KeyEventTarget,
  blurTarget?: { addEventListener(type: 'blur', handler: () => void): void },
  visibilityTarget?: VisibilityTarget,
): void {
  function resetInput(): void {
    input.moveUp = false
    input.moveDown = false
    input.moveLeft = false
    input.moveRight = false
    input.primaryPressed = false
    input.activePointerId = null
    // AC6: blur and visibilitychange hidden clear physical pressed state and rising edge latch
    input.assistPlayerPressed = false
    // Blocker 4 (Issue #982): clear risingEdge so stale sample_assist_player is not emitted
    // after blur/visibilitychange in the next mapInputToCommands call.
    input.assistPlayerRisingEdge = false
  }

  keyTarget.addEventListener('keydown', (event: KeyboardEvent) => {
    // AC6: event.repeat === true does NOT refresh rising edge
    const key = KEY_CODE_MAP.get(event.code)
    if (key) {
      input[key] = true
    }
    if (event.code === ASSIST_PLAYER_KEY_CODE) {
      if (!event.repeat && !input.assistPlayerPressed) {
        // Rising edge: transition false → true (AC6)
        input.assistPlayerRisingEdge = true
      }
      input.assistPlayerPressed = true
    }
  })

  keyTarget.addEventListener('keyup', (event: KeyboardEvent) => {
    const key = KEY_CODE_MAP.get(event.code)
    if (key) {
      input[key] = false
    }
    if (event.code === ASSIST_PLAYER_KEY_CODE) {
      // AC6: keyup clears physical pressed state
      input.assistPlayerPressed = false
    }
  })

  const effectiveBlurTarget = blurTarget ?? (typeof window !== 'undefined' ? window as unknown as { addEventListener(type: 'blur', handler: () => void): void } : null)
  if (effectiveBlurTarget) {
    effectiveBlurTarget.addEventListener('blur', resetInput)
  }

  const effectiveVisibilityTarget = visibilityTarget ?? (typeof document !== 'undefined' ? document as unknown as VisibilityTarget : null)
  if (effectiveVisibilityTarget) {
    effectiveVisibilityTarget.addEventListener('visibilitychange', () => {
      if (effectiveVisibilityTarget.hidden) {
        resetInput()
      }
    })
  }

  canvasElement.addEventListener('pointerdown', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    if (!event.isPrimary || event.button !== 0) {
      return
    }
    input.activePointerId = event.pointerId
    canvasElement.setPointerCapture(event.pointerId)
    updatePointerCoords(event, canvasElement, input, getArena)
    input.pointerKnown = true
    input.primaryPressed = true
  })

  canvasElement.addEventListener('pointermove', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    // AC1: Always update aim coordinates on hover (independent of activePointerId).
    // Non-primary pointers (e.g. second finger on touch) are skipped.
    if (!event.isPrimary) {
      return
    }
    updatePointerCoords(event, canvasElement, input, getArena)
    input.pointerKnown = true
  })

  canvasElement.addEventListener('pointerup', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    if (input.activePointerId === null || event.pointerId !== input.activePointerId) {
      return
    }
    input.activePointerId = null
    input.primaryPressed = false
  })

  canvasElement.addEventListener('pointercancel', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    if (input.activePointerId === null || event.pointerId !== input.activePointerId) {
      return
    }
    input.activePointerId = null
    input.primaryPressed = false
  })

  canvasElement.addEventListener('lostpointercapture', (rawEvent: Event) => {
    const event = rawEvent as PointerEvent
    if (input.activePointerId === null || event.pointerId !== input.activePointerId) {
      return
    }
    input.activePointerId = null
    input.primaryPressed = false
  })
}

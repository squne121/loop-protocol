export interface InputState {
  moveUp: boolean
  moveDown: boolean
  moveLeft: boolean
  moveRight: boolean
  pointerX: number
  pointerY: number
  primaryPressed: boolean
  /** Tracks the currently captured pointer id. null when no pointer is captured. */
  activePointerId: number | null
  /**
   * True once the pointer has entered the canvas via pointermove at least once.
   * Used by InputMapper to suppress aim commands before the first hover.
   */
  pointerKnown: boolean
}

export function createInputState(): InputState {
  return {
    moveUp: false,
    moveDown: false,
    moveLeft: false,
    moveRight: false,
    pointerX: 0,
    pointerY: 0,
    primaryPressed: false,
    activePointerId: null,
    pointerKnown: false,
  }
}

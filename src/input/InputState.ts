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
  /**
   * Physical pressed state for the assist_player key (KeyZ by default).
   * True while the key is physically held. Set on rising edge only (AC6).
   * Cleared on keyup, blur, and visibilitychange hidden (AC6).
   */
  assistPlayerPressed: boolean
  /**
   * Rising-edge latch: set to true for exactly one tick when assistPlayerPressed
   * transitions false → true. Consumed by InputMapper to sample the intent (AC6).
   */
  assistPlayerRisingEdge: boolean
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
    assistPlayerPressed: false,
    assistPlayerRisingEdge: false,
  }
}

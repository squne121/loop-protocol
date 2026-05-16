export interface InputState {
  moveUp: boolean
  moveDown: boolean
  moveLeft: boolean
  moveRight: boolean
  pointerX: number
  pointerY: number
  primaryPressed: boolean
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
  }
}

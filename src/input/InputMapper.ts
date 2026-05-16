import type { InputState } from './InputState'

export type InputCommand =
  | {
      type: 'move'
      axisX: number
      axisY: number
    }
  | {
      type: 'aim'
      x: number
      y: number
    }
  | {
      type: 'fire'
    }

export function mapInputToCommands(input: InputState): InputCommand[] {
  const commands: InputCommand[] = []
  const axisX = Number(input.moveRight) - Number(input.moveLeft)
  const axisY = Number(input.moveDown) - Number(input.moveUp)

  if (axisX !== 0 || axisY !== 0) {
    commands.push({
      type: 'move',
      axisX,
      axisY,
    })
  }

  commands.push({
    type: 'aim',
    x: input.pointerX,
    y: input.pointerY,
  })

  if (input.primaryPressed) {
    commands.push({ type: 'fire' })
  }

  return commands
}

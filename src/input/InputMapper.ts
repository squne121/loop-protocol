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
  | {
      /**
       * Sampling event for assist_player CommandIntent (AC5, Issue #982).
       * Emitted on rising edge only (AC6). Consumed by tick step1 to buffer the intent.
       * Not exposed in normal UI; raw key → intent routing is via InputBindings (KeyZ).
       */
      type: 'sample_assist_player'
    }

/**
 * Maps InputState to a list of InputCommands for the current tick.
 *
 * Rising-edge latch (assistPlayerRisingEdge) is consumed here and MUST be
 * cleared after sampling to avoid double-sampling across ticks (AC6).
 */
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

  // AC3: Only emit aim command once the pointer position is known (first pointermove).
  if (input.pointerKnown) {
    commands.push({
      type: 'aim',
      x: input.pointerX,
      y: input.pointerY,
    })
  }

  if (input.primaryPressed) {
    commands.push({ type: 'fire' })
  }

  // AC6: Rising edge only.
  // event.repeat guard is in InputBindings (keydown handler).
  // keyup, blur, visibilitychange handlers in InputBindings clear assistPlayerPressed.
  // Consume latch here so it is not sampled again in subsequent ticks.
  if (input.assistPlayerRisingEdge) {
    commands.push({ type: 'sample_assist_player' })
    input.assistPlayerRisingEdge = false
  }

  return commands
}

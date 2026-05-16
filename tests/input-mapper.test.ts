import { describe, expect, it } from 'vitest'

import { mapInputToCommands } from '../src/input'

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
    })

    expect(commands).toEqual([
      { type: 'move', axisX: 1, axisY: -1 },
      { type: 'aim', x: 120, y: 340 },
      { type: 'fire' },
    ])
  })
})

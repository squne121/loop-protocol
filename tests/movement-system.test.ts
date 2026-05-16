import { describe, expect, it } from 'vitest'

import { createInitialGameState } from '../src/state'
import { runMovementSystem } from '../src/systems'

describe('runMovementSystem', () => {
  it('moves the player using normalized input axes', () => {
    const state = createInitialGameState()

    runMovementSystem(
      state,
      [{ type: 'move', axisX: 1, axisY: 0 }],
      1000,
    )

    expect(state.player.x).toBe(450)
    expect(state.player.y).toBe(270)
  })
})

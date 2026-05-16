import { describe, expect, it } from 'vitest'

import { createInitialGameState } from '../src/state'
import { runCombatSystem } from '../src/systems'

describe('runCombatSystem', () => {
  it('fires once and grants resources when the weapon is ready', () => {
    const state = createInitialGameState()

    runCombatSystem(
      state,
      [
        { type: 'aim', x: 800, y: 200 },
        { type: 'fire' },
      ],
      16,
    )

    expect(state.player.shotsFired).toBe(1)
    expect(state.progress.resources).toBe(1)
    expect(state.player.weaponCooldownMs).toBe(state.player.weaponIntervalMs)
  })
})

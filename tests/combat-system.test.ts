import { describe, expect, it } from 'vitest'

import { createInitialGameState } from '../src/state'
import { runCombatSystem } from '../src/systems'

describe('runCombatSystem', () => {
  it('GIVEN weapon ready WHEN fire command with aim THEN shotsFired increments and cooldown resets', () => {
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
    expect(state.player.weaponCooldownMs).toBe(state.player.weaponIntervalMs)
  })

  it('GIVEN weapon ready WHEN fire command THEN resources are NOT mutated (no resources += weaponPower)', () => {
    const state = createInitialGameState()
    const resourcesBefore = state.progress.resources

    runCombatSystem(
      state,
      [
        { type: 'aim', x: 800, y: 200 },
        { type: 'fire' },
      ],
      16,
    )

    expect(state.progress.resources).toBe(resourcesBefore)
  })

  it('GIVEN weapon ready WHEN fire command with aim at (800, 200) THEN projectile is spawned in state.projectiles', () => {
    const state = createInitialGameState()

    runCombatSystem(
      state,
      [
        { type: 'aim', x: 800, y: 200 },
        { type: 'fire' },
      ],
      16,
    )

    expect(state.projectiles).toHaveLength(1)
    const p = state.projectiles[0]
    expect(p.x).toBe(state.player.x)
    expect(p.y).toBe(state.player.y)
    expect(p.ageMs).toBe(0)
    expect(p.lifetimeMs).toBeGreaterThan(0)
    expect(p.speedPxPerSec).toBeGreaterThan(0)
  })

  it('GIVEN weapon ready WHEN fire command with aim THEN projectile direction is unit vector toward aim', () => {
    const state = createInitialGameState()
    const aimX = 800
    const aimY = 200

    runCombatSystem(
      state,
      [
        { type: 'aim', x: aimX, y: aimY },
        { type: 'fire' },
      ],
      16,
    )

    const p = state.projectiles[0]
    const expectedDx = aimX - state.player.x
    const expectedDy = aimY - state.player.y
    const dist = Math.hypot(expectedDx, expectedDy)
    expect(p.directionX).toBeCloseTo(expectedDx / dist)
    expect(p.directionY).toBeCloseTo(expectedDy / dist)
    // Must be a unit vector
    expect(Math.hypot(p.directionX, p.directionY)).toBeCloseTo(1)
  })

  it('GIVEN weapon on cooldown WHEN fire command THEN no new projectile spawned', () => {
    const state = createInitialGameState()
    state.player.weaponCooldownMs = 200

    runCombatSystem(
      state,
      [
        { type: 'aim', x: 800, y: 200 },
        { type: 'fire' },
      ],
      16,
    )

    expect(state.projectiles).toHaveLength(0)
    expect(state.player.shotsFired).toBe(0)
  })

  it('GIVEN aim at same position as player WHEN fire command THEN projectile direction defaults to +X (initial fallback)', () => {
    const state = createInitialGameState()
    // aim at same position as player — no previous non-zero direction, initial default is +X
    runCombatSystem(
      state,
      [
        { type: 'aim', x: state.player.x, y: state.player.y },
        { type: 'fire' },
      ],
      16,
    )

    const p = state.projectiles[0]
    expect(p.directionX).toBeCloseTo(1)
    expect(p.directionY).toBeCloseTo(0)
  })

  it('GIVEN previous aim upward WHEN next tick aim overlaps player position THEN projectile uses last non-zero direction', () => {
    const state = createInitialGameState()

    // Tick 1: aim above player (upward direction) and fire — saves lastAimDirection = (0, -1)
    runCombatSystem(
      state,
      [
        { type: 'aim', x: state.player.x, y: state.player.y - 100 },
        { type: 'fire' },
      ],
      16,
    )
    expect(state.player.lastAimDirectionX).toBeCloseTo(0)
    expect(state.player.lastAimDirectionY).toBeCloseTo(-1)

    // Reset cooldown for second shot
    state.player.weaponCooldownMs = 0

    // Tick 2: aim at same position as player (distance < epsilon) — should use last direction
    runCombatSystem(
      state,
      [
        { type: 'aim', x: state.player.x, y: state.player.y },
        { type: 'fire' },
      ],
      16,
    )

    const p = state.projectiles[1]
    expect(p.directionX).toBeCloseTo(0)
    expect(p.directionY).toBeCloseTo(-1)
    // Must be a unit vector
    expect(Math.hypot(p.directionX, p.directionY)).toBeCloseTo(1)
  })

  it('GIVEN weapon ready WHEN aim command THEN aimX/aimY updated', () => {
    const state = createInitialGameState()

    runCombatSystem(
      state,
      [{ type: 'aim', x: 500, y: 300 }],
      16,
    )

    expect(state.player.aimX).toBe(500)
    expect(state.player.aimY).toBe(300)
  })

  it('GIVEN cooldown > deltaMs WHEN tick advances THEN cooldown decrements', () => {
    const state = createInitialGameState()
    state.player.weaponCooldownMs = 100

    runCombatSystem(state, [], 50)

    expect(state.player.weaponCooldownMs).toBe(50)
  })

  it('GIVEN cooldown at 0 WHEN fire command is sent twice THEN second fire waits for cooldown', () => {
    const state = createInitialGameState()

    runCombatSystem(state, [{ type: 'aim', x: 800, y: 200 }, { type: 'fire' }], 16)
    expect(state.projectiles).toHaveLength(1)

    // Second fire immediately (cooldown is full now)
    runCombatSystem(state, [{ type: 'fire' }], 16)
    expect(state.projectiles).toHaveLength(1) // no new projectile
  })

  it('GIVEN multiple fire shots WHEN projectiles are spawned THEN ids are incrementing', () => {
    const state = createInitialGameState()

    runCombatSystem(state, [{ type: 'aim', x: 800, y: 200 }, { type: 'fire' }], 16)
    // Reset cooldown for second shot
    state.player.weaponCooldownMs = 0
    runCombatSystem(state, [{ type: 'aim', x: 800, y: 200 }, { type: 'fire' }], 16)

    expect(state.projectiles[0].id).toBeLessThan(state.projectiles[1].id)
  })
})

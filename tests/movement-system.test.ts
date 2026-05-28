import { describe, expect, it } from 'vitest'

import { createInitialGameState } from '../src/state'
import { clampPlayerToArena, runMovementSystem } from '../src/systems'

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

  it('GIVEN diagonal input (axisX=1, axisY=1) WHEN movement runs THEN speed is same as cardinal direction (normalized)', () => {
    const state = createInitialGameState()
    const startX = state.player.x
    const startY = state.player.y
    const speed = state.player.speed
    const deltaMs = 1000

    runMovementSystem(
      state,
      [{ type: 'move', axisX: 1, axisY: 1 }],
      deltaMs,
    )

    const expectedOffset = speed * (deltaMs / 1000) * (1 / Math.SQRT2)
    expect(state.player.x).toBeCloseTo(startX + expectedOffset)
    expect(state.player.y).toBeCloseTo(startY + expectedOffset)
  })

  it('GIVEN player near left edge WHEN moving left THEN player is clamped at left boundary', () => {
    const state = createInitialGameState()
    state.player.x = state.player.radius + 1 // very close to left edge
    const deltaMs = 1000 // large enough to push beyond boundary

    runMovementSystem(
      state,
      [{ type: 'move', axisX: -1, axisY: 0 }],
      deltaMs,
    )

    expect(state.player.x).toBeGreaterThanOrEqual(state.player.radius)
  })

  it('GIVEN player near right edge WHEN moving right THEN player is clamped at right boundary', () => {
    const state = createInitialGameState()
    state.player.x = state.arena.width - state.player.radius - 1
    const deltaMs = 1000

    runMovementSystem(
      state,
      [{ type: 'move', axisX: 1, axisY: 0 }],
      deltaMs,
    )

    expect(state.player.x).toBeLessThanOrEqual(state.arena.width - state.player.radius)
  })

  it('GIVEN player near top edge WHEN moving up THEN player is clamped at top boundary', () => {
    const state = createInitialGameState()
    state.player.y = state.player.radius + 1
    const deltaMs = 1000

    runMovementSystem(
      state,
      [{ type: 'move', axisX: 0, axisY: -1 }],
      deltaMs,
    )

    expect(state.player.y).toBeGreaterThanOrEqual(state.player.radius)
  })

  it('GIVEN player near bottom edge WHEN moving down THEN player is clamped at bottom boundary', () => {
    const state = createInitialGameState()
    state.player.y = state.arena.height - state.player.radius - 1
    const deltaMs = 1000

    runMovementSystem(
      state,
      [{ type: 'move', axisX: 0, axisY: 1 }],
      deltaMs,
    )

    expect(state.player.y).toBeLessThanOrEqual(state.arena.height - state.player.radius)
  })

  it('GIVEN no move command WHEN movement runs THEN player position unchanged', () => {
    const state = createInitialGameState()
    const startX = state.player.x
    const startY = state.player.y

    runMovementSystem(state, [], 1000)

    expect(state.player.x).toBe(startX)
    expect(state.player.y).toBe(startY)
  })

  it('GIVEN very large deltaMs WHEN moving right THEN player is clamped within arena', () => {
    const state = createInitialGameState()
    const deltaMs = 999999

    runMovementSystem(
      state,
      [{ type: 'move', axisX: 1, axisY: 0 }],
      deltaMs,
    )

    expect(state.player.x).toBeLessThanOrEqual(state.arena.width - state.player.radius)
    expect(state.player.x).toBeGreaterThanOrEqual(state.player.radius)
  })

  it('GIVEN player placed outside arena bounds WHEN no move command THEN runMovementSystem clamps player inside arena', () => {
    const state = createInitialGameState()
    // Force player outside right and bottom boundaries
    state.player.x = state.arena.width + 100
    state.player.y = state.arena.height + 100

    runMovementSystem(state, [], 16)

    expect(state.player.x).toBeLessThanOrEqual(state.arena.width - state.player.radius)
    expect(state.player.y).toBeLessThanOrEqual(state.arena.height - state.player.radius)
  })

  it('GIVEN player at valid position WHEN arena is shrunk and clampPlayerToArena called THEN player stays inside new bounds', () => {
    const state = createInitialGameState()
    // Place player near right edge of original arena
    state.player.x = state.arena.width - state.player.radius

    // Simulate arena resize (shrink width significantly)
    state.arena.width = 400
    state.arena.height = 225

    clampPlayerToArena(state)

    expect(state.player.x).toBeLessThanOrEqual(state.arena.width - state.player.radius)
    expect(state.player.x).toBeGreaterThanOrEqual(state.player.radius)
    expect(state.player.y).toBeLessThanOrEqual(state.arena.height - state.player.radius)
    expect(state.player.y).toBeGreaterThanOrEqual(state.player.radius)
  })
})

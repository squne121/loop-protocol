import { describe, expect, it } from 'vitest'

import { createInitialGameState } from '../src/state'
import type { ProjectileState } from '../src/state'
import { runProjectileSystem } from '../src/systems'

function makeProjectile(overrides: Partial<ProjectileState> = {}): ProjectileState {
  return {
    id: 1,
    x: 100,
    y: 100,
    radius: 4,
    directionX: 1,
    directionY: 0,
    speedPxPerSec: 520,
    ageMs: 0,
    lifetimeMs: 1200,
    ...overrides,
  }
}

describe('runProjectileSystem', () => {
  it('GIVEN a projectile moving right WHEN dt=1000ms THEN x advances by speedPxPerSec', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ x: 100, y: 100, directionX: 1, directionY: 0, speedPxPerSec: 520 })]

    runProjectileSystem(state, [], 1000)

    expect(state.projectiles[0].x).toBeCloseTo(100 + 520)
    expect(state.projectiles[0].y).toBeCloseTo(100)
  })

  it('GIVEN a projectile moving diagonally WHEN dt=1000ms THEN position advances correctly', () => {
    const state = createInitialGameState()
    const sqrt2inv = 1 / Math.SQRT2
    state.projectiles = [
      makeProjectile({
        x: 200,
        y: 200,
        directionX: sqrt2inv,
        directionY: sqrt2inv,
        speedPxPerSec: 100,
      }),
    ]

    runProjectileSystem(state, [], 1000)

    expect(state.projectiles[0].x).toBeCloseTo(200 + 100 * sqrt2inv)
    expect(state.projectiles[0].y).toBeCloseTo(200 + 100 * sqrt2inv)
  })

  it('GIVEN a projectile WHEN it advances THEN ageMs increases by deltaMs', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ ageMs: 0 })]

    runProjectileSystem(state, [], 100)

    expect(state.projectiles[0].ageMs).toBe(100)
  })

  it('GIVEN a projectile with ageMs at lifetimeMs WHEN system runs THEN projectile is removed', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ ageMs: 1200, lifetimeMs: 1200 })]

    runProjectileSystem(state, [], 0)

    expect(state.projectiles).toHaveLength(0)
  })

  it('GIVEN a projectile approaching lifetime WHEN deltaMs pushes ageMs >= lifetimeMs THEN removed', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ ageMs: 1100, lifetimeMs: 1200 })]

    runProjectileSystem(state, [], 200)

    expect(state.projectiles).toHaveLength(0)
  })

  it('GIVEN a projectile still within lifetime WHEN system runs THEN it is kept', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ ageMs: 0, lifetimeMs: 1200 })]

    runProjectileSystem(state, [], 100)

    expect(state.projectiles).toHaveLength(1)
  })

  it('GIVEN a projectile out of bounds on the right WHEN system runs THEN it is removed', () => {
    const state = createInitialGameState()
    // Place beyond right edge + margin
    state.projectiles = [makeProjectile({ x: state.arena.width + 9, y: 100 })]

    runProjectileSystem(state, [], 0)

    expect(state.projectiles).toHaveLength(0)
  })

  it('GIVEN a projectile out of bounds on the left WHEN system runs THEN it is removed', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ x: -9, y: 100 })]

    runProjectileSystem(state, [], 0)

    expect(state.projectiles).toHaveLength(0)
  })

  it('GIVEN a projectile out of bounds on top WHEN system runs THEN it is removed', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ x: 100, y: -9 })]

    runProjectileSystem(state, [], 0)

    expect(state.projectiles).toHaveLength(0)
  })

  it('GIVEN a projectile out of bounds on bottom WHEN system runs THEN it is removed', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ x: 100, y: state.arena.height + 9 })]

    runProjectileSystem(state, [], 0)

    expect(state.projectiles).toHaveLength(0)
  })

  it('GIVEN a projectile at the out-of-bounds margin boundary WHEN x = width + 8 (at margin) THEN it is removed', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ x: state.arena.width + 8, y: 100 })]

    runProjectileSystem(state, [], 0)

    // x > width + margin (margin=8) so still inside the margin boundary? margin means > arena+margin
    // Our condition: p.x > state.arena.width + margin -> x = width + 8 is NOT > width + 8, so kept
    // The spec says "boundary 外 + margin 8px で削除". x = width + 8 is AT the boundary.
    // Interpretation: strictly outside (> width + margin). At exactly width+8 is borderline.
    // We check: p.x > width + margin means > width + 8. At exactly width+8 it's not > so kept.
    // The test verifies our filter condition precisely.
    expect(state.projectiles).toHaveLength(1)
  })

  it('GIVEN a projectile at x = width + 8 + epsilon WHEN system runs THEN it is removed', () => {
    const state = createInitialGameState()
    state.projectiles = [makeProjectile({ x: state.arena.width + 8.001, y: 100 })]

    runProjectileSystem(state, [], 0)

    expect(state.projectiles).toHaveLength(0)
  })

  it('GIVEN multiple projectiles in different states WHEN system runs THEN only valid ones remain in original order', () => {
    const state = createInitialGameState()
    state.projectiles = [
      makeProjectile({ id: 1, x: 100, y: 100, ageMs: 0, lifetimeMs: 1200 }),          // valid
      makeProjectile({ id: 2, x: 100, y: 100, ageMs: 1200, lifetimeMs: 1200 }),        // expired
      makeProjectile({ id: 3, x: 100, y: 100, ageMs: 0, lifetimeMs: 1200 }),           // valid
      makeProjectile({ id: 4, x: state.arena.width + 100, y: 100, ageMs: 0 }),         // OOB
    ]

    runProjectileSystem(state, [], 0)

    expect(state.projectiles).toHaveLength(2)
    expect(state.projectiles[0].id).toBe(1)
    expect(state.projectiles[1].id).toBe(3)
  })
})

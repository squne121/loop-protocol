/**
 * tests/sortie-system.test.ts
 *
 * Vitest unit tests for SortieSystem (AC9).
 * Required test cases: bootstrap, no-start, victory, defeat, double-result,
 * timer-authority, defeat-precedence, terminal-gate, kills-boundary.
 */

import { describe, it, expect } from 'vitest'
import { createInitialGameState } from '../src/state/GameState'
import {
  startSortie,
  runSortieSystem,
  SORTIE_DURATION_MS,
} from '../src/systems/SortieSystem'
import { defaultSimulationConfig } from '../src/state/SimulationConfig'

const FDT = defaultSimulationConfig.fixedDeltaMs // ~16.667 ms

/** Number of ticks required for 120-second victory */
const TARGET_TICKS = Math.ceil(SORTIE_DURATION_MS / FDT)

// ---------------------------------------------------------------------------
// bootstrap
// ---------------------------------------------------------------------------
describe('GIVEN createInitialGameState and startSortie', () => {
  it('bootstrap: WHEN startSortie called THEN status=running, elapsedTicks=0, result=null', () => {
    const state = createInitialGameState()
    expect(state.sortie.status).toBe('idle')
    expect(state.sortie.result).toBeNull()

    startSortie(state, FDT)

    expect(state.sortie.status).toBe('running')
    expect(state.sortie.elapsedTicks).toBe(0)
    expect(state.sortie.result).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// no-start
// ---------------------------------------------------------------------------
describe('GIVEN sortie is idle', () => {
  it('no-start: WHEN runSortieSystem called without startSortie THEN elapsedTicks stays 0', () => {
    const state = createInitialGameState()
    runSortieSystem(state, FDT)
    expect(state.sortie.status).toBe('idle')
    expect(state.sortie.elapsedTicks).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// victory
// ---------------------------------------------------------------------------
describe('GIVEN sortie is running', () => {
  it('victory: WHEN TARGET_TICKS elapsed THEN outcome=victory, status=victory', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    // Advance enough ticks to trigger victory
    for (let i = 0; i < TARGET_TICKS; i++) {
      runSortieSystem(state, FDT)
      state.tick += 1
    }

    expect(state.sortie.status).toBe('victory')
    expect(state.sortie.result).not.toBeNull()
    expect(state.sortie.result!.outcome).toBe('victory')
  })
})

// ---------------------------------------------------------------------------
// defeat
// ---------------------------------------------------------------------------
describe('GIVEN sortie is running and player hp reaches 0', () => {
  it('defeat: WHEN player.hp <= 0 THEN outcome=defeat, status=defeat', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    state.player.hp = 0
    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('defeat')
    expect(state.sortie.result).not.toBeNull()
    expect(state.sortie.result!.outcome).toBe('defeat')
  })
})

// ---------------------------------------------------------------------------
// double-result
// ---------------------------------------------------------------------------
describe('GIVEN sortie already has a result (victory)', () => {
  it('double-result: WHEN runSortieSystem called again THEN result is not overwritten', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    for (let i = 0; i < TARGET_TICKS; i++) {
      runSortieSystem(state, FDT)
      state.tick += 1
    }

    const firstResult = state.sortie.result
    expect(firstResult).not.toBeNull()

    // Extra tick after terminal state
    runSortieSystem(state, FDT)

    expect(state.sortie.result).toBe(firstResult) // same reference
  })
})

// ---------------------------------------------------------------------------
// timer-authority
// ---------------------------------------------------------------------------
describe('GIVEN sortie victory', () => {
  it('timer-authority: THEN durationMs = elapsedTicks * fixedDeltaMs (not elapsedMs)', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    // Deliberately set elapsedMs to a wrong value to ensure it is NOT used
    state.elapsedMs = 99999

    for (let i = 0; i < TARGET_TICKS; i++) {
      runSortieSystem(state, FDT)
      state.tick += 1
    }

    expect(state.sortie.result).not.toBeNull()
    const expected = state.sortie.elapsedTicks * FDT
    expect(state.sortie.result!.durationMs).toBeCloseTo(expected)
  })
})

// ---------------------------------------------------------------------------
// defeat-precedence
// ---------------------------------------------------------------------------
describe('GIVEN same tick: player hp=0 and elapsedTicks >= targetTicks', () => {
  it('defeat-precedence: THEN outcome=defeat', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    // Manually advance elapsedTicks to the brink (TARGET_TICKS - 1)
    // After runSortieSystem increments, it will reach TARGET_TICKS on this call
    const mutableSortie = state.sortie as { elapsedTicks: number }
    mutableSortie.elapsedTicks = TARGET_TICKS - 1

    // Player hp = 0 on the same tick -> defeat should win
    state.player.hp = 0
    runSortieSystem(state, FDT)

    expect(state.sortie.result).not.toBeNull()
    expect(state.sortie.result!.outcome).toBe('defeat')
  })
})

// ---------------------------------------------------------------------------
// terminal-gate
// ---------------------------------------------------------------------------
describe('GIVEN sortie has reached terminal state (defeat)', () => {
  it('terminal-gate: WHEN runSortieSystem called multiple times THEN state does not change', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.player.hp = 0
    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('defeat')
    const snapshotTicks = state.sortie.elapsedTicks
    const snapshotResult = state.sortie.result

    // Call multiple times
    for (let i = 0; i < 5; i++) {
      runSortieSystem(state, FDT)
    }

    expect(state.sortie.status).toBe('defeat')
    expect(state.sortie.elapsedTicks).toBe(snapshotTicks)
    expect(state.sortie.result).toBe(snapshotResult) // same reference
  })
})

// ---------------------------------------------------------------------------
// kills-boundary
// ---------------------------------------------------------------------------
describe('GIVEN enemies with various defeatedAtTick values', () => {
  it('kills-boundary: THEN only enemies with defeatedAtTick <= terminalTick count as kills', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    // Advance elapsedTicks to TARGET_TICKS - 1 so victory fires on next call
    const mutableSortie = state.sortie as { elapsedTicks: number }
    mutableSortie.elapsedTicks = TARGET_TICKS - 1

    // Set current tick to 42 (will be the terminalTick when victory is recorded)
    state.tick = 42

    // Enemy killed before terminal tick
    state.enemies.push({
      id: 1,
      definitionId: 'enemy-basic',
      hp: 0,
      maxHp: 5,
      x: 0,
      y: 0,
      radius: 12,
      speedPxPerSec: 60,
      contactDamage: 1,
      defeated: true,
      defeatedAtTick: 40, // <= 42
    })

    // Enemy killed exactly at terminal tick
    state.enemies.push({
      id: 2,
      definitionId: 'enemy-basic',
      hp: 0,
      maxHp: 5,
      x: 0,
      y: 0,
      radius: 12,
      speedPxPerSec: 60,
      contactDamage: 1,
      defeated: true,
      defeatedAtTick: 42, // === 42
    })

    // Enemy killed after terminal tick (should NOT count)
    state.enemies.push({
      id: 3,
      definitionId: 'enemy-basic',
      hp: 0,
      maxHp: 5,
      x: 0,
      y: 0,
      radius: 12,
      speedPxPerSec: 60,
      contactDamage: 1,
      defeated: true,
      defeatedAtTick: 43, // > 42, should NOT count
    })

    // Enemy not yet defeated
    state.enemies.push({
      id: 4,
      definitionId: 'enemy-basic',
      hp: 5,
      maxHp: 5,
      x: 0,
      y: 0,
      radius: 12,
      speedPxPerSec: 60,
      contactDamage: 1,
      defeated: false,
      defeatedAtTick: null,
    })

    runSortieSystem(state, FDT)

    expect(state.sortie.result).not.toBeNull()
    expect(state.sortie.result!.outcome).toBe('victory')
    expect(state.sortie.result!.kills).toBe(2) // ids 1 and 2 only
  })
})

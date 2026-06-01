/**
 * tests/sortie-system.test.ts
 *
 * Vitest unit tests for SortieSystem (AC7–AC12).
 * Required test cases: bootstrap, no-start, victory (allEnemiesDefeated), defeat,
 * timeout→defeat, defeat-precedence, vacuous-truth, double-result,
 * timer-authority, terminal-gate, kills-boundary, playerHpRemaining-clamp.
 */

import { describe, it, expect } from 'vitest'
import { createInitialGameState } from '../src/state/GameState'
import {
  startSortie,
  runSortieSystem,
  runSortieSimulationStep,
  SORTIE_DURATION_MS,
} from '../src/systems/SortieSystem'
import { defaultSimulationConfig } from '../src/state/SimulationConfig'
import { createInputState, mapInputToCommands } from '../src/input'
import type { EnemyState } from '../src/state/GameState'

const FDT = defaultSimulationConfig.fixedDeltaMs // ~16.667 ms

/** Number of ticks required for 30-second timeout */
const TARGET_TICKS = Math.ceil(SORTIE_DURATION_MS / FDT)

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function makeDefeatedEnemy(id: number, defeatedAtTick: number): EnemyState {
  return {
    id,
    definitionId: 'enemy-basic',
    hp: 0,
    maxHp: 5,
    x: 0,
    y: 0,
    radius: 12,
    speedPxPerSec: 60,
    contactDamage: 1,
    defeated: true,
    defeatedAtTick,
  }
}

function makeLiveEnemy(id: number): EnemyState {
  return {
    id,
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
  }
}

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
// AC7: all enemies defeated → victory
// ---------------------------------------------------------------------------
describe('GIVEN sortie is running and all enemies are defeated', () => {
  it('AC7: WHEN all spawned enemies defeated THEN outcome=victory, status=victory', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('victory')
    expect(state.sortie.result).not.toBeNull()
    expect(state.sortie.result!.outcome).toBe('victory')
  })
})

// ---------------------------------------------------------------------------
// AC8: defeat when player hp reaches 0
// ---------------------------------------------------------------------------
describe('GIVEN sortie is running and player hp reaches 0', () => {
  it('AC8: WHEN player.hp <= 0 THEN outcome=defeat, status=defeat', () => {
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
// AC9: 30s timeout → defeat
// ---------------------------------------------------------------------------
describe('GIVEN sortie is running and 30s elapses with enemies remaining', () => {
  it('AC9: WHEN elapsedTicks >= targetTicks with live enemies THEN outcome=defeat', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    // Add a live enemy so allEnemiesDefeated is false
    state.enemies.push(makeLiveEnemy(1))

    ;(state.sortie as { elapsedTicks: number }).elapsedTicks = TARGET_TICKS - 1
    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('defeat')
    expect(state.sortie.result).not.toBeNull()
    expect(state.sortie.result!.outcome).toBe('defeat')
  })
})

// ---------------------------------------------------------------------------
// AC10: defeat-precedence — defeat > victory > timeout
// ---------------------------------------------------------------------------
describe('GIVEN same tick: player hp=0 AND all enemies defeated', () => {
  it('AC10: defeat-precedence: THEN outcome=defeat (defeat beats victory)', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    // All enemies defeated → would be victory, but player.hp=0 → defeat wins
    state.enemies.push(makeDefeatedEnemy(1, 0))
    state.player.hp = 0
    runSortieSystem(state, FDT)

    expect(state.sortie.result).not.toBeNull()
    expect(state.sortie.result!.outcome).toBe('defeat')
  })
})

describe('GIVEN same tick: player hp=0, all enemies defeated, AND elapsedTicks >= targetTicks', () => {
  it('AC10: defeat-precedence: THEN outcome=defeat (defeat beats victory and timeout)', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    state.enemies.push(makeDefeatedEnemy(1, 0))
    state.player.hp = 0
    ;(state.sortie as { elapsedTicks: number }).elapsedTicks = TARGET_TICKS - 1
    runSortieSystem(state, FDT)

    expect(state.sortie.result!.outcome).toBe('defeat')
  })
})

describe('GIVEN same tick: all enemies defeated AND elapsedTicks >= targetTicks, player alive', () => {
  it('AC10: victory-over-timeout: THEN outcome=victory (victory beats timeout)', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    state.enemies.push(makeDefeatedEnemy(1, 0))
    // No player defeat
    ;(state.sortie as { elapsedTicks: number }).elapsedTicks = TARGET_TICKS - 1
    runSortieSystem(state, FDT)

    expect(state.sortie.result!.outcome).toBe('victory')
  })
})

// ---------------------------------------------------------------------------
// vacuous truth: no enemies → no victory
// ---------------------------------------------------------------------------
describe('GIVEN sortie is running with no enemies', () => {
  it('vacuous-truth: WHEN no enemies in array THEN victory does not trigger', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    // No enemies pushed — allEnemiesDefeated must be false (length === 0 guard)
    runSortieSystem(state, FDT)
    expect(state.sortie.status).toBe('running')
  })
})

// ---------------------------------------------------------------------------
// AC11: double-result (result generated exactly once)
// ---------------------------------------------------------------------------
describe('GIVEN sortie already has a result (victory)', () => {
  it('AC11: double-result: WHEN runSortieSystem called again THEN result is not overwritten', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)

    const firstResult = state.sortie.result
    expect(firstResult).not.toBeNull()

    // Extra tick after terminal state
    runSortieSystem(state, FDT)

    expect(state.sortie.result).toBe(firstResult) // same reference
  })
})

// ---------------------------------------------------------------------------
// AC12: timer-authority — durationMs uses elapsedTicks, not elapsedMs
// ---------------------------------------------------------------------------
describe('GIVEN sortie victory', () => {
  it('AC12: timer-authority: THEN durationMs = elapsedTicks * fixedDeltaMs (not elapsedMs)', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

    // Deliberately set elapsedMs to a wrong value to ensure it is NOT used
    state.elapsedMs = 99999

    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)

    expect(state.sortie.result).not.toBeNull()
    const expected = state.sortie.elapsedTicks * FDT
    expect(state.sortie.result!.durationMs).toBeCloseTo(expected)
  })
})

// ---------------------------------------------------------------------------
// terminal-gate
// ---------------------------------------------------------------------------
describe('GIVEN sortie has reached terminal state (defeat)', () => {
  it('terminal-gate: WHEN runSortieSimulationStep called multiple times after defeat THEN state does not change', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.player.hp = 0

    const inputState = createInputState()
    const commands = mapInputToCommands(inputState)

    // First step triggers defeat
    runSortieSimulationStep(state, commands, FDT)

    expect(state.sortie.status).toBe('defeat')
    const snapshotTicks = state.sortie.elapsedTicks
    const snapshotResult = state.sortie.result

    // Call multiple times via orchestration gate
    for (let i = 0; i < 5; i++) {
      runSortieSimulationStep(state, commands, FDT)
    }

    expect(state.sortie.status).toBe('defeat')
    expect(state.sortie.elapsedTicks).toBe(snapshotTicks)
    expect(state.sortie.result).toBe(snapshotResult) // same reference
  })
})

// ---------------------------------------------------------------------------
// playerHpRemaining clamp
// ---------------------------------------------------------------------------
describe('GIVEN sortie result playerHpRemaining', () => {
  it('clamps playerHpRemaining to [0, maxHp] on victory', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.player.hp = state.player.maxHp + 999
    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)
    expect(state.sortie.result!.playerHpRemaining).toBe(state.player.maxHp)
  })

  it('records defeat playerHpRemaining as 0 even if hp is negative', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.player.hp = -5
    runSortieSystem(state, FDT)
    expect(state.sortie.result!.outcome).toBe('defeat')
    expect(state.sortie.result!.playerHpRemaining).toBe(0)
  })

  it('timeout defeat: playerHpRemaining is HP snapshot, not 0', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.player.hp = state.player.maxHp // player alive with full HP
    state.enemies.push(makeLiveEnemy(1))   // enemies remain → allEnemiesDefeated = false
    ;(state.sortie as { elapsedTicks: number }).elapsedTicks = TARGET_TICKS - 1
    runSortieSystem(state, FDT)
    expect(state.sortie.result!.outcome).toBe('defeat')
    expect(state.sortie.result!.playerHpRemaining).toBe(state.player.maxHp) // NOT 0
  })
})

// ---------------------------------------------------------------------------
// kills-boundary
// ---------------------------------------------------------------------------
describe('GIVEN enemies with various defeatedAtTick values', () => {
  it('kills-boundary: THEN only enemies with defeatedAtTick <= terminalTick count as kills', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)

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

    // All 3 enemies are defeated → allEnemiesDefeated = true → victory
    runSortieSystem(state, FDT)

    expect(state.sortie.result).not.toBeNull()
    expect(state.sortie.result!.outcome).toBe('victory')
    expect(state.sortie.result!.kills).toBe(2) // ids 1 and 2 only
  })
})

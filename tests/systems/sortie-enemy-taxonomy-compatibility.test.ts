import { describe, expect, it } from 'vitest'

import { createInputState, mapInputToCommands } from '../../src/input'
import { createInitialGameState, defaultSimulationConfig, type CollisionPair, type EnemyState, type GameState } from '../../src/state'
import { runCollisionSystem } from '../../src/systems/CollisionSystem'
import { runSortieSimulationStep, startSortie } from '../../src/systems/SortieSystem'

const FDT = defaultSimulationConfig.fixedDeltaMs

function makeEnemy(overrides: Partial<EnemyState> = {}): EnemyState {
  return {
    id: 1,
    definitionId: 'enemy-basic',
    hp: 5,
    maxHp: 5,
    x: 340,
    y: 270,
    radius: 12,
    speedPxPerSec: 60,
    contactDamage: 1,
    defeated: false,
    defeatedAtTick: null,
    faction: 'enemy',
    role: 'enemy_chaser',
    behaviorState: 'move_to_engage',
    targetingPolicy: 'focus_player',
    targetEntityId: 'player-alpha',
    ...overrides,
  }
}

function createNoAllySortieState(): GameState {
  const state = createInitialGameState()
  state.loopPhase = 'preparation'
  startSortie(state, FDT)
  // Compatibility-only fixture for #985 / SSOT §14.1.
  // startSortie() intentionally creates one default ally in production;
  // this test clears allies to freeze the legacy no-ally enemy_chaser contract.
  state.allies = []
  state.nextAllyId = 1
  state.commandIntentRuntime.activeIntent = 'none'
  state.commandIntentRuntime.bufferedIntent = null
  expect(state.allies.length).toBe(0)
  return state
}

function commandsForLane(lane: 'none' | 'sample_assist_player', tick: number) {
  const input = createInputState()
  if (lane === 'sample_assist_player' && tick === 0) {
    input.assistPlayerRisingEdge = true
  }
  return mapInputToCommands(input)
}

function runLaneTrace(lane: 'none' | 'sample_assist_player') {
  const state = createNoAllySortieState()
  state.enemies = [
    makeEnemy({ id: 1, x: 340, y: 270, speedPxPerSec: 60 }),
    makeEnemy({ id: 2, x: 240, y: 330, speedPxPerSec: 60 }),
  ]

  const trace: Array<Array<{ id: number; x: number; y: number }>> = []
  const activeIntents: string[] = []
  for (let tick = 0; tick < 3; tick += 1) {
    runSortieSimulationStep(state, commandsForLane(lane, tick), FDT)
    trace.push(
      state.enemies.map((enemy) => ({
        id: enemy.id,
        x: Number(enemy.x.toFixed(6)),
        y: Number(enemy.y.toFixed(6)),
      })),
    )
    activeIntents.push(state.commandIntentRuntime.activeIntent)
  }

  return { state, trace, activeIntents }
}

function previewCollisionPairs(lane: 'none' | 'sample_assist_player'): readonly CollisionPair[] {
  const state = createNoAllySortieState()
  if (lane === 'sample_assist_player') {
    state.commandIntentRuntime.activeIntent = 'assist_player'
    state.commandIntentRuntime.bufferedIntent = {
      intent: 'assist_player',
      sampledAtTick: 0,
      expiresAtTick: state.commandIntentRuntime.assistPlayerTtlTicks,
    }
  }
  state.enemies = [
    makeEnemy({ id: 2, x: 242, y: 270, speedPxPerSec: 60 }),
    makeEnemy({ id: 1, x: 241, y: 270, speedPxPerSec: 60 }),
  ]
  return runCollisionSystem(state)
}

function runTerminalLane(lane: 'none' | 'sample_assist_player') {
  const state = createNoAllySortieState()
  state.enemies = [
    makeEnemy({ id: 2, x: 242, y: 270, speedPxPerSec: 60, contactDamage: state.player.maxHp }),
    makeEnemy({ id: 1, x: 241, y: 270, speedPxPerSec: 60, contactDamage: state.player.maxHp }),
  ]
  runSortieSimulationStep(state, commandsForLane(lane, 0), FDT)
  return state
}

describe('sortie enemy taxonomy compatibility gate', () => {
  it('GIVEN no allies WHEN CommandIntent.none and sample_assist_player lanes run THEN enemy tick trace stays identical while command runtime may diverge', () => {
    const noneLane = runLaneTrace('none')
    const assistLane = runLaneTrace('sample_assist_player')

    expect(noneLane.state.allies.length).toBe(0)
    expect(assistLane.state.allies.length).toBe(0)
    expect(noneLane.trace).toEqual(assistLane.trace)
    expect(noneLane.trace).toEqual([
      [
        { id: 1, x: 339, y: 270 },
        { id: 2, x: 240, y: 329 },
      ],
      [
        { id: 1, x: 338, y: 270 },
        { id: 2, x: 240, y: 328 },
      ],
      [
        { id: 1, x: 337, y: 270 },
        { id: 2, x: 240, y: 327 },
      ],
    ])
    expect(noneLane.activeIntents).toEqual(['none', 'none', 'none'])
    expect(assistLane.activeIntents[0]).toBe('assist_player')
  })

  it('GIVEN no allies WHEN collision ordering and terminal defeat are compared across lanes THEN ordering and terminal tick stay unchanged', () => {
    const nonePairs = previewCollisionPairs('none')
    const assistPairs = previewCollisionPairs('sample_assist_player')

    expect(nonePairs.map((pair) => pair.kind === 'player-enemy' ? pair.enemyId : -1)).toEqual([1, 2])
    expect(assistPairs).toEqual(nonePairs)

    const noneLane = runTerminalLane('none')
    const assistLane = runTerminalLane('sample_assist_player')

    expect(noneLane.sortie.status).toBe('defeat')
    expect(noneLane.sortie.elapsedTicks).toBe(1)
    expect(noneLane.tick).toBe(1)
    expect(noneLane.sortie.result?.durationMs).toBe(FDT)
    expect(noneLane.sortie.result?.endReason).toBe('player_hp_zero')
    expect(assistLane.sortie.status).toBe('defeat')
    expect(assistLane.sortie.elapsedTicks).toBe(noneLane.sortie.elapsedTicks)
    expect(assistLane.tick).toBe(noneLane.tick)
    expect(assistLane.sortie.result?.durationMs).toBe(noneLane.sortie.result?.durationMs)
    expect(assistLane.sortie.result?.endReason).toBe(noneLane.sortie.result?.endReason)
  })
})

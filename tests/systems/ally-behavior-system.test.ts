import { describe, expect, it } from 'vitest'

import { createDefaultAllyState, createInitialGameState } from '../../src/state'
import { runAllyBehaviorSystem } from '../../src/systems'

describe('runAllyBehaviorSystem', () => {
  it('GIVEN CommandIntent.none acquires target deterministically WHEN one alive enemy exists THEN ally_basic keeps enemy:1 and moves toward it', () => {
    const state = createInitialGameState()
    state.allies = [createDefaultAllyState(1)]
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 300,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
      },
    ]

    runAllyBehaviorSystem(state, 1000)

    expect(state.allies[0].targetEntityId).toBe('enemy:1')
    expect(state.allies[0].behaviorState).toBe('move_to_engage')
    expect(state.allies[0].x).toBeCloseTo(276)
    expect(state.allies[0].y).toBe(270)
  })

  it('GIVEN CommandIntent.none acquires target deterministically WHEN equal-score enemies are reversed THEN selected targetEntityId remains stable', () => {
    const forward = createInitialGameState()
    forward.allies = [createDefaultAllyState(1)]
    forward.allies[0].x = 200
    forward.allies[0].y = 200
    forward.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 260,
        y: 200,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
      },
      {
        id: 2,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 260,
        y: 200,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
      },
    ]

    const reversed = createInitialGameState()
    reversed.allies = [createDefaultAllyState(1)]
    reversed.allies[0].x = 200
    reversed.allies[0].y = 200
    reversed.enemies = [...forward.enemies].reverse()

    runAllyBehaviorSystem(forward, 16)
    runAllyBehaviorSystem(reversed, 16)

    expect(forward.allies[0].targetEntityId).toBe('enemy:1')
    expect(reversed.allies[0].targetEntityId).toBe('enemy:1')
  })

  it('GIVEN stale target is cleared deterministically WHEN previous target is defeated THEN a valid enemy is reselected', () => {
    const state = createInitialGameState()
    state.allies = [createDefaultAllyState(1)]
    state.allies[0].targetEntityId = 'enemy:1'
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 0,
        maxHp: 5,
        x: 300,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: true,
        defeatedAtTick: 3,
      },
      {
        id: 2,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 260,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
      },
    ]

    runAllyBehaviorSystem(state, 1000)

    expect(state.allies[0].targetEntityId).toBe('enemy:2')
    expect(state.allies[0].behaviorState).toBe('move_to_engage')
  })

  it('GIVEN assist_player is active WHEN a player-near threat is farther from ally THEN ally_basic prioritizes that threat', () => {
    const state = createInitialGameState()
    state.allies = [createDefaultAllyState(1)]
    state.allies[0].x = 80
    state.allies[0].y = 270
    state.commandIntentRuntime.activeIntent = 'assist_player'
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 130,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
      },
      {
        id: 2,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 250,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
      },
    ]

    runAllyBehaviorSystem(state, 16)

    expect(state.allies[0].targetingPolicy).toBe('assist_player_threat')
    expect(state.allies[0].targetEntityId).toBe('enemy:2')
  })

  it('GIVEN assist_player has ended WHEN nearest_hostile becomes active again THEN ally_basic returns to nearest-hostile selection', () => {
    const state = createInitialGameState()
    state.allies = [createDefaultAllyState(1)]
    state.allies[0].x = 80
    state.allies[0].y = 270
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 130,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
      },
      {
        id: 2,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 250,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
      },
    ]

    state.commandIntentRuntime.activeIntent = 'assist_player'
    runAllyBehaviorSystem(state, 16)
    expect(state.allies[0].targetEntityId).toBe('enemy:2')

    state.commandIntentRuntime.activeIntent = 'none'
    state.allies[0].targetEntityId = null
    state.allies[0].x = 80
    state.allies[0].y = 270

    runAllyBehaviorSystem(state, 16)

    expect(state.allies[0].targetingPolicy).toBe('nearest_hostile')
    expect(state.allies[0].targetEntityId).toBe('enemy:1')
  })

  it('GIVEN no enemies WHEN ally behavior runs THEN target is cleared, ally becomes inactive, and position stays unchanged', () => {
    const state = createInitialGameState()
    state.allies = [createDefaultAllyState(1)]
    state.allies[0].x = 180
    state.allies[0].y = 220
    state.allies[0].targetEntityId = 'enemy:99'

    runAllyBehaviorSystem(state, 16)

    expect(state.allies[0].targetEntityId).toBeNull()
    expect(state.allies[0].behaviorState).toBe('inactive')
    expect(state.allies[0].x).toBe(180)
    expect(state.allies[0].y).toBe(220)
  })

  it('GIVEN ally emits no projectile and no damage WHEN ally behavior runs THEN projectile list and HP stay unchanged', () => {
    const state = createInitialGameState()
    state.allies = [createDefaultAllyState(1)]
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 260,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
      },
    ]
    const playerHpBefore = state.player.hp
    const enemyHpBefore = state.enemies[0].hp

    runAllyBehaviorSystem(state, 1000)

    expect(state.projectiles).toHaveLength(0)
    expect(state.player.hp).toBe(playerHpBefore)
    expect(state.enemies[0].hp).toBe(enemyHpBefore)
  })
})

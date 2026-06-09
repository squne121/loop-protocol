import { describe, expect, it } from 'vitest'
import { createInputState, mapInputToCommands } from '../src/input'
import { createInitialGameState } from '../src/state/GameState'
import type { EnemyState } from '../src/state/GameState'
import { defaultSimulationConfig } from '../src/state/SimulationConfig'
import {
  claimPendingReward,
  runSortieSimulationStep,
  runSortieSystem,
  SORTIE_DURATION_MS,
  startSortie,
} from '../src/systems/SortieSystem'

const FDT = defaultSimulationConfig.fixedDeltaMs
const TARGET_TICKS = Math.ceil(SORTIE_DURATION_MS / FDT)

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

describe('GIVEN bootstrap preparation', () => {
  it("bootstrap preparation: WHEN createInitialGameState called THEN loopPhase='preparation' and sortie is idle", () => {
    const state = createInitialGameState()

    expect(state.loopPhase).toBe('preparation')
    expect(state.pendingRewardApplicationId).toBeNull()
    expect(state.sortie.status).toBe('idle')
    expect(state.sortie.result).toBeNull()
  })

  it('preparation before start: WHEN runSortieSimulationStep called before startSortie THEN state does not advance outside running', () => {
    const state = createInitialGameState()
    const commands = mapInputToCommands(createInputState())

    runSortieSimulationStep(state, commands, FDT)

    expect(state.loopPhase).toBe('preparation')
    expect(state.sortie.status).toBe('idle')
    expect(state.tick).toBe(0)
    expect(state.elapsedMs).toBe(0)
  })
})

describe('GIVEN startSortie from preparation', () => {
  it('WHEN startSortie called THEN loopPhase=running and sortie enters running state', () => {
    const state = createInitialGameState()

    startSortie(state, FDT)

    expect(state.loopPhase).toBe('running')
    expect(state.pendingRewardApplicationId).toBeNull()
    expect(state.sortie.status).toBe('running')
    expect(state.sortie.elapsedTicks).toBe(0)
  })
})

describe('GIVEN a terminal outcome', () => {
  it('WHEN player HP reaches zero THEN defeat also enters debrief_pending_reward', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.player.hp = 0

    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('defeat')
    expect(state.sortie.result?.outcome).toBe('defeat')
    expect(state.loopPhase).toBe('debrief_pending_reward')
    expect(state.pendingRewardApplicationId).toBe('sortie-reward-1')
  })

  it('timeout transition: WHEN elapsedTicks reaches targetTicks with enemies remaining THEN debrief_pending_reward is entered', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.enemies.push(makeLiveEnemy(1))

    ;(state.sortie as { elapsedTicks: number }).elapsedTicks = TARGET_TICKS - 1
    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('timeout')
    expect(state.sortie.result?.outcome).toBe('timeout')
    expect(state.loopPhase).toBe('debrief_pending_reward')
    expect(state.pendingRewardApplicationId).not.toBeNull()
  })

  it('WHEN victory occurs THEN debrief_pending_reward is entered with a single pendingRewardApplicationId', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))

    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('victory')
    expect(state.loopPhase).toBe('debrief_pending_reward')
    expect(state.pendingRewardApplicationId).toMatch(/^sortie-/)
  })
})

describe('GIVEN debrief_pending_reward', () => {
  it('same-token no-op: WHEN claimPendingReward called twice THEN second call is already-claimed and resources stay unchanged', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)

    const beforeFirstClaim = state.progress.resources
    const pendingRewardApplicationId = state.pendingRewardApplicationId
    const firstClaim = claimPendingReward(state)
    const afterFirstClaim = state.progress.resources
    const secondClaim = claimPendingReward(state)

    expect(firstClaim.ok).toBe(true)
    expect(afterFirstClaim).toBeGreaterThan(beforeFirstClaim)
    expect(state.loopPhase).toBe('debrief_reward_claimed')
    expect(state.pendingRewardApplicationId).toBe(pendingRewardApplicationId)
    expect(secondClaim).toEqual({ ok: false, reason: 'already-claimed' })
    expect(state.progress.resources).toBe(afterFirstClaim)
  })

  it('terminal halt: WHEN claim succeeds THEN runSortieSimulationStep does not advance after claim', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)
    const claim = claimPendingReward(state)
    expect(claim.ok).toBe(true)

    const commands = mapInputToCommands(createInputState())
    const snapshot = {
      tick: state.tick,
      elapsedMs: state.elapsedMs,
      shotsFired: state.player.shotsFired,
      resources: state.progress.resources,
    }

    runSortieSimulationStep(state, commands, FDT)

    expect(state.loopPhase).toBe('debrief_reward_claimed')
    expect(state.tick).toBe(snapshot.tick)
    expect(state.elapsedMs).toBe(snapshot.elapsedMs)
    expect(state.player.shotsFired).toBe(snapshot.shotsFired)
    expect(state.progress.resources).toBe(snapshot.resources)
  })
})

describe('GIVEN debrief_reward_claimed', () => {
  it('next sortie reset: WHEN startSortie called THEN resets combat runtime and preserves progression', () => {
    const state = createInitialGameState()
    state.progress.resources = 123
    state.progress.weaponPower = 7
    state.player.maxHp = 11
    state.player.hp = 11
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))
    state.projectiles.push({
      id: 1,
      x: 10,
      y: 10,
      radius: 2,
      directionX: 1,
      directionY: 0,
      speedPxPerSec: 120,
      ageMs: 100,
      lifetimeMs: 500,
      damage: 1,
    })
    state.player.shotsFired = 7
    runSortieSystem(state, FDT)
    const claim = claimPendingReward(state)
    expect(claim.ok).toBe(true)

    const resourcesAfterClaim = state.progress.resources
    const weaponPower = state.progress.weaponPower
    const maxHp = state.player.maxHp
    startSortie(state, FDT)

    expect(state.loopPhase).toBe('running')
    expect(state.pendingRewardApplicationId).toBeNull()
    expect(state.sortie.status).toBe('running')
    expect(state.sortie.elapsedTicks).toBe(0)
    expect(state.projectiles).toHaveLength(0)
    expect(state.enemies).toHaveLength(0)
    expect(state.player.shotsFired).toBe(0)
    expect(state.player.hp).toBe(maxHp)
    expect(state.player.maxHp).toBe(maxHp)
    expect(state.progress.resources).toBe(resourcesAfterClaim)
    expect(state.progress.weaponPower).toBe(weaponPower)
  })
})

describe('GIVEN terminal combat states', () => {
  it('does not advance outside running: WHEN sortie status is terminal THEN runSortieSimulationStep is a no-op', () => {
    const state = createInitialGameState()
    startSortie(state, FDT)
    state.player.hp = 0
    runSortieSystem(state, FDT)

    const commands = mapInputToCommands(createInputState())
    const tickBefore = state.tick
    const elapsedBefore = state.elapsedMs

    runSortieSimulationStep(state, commands, FDT)

    expect(state.loopPhase).toBe('debrief_pending_reward')
    expect(state.tick).toBe(tickBefore)
    expect(state.elapsedMs).toBe(elapsedBefore)
  })
})

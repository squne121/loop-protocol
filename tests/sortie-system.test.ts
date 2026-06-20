import { describe, expect, it } from 'vitest'
import { createInputState, mapInputToCommands } from '../src/input'
import { createInitialGameState } from '../src/state/GameState'
import type { EnemyState, SortieResult } from '../src/state/GameState'
import { defaultSimulationConfig } from '../src/state/SimulationConfig'
import {
  claimPendingReward,
  confirmResult,
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

function expectPendingReward(state: ReturnType<typeof createInitialGameState>): void {
  // AC4: running → result (not debrief_pending_reward) after combat ends
  expect(state.loopPhase).toBe('result')
  expect(state.resultRewardStatus).toBe('pending')
  expect(state.pendingRewardApplicationId).not.toBeNull()
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

describe('GIVEN bootstrap state', () => {
  it("bootstrap: WHEN createInitialGameState called THEN loopPhase='preparation' and sortie is idle (AC1)", () => {
    const state = createInitialGameState()

    // createInitialGameState starts in preparation; app shell may transition to title_menu
    expect(state.loopPhase).toBe('preparation')
    expect(state.resultRewardStatus).toBe('pending')
    expect(state.pendingRewardApplicationId).toBeNull()
    expect(state.sortie.status).toBe('idle')
    expect(state.sortie.result).toBeNull()
  })

  it('AC1: LoopPhase type includes all required phases', () => {
    // Type-level check: all required phases are assignable to LoopPhase
    const phases: Array<import('../src/state').LoopPhase> = [
      'title_menu', 'load_menu', 'preparation', 'running', 'result',
    ]
    expect(phases).toHaveLength(5)
  })

  it('AC7: WHEN runSortieSimulationStep called from title_menu THEN state does not advance', () => {
    const state = createInitialGameState()
    state.loopPhase = 'title_menu'
    const commands = mapInputToCommands(createInputState())

    runSortieSimulationStep(state, commands, FDT)

    expect(state.loopPhase).toBe('title_menu')
    expect(state.sortie.status).toBe('idle')
    expect(state.tick).toBe(0)
    expect(state.elapsedMs).toBe(0)
  })

  it('AC7: WHEN startSortie called from title_menu THEN no-op (state mutation blocked)', () => {
    const state = createInitialGameState()
    state.loopPhase = 'title_menu'

    startSortie(state, FDT)

    expect(state.loopPhase).toBe('title_menu')
    expect(state.sortie.status).toBe('idle')
  })
})

describe('GIVEN startSortie from preparation', () => {
  it('WHEN startSortie called from preparation THEN loopPhase=running and sortie enters running state (AC7)', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    state.commandIntentRuntime.activeIntent = 'assist_player'
    state.commandIntentRuntime.bufferedIntent = {
      intent: 'assist_player',
      sampledAtTick: 1,
      expiresAtTick: 9,
    }

    startSortie(state, FDT)

    expect(state.loopPhase).toBe('running')
    expect(state.pendingRewardApplicationId).toBeNull()
    expect(state.sortie.status).toBe('running')
    expect(state.sortie.elapsedTicks).toBe(0)
    expect(state.commandIntentRuntime.activeIntent).toBe('none')
    expect(state.commandIntentRuntime.bufferedIntent).toBeNull()
    expect(state.allies).toHaveLength(1)
    expect(state.allies[0].role).toBe('ally_basic')
    expect(state.allies[0].faction).toBe('ally')
    expect(state.nextAllyId).toBe(2)
  })

  it('GIVEN one enemy and one ally WHEN runSortieSimulationStep runs THEN ally behavior is integrated after runEnemyAISystem and before runCombatSystem', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies = [makeLiveEnemy(1)]
    state.enemies[0].x = 340
    state.enemies[0].y = 270
    state.enemies[0].speedPxPerSec = 60
    state.allies[0].x = 140
    state.allies[0].y = 270
    state.allies[0].speedPxPerSec = 60

    const commands = mapInputToCommands(createInputState())

    runSortieSimulationStep(state, commands, 1000)

    expect(state.enemies[0].x).toBeCloseTo(280)
    expect(state.allies[0].targetEntityId).toBe('enemy:1')
    expect(state.allies[0].x).toBeCloseTo(200)
    expect(state.projectiles).toHaveLength(0)
    expect(state.player.hp).toBe(state.player.maxHp)
  })
})

describe('GIVEN a terminal outcome', () => {
  it('WHEN player HP reaches zero THEN defeat transitions to result phase with pending reward (AC4)', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.player.hp = 0

    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('defeat')
    expect(state.sortie.result?.outcome).toBe('defeat')
    expectPendingReward(state)
    expect(state.pendingRewardApplicationId).toBe('sortie-reward-1')
  })

  it('timeout transition: WHEN elapsedTicks reaches targetTicks with enemies remaining THEN result phase is entered (AC4)', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies.push(makeLiveEnemy(1))

    ;(state.sortie as { elapsedTicks: number }).elapsedTicks = TARGET_TICKS - 1
    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('timeout')
    expect(state.sortie.result?.outcome).toBe('timeout')
    expectPendingReward(state)
  })

  it('WHEN victory occurs THEN result phase is entered with a single pendingRewardApplicationId (AC4)', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))

    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('victory')
    expectPendingReward(state)
    expect(state.pendingRewardApplicationId).toMatch(/^sortie-/)
  })

  it('same-tick defeat precedence: WHEN defeat, victory, and timeout all happen together THEN defeat wins', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))
    state.player.hp = 0
    state.sortie.elapsedTicks = TARGET_TICKS - 1

    runSortieSystem(state, FDT)

    expect(state.sortie.result?.outcome).toBe('defeat')
    expect(state.sortie.result?.endReason).toBe('player_hp_zero')
    expectPendingReward(state)
  })

  it('victory-over-timeout: WHEN all enemies are defeated on the timeout tick with player alive THEN victory wins', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))
    state.sortie.elapsedTicks = TARGET_TICKS - 1

    runSortieSystem(state, FDT)

    expect(state.sortie.result?.outcome).toBe('victory')
    expect(state.sortie.result?.endReason).toBe('all_enemies_defeated')
    expectPendingReward(state)
  })

  it('vacuous truth: WHEN no enemies exist THEN victory does not trigger', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)

    runSortieSystem(state, FDT)

    expect(state.sortie.status).toBe('running')
    expect(state.sortie.result).toBeNull()
    expect(state.pendingRewardApplicationId).toBeNull()
  })

  it('result exactly-once: WHEN runSortieSystem is called again after terminal THEN result reference and pending ID stay stable', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))

    runSortieSystem(state, FDT)
    const firstResult = state.sortie.result
    const firstPendingRewardApplicationId = state.pendingRewardApplicationId
    runSortieSystem(state, FDT)

    expect(state.sortie.result).toBe(firstResult)
    expect(state.pendingRewardApplicationId).toBe(firstPendingRewardApplicationId)
  })

  it('timer authority: WHEN elapsedMs disagrees with elapsedTicks THEN terminal duration uses elapsedTicks', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.elapsedMs = 99999
    state.enemies.push(makeDefeatedEnemy(1, 0))

    runSortieSystem(state, FDT)

    expect(state.sortie.result?.durationMs).toBeCloseTo(state.sortie.elapsedTicks * FDT)
  })

  it('kills boundary: WHEN defeatedAtTick exceeds terminalTick THEN that enemy does not count as a kill', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.tick = 42
    state.enemies.push(makeDefeatedEnemy(1, 40))
    state.enemies.push(makeDefeatedEnemy(2, 42))
    state.enemies.push(makeDefeatedEnemy(3, 43))

    runSortieSystem(state, FDT)

    expect(state.sortie.result?.outcome).toBe('victory')
    expect(state.sortie.result?.kills).toBe(2)
  })

  it('playerHpRemaining clamp: WHEN terminal result samples HP THEN it is clamped into [0, maxHp]', () => {
    const victoryState = createInitialGameState()
    victoryState.loopPhase = 'preparation'
    startSortie(victoryState, FDT)
    victoryState.player.hp = victoryState.player.maxHp + 999
    victoryState.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(victoryState, FDT)
    expect(victoryState.sortie.result?.playerHpRemaining).toBe(victoryState.player.maxHp)

    const defeatState = createInitialGameState()
    defeatState.loopPhase = 'preparation'
    startSortie(defeatState, FDT)
    defeatState.player.hp = -5
    runSortieSystem(defeatState, FDT)
    expect(defeatState.sortie.result?.outcome).toBe('defeat')
    expect(defeatState.sortie.result?.playerHpRemaining).toBe(0)
  })

  it('runSortieSystem phase gate: WHEN loopPhase is not running but sortie.status is running THEN system is a no-op', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.loopPhase = 'preparation'
    state.enemies.push(makeDefeatedEnemy(1, 0))
    const snapshot = {
      elapsedTicks: state.sortie.elapsedTicks,
      result: state.sortie.result,
      pendingRewardApplicationId: state.pendingRewardApplicationId,
    }

    runSortieSystem(state, FDT)

    expect(state.sortie.elapsedTicks).toBe(snapshot.elapsedTicks)
    expect(state.sortie.result).toBe(snapshot.result)
    expect(state.pendingRewardApplicationId).toBe(snapshot.pendingRewardApplicationId)
    expect(state.sortie.status).toBe('running')
  })
})

describe('GIVEN result phase (AC4, AC5, AC10)', () => {
  it('same-token no-op: WHEN claimPendingReward called twice in result phase THEN second call is already-claimed and resources stay unchanged', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)

    expect(state.loopPhase).toBe('result')
    expect(state.resultRewardStatus).toBe('pending')

    const beforeFirstClaim = state.progress.resources
    const pendingRewardApplicationId = state.pendingRewardApplicationId
    const firstClaim = claimPendingReward(state)
    const afterFirstClaim = state.progress.resources
    const secondClaim = claimPendingReward(state)

    expect(firstClaim.ok).toBe(true)
    expect(afterFirstClaim).toBeGreaterThan(beforeFirstClaim)
    // AC10: stays in result phase after claim; resultRewardStatus becomes 'claimed'
    expect(state.loopPhase).toBe('result')
    expect(state.resultRewardStatus).toBe('claimed')
    expect(state.pendingRewardApplicationId).toBe(pendingRewardApplicationId)
    expect(secondClaim).toEqual({ ok: false, reason: 'already-claimed' })
    expect(state.progress.resources).toBe(afterFirstClaim)
  })

  it('claimed phase invariant: WHEN ledger is missing for a claimed result token THEN reward is not applied again', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)
    const pendingRewardApplicationId = state.pendingRewardApplicationId
    expect(pendingRewardApplicationId).not.toBeNull()

    state.resultRewardStatus = 'claimed'
    delete state.rewardClaims.claimedApplicationIds[pendingRewardApplicationId as string]
    const resourcesBefore = state.progress.resources

    const claim = claimPendingReward(state)

    expect(claim).toEqual({ ok: false, reason: 'claimed-phase-ledger-miss' })
    expect(state.progress.resources).toBe(resourcesBefore)
  })

  it('terminal halt: WHEN claim succeeds THEN runSortieSimulationStep does not advance after claim', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
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

    expect(state.loopPhase).toBe('result')
    expect(state.tick).toBe(snapshot.tick)
    expect(state.elapsedMs).toBe(snapshot.elapsedMs)
    expect(state.player.shotsFired).toBe(snapshot.shotsFired)
    expect(state.progress.resources).toBe(snapshot.resources)
  })

  it('AC5: WHEN confirmResult called from result phase THEN transitions to preparation', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)
    claimPendingReward(state)

    confirmResult(state)

    expect(state.loopPhase).toBe('preparation')
  })

  it('AC5: WHEN confirmResult called from non-result phase THEN no-op', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'

    confirmResult(state)

    expect(state.loopPhase).toBe('preparation')
  })

  it('AC7: WHEN startSortie called from result phase THEN no-op (state mutation blocked)', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))
    runSortieSystem(state, FDT)

    expect(state.loopPhase).toBe('result')
    const tickBefore = state.tick

    startSortie(state, FDT)

    // AC7: startSortie from result phase must be a no-op
    expect(state.loopPhase).toBe('result')
    expect(state.tick).toBe(tickBefore)
    expect(state.sortie.status).not.toBe('running')
  })
})

describe('GIVEN result → preparation → next sortie flow (AC5)', () => {
  it('next sortie reset: WHEN result confirmed then startSortie called THEN resets combat runtime and preserves progression', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
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
    expect(state.loopPhase).toBe('result')
    const claim = claimPendingReward(state)
    expect(claim.ok).toBe(true)
    expect(state.loopPhase).toBe('result')

    // AC5: confirm result → preparation
    confirmResult(state)
    expect(state.loopPhase).toBe('preparation')

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

  it('reward application ID skips claimed collisions: WHEN ledger already contains the next sequence token THEN a fresh token is generated', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    state.rewardClaims.claimedApplicationIds['sortie-reward-1'] = true
    state.nextRewardApplicationSequence = 1
    startSortie(state, FDT)
    state.enemies.push(makeDefeatedEnemy(1, 0))

    runSortieSystem(state, FDT)

    expect(state.pendingRewardApplicationId).toBe('sortie-reward-2')
    expect(state.nextRewardApplicationSequence).toBe(3)
  })
})

describe('GIVEN terminal combat states', () => {
  it('does not advance outside running: WHEN sortie status is terminal THEN runSortieSimulationStep is a no-op', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.player.hp = 0
    runSortieSystem(state, FDT)

    const commands = mapInputToCommands(createInputState())
    const tickBefore = state.tick
    const elapsedBefore = state.elapsedMs

    runSortieSimulationStep(state, commands, FDT)

    // AC4: result phase (not debrief_pending_reward)
    expect(state.loopPhase).toBe('result')
    expect(state.tick).toBe(tickBefore)
    expect(state.elapsedMs).toBe(elapsedBefore)
  })
})

void Object.freeze({
  outcome: 'victory',
  endReason: 'all_enemies_defeated',
  durationMs: 0,
  kills: 0,
  shotsFired: 0,
  playerHpRemaining: 1,
} satisfies SortieResult)

void Object.freeze({
  outcome: 'defeat',
  endReason: 'player_hp_zero',
  durationMs: 1000,
  kills: 0,
  shotsFired: 0,
  playerHpRemaining: 0,
} satisfies SortieResult)

void Object.freeze({
  outcome: 'timeout',
  endReason: 'timeout',
  durationMs: 30000,
  kills: 0,
  shotsFired: 0,
  playerHpRemaining: 1,
} satisfies SortieResult)

// @ts-expect-error outcome: 'victory' に endReason: 'timeout' は型エラー
void ({
  outcome: 'victory',
  endReason: 'timeout',
  durationMs: 0,
  kills: 0,
  shotsFired: 0,
  playerHpRemaining: 1,
} satisfies SortieResult)

import {
  createDefaultAllyState,
  resetCommandIntentRuntime,
  sampleAssistPlayerIntent,
  tickCommandIntentRuntime,
  type GameState,
  type RewardApplicationId,
  type SortieResult,
  type SortieState,
} from '../state/GameState'
import { mapInputToCommands } from '../input'
import {
  beginPlaytestEvidenceSortie,
  markTerminalState,
  nextPlaytestCommandSequence,
  recordAllySurvival,
  recordCommandNoop,
  recordCommandUse,
  recordLocalThreatSample,
  setSelfExplanationPrompt,
} from '../playtest/assistPlayerEventLog'
import type { PlaytestEvidenceRuntimeState } from '../playtest/assistPlayerEventLog'
import { runMovementSystem } from './MovementSystem'
import { runEnemySpawnSystem } from './EnemySpawnSystem'
import { runEnemyAISystem } from './EnemyAISystem'
import { runAllyBehaviorSystem } from './AllyBehaviorSystem'
import { runCombatSystem } from './CombatSystem'
import { runProjectileSystem } from './ProjectileSystem'
import { runCollisionSystem } from './CollisionSystem'
import { resolveCombatCollisions } from './CombatSystem'
import { RewardSystem } from './RewardSystem'
import { resolvePhaseTransition } from './PhaseTransitionSystem'

/** Total sortie duration in milliseconds (30 seconds). */
export const SORTIE_DURATION_MS = 30_000
const LOCAL_THREAT_RADIUS_PX = 60

function countLocalThreats(state: GameState): number {
  return state.enemies.filter((enemy) => {
    if (enemy.defeated) {
      return false
    }
    const dx = enemy.x - state.player.x
    const dy = enemy.y - state.player.y
    return dx * dx + dy * dy <= LOCAL_THREAT_RADIUS_PX * LOCAL_THREAT_RADIUS_PX
  }).length
}

function resolveSelfExplanationPrompt(
  terminalState: SortieResult['outcome'],
): string {
  switch (terminalState) {
    case 'victory':
      return 'Victory secured. What changed the battle outcome most, and why?'
    case 'defeat':
      return 'Defeat logged. What changed the battle outcome most, and why?'
    case 'timeout':
      return 'Timeout reached. What changed the battle outcome most, and why?'
  }
}

function buildRewardApplicationId(state: GameState): RewardApplicationId {
  let nextSequence = Math.max(1, state.nextRewardApplicationSequence)
  let applicationId: RewardApplicationId = `sortie-reward-${nextSequence}`

  while (Object.prototype.hasOwnProperty.call(state.rewardClaims.claimedApplicationIds, applicationId)) {
    nextSequence += 1
    applicationId = `sortie-reward-${nextSequence}`
  }

  state.nextRewardApplicationSequence = nextSequence + 1
  return applicationId
}

function resetCombatRuntime(state: GameState): void {
  state.tick = 0
  state.elapsedMs = 0
  state.player.x = 240
  state.player.y = 270
  state.player.hp = state.player.maxHp
  state.player.aimX = 540
  state.player.aimY = 270
  state.player.weaponCooldownMs = 0
  state.player.shotsFired = 0
  state.player.lastAimDirectionX = 1
  state.player.lastAimDirectionY = 0
  state.projectiles = []
  state.nextProjectileId = 1
  state.enemies = []
  state.nextEnemyId = 1
  state.allies = [createDefaultAllyState(1)]
  state.nextAllyId = 2
  resetCommandIntentRuntime(state.commandIntentRuntime)
}

export function claimPendingReward(
  state: GameState,
): ReturnType<typeof RewardSystem.claim> | { ok: false; reason: 'no-pending-reward' | 'claimed-phase-ledger-miss' } {
  const applicationId = state.pendingRewardApplicationId
  if (applicationId === null || state.sortie.result === null) {
    return { ok: false, reason: 'no-pending-reward' }
  }

  // Legacy debrief phases: support existing claimed state
  if (state.loopPhase === 'debrief_reward_claimed') {
    if (Object.prototype.hasOwnProperty.call(state.rewardClaims.claimedApplicationIds, applicationId)) {
      return { ok: false, reason: 'already-claimed' }
    }

    return { ok: false, reason: 'claimed-phase-ledger-miss' }
  }

  // New result phase: reward is pending or already claimed
  if (state.loopPhase === 'result') {
    if (state.resultRewardStatus === 'claimed') {
      if (Object.prototype.hasOwnProperty.call(state.rewardClaims.claimedApplicationIds, applicationId)) {
        return { ok: false, reason: 'already-claimed' }
      }
      return { ok: false, reason: 'claimed-phase-ledger-miss' }
    }

    const claimResult = RewardSystem.claim(state, applicationId, state.sortie.result)
    if (!claimResult.ok && claimResult.reason !== 'already-claimed') {
      return claimResult
    }

    state.resultRewardStatus = 'claimed'
    // Stay in result phase — transition to preparation happens via confirmResult()
    return claimResult
  }

  if (state.loopPhase !== 'debrief_pending_reward') {
    return { ok: false, reason: 'no-pending-reward' }
  }

  const claimResult = RewardSystem.claim(state, applicationId, state.sortie.result)
  if (!claimResult.ok && claimResult.reason !== 'already-claimed') {
    return claimResult
  }

  const transition = resolvePhaseTransition(state.loopPhase, 'debrief_reward_claimed')
  if (transition.ok) {
    state.loopPhase = transition.nextPhase
  }
  return claimResult
}

/**
 * Confirms the result screen. If reward is still pending, auto-claims it first (B3).
 * Then transitions from 'result' to 'preparation' (AC5).
 * No-op if called from any other phase.
 *
 * Caller is responsible for calling storage.save() after this returns,
 * since save must occur in preparation phase (AC2, AC8).
 */
export function confirmResult(state: GameState): boolean {
  if (state.loopPhase !== 'result') {
    return false
  }

  // B3: auto-claim pending reward before transitioning to preparation
  if (state.resultRewardStatus === 'pending') {
    const applicationId = state.pendingRewardApplicationId
    if (applicationId !== null && state.sortie.result !== null) {
      const claimResult = RewardSystem.claim(state, applicationId, state.sortie.result)
      if (claimResult.ok || claimResult.reason === 'already-claimed') {
        state.resultRewardStatus = 'claimed'
      }
    }
  }

  const transition = resolvePhaseTransition(state.loopPhase, 'preparation')
  if (!transition.ok) {
    return false
  }

  state.loopPhase = transition.nextPhase
  return true
}


/**
 * Initialises the sortie state machine from `preparation` to `running` (AC7).
 * No-op and state-mutation-free if called from any other phase.
 *
 * @param state        Mutable game state
 * @param fixedDeltaMs Fixed timestep in milliseconds (used to compute targetTicks)
 */
export function startSortie(state: GameState, fixedDeltaMs: number): boolean {
  const transition = resolvePhaseTransition(state.loopPhase, 'running')
  if (!transition.ok) {
    return false
  }

  resetCombatRuntime(state)
  const targetTicks = Math.ceil(SORTIE_DURATION_MS / fixedDeltaMs)

  state.loopPhase = transition.nextPhase
  state.pendingRewardApplicationId = null
  beginPlaytestEvidenceSortie(state.playtestEvidenceRuntime)
  state.sortie = {
    status: 'running',
    elapsedTicks: 0,
    targetTicks,
    result: null,
  }
  return true
}

/**
 * Advances the sortie state machine by one fixed timestep.
 *
 * Terminal condition priority: defeat > victory > timeout
 * - defeat:  `player.hp <= 0`
 * - victory: all spawned enemies defeated (`state.enemies.length > 0 && every(e => e.defeated)`)
 *            `state.enemies.length > 0` guard prevents vacuous truth when no enemies have spawned yet.
 * - timeout: `elapsedTicks >= targetTicks` with enemies remaining → emits timeout
 * - `durationMs` is derived from `elapsedTicks * fixedDeltaMs` — never from `state.elapsedMs`.
 *
 * Terminal gate: if `status` is not `'running'`, the function returns immediately without mutation.
 *
 * @param state        Mutable game state
 * @param fixedDeltaMs Fixed timestep in milliseconds
 */
export function runSortieSystem(state: GameState, fixedDeltaMs: number): void {
  // AC13: terminal gate — only advance if running
  if (state.loopPhase !== 'running' || state.sortie.status !== 'running') {
    return
  }

  // Increment elapsed ticks
  state.sortie.elapsedTicks += 1

  const elapsedTicks = state.sortie.elapsedTicks
  const targetTicks = state.sortie.targetTicks

  // Capture terminalTick consistent with #488: defeatedAtTick = state.tick before increment
  const terminalTick = state.tick

  // Priority: defeat (player.hp <= 0) > victory (all enemies defeated) > timeout (30s → timeout)
  const isDefeat = state.player.hp <= 0
  const allEnemiesDefeated =
    state.enemies.length > 0 && state.enemies.every((e) => e.defeated)
  const isVictory = allEnemiesDefeated
  const isTimeout = elapsedTicks >= targetTicks

  if (!isDefeat && !isVictory && !isTimeout) {
    return
  }

  const kills = state.enemies.filter(
    (e) =>
      e.defeated &&
      e.defeatedAtTick !== null &&
      e.defeatedAtTick <= terminalTick,
  ).length

  const durationMs = elapsedTicks * fixedDeltaMs
  const shotsFired = state.player.shotsFired

  // Build a narrowed SortieResult with correct outcome/endReason pairing.
  // Priority: defeat (player.hp <= 0) > victory (all enemies defeated) > timeout (30s → timeout).
  // Each branch returns a concrete union member so TypeScript can verify the discriminated union.
  let result: SortieResult
  if (isDefeat) {
    // player_hp_zero: playerHpRemaining is always 0
    result = Object.freeze({
      outcome: 'defeat',
      endReason: 'player_hp_zero',
      durationMs,
      kills,
      shotsFired,
      playerHpRemaining: 0,
    } satisfies SortieResult)
  } else if (isVictory) {
    // all_enemies_defeated: retain actual HP snapshot
    const playerHpRemaining = Math.min(state.player.maxHp, Math.max(0, state.player.hp))
    result = Object.freeze({
      outcome: 'victory',
      endReason: 'all_enemies_defeated',
      durationMs,
      kills,
      shotsFired,
      playerHpRemaining,
    } satisfies SortieResult)
  } else {
    // timeout: retain actual HP snapshot
    const playerHpRemaining = Math.min(state.player.maxHp, Math.max(0, state.player.hp))
    result = Object.freeze({
      outcome: 'timeout',
      endReason: 'timeout',
      durationMs,
      kills,
      shotsFired,
      playerHpRemaining,
    } satisfies SortieResult)
  }

  const terminalState: SortieState = {
    status: result.outcome,
    elapsedTicks,
    targetTicks,
    result,
  }

  state.sortie = terminalState
  const transition = resolvePhaseTransition(state.loopPhase, 'result')
  if (!transition.ok) {
    return
  }

  // AC4: transition to result phase, not directly to next sortie
  state.loopPhase = transition.nextPhase
  state.resultRewardStatus = 'pending'
  markTerminalState(state.playtestEvidenceRuntime, result.outcome)
  setSelfExplanationPrompt(
    state.playtestEvidenceRuntime,
    resolveSelfExplanationPrompt(result.outcome),
  )
  recordAllySurvival(state.playtestEvidenceRuntime, {
    tick: terminalTick,
    commandSeq: Math.max(0, state.playtestEvidenceRuntime.nextCommandSequence - 1),
    sortieId: state.playtestEvidenceRuntime.currentSortieId,
    alliesSpawned: Math.max(0, state.nextAllyId - 1),
    alliesSurvived: state.allies.length,
    protectedZoneStable: result.outcome !== 'defeat',
  })
  if (state.pendingRewardApplicationId === null) {
    state.pendingRewardApplicationId = buildRewardApplicationId(state)
  }
}

/**
 * Orchestrates one full simulation step: all sub-systems + sortie state machine.
 * Used by `src/main.ts` to avoid logic duplication.
 *
 * @param state        Mutable game state
 * @param commands     Input commands for this tick
 * @param fixedDeltaMs Fixed timestep in milliseconds
 */
export function runSortieSimulationStep(
  state: GameState,
  commands: ReturnType<typeof mapInputToCommands>,
  fixedDeltaMs: number,
): void {
  const evidence: PlaytestEvidenceRuntimeState = state.playtestEvidenceRuntime
  const sampledAssistPlayer = commands.some((command) => command.type === 'sample_assist_player')
  const inCombat = state.loopPhase === 'running' && state.sortie.status === 'running'

  // B3 (not_combat): a command attempt that arrives while the sortie is not in
  // the running phase can never be accepted. Record it before the phase gate so
  // the deterministic log surfaces the rejected attempt rather than dropping it.
  if (!inCombat) {
    if (sampledAssistPlayer) {
      const commandSeq = nextPlaytestCommandSequence(evidence)
      recordCommandUse(evidence, state.tick, commandSeq, false)
      recordCommandNoop(evidence, state.tick, commandSeq, 'not_combat')
    }
    return
  }

  const commandSeq = sampledAssistPlayer
    ? nextPlaytestCommandSequence(evidence)
    : null
  const livingEnemyCount = state.enemies.filter((enemy) => !enemy.defeated).length
  const noopReason =
    !sampledAssistPlayer
      ? null
      : state.allies.length === 0
        ? 'no_ally'
        : livingEnemyCount === 0
          ? 'no_target'
          : null
  if (commandSeq !== null) {
    recordCommandUse(evidence, state.tick, commandSeq, noopReason === null)
    recordLocalThreatSample(evidence, {
      tick: state.tick,
      commandSeq,
      phase: 'before',
      threatCount: countLocalThreats(state),
    })
    if (noopReason !== null) {
      recordCommandNoop(evidence, state.tick, commandSeq, noopReason)
    }
  }
  if (sampledAssistPlayer) {
    sampleAssistPlayerIntent(state.commandIntentRuntime, state.tick, commandSeq)
  }
  runMovementSystem(state, commands, fixedDeltaMs)
  runEnemySpawnSystem(state)
  runEnemyAISystem(state, fixedDeltaMs)
  runAllyBehaviorSystem(state, fixedDeltaMs, commandSeq)
  runCombatSystem(state, commands, fixedDeltaMs)
  runProjectileSystem(state, commands, fixedDeltaMs)
  const pairs = runCollisionSystem(state)
  resolveCombatCollisions(state, pairs)
  // B5: sample local threats *after* collision resolution so the `after` phase
  // observes threats that were removed during this tick. The `before` sample
  // above is taken prior to targeting/movement; this one reflects resolution.
  if (commandSeq !== null) {
    recordLocalThreatSample(evidence, {
      tick: state.tick,
      commandSeq,
      phase: 'after',
      threatCount: countLocalThreats(state),
    })
  }
  runSortieSystem(state, fixedDeltaMs)
  state.tick += 1
  state.elapsedMs += fixedDeltaMs
  tickCommandIntentRuntime(state.commandIntentRuntime, state.tick, evidence)
}

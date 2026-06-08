import type { GameState, RewardApplicationId, RewardClaimState, SortieResult } from '../state/GameState'

export const REWARD_FORMULA_VERSION = 1 as const
export const RESOURCE_CAP = 9_999_999
export const PER_SORTIE_REWARD_CAP = 500
const KILL_BONUS_PER_KILL = 5

export type RewardQuote = Readonly<{
  formulaVersion: typeof REWARD_FORMULA_VERSION
  outcome: SortieResult['outcome']
  base: number
  killBonus: number
  hpBonus: number
  delta: number
}>

export type RewardClaimResult =
  | {
      ok: true
      quote: RewardQuote
      resourcesBefore: number
      resourcesAfter: number
    }
  | {
      ok: false
      reason: 'not-terminal' | 'already-claimed' | 'invalid-result'
    }

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function sanitizeNonNegativeSafeInteger(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    return 0
  }
  return value
}

function createRewardClaimState(): RewardClaimState {
  return {
    claimedApplicationIds: Object.create(null) as Record<RewardApplicationId, true>,
  }
}

function ensureRewardClaims(state: GameState): RewardClaimState {
  const stateWithOptionalRewardClaims = state as GameState & {
    rewardClaims?: RewardClaimState
  }

  if (!stateWithOptionalRewardClaims.rewardClaims) {
    stateWithOptionalRewardClaims.rewardClaims = createRewardClaimState()
  }

  if (!stateWithOptionalRewardClaims.rewardClaims.claimedApplicationIds) {
    stateWithOptionalRewardClaims.rewardClaims.claimedApplicationIds =
      Object.create(null) as Record<RewardApplicationId, true>
  }

  return stateWithOptionalRewardClaims.rewardClaims
}

function hasClaimed(
  claimedApplicationIds: Record<RewardApplicationId, true>,
  applicationId: RewardApplicationId,
): boolean {
  return Object.prototype.hasOwnProperty.call(claimedApplicationIds, applicationId)
}

function isTerminalOutcomePair(result: unknown): result is SortieResult {
  if (!result || typeof result !== 'object') {
    return false
  }

  const candidate = result as Record<string, unknown>
  const outcome = candidate.outcome
  const endReason = candidate.endReason

  return (
    (outcome === 'victory' && endReason === 'all_enemies_defeated')
    || (outcome === 'defeat' && endReason === 'player_hp_zero')
    || (outcome === 'timeout' && endReason === 'timeout')
  )
}

function getBaseReward(outcome: SortieResult['outcome']): number {
  switch (outcome) {
    case 'victory':
      return 100
    case 'defeat':
      return 10
    case 'timeout':
      return 30
  }
}

function calculate(result: SortieResult): RewardQuote {
  const kills = sanitizeNonNegativeSafeInteger(result.kills)
  const playerHpRemaining = sanitizeNonNegativeSafeInteger(result.playerHpRemaining)
  const base = getBaseReward(result.outcome)
  const killBonus = kills * KILL_BONUS_PER_KILL
  const hpBonus = result.outcome === 'victory' ? playerHpRemaining : 0
  const rawDelta = base + killBonus + hpBonus

  return Object.freeze({
    formulaVersion: REWARD_FORMULA_VERSION,
    outcome: result.outcome,
    base,
    killBonus,
    hpBonus,
    delta: clamp(rawDelta, 0, PER_SORTIE_REWARD_CAP),
  })
}

function claim(
  state: GameState,
  applicationId: RewardApplicationId,
  result: SortieResult,
): RewardClaimResult {
  if (typeof applicationId !== 'string' || applicationId.length === 0) {
    return { ok: false, reason: 'invalid-result' }
  }

  if (!isTerminalOutcomePair(result)) {
    return { ok: false, reason: 'not-terminal' }
  }

  const rewardClaims = ensureRewardClaims(state)
  if (hasClaimed(rewardClaims.claimedApplicationIds, applicationId)) {
    return { ok: false, reason: 'already-claimed' }
  }

  const quote = calculate(result)
  const resourcesBefore = sanitizeNonNegativeSafeInteger(state.progress.resources)
  const resourcesAfter = clamp(resourcesBefore + quote.delta, 0, RESOURCE_CAP)

  state.progress.resources = resourcesAfter
  rewardClaims.claimedApplicationIds[applicationId] = true

  return {
    ok: true,
    quote,
    resourcesBefore,
    resourcesAfter,
  }
}

export const RewardSystem = {
  calculate,
  claim,
} as const

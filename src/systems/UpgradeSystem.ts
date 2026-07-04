import {
  gameSnapshotSchemaVersion,
  type GameSnapshot,
  type GameState,
  type ProgressState,
} from '../state/GameState'
import type { UpgradeDefinition } from '../data/upgrades'
import type { GameStorage } from '../storage/LocalGameStorage'
import { resolvePhaseTransition } from './PhaseTransitionSystem'

const DEFAULT_WEAPON_POWER = 1
const ALREADY_PURCHASED_WEAPON_POWER_THRESHOLD = 1

export type UpgradeFailureReason =
  | 'invalid-definition'
  | 'already-purchased'
  | 'insufficient-resources'
  | 'not-preparation'

export type UpgradePurchaseFailureReason =
  | UpgradeFailureReason
  | 'write-error'
  | 'storage-unavailable'

export type UpgradeQuote = Readonly<{
  definitionId: string
  cost: number
  resourcesBefore: number
  resourcesAfter: number
  weaponPowerBefore: number
  weaponPowerAfter: number
}>

export type QuoteUpgradeResult =
  | { ok: true; quote: UpgradeQuote }
  | { ok: false; reason: UpgradeFailureReason }

export type PurchaseUpgradeResult =
  | { ok: true; quote: UpgradeQuote }
  | { ok: false; reason: UpgradePurchaseFailureReason }

type UpgradeEvaluation =
  | {
      ok: true
      cost: number
      resourcesBefore: number
      resourcesAfter: number
      weaponPowerBefore: number
      weaponPowerAfter: number
    }
  | { ok: false; reason: UpgradeFailureReason }

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

function sanitizeNonNegativeSafeInteger(value: unknown, fallback: number): number {
  if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) {
    return value
  }
  return fallback
}

/**
 * Runtime-validates an UpgradeDefinition-shaped value. TypeScript's static
 * `UpgradeDefinition` type is never trusted at the call boundary (AC1, AC2):
 * malformed catalog data, forged objects, or unsafe casts must all be caught
 * here rather than relying on the compile-time type.
 */
function isValidUpgradeDefinitionShape(definition: unknown): definition is UpgradeDefinition {
  if (definition === null || typeof definition !== 'object') {
    return false
  }

  const candidate = definition as Record<string, unknown>

  if (typeof candidate.definitionId !== 'string' || candidate.definitionId.length === 0) {
    return false
  }
  if (candidate.schemaVersion !== 1) {
    return false
  }
  if (candidate.currency !== 'resources') {
    return false
  }
  if (!isPositiveSafeInteger(candidate.cost)) {
    return false
  }

  const effect = candidate.effect
  if (effect === null || typeof effect !== 'object') {
    return false
  }
  const effectCandidate = effect as Record<string, unknown>
  if (effectCandidate.target !== 'progress.weaponPower') {
    return false
  }
  if (effectCandidate.operation !== 'add') {
    return false
  }
  if (!isPositiveSafeInteger(effectCandidate.value)) {
    return false
  }

  const availability = candidate.availability
  if (availability === null || typeof availability !== 'object') {
    return false
  }
  const availabilityCandidate = availability as Record<string, unknown>
  if (availabilityCandidate.phase !== 'preparation') {
    return false
  }
  if (availabilityCandidate.repeatable !== false) {
    return false
  }

  return true
}

/**
 * Shared validate step for quote / purchase (AC2: validate -> debit -> apply -> snapshot).
 * Never mutates `state` and never touches storage — pure evaluation only.
 *
 * Precedence: invalid-definition (structural) -> not-preparation (phase gate,
 * AC7) -> already-purchased (M4 minimal ledger, AC6) -> insufficient-resources.
 */
function evaluateUpgrade(
  state: GameState,
  definitionId: string,
  definition: UpgradeDefinition,
): UpgradeEvaluation {
  if (typeof definitionId !== 'string' || definitionId.length === 0) {
    return { ok: false, reason: 'invalid-definition' }
  }
  if (!isValidUpgradeDefinitionShape(definition)) {
    return { ok: false, reason: 'invalid-definition' }
  }
  if (definition.definitionId !== definitionId) {
    return { ok: false, reason: 'invalid-definition' }
  }

  const transition = resolvePhaseTransition(state.loopPhase, 'purchase_upgrade')
  if (!transition.ok) {
    return { ok: false, reason: 'not-preparation' }
  }

  const weaponPowerBefore = sanitizeNonNegativeSafeInteger(
    state.progress.weaponPower,
    DEFAULT_WEAPON_POWER,
  )
  if (weaponPowerBefore > ALREADY_PURCHASED_WEAPON_POWER_THRESHOLD) {
    return { ok: false, reason: 'already-purchased' }
  }

  const resourcesBefore = sanitizeNonNegativeSafeInteger(state.progress.resources, 0)
  const cost = definition.cost
  if (resourcesBefore < cost) {
    return { ok: false, reason: 'insufficient-resources' }
  }

  const resourcesAfter = resourcesBefore - cost
  const weaponPowerAfter = weaponPowerBefore + definition.effect.value

  return {
    ok: true,
    cost,
    resourcesBefore,
    resourcesAfter,
    weaponPowerBefore,
    weaponPowerAfter,
  }
}

function buildQuote(
  definitionId: string,
  evaluation: Extract<UpgradeEvaluation, { ok: true }>,
): UpgradeQuote {
  return {
    definitionId,
    cost: evaluation.cost,
    resourcesBefore: evaluation.resourcesBefore,
    resourcesAfter: evaluation.resourcesAfter,
    weaponPowerBefore: evaluation.weaponPowerBefore,
    weaponPowerAfter: evaluation.weaponPowerAfter,
  }
}

/**
 * Pure quote for an upgrade purchase (AC8). Never mutates `state` and never
 * touches storage. Returns a structured quote payload on success, or the
 * failure reason that a subsequent `purchaseUpgrade` call would hit.
 */
export function quoteUpgrade(
  state: GameState,
  definitionId: string,
  definition: UpgradeDefinition,
): QuoteUpgradeResult {
  const evaluation = evaluateUpgrade(state, definitionId, definition)
  if (!evaluation.ok) {
    return evaluation
  }

  return { ok: true, quote: buildQuote(definitionId, evaluation) }
}

/**
 * Atomically purchases an upgrade (AC2, AC3, AC4).
 *
 * Order: validate -> debit -> apply -> snapshot -> storage.save().
 * `state.progress` is only ever assigned to the candidate progress AFTER
 * `storage.save()` resolves `ok: true`. On any failure (including save
 * failure) `state` is left completely untouched — no live mutate + rollback
 * is needed because nothing is mutated before save succeeds.
 */
export function purchaseUpgrade(
  state: GameState,
  definitionId: string,
  definition: UpgradeDefinition,
  storage: Pick<GameStorage, 'save'>,
): PurchaseUpgradeResult {
  const evaluation = evaluateUpgrade(state, definitionId, definition)
  if (!evaluation.ok) {
    return evaluation
  }

  const candidateProgress: ProgressState = {
    ...state.progress,
    resources: evaluation.resourcesAfter,
    weaponPower: evaluation.weaponPowerAfter,
  }

  const candidateSnapshot: GameSnapshot = {
    schemaVersion: gameSnapshotSchemaVersion,
    resources: candidateProgress.resources,
    weaponPower: candidateProgress.weaponPower,
    playerMaxHp: state.player.maxHp,
  }

  const saveResult = storage.save(candidateSnapshot)
  if (!saveResult.ok) {
    return { ok: false, reason: saveResult.reason }
  }

  state.progress = candidateProgress

  return { ok: true, quote: buildQuote(definitionId, evaluation) }
}

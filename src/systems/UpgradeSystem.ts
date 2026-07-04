import {
  gameSnapshotSchemaVersion,
  type GameSnapshot,
  type GameState,
  type ProgressState,
} from '../state/GameState'
import type { UpgradeDefinition } from '../data/upgrades'
import type { GameStorage } from '../storage/LocalGameStorage'
import { resolvePhaseTransition } from './PhaseTransitionSystem'
import { RESOURCE_CAP } from './RewardSystem'

const DEFAULT_WEAPON_POWER = 1
const ALREADY_PURCHASED_WEAPON_POWER_THRESHOLD = 1

export type UpgradeFailureReason =
  | 'invalid-definition'
  | 'invalid-state'
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

/**
 * Validates a cost-like positive safe integer bounded by RESOURCE_CAP
 * (Blocker 2, iteration 2 PR review): `1 <= cost <= RESOURCE_CAP`. A cost
 * above the storage layer's resource ceiling can never legitimately be
 * affordable and must be rejected as a malformed definition rather than
 * silently clamped.
 */
function isBoundedPositiveSafeInteger(value: unknown): value is number {
  return isPositiveSafeInteger(value) && value <= RESOURCE_CAP
}

/**
 * Validates `progress.resources` as a *state* value (as opposed to a
 * definition field): a non-negative safe integer within RESOURCE_CAP.
 * Unlike the pre-fix behavior, an invalid value here is never sanitized to a
 * fallback — it is treated as corrupt state and rejected fail-closed
 * (Blocker 1, iteration 2 PR review).
 */
function isValidProgressResources(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= RESOURCE_CAP
}

/**
 * Validates `progress.weaponPower` as a *state* value: a safe integer at or
 * above the default floor. An invalid value (negative, NaN, non-integer,
 * unsafe) is never sanitized to `DEFAULT_WEAPON_POWER` — doing so previously
 * allowed a corrupt-but-affordable purchase to be silently accepted and
 * persisted (Blocker 1, iteration 2 PR review).
 */
function isValidProgressWeaponPower(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= DEFAULT_WEAPON_POWER
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
  if (!isBoundedPositiveSafeInteger(candidate.cost)) {
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
 * Precedence (iteration 2 PR review, Blocker 1 & 2): invalid-definition
 * (structural, including cost bounds) -> not-preparation (phase gate, AC7)
 * -> invalid-state (corrupt progress.resources / progress.weaponPower,
 * fail-closed rather than sanitized) -> already-purchased (M4 minimal
 * ledger, AC6) -> insufficient-resources -> invalid-definition (post-hoc:
 * projected weaponPowerAfter overflow).
 *
 * `invalid-state` reason mapping note (Blocker 1): prior to this fix,
 * malformed `progress.weaponPower` / `progress.resources` were silently
 * sanitized to a safe default and evaluation continued, which allowed a
 * corrupt-but-affordable state to be purchased and persisted. This is now a
 * dedicated failure reason (added to `UpgradeFailureReason`) instead of being
 * folded into `insufficient-resources`, so callers/tests can distinguish
 * "state is corrupt" from "state is valid but unaffordable".
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

  if (
    !isValidProgressWeaponPower(state.progress.weaponPower) ||
    !isValidProgressResources(state.progress.resources)
  ) {
    return { ok: false, reason: 'invalid-state' }
  }

  const weaponPowerBefore = state.progress.weaponPower
  if (weaponPowerBefore > ALREADY_PURCHASED_WEAPON_POWER_THRESHOLD) {
    return { ok: false, reason: 'already-purchased' }
  }

  const resourcesBefore = state.progress.resources
  const cost = definition.cost
  if (resourcesBefore < cost) {
    return { ok: false, reason: 'insufficient-resources' }
  }

  const resourcesAfter = resourcesBefore - cost
  const weaponPowerAfter = weaponPowerBefore + definition.effect.value
  if (!Number.isSafeInteger(weaponPowerAfter)) {
    // Blocker 2 (iteration 2 PR review): weaponPowerBefore + effect.value can
    // overflow past Number.MAX_SAFE_INTEGER even when both operands
    // individually passed their own bounds checks. Reject as a malformed
    // definition (its `effect.value` is unsafe to apply to this state) rather
    // than persisting a precision-corrupted weaponPower.
    return { ok: false, reason: 'invalid-definition' }
  }

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
 * failure or a rejected `evaluateUpgrade`, e.g. `invalid-state`)
 * `state` is left completely untouched — no live mutate + rollback is
 * needed because nothing is mutated before save succeeds.
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

import { describe, expect, it, vi } from 'vitest'

import { createInitialGameState, type GameState } from '../src/state/GameState'
import { resolvePhaseTransition } from '../src/systems/PhaseTransitionSystem'
import { purchaseUpgrade, quoteUpgrade } from '../src/systems/UpgradeSystem'
import type { UpgradeDefinition } from '../src/data/upgrades'
import type { GameStorage, SaveResult } from '../src/storage/LocalGameStorage'

const VALID_DEFINITION: UpgradeDefinition = {
  definitionId: 'weapon_power_plus_1',
  schemaVersion: 1,
  currency: 'resources',
  cost: 100,
  effect: {
    target: 'progress.weaponPower',
    operation: 'add',
    value: 1,
  },
  availability: {
    phase: 'preparation',
    repeatable: false,
  },
}

function createFakeStorage(saveImpl?: (snapshot: unknown) => SaveResult) {
  const save = vi.fn(
    saveImpl ?? ((): SaveResult => ({ ok: true, reason: 'saved' })),
  ) as unknown as GameStorage['save']

  return { save } satisfies Pick<GameStorage, 'save'>
}

function withResources(resources: number, weaponPower = 1): GameState {
  return createInitialGameState({ resources, weaponPower })
}

describe('quoteUpgrade', () => {
  it('GIVEN a valid definition and affordable resources WHEN quoteUpgrade is called THEN it returns a structured success quote without mutating state', () => {
    const state = withResources(100)
    const before = JSON.parse(JSON.stringify(state))

    const result = quoteUpgrade(state, 'weapon_power_plus_1', VALID_DEFINITION)

    expect(result).toEqual({
      ok: true,
      quote: {
        definitionId: 'weapon_power_plus_1',
        cost: 100,
        resourcesBefore: 100,
        resourcesAfter: 0,
        weaponPowerBefore: 1,
        weaponPowerAfter: 2,
      },
    })
    expect(state).toEqual(before)
  })

  it('GIVEN a definitionId mismatch WHEN quoteUpgrade is called THEN it returns invalid-definition', () => {
    const state = withResources(100)

    expect(quoteUpgrade(state, 'some-other-id', VALID_DEFINITION)).toEqual({
      ok: false,
      reason: 'invalid-definition',
    })
  })

  it.each([
    { name: 'wrong schemaVersion', overrides: { schemaVersion: 2 } },
    { name: 'wrong currency', overrides: { currency: 'gems' } },
    { name: 'zero cost', overrides: { cost: 0 } },
    { name: 'negative cost', overrides: { cost: -1 } },
    { name: 'NaN cost', overrides: { cost: Number.NaN } },
    { name: 'Infinity cost', overrides: { cost: Number.POSITIVE_INFINITY } },
    { name: 'fractional cost', overrides: { cost: 1.5 } },
    { name: 'unsafe integer cost', overrides: { cost: Number.MAX_SAFE_INTEGER + 1 } },
    { name: 'wrong availability.phase', overrides: { availability: { phase: 'running', repeatable: false } } },
    { name: 'wrong availability.repeatable', overrides: { availability: { phase: 'preparation', repeatable: true } } },
  ])(
    'GIVEN a malformed definition ($name) WHEN quoteUpgrade is called THEN it returns invalid-definition without trusting the TypeScript type',
    ({ overrides }) => {
      const state = withResources(100)
      const malformed = {
        ...VALID_DEFINITION,
        ...overrides,
      } as unknown as UpgradeDefinition

      expect(quoteUpgrade(state, VALID_DEFINITION.definitionId, malformed)).toEqual({
        ok: false,
        reason: 'invalid-definition',
      })
    },
  )

  it.each([
    { name: 'wrong effect.target', effect: { target: 'progress.resources', operation: 'add', value: 1 } },
    { name: 'wrong effect.operation', effect: { target: 'progress.weaponPower', operation: 'multiply', value: 1 } },
    { name: 'zero effect.value', effect: { target: 'progress.weaponPower', operation: 'add', value: 0 } },
    { name: 'negative effect.value', effect: { target: 'progress.weaponPower', operation: 'add', value: -1 } },
    { name: 'NaN effect.value', effect: { target: 'progress.weaponPower', operation: 'add', value: Number.NaN } },
    {
      name: 'fractional effect.value',
      effect: { target: 'progress.weaponPower', operation: 'add', value: 1.5 },
    },
  ])(
    'GIVEN a malformed effect ($name) WHEN quoteUpgrade is called THEN it returns invalid-definition',
    ({ effect }) => {
      const state = withResources(100)
      const malformed = { ...VALID_DEFINITION, effect } as unknown as UpgradeDefinition

      expect(quoteUpgrade(state, VALID_DEFINITION.definitionId, malformed)).toEqual({
        ok: false,
        reason: 'invalid-definition',
      })
    },
  )

  it('GIVEN a non-preparation phase WHEN quoteUpgrade is called THEN it returns not-preparation', () => {
    const state = withResources(100)
    state.loopPhase = 'running'

    expect(quoteUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION)).toEqual({
      ok: false,
      reason: 'not-preparation',
    })
  })

  it('GIVEN weaponPower already above the M4 minimal threshold WHEN quoteUpgrade is called THEN it returns already-purchased', () => {
    const state = withResources(1000, 2)

    expect(quoteUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION)).toEqual({
      ok: false,
      reason: 'already-purchased',
    })
  })

  it('GIVEN insufficient resources WHEN quoteUpgrade is called THEN it returns insufficient-resources', () => {
    const state = withResources(99)

    expect(quoteUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION)).toEqual({
      ok: false,
      reason: 'insufficient-resources',
    })
  })
})

describe('purchaseUpgrade', () => {
  it('GIVEN a valid affordable purchase WHEN purchaseUpgrade is called THEN it validates, debits, applies, snapshots, and commits only after storage.save succeeds', () => {
    const state = withResources(100)
    const storage = createFakeStorage()

    const result = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)

    expect(result).toEqual({
      ok: true,
      quote: {
        definitionId: 'weapon_power_plus_1',
        cost: 100,
        resourcesBefore: 100,
        resourcesAfter: 0,
        weaponPowerBefore: 1,
        weaponPowerAfter: 2,
      },
    })
    expect(state.progress.resources).toBe(0)
    expect(state.progress.weaponPower).toBe(2)
    expect(storage.save).toHaveBeenCalledTimes(1)
    expect(storage.save).toHaveBeenCalledWith({
      schemaVersion: 1,
      resources: 0,
      weaponPower: 2,
      playerMaxHp: state.player.maxHp,
    })
  })

  it('GIVEN storage.save returns write-error WHEN purchaseUpgrade is called THEN it returns write-error and leaves state untouched (no false success)', () => {
    const state = withResources(100)
    const storage = createFakeStorage(() => ({ ok: false, reason: 'write-error' }))
    const before = JSON.parse(JSON.stringify(state))

    const result = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)

    expect(result).toEqual({ ok: false, reason: 'write-error' })
    expect(state).toEqual(before)
    expect(storage.save).toHaveBeenCalledTimes(1)
  })

  it('GIVEN storage.save returns storage-unavailable WHEN purchaseUpgrade is called THEN it returns storage-unavailable and leaves state untouched', () => {
    const state = withResources(100)
    const storage = createFakeStorage(() => ({ ok: false, reason: 'storage-unavailable' }))
    const before = JSON.parse(JSON.stringify(state))

    const result = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)

    expect(result).toEqual({ ok: false, reason: 'storage-unavailable' })
    expect(state).toEqual(before)
    expect(storage.save).toHaveBeenCalledTimes(1)
  })

  it('GIVEN a malformed definition WHEN purchaseUpgrade is called THEN it returns invalid-definition with no mutation and no storage.save call', () => {
    const state = withResources(100)
    const storage = createFakeStorage()
    const malformed = { ...VALID_DEFINITION, cost: Number.NaN } as unknown as UpgradeDefinition
    const before = JSON.parse(JSON.stringify(state))

    const result = purchaseUpgrade(state, VALID_DEFINITION.definitionId, malformed, storage)

    expect(result).toEqual({ ok: false, reason: 'invalid-definition' })
    expect(state).toEqual(before)
    expect(storage.save).toHaveBeenCalledTimes(0)
  })

  it('GIVEN already-purchased state WHEN purchaseUpgrade is called THEN it rejects without calling storage.save', () => {
    const state = withResources(1000, 2)
    const storage = createFakeStorage()

    const result = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)

    expect(result).toEqual({ ok: false, reason: 'already-purchased' })
    expect(storage.save).toHaveBeenCalledTimes(0)
  })

  it('GIVEN insufficient resources WHEN purchaseUpgrade is called THEN it rejects without calling storage.save', () => {
    const state = withResources(50)
    const storage = createFakeStorage()

    const result = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)

    expect(result).toEqual({ ok: false, reason: 'insufficient-resources' })
    expect(storage.save).toHaveBeenCalledTimes(0)
  })

  it('GIVEN a non-preparation phase WHEN purchaseUpgrade is called THEN it rejects without calling storage.save', () => {
    const state = withResources(100)
    state.loopPhase = 'result'
    const storage = createFakeStorage()

    const result = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)

    expect(result).toEqual({ ok: false, reason: 'not-preparation' })
    expect(storage.save).toHaveBeenCalledTimes(0)
  })

  it('GIVEN an invalid (NaN) resources state WHEN purchaseUpgrade is called THEN it fails closed (insufficient-resources) without calling storage.save', () => {
    const state = withResources(0)
    state.progress.resources = Number.NaN
    const storage = createFakeStorage()

    const result = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)

    expect(result).toEqual({ ok: false, reason: 'insufficient-resources' })
    expect(storage.save).toHaveBeenCalledTimes(0)
  })

  it('GIVEN an invalid (negative) weaponPower state WHEN purchaseUpgrade is called THEN it sanitizes to the default and still requires sufficient resources without calling storage.save', () => {
    const state = withResources(50)
    state.progress.weaponPower = -3

    const storage = createFakeStorage()

    const result = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)

    expect(result).toEqual({ ok: false, reason: 'insufficient-resources' })
    expect(storage.save).toHaveBeenCalledTimes(0)
  })

  it('GIVEN two sequential purchases with sufficient resources WHEN purchaseUpgrade is called twice THEN the second is rejected as already-purchased', () => {
    const state = withResources(1000)
    const storage = createFakeStorage()

    const first = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)
    expect(first.ok).toBe(true)

    const second = purchaseUpgrade(state, VALID_DEFINITION.definitionId, VALID_DEFINITION, storage)
    expect(second).toEqual({ ok: false, reason: 'already-purchased' })
    expect(storage.save).toHaveBeenCalledTimes(1)
  })
})

describe('purchase_upgrade phase transition', () => {
  it('GIVEN preparation phase WHEN resolvePhaseTransition(purchase_upgrade) is called THEN it self-transitions to preparation', () => {
    expect(resolvePhaseTransition('preparation', 'purchase_upgrade')).toEqual({
      ok: true,
      from: 'preparation',
      to: 'preparation',
      intent: 'purchase_upgrade',
    })
  })

  it.each(['running', 'result', 'title_menu', 'load_menu'] as const)(
    'GIVEN %s phase WHEN resolvePhaseTransition(purchase_upgrade) is called THEN it is an illegal-transition',
    (phase) => {
      expect(resolvePhaseTransition(phase, 'purchase_upgrade')).toEqual({
        ok: false,
        error: {
          code: 'illegal-transition',
          from: phase,
          intent: 'purchase_upgrade',
        },
      })
    },
  )
})

import { describe, expect, it } from 'vitest'

import { resolveProgressionSaveFailureFeedback } from '../src/main'
import {
  createLocalGameStorage,
  parseSnapshot,
  serializeSnapshot,
} from '../src/storage'
import { createGameSnapshot, createInitialGameState } from '../src/state'

function createMemoryStorage(initialEntries?: Record<string, string>) {
  const bag = new Map<string, string>(Object.entries(initialEntries ?? {}))

  return {
    getItem(key: string) {
      return bag.get(key) ?? null
    },
    setItem(key: string, value: string) {
      bag.set(key, value)
    },
    removeItem(key: string) {
      bag.delete(key)
    },
  }
}

describe('LocalGameStorage', () => {
  it('GIVEN a v1 snapshot WHEN it is serialized and parsed THEN schemaVersion 1 is preserved', () => {
    const snapshot = {
      schemaVersion: 1,
      resources: 5,
      weaponPower: 2,
      playerMaxHp: 10,
    } as const

    expect(parseSnapshot(serializeSnapshot(snapshot))).toEqual({
      ok: true,
      snapshot,
      reason: 'loaded',
    })
  })

  it('GIVEN a legacy snapshot without schemaVersion WHEN it is parsed THEN it migrates to v1 and preserves existing values', () => {
    const legacySnapshot = {
      resources: 5,
      weaponPower: 2,
      playerMaxHp: 10,
    } as const

    expect(parseSnapshot(JSON.stringify(legacySnapshot))).toEqual({
      ok: true,
      snapshot: {
        schemaVersion: 1,
        resources: 5,
        weaponPower: 2,
        playerMaxHp: 10,
      },
      reason: 'loaded',
    })
  })

  it('GIVEN a legacy snapshot missing required fields WHEN it is parsed THEN it reports invalid schema', () => {
    expect(parseSnapshot(JSON.stringify({ resources: 5, weaponPower: 2 }))).toEqual({
      ok: false,
      snapshot: null,
      reason: 'invalid-schema',
      errorName: undefined,
    })
    expect(parseSnapshot(JSON.stringify({}))).toEqual({
      ok: false,
      snapshot: null,
      reason: 'invalid-schema',
      errorName: undefined,
    })
  })

  it('GIVEN JSON.stringify converted non-finite numbers to null WHEN a v1 snapshot is parsed THEN fields fall back', () => {
    const raw = JSON.stringify({
      schemaVersion: 1,
      resources: Number.NaN,
      weaponPower: Number.POSITIVE_INFINITY,
      playerMaxHp: Number.NaN,
    })

    expect(raw).toContain('"resources":null')
    expect(raw).toContain('"weaponPower":null')
    expect(raw).toContain('"playerMaxHp":null')
    expect(parseSnapshot(raw)).toEqual({
      ok: true,
      snapshot: {
        schemaVersion: 1,
        resources: 0,
        weaponPower: 1,
        playerMaxHp: 8,
      },
      reason: 'loaded',
    })
  })

  it('GIVEN syntactically invalid non-finite JSON tokens WHEN parsed THEN it reports corrupt JSON', () => {
    expect(
      parseSnapshot('{"schemaVersion":1,"resources":NaN,"weaponPower":1,"playerMaxHp":8}'),
    ).toEqual({
      ok: false,
      snapshot: null,
      reason: 'corrupt-json',
      errorName: 'SyntaxError',
    })
  })

  it('GIVEN a v1 snapshot missing required fields WHEN it is parsed THEN it reports invalid schema', () => {
    expect(
      parseSnapshot(
        JSON.stringify({
          schemaVersion: 1,
          resources: 5,
          weaponPower: 2,
        }),
      ),
    ).toEqual({
      ok: false,
      snapshot: null,
      reason: 'invalid-schema',
      errorName: undefined,
    })
  })

  it('GIVEN an unknown future schemaVersion WHEN it is parsed THEN it reports unsupported schema', () => {
    expect(
      parseSnapshot(
        JSON.stringify({
          schemaVersion: 999,
          resources: 5,
          weaponPower: 2,
          playerMaxHp: 10,
        }),
      ),
    ).toEqual({
      ok: false,
      snapshot: null,
      reason: 'unsupported-schema',
      errorName: undefined,
    })
  })

  it('GIVEN corrupted JSON WHEN it is parsed THEN it reports corrupt JSON', () => {
    expect(parseSnapshot('{not-json')).toEqual({
      ok: false,
      snapshot: null,
      reason: 'corrupt-json',
      errorName: 'SyntaxError',
    })
  })

  it('GIVEN top-level non-object JSON values WHEN parsed THEN they report invalid schema', () => {
    expect(parseSnapshot('null')).toEqual({
      ok: false,
      snapshot: null,
      reason: 'invalid-schema',
      errorName: undefined,
    })
    expect(parseSnapshot('[]')).toEqual({
      ok: false,
      snapshot: null,
      reason: 'invalid-schema',
      errorName: undefined,
    })
    expect(parseSnapshot('42')).toEqual({
      ok: false,
      snapshot: null,
      reason: 'invalid-schema',
      errorName: undefined,
    })
    expect(parseSnapshot('"snapshot"')).toEqual({
      ok: false,
      snapshot: null,
      reason: 'invalid-schema',
      errorName: undefined,
    })
  })

  it('GIVEN empty storage WHEN loaded THEN it reports a successful empty result', () => {
    const storage = createMemoryStorage()
    const gameStorage = createLocalGameStorage('test-save', storage)

    expect(gameStorage.load()).toEqual({
      ok: true,
      snapshot: null,
      reason: 'empty',
    })
  })

  it('GIVEN a storage adapter WHEN it saves and loads a v1 snapshot THEN the data round-trips with schemaVersion', () => {
    const storage = createMemoryStorage()
    const gameStorage = createLocalGameStorage('test-save', storage)

    const snapshot = {
      schemaVersion: 1,
      resources: 3,
      weaponPower: 4,
      playerMaxHp: 9,
    } as const

    expect(gameStorage.save(snapshot)).toEqual({
      ok: true,
      reason: 'saved',
    })
    expect(gameStorage.load()).toEqual({
      ok: true,
      snapshot,
      reason: 'loaded',
    })
  })

  it('GIVEN localStorage accessor throws WHEN the default storage is resolved THEN load and save report storage unavailable with errorName', () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() {
        throw new DOMException('blocked', 'SecurityError')
      },
    })

    try {
      const gameStorage = createLocalGameStorage('test-save')

      expect(gameStorage.load()).toEqual({
        ok: false,
        snapshot: null,
        reason: 'storage-unavailable',
        errorName: 'SecurityError',
      })
      expect(gameStorage.save({
        schemaVersion: 1,
        resources: 1,
        weaponPower: 1,
        playerMaxHp: 8,
      })).toEqual({
        ok: false,
        reason: 'storage-unavailable',
        errorName: 'SecurityError',
      })
    } finally {
      if (descriptor) {
        Object.defineProperty(globalThis, 'localStorage', descriptor)
      } else {
        Reflect.deleteProperty(globalThis, 'localStorage')
      }
    }
  })

  it('GIVEN a storage adapter whose writes fail but reads work WHEN load is called THEN it still reads the stored snapshot', () => {
    const storage = {
      value: serializeSnapshot({
        schemaVersion: 1,
        resources: 7,
        weaponPower: 3,
        playerMaxHp: 12,
      }),
      getItem() {
        return this.value
      },
      setItem() {
        throw new DOMException('blocked', 'SecurityError')
      },
      removeItem() {
        // no-op
      },
    }
    const gameStorage = createLocalGameStorage('test-save', storage)

    expect(gameStorage.load()).toEqual({
      ok: true,
      snapshot: {
        schemaVersion: 1,
        resources: 7,
        weaponPower: 3,
        playerMaxHp: 12,
      },
      reason: 'loaded',
    })
    expect(gameStorage.save({
      schemaVersion: 1,
      resources: 1,
      weaponPower: 1,
      playerMaxHp: 8,
    })).toEqual({
      ok: false,
      reason: 'write-error',
      errorName: 'SecurityError',
    })
  })

  it('GIVEN getItem throws WHEN load is called THEN it reports read-error', () => {
    const storage = {
      getItem() {
        throw new DOMException('blocked', 'SecurityError')
      },
      setItem() {
        // probe succeeds
      },
      removeItem() {
        // probe succeeds
      },
    }
    const gameStorage = createLocalGameStorage('test-save', storage)

    expect(gameStorage.load()).toEqual({
      ok: false,
      snapshot: null,
      reason: 'read-error',
      errorName: 'SecurityError',
    })
  })

  it('GIVEN setItem throws WHEN save is called THEN it reports write-error', () => {
    const storage = {
      getItem() {
        return null
      },
      setItem() {
        throw new DOMException('quota', 'QuotaExceededError')
      },
      removeItem() {
        // no-op
      },
    }
    const gameStorage = createLocalGameStorage('test-save', storage)

    expect(gameStorage.save({
      schemaVersion: 1,
      resources: 1,
      weaponPower: 1,
      playerMaxHp: 8,
    })).toEqual({
      ok: false,
      reason: 'write-error',
      errorName: 'QuotaExceededError',
    })
  })

  it('GIVEN an empty string in storage WHEN it is parsed THEN it reports corrupt JSON instead of empty storage', () => {
    expect(parseSnapshot('')).toEqual({
      ok: false,
      snapshot: null,
      reason: 'corrupt-json',
      errorName: 'SyntaxError',
    })
  })

  it('GIVEN a state with runtime fields WHEN createGameSnapshot is called THEN it keep only progression fields', () => {
    const state = createInitialGameState({
      resources: 123,
      weaponPower: 4,
      playerMaxHp: 9,
    })

    state.loopPhase = 'running'
    state.pendingRewardApplicationId = 'sortie-reward-1'
    state.projectiles.push({
      id: 1,
      x: 1,
      y: 2,
      radius: 3,
      directionX: 0,
      directionY: 1,
      speedPxPerSec: 120,
      ageMs: 0,
      lifetimeMs: 100,
      damage: 4,
    })

    state.enemies.push({
      id: 1,
      definitionId: 'basic',
      hp: 10,
      maxHp: 10,
      x: 0,
      y: 0,
      radius: 3,
      speedPxPerSec: 60,
      contactDamage: 1,
      defeated: false,
      defeatedAtTick: null,
    })

    const snapshot = createGameSnapshot(state)

    expect(snapshot).toEqual({
      schemaVersion: 1,
      resources: 123,
      weaponPower: 4,
      playerMaxHp: 9,
    })

    const raw = serializeSnapshot(snapshot)
    expect(raw).toContain('"schemaVersion":1')
    expect(raw).toContain('"resources":123')
    expect(raw).toContain('"weaponPower":4')
    expect(raw).toContain('"playerMaxHp":9')
    expect(raw).not.toContain('"loopPhase"')
    expect(raw).not.toContain('"pendingRewardApplicationId"')
    expect(raw).not.toContain('"rewardClaims"')
    expect(raw).not.toContain('"tick"')
    expect(raw).not.toContain('"elapsedMs"')
    expect(raw).not.toContain('"enemies"')
    expect(raw).not.toContain('"projectiles"')
    expect(raw).not.toContain('"playerHpRemaining"')
  })
})

describe('progression save failure feedback', () => {
  it('GIVEN reward-claim save failure without a prior snapshot WHEN feedback is resolved THEN it keeps loadable snapshot false', () => {
    const result = resolveProgressionSaveFailureFeedback('reward-claim', false)

    expect(result).toEqual({
      hasLoadableSnapshot: false,
      status: 'Result confirmed; progress not saved.',
      summary: 'No local save is currently available for Quick Load.',
      readbackAttempted: false,
    })
  })

  it('GIVEN reward-claim save failure with an existing loadable snapshot WHEN feedback is resolved THEN it keeps existing loadable snapshot', () => {
    const result = resolveProgressionSaveFailureFeedback('reward-claim', true)

    expect(result.hasLoadableSnapshot).toBe(true)
    expect(result.status).toBe('Result confirmed; progress not saved.')
    expect(result.summary).toContain('Previous local save is still available')
    expect(result.readbackAttempted).toBe(false)
  })

  it('GIVEN quick-save save failure WHEN feedback is resolved THEN it does not read back after save failure', () => {
    const withoutSnapshot = resolveProgressionSaveFailureFeedback('quick-save', false)
    const withSnapshot = resolveProgressionSaveFailureFeedback('quick-save', true)

    expect(withoutSnapshot).toEqual({
      hasLoadableSnapshot: false,
      status: 'Quick Save failed.',
      summary: 'No local save is currently available for Quick Load.',
      readbackAttempted: false,
    })
    expect(withSnapshot.hasLoadableSnapshot).toBe(true)
    expect(withSnapshot.status).toBe('Quick Save failed.')
    expect(withSnapshot.summary).toContain('Previous local save is still available')
    expect(withSnapshot.readbackAttempted).toBe(false)
  })
})

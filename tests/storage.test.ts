import { describe, expect, it, vi } from 'vitest'

import { resolveProgressionSaveFailureFeedback, runProgressionSave } from '../src/main'
// Note: ProgressionSaveReason is now 'reward-claim' | 'save' (Quick Save renamed to Save — #619)
import {
  createLocalGameStorage,
  defaultSaveKey,
  parseSnapshot,
  resolveStorageKey,
  serializeSnapshot,
  type SaveResult,
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
  it('GIVEN no override WHEN resolveStorageKey is called THEN returns production save key', () => {
    expect(resolveStorageKey()).toBe(defaultSaveKey)
  })

  it('GIVEN numeric namespace override WHEN resolveStorageKey is called THEN returns preview namespace key', () => {
    expect(resolveStorageKey('885')).toBe('loop-protocol.preview.pr-885.mvp.save')
  })

  it('GIVEN pr-number namespace override WHEN resolveStorageKey is called THEN returns preview namespace key', () => {
    expect(resolveStorageKey('pr-885')).toBe('loop-protocol.preview.pr-885.mvp.save')
  })

  it('GIVEN invalid preview namespace WHEN resolveStorageKey is called THEN it fails closed', () => {
    expect(() => resolveStorageKey('preview-pr-885')).toThrowError(
      'Invalid VITE_LOOP_STORAGE_NAMESPACE: preview-pr-885',
    )
    expect(() => resolveStorageKey('foobar')).toThrowError(
      'Invalid VITE_LOOP_STORAGE_NAMESPACE: foobar',
    )
  })

  it('GIVEN allowed runtime __LOOP_STORAGE_KEY__ override WHEN resolveStorageKey is called THEN uses runtime key', () => {
    const targetKey = 'loop-protocol.e2e.storage-resolver.mvp.save'
    const storageWindow = globalThis as Window & { __LOOP_STORAGE_KEY__?: string }
    const previousOverride = storageWindow.__LOOP_STORAGE_KEY__

    try {
      storageWindow.__LOOP_STORAGE_KEY__ = targetKey
      expect(resolveStorageKey()).toBe(targetKey)
    } finally {
      if (previousOverride === undefined) {
        Reflect.deleteProperty(storageWindow, '__LOOP_STORAGE_KEY__')
      } else {
        storageWindow.__LOOP_STORAGE_KEY__ = previousOverride
      }
    }
  })

  it('GIVEN disallowed runtime __LOOP_STORAGE_KEY__ override WHEN resolveStorageKey is called THEN it throws', () => {
    const storageWindow = globalThis as Window & { __LOOP_STORAGE_KEY__?: string }
    const previousOverride = storageWindow.__LOOP_STORAGE_KEY__

    try {
      storageWindow.__LOOP_STORAGE_KEY__ = defaultSaveKey
      expect(() => resolveStorageKey()).toThrowError(
        `Invalid __LOOP_STORAGE_KEY__: ${defaultSaveKey}`,
      )
    } finally {
      if (previousOverride === undefined) {
        Reflect.deleteProperty(storageWindow, '__LOOP_STORAGE_KEY__')
      } else {
        storageWindow.__LOOP_STORAGE_KEY__ = previousOverride
      }
    }
  })

  it('GIVEN allowed runtime override key WHEN default createLocalGameStorage is used THEN it uses override key', () => {
    const targetKey = 'loop-protocol.e2e.storage-internal.mvp.save'
    const storageWindow = globalThis as Window & { __LOOP_STORAGE_KEY__?: string }
    const previousOverride = storageWindow.__LOOP_STORAGE_KEY__

    try {
      const storage = createMemoryStorage()
      storageWindow.__LOOP_STORAGE_KEY__ = targetKey

      const gameStorage = createLocalGameStorage(undefined, storage)
      expect(gameStorage.save({ schemaVersion: 1, resources: 1, weaponPower: 2, playerMaxHp: 3 }))
        .toEqual({ ok: true, reason: 'saved' })

      expect(storage.getItem(targetKey)).toEqual(
        '{"schemaVersion":1,"resources":1,"weaponPower":2,"playerMaxHp":3}',
      )
    } finally {
      if (previousOverride === undefined) {
        Reflect.deleteProperty(storageWindow, '__LOOP_STORAGE_KEY__')
      } else {
        storageWindow.__LOOP_STORAGE_KEY__ = previousOverride
      }
    }
  })

  it('GIVEN disallowed runtime override key WHEN default createLocalGameStorage is used THEN it throws', () => {
    const storageWindow = globalThis as Window & { __LOOP_STORAGE_KEY__?: string }
    const previousOverride = storageWindow.__LOOP_STORAGE_KEY__

    try {
      storageWindow.__LOOP_STORAGE_KEY__ = 'loop-protocol.preview.pr-885.mvp.save'

      expect(() => createLocalGameStorage()).toThrowError(
        'Invalid __LOOP_STORAGE_KEY__: loop-protocol.preview.pr-885.mvp.save',
      )
    } finally {
      if (previousOverride === undefined) {
        Reflect.deleteProperty(storageWindow, '__LOOP_STORAGE_KEY__')
      } else {
        storageWindow.__LOOP_STORAGE_KEY__ = previousOverride
      }
    }
  })

  it('GIVEN non-string runtime override WHEN resolveStorageKey is called THEN it throws', () => {
    const storageWindow = globalThis as Window & { __LOOP_STORAGE_KEY__?: unknown }
    const previousOverride = storageWindow.__LOOP_STORAGE_KEY__

    try {
      storageWindow.__LOOP_STORAGE_KEY__ = 123
      expect(() => resolveStorageKey()).toThrowError('__LOOP_STORAGE_KEY__ must be a string')
    } finally {
      if (previousOverride === undefined) {
        Reflect.deleteProperty(storageWindow, '__LOOP_STORAGE_KEY__')
      } else {
        storageWindow.__LOOP_STORAGE_KEY__ = previousOverride
      }
    }
  })

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
  function runFailurePath(reason: 'reward-claim' | 'save', hadLoadableSnapshot: boolean) {
    const createSnapshot = vi.fn(() => ({
      schemaVersion: 1 as const,
      resources: 11,
      weaponPower: 3,
      playerMaxHp: 9,
    }))
    const save = vi.fn<() => SaveResult>(() => ({
      ok: false,
      reason: 'write-error',
      errorName: 'QuotaExceededError',
    }))
    const load = vi.fn(() => ({
      ok: true as const,
      snapshot: null,
      reason: 'empty' as const,
    }))
    const reportSaveFailure = vi.fn()
    const setHudFeedback = vi.fn<(status: string, summary: string) => void>()

    const nextHasLoadableSnapshot = runProgressionSave(reason, hadLoadableSnapshot, {
      storage: { save, load },
      createSnapshot,
      reportSaveFailure,
      setHudFeedback,
    })

    return {
      createSnapshot,
      load,
      nextHasLoadableSnapshot,
      reportSaveFailure,
      save,
      setHudFeedback,
    }
  }

  it('GIVEN reward-claim save failure without a prior snapshot WHEN the production seam runs THEN it keeps loadable snapshot false and explains reload loss risk', () => {
    const result = runFailurePath('reward-claim', false)

    expect(result.nextHasLoadableSnapshot).toBe(false)
    expect(result.createSnapshot).toHaveBeenCalledTimes(1)
    expect(result.save).toHaveBeenCalledTimes(1)
    expect(result.load).not.toHaveBeenCalled()
    expect(result.reportSaveFailure).toHaveBeenCalledWith({
      ok: false,
      reason: 'write-error',
      errorName: 'QuotaExceededError',
    })
    expect(result.setHudFeedback).toHaveBeenCalledWith(
      'Result confirmed; progress not saved.',
      'No local save is available; this result may be lost after reload.',
    )
    expect(result.setHudFeedback).not.toHaveBeenCalledWith('Result confirmed.', 'Progress saved locally.')
  })

  it('GIVEN reward-claim save failure with an existing loadable snapshot WHEN the production seam runs THEN it keeps the previous quick load available', () => {
    const result = runFailurePath('reward-claim', true)

    expect(result.nextHasLoadableSnapshot).toBe(true)
    expect(result.load).not.toHaveBeenCalled()
    expect(result.setHudFeedback).toHaveBeenCalledWith(
      'Result confirmed; progress not saved.',
      'Previous local save is still available; this result may be lost after reload.',
    )
    expect(result.setHudFeedback).not.toHaveBeenCalledWith('Result confirmed.', 'Progress saved locally.')
  })

  it('GIVEN save failure WHEN the production seam runs THEN it keeps the previous snapshot state and does not report save success', () => {
    const withoutSnapshot = runFailurePath('save', false)
    const withSnapshot = runFailurePath('save', true)

    expect(withoutSnapshot.nextHasLoadableSnapshot).toBe(false)
    expect(withSnapshot.nextHasLoadableSnapshot).toBe(true)
    expect(withoutSnapshot.load).not.toHaveBeenCalled()
    expect(withSnapshot.load).not.toHaveBeenCalled()
    expect(withoutSnapshot.setHudFeedback).toHaveBeenCalledWith(
      'Save failed.',
      'No local save is available; this result may be lost after reload.',
    )
    expect(withSnapshot.setHudFeedback).toHaveBeenCalledWith(
      'Save failed.',
      'Previous local save is still available; this result may be lost after reload.',
    )
    expect(withoutSnapshot.setHudFeedback).not.toHaveBeenCalledWith(
      'Save complete.',
      'Progression snapshot saved locally.',
    )
    expect(withSnapshot.setHudFeedback).not.toHaveBeenCalledWith(
      'Save complete.',
      'Progression snapshot saved locally.',
    )
  })

  it('GIVEN save failure feedback is resolved directly WHEN the helper is called THEN the summary still states that this result may be lost after reload', () => {
    expect(resolveProgressionSaveFailureFeedback('reward-claim', true)).toEqual({
      hasLoadableSnapshot: true,
      status: 'Result confirmed; progress not saved.',
      summary: 'Previous local save is still available; this result may be lost after reload.',
    })
    expect(resolveProgressionSaveFailureFeedback('save', false)).toEqual({
      hasLoadableSnapshot: false,
      status: 'Save failed.',
      summary: 'No local save is available; this result may be lost after reload.',
    })
  })
})

describe('phase guard: storage.save() only in preparation (AC2, AC8)', () => {
  function makeSaveSpySeam(mockSaveResult: SaveResult = { ok: true, reason: 'saved' }) {
    const save = vi.fn<() => SaveResult>(() => mockSaveResult)
    const load = vi.fn(() => ({ ok: true as const, snapshot: null, reason: 'empty' as const }))
    const createSnapshot = vi.fn(() => ({
      schemaVersion: 1 as const,
      resources: 0,
      weaponPower: 1,
      playerMaxHp: 8,
    }))
    const reportSaveFailure = vi.fn()
    const setHudFeedback = vi.fn()
    return {
      storage: { save, load },
      createSnapshot,
      reportSaveFailure,
      setHudFeedback,
      // direct references for assertion convenience
      save,
      load,
    }
  }

  it('GIVEN preparation WHEN runProgressionSave called THEN storage.save() is invoked exactly once (AC8)', () => {
    const seam = makeSaveSpySeam()
    runProgressionSave('save', false, seam)
    expect(seam.save).toHaveBeenCalledTimes(1)
  })

  it('GIVEN save success WHEN runProgressionSave called THEN setHudFeedback shows save complete (AC2)', () => {
    const seam = makeSaveSpySeam()
    runProgressionSave('save', false, seam)
    expect(seam.setHudFeedback).toHaveBeenCalledWith('Save complete.', 'Progression snapshot saved locally.')
  })
})

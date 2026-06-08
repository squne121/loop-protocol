import { describe, expect, it } from 'vitest'

import {
  createLocalGameStorage,
  parseSnapshot,
  serializeSnapshot,
} from '../src/storage'

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

  it('GIVEN a storage adapter whose accessor probe throws WHEN created THEN load and save report storage unavailable', () => {
    const storage = {
      getItem() {
        return null
      },
      setItem() {
        throw new DOMException('blocked', 'SecurityError')
      },
      removeItem() {
        // unreachable because setItem throws first
      },
    }
    const gameStorage = createLocalGameStorage('test-save', storage)

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

  it('GIVEN setItem throws after a successful probe WHEN save is called THEN it reports write-error', () => {
    let writes = 0
    const storage = {
      getItem() {
        return null
      },
      setItem() {
        writes += 1
        if (writes > 1) {
          throw new DOMException('quota', 'QuotaExceededError')
        }
      },
      removeItem() {
        // probe succeeds
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
})

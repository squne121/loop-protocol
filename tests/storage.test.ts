import { describe, expect, it } from 'vitest'

import {
  createLocalGameStorage,
  parseSnapshot,
  serializeSnapshot,
} from '../src/storage'

describe('LocalGameStorage', () => {
  it('GIVEN a v1 snapshot WHEN it is serialized and parsed THEN schemaVersion 1 is preserved', () => {
    const snapshot = {
      schemaVersion: 1,
      resources: 5,
      weaponPower: 2,
      playerMaxHp: 10,
    } as const

    expect(parseSnapshot(serializeSnapshot(snapshot))).toEqual(snapshot)
  })

  it('GIVEN a legacy snapshot without schemaVersion WHEN it is parsed THEN it migrates to v1 and preserves existing values', () => {
    const legacySnapshot = {
      resources: 5,
      weaponPower: 2,
      playerMaxHp: 10,
    } as const

    expect(parseSnapshot(JSON.stringify(legacySnapshot))).toEqual({
      schemaVersion: 1,
      resources: 5,
      weaponPower: 2,
      playerMaxHp: 10,
    })
  })

  it('GIVEN invalid numeric values WHEN a v1 snapshot is parsed THEN resources clamp and other fields fall back', () => {
    expect(
      parseSnapshot(
        JSON.stringify({
          schemaVersion: 1,
          resources: 10_000_000,
          weaponPower: 0,
          playerMaxHp: Number.NaN,
        }),
      ),
    ).toEqual({
      schemaVersion: 1,
      resources: 9_999_999,
      weaponPower: 1,
      playerMaxHp: 8,
    })

    expect(
      parseSnapshot(
        JSON.stringify({
          schemaVersion: 1,
          resources: Number.NaN,
          weaponPower: Number.POSITIVE_INFINITY,
          playerMaxHp: 1.5,
        }),
      ),
    ).toEqual({
      schemaVersion: 1,
      resources: 0,
      weaponPower: 1,
      playerMaxHp: 8,
    })
  })

  it('GIVEN a v1 snapshot missing required fields WHEN it is parsed THEN it falls back to null', () => {
    expect(
      parseSnapshot(
        JSON.stringify({
          schemaVersion: 1,
          resources: 5,
          weaponPower: 2,
        }),
      ),
    ).toBeNull()
  })

  it('GIVEN an unknown future schemaVersion WHEN it is parsed THEN it falls back to null', () => {
    expect(
      parseSnapshot(
        JSON.stringify({
          schemaVersion: 999,
          resources: 5,
          weaponPower: 2,
          playerMaxHp: 10,
        }),
      ),
    ).toBeNull()
  })

  it('GIVEN corrupted JSON WHEN it is parsed THEN it falls back to null', () => {
    expect(parseSnapshot('{not-json')).toBeNull()
  })

  it('GIVEN a storage adapter WHEN it saves and loads a v1 snapshot THEN the data round-trips with schemaVersion', () => {
    const bag = new Map<string, string>()
    const storage = {
      getItem(key: string) {
        return bag.get(key) ?? null
      },
      setItem(key: string, value: string) {
        bag.set(key, value)
      },
    }
    const gameStorage = createLocalGameStorage('test-save', storage)

    const snapshot = {
      schemaVersion: 1,
      resources: 3,
      weaponPower: 4,
      playerMaxHp: 9,
    } as const

    gameStorage.save(snapshot)

    expect(gameStorage.load()).toEqual(snapshot)
  })
})

import { describe, expect, it } from 'vitest'

import {
  createLocalGameStorage,
  parseSnapshot,
  serializeSnapshot,
} from '../src/storage'

describe('LocalGameStorage', () => {
  it('serializes and restores a valid snapshot', () => {
    const snapshot = {
      resources: 5,
      weaponPower: 2,
      playerMaxHp: 10,
    }

    expect(parseSnapshot(serializeSnapshot(snapshot))).toEqual(snapshot)
  })

  it('loads persisted data from the provided storage adapter', () => {
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

    gameStorage.save({
      resources: 3,
      weaponPower: 4,
      playerMaxHp: 9,
    })

    expect(gameStorage.load()).toEqual({
      resources: 3,
      weaponPower: 4,
      playerMaxHp: 9,
    })
  })
})

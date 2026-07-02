import { describe, expect, it } from 'vitest'

import { upgradeDefinitions } from '../src/data/upgrades'

describe('upgradeDefinitions', () => {
  it('GIVEN the M4 minimal upgrade catalog WHEN loaded THEN it exposes only the product-spec entry as pure data', () => {
    expect(upgradeDefinitions).toHaveLength(1)
    expect(upgradeDefinitions[0]).toEqual({
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
    })
  })

  it('GIVEN the M4 minimal upgrade catalog WHEN definition identifiers are collected THEN every definitionId is unique', () => {
    const ids = upgradeDefinitions.map((definition) => definition.definitionId)

    expect(new Set(ids).size).toBe(ids.length)
  })
})

export interface UpgradeDefinition {
  readonly definitionId: string
  readonly schemaVersion: 1
  readonly currency: 'resources'
  readonly cost: number
  readonly effect: {
    readonly target: 'progress.weaponPower'
    readonly operation: 'add'
    readonly value: number
  }
  readonly availability: {
    readonly phase: 'preparation'
    readonly repeatable: false
  }
}

export const upgradeDefinitions = [
  {
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
  },
] as const satisfies readonly UpgradeDefinition[]

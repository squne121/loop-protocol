export interface EnemyDefinition {
  readonly definitionId: string
  readonly maxHp: number
  readonly radius: number
  readonly speedPxPerSec: number
  readonly contactDamage: number
}

export const enemyDefinitions = [
  {
    definitionId: 'enemy-basic',
    maxHp: 3,
    radius: 16,
    speedPxPerSec: 80,
    contactDamage: 0.05,
  },
] as const satisfies readonly EnemyDefinition[]

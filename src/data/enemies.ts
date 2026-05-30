export interface EnemyDefinition {
  definitionId: string
  maxHp: number
  radius: number
  speedPxPerSec: number
  contactDamage: number
}

export const enemyDefinitions: EnemyDefinition[] = [
  {
    definitionId: 'enemy-basic',
    maxHp: 3,
    radius: 16,
    speedPxPerSec: 80,
    contactDamage: 0.05,
  },
]

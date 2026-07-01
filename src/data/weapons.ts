export interface WeaponDefinition {
  readonly definitionId: string
  readonly ownerFaction: 'player' | 'ally' | 'enemy' | 'neutral'
  readonly fireCooldownMs: number
  readonly projectileRadius: number
  readonly projectileSpeedPxPerSec: number
  readonly projectileLifetimeMs: number
}

export const weaponDefinitions = [
  {
    definitionId: 'player_weapon_basic',
    ownerFaction: 'player',
    fireCooldownMs: 280,
    projectileRadius: 4,
    projectileSpeedPxPerSec: 520,
    projectileLifetimeMs: 1200,
  },
] as const satisfies readonly WeaponDefinition[]

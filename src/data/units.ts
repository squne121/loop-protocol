export interface UnitDefinition {
  readonly definitionId: string
  readonly faction: 'player' | 'ally' | 'enemy' | 'neutral'
  readonly role: 'ace_player' | 'ally_basic' | 'enemy_chaser' | 'objective' | 'neutral_obstacle'
  readonly radius: number
  readonly speedPxPerSec: number
  readonly initialBehaviorState: 'inactive' | 'acquire_target' | 'move_to_engage' | 'attack' | 'retreat' | 'destroyed'
  readonly defaultTargetingPolicy: 'focus_player' | 'assist_player_threat' | 'nearest_hostile' | 'ignore'
}

export const unitDefinitions = [
  {
    definitionId: 'ally_basic',
    faction: 'ally',
    role: 'ally_basic',
    radius: 12,
    speedPxPerSec: 140,
    initialBehaviorState: 'inactive',
    defaultTargetingPolicy: 'nearest_hostile',
  },
] as const satisfies readonly UnitDefinition[]

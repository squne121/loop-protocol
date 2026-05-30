import type { EntityId } from '../entities'

export interface ArenaState {
  width: number
  height: number
}

export interface EnemyState {
  id: number
  definitionId: string
  hp: number
  maxHp: number
  x: number
  y: number
  radius: number
  speedPxPerSec: number
  contactDamage: number
  defeated: boolean
  defeatedAtTick: number | null
}

export interface PlayerState {
  id: EntityId
  x: number
  y: number
  radius: number
  speed: number
  hp: number
  maxHp: number
  aimX: number
  aimY: number
  weaponCooldownMs: number
  weaponIntervalMs: number
  shotsFired: number
  /** Runtime-only: last non-zero aim direction. Not persisted in GameSnapshot. */
  lastAimDirectionX: number
  lastAimDirectionY: number
}

export interface ProjectileState {
  id: number
  x: number
  y: number
  radius: number
  directionX: number
  directionY: number
  speedPxPerSec: number
  ageMs: number
  lifetimeMs: number
  /** Damage snapshot taken from state.progress.weaponPower at fire time (AC4). */
  damage: number
}

/** Discriminated union for collision pairs resolved in a single tick (AC1, AC7). */
export type CollisionPair =
  | {
      kind: 'projectile-enemy'
      projectileId: number
      enemyId: number
      /** Squared distance between centers at collision time. */
      distSq: number
    }
  | {
      kind: 'player-enemy'
      enemyId: number
    }

export interface ProgressState {
  stageLabel: string
  resources: number
  weaponPower: number
}

export interface TelemetryState {
  status: string
  lastCommandSummary: string
}

export interface GameState {
  tick: number
  elapsedMs: number
  arena: ArenaState
  player: PlayerState
  projectiles: ProjectileState[]
  nextProjectileId: number
  enemies: EnemyState[]
  nextEnemyId: number
  progress: ProgressState
  telemetry: TelemetryState
}

export interface GameSnapshot {
  resources: number
  weaponPower: number
  playerMaxHp: number
}

export function createInitialGameState(
  snapshot: Partial<GameSnapshot> = {},
): GameState {
  const playerMaxHp = snapshot.playerMaxHp ?? 8

  return {
    tick: 0,
    elapsedMs: 0,
    arena: {
      width: 960,
      height: 540,
    },
    player: {
      id: 'player-alpha',
      x: 240,
      y: 270,
      radius: 14,
      speed: 210,
      hp: playerMaxHp,
      maxHp: playerMaxHp,
      aimX: 540,
      aimY: 270,
      weaponCooldownMs: 0,
      weaponIntervalMs: 280,
      shotsFired: 0,
      lastAimDirectionX: 1,
      lastAimDirectionY: 0,
    },
    progress: {
      stageLabel: 'MVP Sortie',
      resources: snapshot.resources ?? 0,
      weaponPower: snapshot.weaponPower ?? 1,
    },
    projectiles: [],
    nextProjectileId: 1,
    enemies: [],
    nextEnemyId: 1,
    telemetry: {
      status: 'Combat systems green',
      lastCommandSummary: 'Awaiting pilot input',
    },
  }
}

export function createGameSnapshot(state: GameState): GameSnapshot {
  return {
    resources: state.progress.resources,
    weaponPower: state.progress.weaponPower,
    playerMaxHp: state.player.maxHp,
  }
}

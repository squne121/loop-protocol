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
      tick: number
      projectileId: number
      enemyId: number
      priorityKey: string
      /** Squared distance between centers at collision time. Optional; used for tie-breaking. */
      distSq?: number
    }
  | {
      kind: 'player-enemy'
      tick: number
      playerId: EntityId
      enemyId: number
      priorityKey: string
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

// ---------------------------------------------------------------------------
// Sortie state machine types (AC1, AC5)
// ---------------------------------------------------------------------------

export type SortieStatus = 'idle' | 'running' | 'victory' | 'defeat' | 'ended'

export interface SortieResult {
  readonly outcome: 'victory' | 'defeat'
  readonly durationMs: number
  readonly kills: number
  readonly shotsFired: number
  readonly playerHpRemaining: number
}

/**
 * Discriminated union for sortie state.
 * - `idle`: not yet started; elapsedTicks fixed at 0, result is null
 * - `running`: in progress; elapsedTicks advances each tick, result is null
 * - `victory` | `defeat`: terminal; result is populated, no further mutation
 * - `ended`: reserved for post-result acknowledgement flows (not used in M2)
 */
export type SortieState =
  | {
      status: 'idle'
      elapsedTicks: 0
      targetTicks: number
      result: null
    }
  | {
      status: 'running'
      elapsedTicks: number
      targetTicks: number
      result: null
    }
  | {
      status: 'victory' | 'defeat'
      elapsedTicks: number
      targetTicks: number
      result: Readonly<SortieResult>
    }
  | {
      status: 'ended'
      elapsedTicks: number
      targetTicks: number
      result: Readonly<SortieResult>
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
  /** Sortie state machine (AC1). Managed by SortieSystem. */
  sortie: SortieState
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
    sortie: {
      status: 'idle',
      elapsedTicks: 0,
      // targetTicks will be set by startSortie(); placeholder 0 is overwritten before use
      targetTicks: 0,
      result: null,
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

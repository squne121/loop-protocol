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

export type RewardApplicationId = string

export interface RewardClaimState {
  /** Runtime-only ledger for exactly-once reward application. Not persisted in GameSnapshot. */
  claimedApplicationIds: Record<RewardApplicationId, true>
}

export interface TelemetryState {
  status: string
  lastCommandSummary: string
}

// ---------------------------------------------------------------------------
// Sortie state machine types (AC1, AC5)
// ---------------------------------------------------------------------------

export type SortieStatus = 'idle' | 'running' | 'victory' | 'defeat' | 'timeout' | 'ended'
/**
 * Game loop phase state machine (AC1).
 *
 * Transition policy:
 * - [*] → title_menu (initial)
 * - title_menu → preparation: New Game
 * - title_menu → load_menu: Load Game
 * - load_menu → preparation: Load slot-1 (storage.load())
 * - load_menu → title_menu: Back
 * - preparation → preparation: Save (storage.save())
 * - preparation → running: Start Sortie
 * - running → result: Victory / Defeat / Timeout
 * - result → preparation: Confirm result
 *
 * Save policy: storage.save() ONLY in preparation (AC2, AC8).
 * Load policy: storage.load() ONLY from title_menu / load_menu (AC3, AC9).
 */
export type LoopPhase =
  | 'title_menu'
  | 'load_menu'
  | 'preparation'
  | 'running'
  | 'result'
  | 'debrief_pending_reward'
  | 'debrief_reward_claimed'

/**
 * Result reward claim status — separates result display from reward application (AC10).
 */
export type ResultRewardStatus = 'pending' | 'claimed'

/**
 * Reason why a sortie ended. Discriminates the three terminal conditions.
 * - `all_enemies_defeated`: victory — all spawned enemies were defeated
 * - `player_hp_zero`:       defeat  — player HP reached 0
 * - `timeout`:              timeout — 30-second time limit elapsed with enemies remaining
 *
 * `survival_timer` is intentionally excluded (M2 scope — see Issue #542).
 */
export type SortieEndReason =
  | 'all_enemies_defeated'
  | 'player_hp_zero'
  | 'timeout'

type SortieResultBase = Readonly<{
  durationMs: number
  kills: number
  shotsFired: number
  playerHpRemaining: number
}>

/**
 * Discriminated union for sortie result.
 * `outcome` and `endReason` are constrained together to prevent invalid combinations
 * such as `{ outcome: 'victory', endReason: 'timeout' }`.
 */
export type SortieResult =
  | (SortieResultBase & {
      readonly outcome: 'victory'
      readonly endReason: 'all_enemies_defeated'
    })
  | (SortieResultBase & {
      readonly outcome: 'defeat'
      readonly endReason: 'player_hp_zero'
    })
  | (SortieResultBase & {
      readonly outcome: 'timeout'
      readonly endReason: 'timeout'
    })

/**
 * Discriminated union for sortie state.
 * - `idle`: not yet started; elapsedTicks fixed at 0, result is null
 * - `running`: in progress; elapsedTicks advances each tick, result is null
 * - `victory` | `defeat` | `timeout`: terminal; result is populated, no further mutation
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
      status: 'victory' | 'defeat' | 'timeout'
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
  loopPhase: LoopPhase
  /** Separates result display from reward application (AC10). Only meaningful in 'result' phase. */
  resultRewardStatus: ResultRewardStatus
  pendingRewardApplicationId: RewardApplicationId | null
  nextRewardApplicationSequence: number
  arena: ArenaState
  player: PlayerState
  projectiles: ProjectileState[]
  nextProjectileId: number
  enemies: EnemyState[]
  nextEnemyId: number
  progress: ProgressState
  rewardClaims: RewardClaimState
  telemetry: TelemetryState
  /** Sortie state machine (AC1). Managed by SortieSystem. */
  sortie: SortieState
}

export const gameSnapshotSchemaVersion = 1 as const

export interface GameSnapshot {
  schemaVersion: typeof gameSnapshotSchemaVersion
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
    loopPhase: 'preparation',
    resultRewardStatus: 'pending',
    pendingRewardApplicationId: null,
    nextRewardApplicationSequence: 1,
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
    rewardClaims: {
      claimedApplicationIds: Object.create(null) as Record<RewardApplicationId, true>,
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
    schemaVersion: gameSnapshotSchemaVersion,
    resources: state.progress.resources,
    weaponPower: state.progress.weaponPower,
    playerMaxHp: state.player.maxHp,
  }
}

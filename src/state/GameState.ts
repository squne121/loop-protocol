import type { EntityId } from '../entities'

// ---------------------------------------------------------------------------
// Unit taxonomy types (§5, unit-operations-and-npc-behavior SSOT)
// Type-level only. No runtime values, no enum objects.
// ---------------------------------------------------------------------------

export type Faction =
  | 'player'
  | 'ally'
  | 'enemy'
  | 'neutral'

export type UnitRole =
  | 'ace_player'
  | 'ally_basic'
  | 'enemy_chaser'
  | 'objective'
  | 'neutral_obstacle'

export type CommandIntent =
  | 'none'
  | 'assist_player'

// ---------------------------------------------------------------------------
// CommandIntent buffer types (§stage-7 contract, Issue #982)
// Wall-clock expiry (Date.now, performance.now) is intentionally absent.
// Expiry is deterministic: currentTick < expiresAtTick (AC2, AC7).
// ---------------------------------------------------------------------------

export type BufferedCommandIntent = Readonly<{
  intent: Extract<CommandIntent, 'assist_player'>
  sampledAtTick: number
  expiresAtTick: number
}>

export interface CommandIntentRuntimeState {
  activeIntent: CommandIntent
  bufferedIntent: BufferedCommandIntent | null
  /**
   * TTL in fixed ticks. Converted from ms at init time:
   *   ceil(ttlMs / fixedDeltaMs), clamped to [1, 180] (AC1).
   */
  assistPlayerTtlTicks: number
}

export type NpcBehaviorState =
  | 'inactive'
  | 'acquire_target'
  | 'move_to_engage'
  | 'attack'
  | 'retreat'
  | 'destroyed'

export type EnemyTaxonomyBehaviorState = Extract<
  NpcBehaviorState,
  'move_to_engage' | 'attack' | 'destroyed'
>

export type TargetingPolicy =
  | 'focus_player'
  | 'assist_player_threat'
  | 'nearest_hostile'
  | 'ignore'

export type TargetEntityId = string

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
  faction: 'enemy'
  role: 'enemy_chaser'
  behaviorState: EnemyTaxonomyBehaviorState
  targetingPolicy: 'focus_player'
  targetEntityId: TargetEntityId
}

export interface AllyState {
  id: number
  role: 'ally_basic'
  faction: 'ally'
  behaviorState: NpcBehaviorState
  targetingPolicy: Extract<TargetingPolicy, 'nearest_hostile' | 'assist_player_threat'>
  targetEntityId: TargetEntityId | null
  x: number
  y: number
  radius: number
  speedPxPerSec: number
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
  allies: AllyState[]
  nextAllyId: number
  progress: ProgressState
  rewardClaims: RewardClaimState
  telemetry: TelemetryState
  /** Sortie state machine (AC1). Managed by SortieSystem. */
  sortie: SortieState
  /** Command intent buffer runtime state (AC4, Issue #982). */
  commandIntentRuntime: CommandIntentRuntimeState
}

export const gameSnapshotSchemaVersion = 1 as const

export interface GameSnapshot {
  schemaVersion: typeof gameSnapshotSchemaVersion
  resources: number
  weaponPower: number
  playerMaxHp: number
}

// ---------------------------------------------------------------------------
// computeAssistPlayerTtlTicks — must precede createInitialGameState (used at init time)
// ---------------------------------------------------------------------------

/**
 * Compute assistPlayerTtlTicks from ms and fixedDeltaMs.
 * Converts wall-clock TTL to fixed-tick count: ceil(ttlMs / fixedDeltaMs).
 * Clamped to [1, 180] (AC1).
 *
 * @param ttlMs       TTL in milliseconds (e.g. 133)
 * @param fixedDeltaMs Fixed timestep in milliseconds (e.g. 1000/60 ≈ 16.667)
 */
export function computeAssistPlayerTtlTicks(ttlMs: number, fixedDeltaMs: number): number {
  return Math.min(180, Math.max(1, Math.ceil(ttlMs / fixedDeltaMs)))
}

// Default TTL parameters (AC1, Blocker 3)
const DEFAULT_TTL_MS = 133
const DEFAULT_FIXED_DELTA_MS = 1000 / 60

export function createDefaultAllyState(id: number): AllyState {
  return {
    id,
    role: 'ally_basic',
    faction: 'ally',
    behaviorState: 'inactive',
    targetingPolicy: 'nearest_hostile',
    targetEntityId: null,
    x: 160,
    y: 270,
    radius: 12,
    speedPxPerSec: 140,
  }
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
    allies: [],
    nextAllyId: 1,
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
    commandIntentRuntime: {
      activeIntent: 'none',
      bufferedIntent: null,
      // AC1: computed via computeAssistPlayerTtlTicks(133ms, 1000/60ms) = 8 ticks at 60Hz.
      // Clamped to [1, 180] as per AC1.
      assistPlayerTtlTicks: computeAssistPlayerTtlTicks(DEFAULT_TTL_MS, DEFAULT_FIXED_DELTA_MS),
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

// ---------------------------------------------------------------------------
// CommandIntent production helpers (Blockers 1–3, Issue #982)
// ---------------------------------------------------------------------------

/**
 * Returns true iff the buffered intent is still active at currentTick.
 * AC2: deterministic tick comparison — no Date.now / performance.now.
 *
 * Active condition: currentTick < expiresAtTick
 */
export function isBufferedCommandIntentActive(
  buffered: BufferedCommandIntent,
  currentTick: number,
): boolean {
  return currentTick < buffered.expiresAtTick
}

/**
 * Samples an assist_player intent at the given tick and updates the runtime state.
 * Creates a BufferedCommandIntent with expiresAtTick = sampledAtTick + assistPlayerTtlTicks (AC7).
 * Also updates activeIntent to 'assist_player' (AC5, Blocker 1).
 *
 * Call this from the tick step when a 'sample_assist_player' InputCommand is received.
 *
 * @param runtime      Mutable CommandIntentRuntimeState
 * @param currentTick  The current simulation tick
 */
export function sampleAssistPlayerIntent(
  runtime: CommandIntentRuntimeState,
  currentTick: number,
): void {
  const buffered: BufferedCommandIntent = {
    intent: 'assist_player',
    sampledAtTick: currentTick,
    expiresAtTick: currentTick + runtime.assistPlayerTtlTicks,
  }
  runtime.bufferedIntent = buffered
  runtime.activeIntent = 'assist_player'
}

/**
 * Advances the CommandIntentRuntimeState by one tick.
 * If the bufferedIntent has expired (currentTick >= expiresAtTick), clears it
 * and resets activeIntent to 'none'.
 *
 * Call this once per tick step, after consuming commands.
 *
 * @param runtime      Mutable CommandIntentRuntimeState
 * @param currentTick  The current simulation tick (post-increment, i.e. the tick being evaluated)
 */
export function tickCommandIntentRuntime(
  runtime: CommandIntentRuntimeState,
  currentTick: number,
): void {
  if (runtime.bufferedIntent !== null && !isBufferedCommandIntentActive(runtime.bufferedIntent, currentTick)) {
    runtime.bufferedIntent = null
    runtime.activeIntent = 'none'
  }
}

/**
 * Resets CommandIntentRuntimeState to initial state (Blocker 5, Issue #982).
 * Call this in resetCombatRuntime / startSortie to clear any stale intents from a previous sortie.
 *
 * @param runtime Mutable CommandIntentRuntimeState
 */
export function resetCommandIntentRuntime(runtime: CommandIntentRuntimeState): void {
  runtime.activeIntent = 'none'
  runtime.bufferedIntent = null
}

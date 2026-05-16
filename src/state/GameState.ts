import type { EntityId } from '../entities'

export interface ArenaState {
  width: number
  height: number
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
    },
    progress: {
      stageLabel: 'MVP Sortie',
      resources: snapshot.resources ?? 0,
      weaponPower: snapshot.weaponPower ?? 1,
    },
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

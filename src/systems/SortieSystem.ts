import type { GameState, SortieResult, SortieState } from '../state/GameState'
import { mapInputToCommands } from '../input'
import { runMovementSystem } from './MovementSystem'
import { runEnemySpawnSystem } from './EnemySpawnSystem'
import { runEnemyAISystem } from './EnemyAISystem'
import { runCombatSystem } from './CombatSystem'
import { runProjectileSystem } from './ProjectileSystem'
import { runCollisionSystem } from './CollisionSystem'
import { resolveCombatCollisions } from './CombatSystem'

/** Total sortie duration in milliseconds (120 seconds). */
export const SORTIE_DURATION_MS = 120_000

/**
 * Initialises the sortie state machine from `idle` to `running`.
 * Must be called exactly once after `createInitialGameState()`.
 *
 * @param state        Mutable game state
 * @param fixedDeltaMs Fixed timestep in milliseconds (used to compute targetTicks)
 */
export function startSortie(state: GameState, fixedDeltaMs: number): void {
  if (state.sortie.status !== 'idle') {
    return
  }

  const targetTicks = Math.ceil(SORTIE_DURATION_MS / fixedDeltaMs)

  state.sortie = {
    status: 'running',
    elapsedTicks: 0,
    targetTicks,
    result: null,
  }
}

/**
 * Advances the sortie state machine by one fixed timestep.
 *
 * Victory / defeat determination:
 * - Defeat is checked first (defeat takes precedence when both conditions hold simultaneously).
 * - Victory: `elapsedTicks >= targetTicks` after increment.
 * - `durationMs` is derived from `elapsedTicks * fixedDeltaMs` — never from `state.elapsedMs`.
 *
 * Terminal gate: if `status` is not `'running'`, the function returns immediately without mutation.
 *
 * @param state        Mutable game state
 * @param fixedDeltaMs Fixed timestep in milliseconds
 */
export function runSortieSystem(state: GameState, fixedDeltaMs: number): void {
  // AC13: terminal gate — only advance if running
  if (state.sortie.status !== 'running') {
    return
  }

  // Increment elapsed ticks
  state.sortie.elapsedTicks += 1

  const elapsedTicks = state.sortie.elapsedTicks
  const targetTicks = state.sortie.targetTicks

  // Capture terminalTick consistent with #488: defeatedAtTick = state.tick before increment
  const terminalTick = state.tick

  // AC14: defeat precedence — check defeat first
  const isDefeat = state.player.hp <= 0
  const isVictory = elapsedTicks >= targetTicks

  if (!isDefeat && !isVictory) {
    return
  }

  const outcome: 'victory' | 'defeat' = isDefeat ? 'defeat' : 'victory'

  const kills = state.enemies.filter(
    (e) =>
      e.defeated &&
      e.defeatedAtTick !== null &&
      e.defeatedAtTick <= terminalTick,
  ).length

  const playerHpRemaining =
    outcome === 'defeat'
      ? 0
      : Math.min(state.player.maxHp, Math.max(0, state.player.hp))

  const result = Object.freeze({
    outcome,
    durationMs: elapsedTicks * fixedDeltaMs,
    kills,
    shotsFired: state.player.shotsFired,
    playerHpRemaining,
  } satisfies SortieResult)

  const terminalState: SortieState = {
    status: outcome,
    elapsedTicks,
    targetTicks,
    result,
  }

  state.sortie = terminalState
}

/**
 * Orchestrates one full simulation step: all sub-systems + sortie state machine.
 * Used by `src/main.ts` to avoid logic duplication.
 *
 * @param state        Mutable game state
 * @param commands     Input commands for this tick
 * @param fixedDeltaMs Fixed timestep in milliseconds
 */
export function runSortieSimulationStep(
  state: GameState,
  commands: ReturnType<typeof mapInputToCommands>,
  fixedDeltaMs: number,
): void {
  if (state.sortie.status !== 'running') return
  runMovementSystem(state, commands, fixedDeltaMs)
  runEnemySpawnSystem(state)
  runEnemyAISystem(state, fixedDeltaMs)
  runCombatSystem(state, commands, fixedDeltaMs)
  runProjectileSystem(state, commands, fixedDeltaMs)
  const pairs = runCollisionSystem(state)
  resolveCombatCollisions(state, pairs)
  runSortieSystem(state, fixedDeltaMs)
  state.tick += 1
  state.elapsedMs += fixedDeltaMs
}

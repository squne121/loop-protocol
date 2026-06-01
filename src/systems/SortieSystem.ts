import type { GameState, SortieResult, SortieState } from '../state/GameState'
import { mapInputToCommands } from '../input'
import { runMovementSystem } from './MovementSystem'
import { runEnemySpawnSystem } from './EnemySpawnSystem'
import { runEnemyAISystem } from './EnemyAISystem'
import { runCombatSystem } from './CombatSystem'
import { runProjectileSystem } from './ProjectileSystem'
import { runCollisionSystem } from './CollisionSystem'
import { resolveCombatCollisions } from './CombatSystem'

/** Total sortie duration in milliseconds (30 seconds). */
export const SORTIE_DURATION_MS = 30_000

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
 * Terminal condition priority: defeat > victory > timeout
 * - defeat:  `player.hp <= 0`
 * - victory: all spawned enemies defeated (`state.enemies.length > 0 && every(e => e.defeated)`)
 *            `state.enemies.length > 0` guard prevents vacuous truth when no enemies have spawned yet.
 * - timeout: `elapsedTicks >= targetTicks` with enemies remaining → treated as defeat
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

  // Priority: defeat (player.hp <= 0) > victory (all enemies defeated) > timeout (30s → defeat)
  const isDefeat = state.player.hp <= 0
  const allEnemiesDefeated =
    state.enemies.length > 0 && state.enemies.every((e) => e.defeated)
  const isVictory = allEnemiesDefeated
  const isTimeout = elapsedTicks >= targetTicks

  if (!isDefeat && !isVictory && !isTimeout) {
    return
  }

  const outcome: 'victory' | 'defeat' = isDefeat ? 'defeat' : isVictory ? 'victory' : 'defeat'

  const kills = state.enemies.filter(
    (e) =>
      e.defeated &&
      e.defeatedAtTick !== null &&
      e.defeatedAtTick <= terminalTick,
  ).length

  // playerHpRemaining is 0 only for HP defeat (player.hp <= 0).
  // timeout defeat and victory retain the actual HP snapshot.
  const playerHpRemaining = isDefeat
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

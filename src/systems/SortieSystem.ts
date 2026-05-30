import type { GameState, SortieResult, SortieState } from '../state/GameState'

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
    (e) => e.defeatedAtTick !== null && e.defeatedAtTick <= terminalTick,
  ).length

  const result: Readonly<SortieResult> = {
    outcome,
    durationMs: elapsedTicks * fixedDeltaMs,
    kills,
    shotsFired: state.player.shotsFired,
    playerHpRemaining: state.player.hp,
  }

  const terminalState: SortieState = {
    status: outcome,
    elapsedTicks,
    targetTicks,
    result,
  }

  state.sortie = terminalState
}

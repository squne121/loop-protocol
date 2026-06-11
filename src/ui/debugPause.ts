/**
 * src/ui/debugPause.ts
 *
 * Runtime-local debug pause/resume surface (Issue #786).
 * Not part of LoopPhase / GameState / snapshot schema.
 */
import type { InputState } from '../input/InputState'

/** Opaque runtime-local debug pause state. */
export interface DebugPauseState {
  isPaused: boolean
}

/** Create a fresh (unpaused) debug pause state. */
export function createDebugPauseState(): DebugPauseState {
  return { isPaused: false }
}

/**
 * Toggle pause/resume.
 * Called from Escape keydown (event.repeat=false guard) and the HUD button.
 */
export function toggleDebugPause(ps: DebugPauseState): void {
  ps.isPaused = !ps.isPaused
}

/**
 * Clear firing / pointer active state when entering pause.
 * Prevents held fire from bleeding into resume (AC7).
 */
export function resetInputOnPause(
  input: Pick<InputState, 'primaryPressed' | 'activePointerId'>,
): void {
  input.primaryPressed = false
  input.activePointerId = null
}

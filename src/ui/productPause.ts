/**
 * src/ui/productPause.ts
 *
 * Product-facing Pause/Resume surface (Issue #884).
 * Promoted from debug-only surface to product Pause.
 * Runtime-local: not persisted to GameSnapshot / localStorage / SortieResult.
 */
import type { InputState } from '../input/InputState'

/** Opaque runtime-local product pause state. */
export interface ProductPauseState {
  isPaused: boolean
}

/** Create a fresh (unpaused) product pause state. */
export function createProductPauseState(): ProductPauseState {
  return { isPaused: false }
}

/**
 * Toggle pause/resume.
 * Called from Escape keydown (event.repeat=false guard), the HUD button,
 * or visibilitychange (auto-pause on hidden only).
 */
export function toggleProductPause(ps: ProductPauseState): void {
  ps.isPaused = !ps.isPaused
}

/**
 * Clear firing / pointer active state when entering pause.
 * Prevents held fire from bleeding into resume (AC5).
 */
export function resetInputOnPause(
  input: Pick<InputState, 'primaryPressed' | 'activePointerId'>,
): void {
  input.primaryPressed = false
  input.activePointerId = null
}

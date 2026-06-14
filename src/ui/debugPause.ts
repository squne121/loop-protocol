/**
 * src/ui/debugPause.ts
 *
 * @deprecated Deprecated wrapper for backward compatibility (Issue #884).
 * Use src/ui/productPause.ts instead.
 * This module re-exports from productPause with legacy naming.
 */
import type { InputState } from '../input/InputState'
import {
  type ProductPauseState,
  createProductPauseState,
  toggleProductPause,
  resetInputOnPause as resetInputOnPauseImpl,
} from './productPause'

/** @deprecated Use ProductPauseState from productPause.ts instead. */
export type DebugPauseState = ProductPauseState

/** @deprecated Use createProductPauseState() instead. */
export function createDebugPauseState(): DebugPauseState {
  return createProductPauseState()
}

/** @deprecated Use toggleProductPause() instead. */
export function toggleDebugPause(ps: DebugPauseState): void {
  toggleProductPause(ps)
}

/**
 * Clear firing / pointer active state when entering pause.
 * @deprecated Use resetInputOnPause() from productPause.ts instead.
 */
export function resetInputOnPause(
  input: Pick<InputState, 'primaryPressed' | 'activePointerId'>,
): void {
  resetInputOnPauseImpl(input)
}

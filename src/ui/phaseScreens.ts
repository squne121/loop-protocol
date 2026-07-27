import type { LoopPhase } from '../state'

/**
 * Named phase screens rendered inside the battle HUD layer (Issue #1374).
 * Exactly one screen (or none) is visible at a time. Visibility is derived
 * solely from `state.loopPhase` (AC5) — this module never introduces a
 * separate `uiScreen` state.
 */
export type PhaseScreenId = 'title' | 'load' | 'preparation'

const PHASE_SCREEN_BY_LOOP_PHASE: Partial<Record<LoopPhase, PhaseScreenId>> = {
  title_menu: 'title',
  load_menu: 'load',
  preparation: 'preparation',
}

/**
 * Returns the phase screen that should be visible for `loopPhase`, or
 * `null` when no phase screen applies (e.g. `running` — AC3, AC6: the
 * large title / preparation panels are hidden and inert and are excluded
 * from the keyboard tab order).
 */
export function getVisiblePhaseScreen(loopPhase: LoopPhase): PhaseScreenId | null {
  return PHASE_SCREEN_BY_LOOP_PHASE[loopPhase] ?? null
}

/**
 * Whether the shared "Load Game" confirm control (the control that
 * performs the actual `storage.load()` via `onLoadGame`) should be
 * reachable. It only makes sense from `title_menu` (opens load_menu) and
 * `load_menu` (performs the load) — AC1, AC3, AC9.
 */
export function isLoadGameReachable(loopPhase: LoopPhase): boolean {
  return loopPhase === 'title_menu' || loopPhase === 'load_menu'
}

/** `load_menu` can be entered from `title_menu` or `preparation` (AC8). */
export type LoadMenuOrigin = 'title_menu' | 'preparation'

/**
 * Resolves the correct Back intent for `load_menu` based on the phase it
 * was opened from (AC8). Unknown / unset origins fall back to
 * `back_to_title` (the pre-existing sole Back destination).
 */
export function resolveLoadMenuBackIntent(
  origin: LoadMenuOrigin,
): 'back_to_title' | 'back_to_preparation' {
  return origin === 'preparation' ? 'back_to_preparation' : 'back_to_title'
}

/**
 * Sets `hidden` and `inert` together so a phase screen panel (or an
 * individual control) is excluded from the accessibility tree and the
 * keyboard tab order when it is not part of the active screen (AC3, AC6).
 */
export function setPhaseScreenVisibility(element: HTMLElement, visible: boolean): void {
  element.hidden = !visible
  if (visible) {
    element.removeAttribute('inert')
  } else {
    element.setAttribute('inert', '')
  }
}

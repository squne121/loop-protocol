import type { GameState, LoopPhase } from '../state'
import type { HudUpgradeViewModel } from './HudController'

/**
 * Named phase screens rendered inside the battle-stage `screen` overlay layer
 * (`battle-screen-layer`, Issue #1374 PR #1815 review fix). Exactly one
 * screen (or none) is visible at a time. Visibility is derived solely from
 * `state.loopPhase` (AC5) — this module never introduces a separate
 * `uiScreen` state.
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
 * `inert` alone has no visual effect, so `hidden` is required alongside it
 * (PR #1815 review: "妥当な実装部分" — this helper is unchanged).
 */
export function setPhaseScreenVisibility(element: HTMLElement, visible: boolean): void {
  element.hidden = !visible
  if (visible) {
    element.removeAttribute('inert')
    // `configureBattleOverlayFoundation()` (src/ui/battleOverlay.ts) sets
    // aria-hidden="true" on the screen layer once at bootstrap, before any
    // phase screen is active. That must be cleared here too, or the
    // accessibility tree (and Playwright's getByRole()) keeps treating this
    // subtree as hidden even after `hidden` / `inert` are correctly removed
    // (Issue #1374 PR #1815 review, required fix 3 regression found while
    // writing the real Playwright e2e coverage).
    element.removeAttribute('aria-hidden')
  } else {
    element.setAttribute('inert', '')
    element.setAttribute('aria-hidden', 'true')
  }
}

/**
 * Intent-only action surface (PR #1815 review, required fix 5): the
 * controller never mutates `GameState` itself. `onOpenLoadMenu` /
 * `onBackFromLoadMenu` only notify the caller (`main.ts`) of the player's
 * intent; the caller owns `transitionByIntent()`, origin bookkeeping,
 * feedback copy, and focus/state consistency in one place.
 */
export interface PhaseScreenActions {
  onNewGame(): void
  /** Player pressed "Open save" on the title or preparation screen (AC8). */
  onOpenLoadMenu(origin: LoadMenuOrigin): void
  /** Player pressed "Back" on the load screen (AC8). */
  onBackFromLoadMenu(): void
  /** Player pressed "Load saved game" while on the load screen (performs the real load). */
  onConfirmLoad(): void
  onStartSortie(): void
  onSave(): void
  onReset(): void
  onUpgradeWeapon(): void
  /** Whether a loadable snapshot exists (AC3, AC9). */
  canLoadGame(): boolean
}

export interface PhaseScreenController {
  /** Renders the phase screens. Called every frame like `HudController.render()`. */
  render(state: GameState, upgradeView?: HudUpgradeViewModel): void
}

function queryAction(container: HTMLElement, name: string): HTMLButtonElement {
  const element = container.querySelector<HTMLButtonElement>(`[data-action="${name}"]`)
  if (!element) {
    throw new Error(`Phase screen action "${name}" is missing.`)
  }
  return element
}

function queryField(container: HTMLElement, name: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(`[data-field="${name}"]`)
  if (!element) {
    throw new Error(`Phase screen field "${name}" is missing.`)
  }
  return element
}

function queryScreen(container: HTMLElement, screen: PhaseScreenId): HTMLElement {
  const element = container.querySelector<HTMLElement>(`[data-phase-screen="${screen}"]`)
  if (!element) {
    throw new Error(`Phase screen "${screen}" is missing.`)
  }
  return element
}

/** Focusable-element query used by the WAI-ARIA APG modal focus trap (PR #1815 review, required fix 4). */
function getFocusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  )
}

/**
 * Creates the phase screen controller. `container` must be the battle-stage
 * `screen` overlay layer (`battle-screen-layer`, `data-battle-layer="screen"`
 * in `src/main.ts`) — never the HUD layer (PR #1815 review, required fix 1).
 * `title` / `load` / `preparation` render as a single centered menu overlay
 * panel here; `HudController` (the HUD layer) keeps only the minimal
 * running-time HUD.
 */
export function createPhaseScreenController(
  container: HTMLElement,
  actions: PhaseScreenActions,
): PhaseScreenController {
  container.innerHTML = `
    <div
      class="phase-screen-panel panel panel--accent"
      data-phase-screen="title"
      role="dialog"
      aria-modal="true"
      aria-labelledby="phase-screen-title-heading"
      hidden
      inert
    >
      <p class="eyebrow">Command deck</p>
      <h1 id="phase-screen-title-heading" tabindex="-1">LOOP_PROTOCOL</h1>
      <p class="lede">Canvas battle sandbox with DOM-side command surfaces.</p>
      <button type="button" data-action="new-game" data-battle-interactive="true">Begin new run</button>
      <button type="button" data-action="open-load-menu-title" data-battle-interactive="true">Open save</button>
    </div>
    <div
      class="phase-screen-panel panel"
      data-phase-screen="load"
      role="dialog"
      aria-modal="true"
      aria-labelledby="phase-screen-load-heading"
      hidden
      inert
    >
      <p class="eyebrow" id="phase-screen-load-heading" tabindex="-1">Load Game</p>
      <p class="status-copy status-copy--muted" data-field="load-availability"></p>
      <p class="status-copy" data-field="load-status" role="status" aria-live="polite" aria-atomic="true"></p>
      <button type="button" data-action="confirm-load" data-battle-interactive="true">Load saved game</button>
      <button type="button" data-action="back-from-load-menu" data-battle-interactive="true">Back</button>
    </div>
    <div
      class="phase-screen-panel panel"
      data-phase-screen="preparation"
      role="dialog"
      aria-modal="true"
      aria-labelledby="phase-screen-preparation-heading"
      hidden
      inert
    >
      <p class="eyebrow" id="phase-screen-preparation-heading" tabindex="-1">Mission briefing</p>
      <dl class="stat-grid">
        <div><dt>Resources</dt><dd data-field="prep-resources"></dd></div>
        <div><dt>Weapon Power</dt><dd data-field="prep-weapon-power"></dd></div>
      </dl>
      <button type="button" data-action="upgrade-weapon" data-battle-interactive="true">Upgrade weapon</button>
      <p class="status-copy status-copy--muted" data-field="prep-upgrade-cost"></p>
      <p class="status-copy" data-field="prep-upgrade-status" role="status" aria-live="polite" aria-atomic="true"></p>
      <button type="button" data-action="start-sortie" data-battle-interactive="true">Launch sortie</button>
      <button type="button" data-action="save" data-battle-interactive="true">Save progress</button>
      <button type="button" data-action="open-load-menu-preparation" data-battle-interactive="true">Open save</button>
      <button
        type="button"
        data-action="reset"
        data-battle-interactive="true"
        title="Reset sortie is a destructive boundary and is only available during preparation."
      >
        Reset sortie
      </button>
    </div>
  `

  const titleScreen = queryScreen(container, 'title')
  const loadScreen = queryScreen(container, 'load')
  const preparationScreen = queryScreen(container, 'preparation')

  const newGameButton = queryAction(container, 'new-game')
  const openLoadMenuTitleButton = queryAction(container, 'open-load-menu-title')
  const confirmLoadButton = queryAction(container, 'confirm-load')
  const backFromLoadMenuButton = queryAction(container, 'back-from-load-menu')
  const startSortieButton = queryAction(container, 'start-sortie')
  const saveButton = queryAction(container, 'save')
  const openLoadMenuPreparationButton = queryAction(container, 'open-load-menu-preparation')
  const resetButton = queryAction(container, 'reset')
  const upgradeWeaponButton = queryAction(container, 'upgrade-weapon')

  const loadAvailabilityField = queryField(container, 'load-availability')
  const loadStatusField = queryField(container, 'load-status')
  const prepResourcesField = queryField(container, 'prep-resources')
  const prepWeaponPowerField = queryField(container, 'prep-weapon-power')
  const prepUpgradeCostField = queryField(container, 'prep-upgrade-cost')
  const prepUpgradeStatusField = queryField(container, 'prep-upgrade-status')

  newGameButton.addEventListener('click', actions.onNewGame)
  openLoadMenuTitleButton.addEventListener('click', () => actions.onOpenLoadMenu('title_menu'))
  openLoadMenuPreparationButton.addEventListener('click', () => actions.onOpenLoadMenu('preparation'))
  backFromLoadMenuButton.addEventListener('click', actions.onBackFromLoadMenu)
  confirmLoadButton.addEventListener('click', actions.onConfirmLoad)
  startSortieButton.addEventListener('click', actions.onStartSortie)
  saveButton.addEventListener('click', actions.onSave)
  resetButton.addEventListener('click', actions.onReset)
  upgradeWeaponButton.addEventListener('click', actions.onUpgradeWeapon)

  // WAI-ARIA APG dialog (modal) focus management (PR #1815 review, required
  // fix 4): move focus into the screen when it opens, trap Tab within it,
  // and return focus to the control that opened it when it closes.
  let invokerElement: HTMLElement | null = null
  let lastVisibleScreen: PhaseScreenId | null = null

  container.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab' || lastVisibleScreen === null) {
      return
    }
    const activeScreenEl = queryScreen(container, lastVisibleScreen)
    const focusable = getFocusableElements(activeScreenEl)
    if (focusable.length === 0) {
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    const current = document.activeElement
    if (event.shiftKey && current === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && current === last) {
      event.preventDefault()
      first.focus()
    }
  })

  return {
    render(state, upgradeView) {
      const visibleScreen = getVisiblePhaseScreen(state.loopPhase)

      // The outer screen-layer container itself is hidden/inert unless a
      // phase screen is active, so it never intercepts pointer/keyboard
      // input over the Canvas during `running` (AC3, AC6).
      setPhaseScreenVisibility(container, visibleScreen !== null)
      setPhaseScreenVisibility(titleScreen, visibleScreen === 'title')
      setPhaseScreenVisibility(loadScreen, visibleScreen === 'load')
      setPhaseScreenVisibility(preparationScreen, visibleScreen === 'preparation')

      if (visibleScreen !== lastVisibleScreen) {
        if (visibleScreen !== null && lastVisibleScreen === null) {
          // Opening: remember the invoker so it can be restored on close.
          invokerElement = document.activeElement instanceof HTMLElement ? document.activeElement : null
          const screenEl = queryScreen(container, visibleScreen)
          const heading = screenEl.querySelector<HTMLElement>('[id$="-heading"]')
          heading?.focus()
        } else if (visibleScreen === null && lastVisibleScreen !== null) {
          // Closing: return focus to whatever opened the screen.
          if (invokerElement && document.contains(invokerElement)) {
            invokerElement.focus()
          }
          invokerElement = null
        } else if (visibleScreen !== null) {
          // Switched directly between two screens (e.g. load -> preparation).
          const screenEl = queryScreen(container, visibleScreen)
          const heading = screenEl.querySelector<HTMLElement>('[id$="-heading"]')
          heading?.focus()
        }
        lastVisibleScreen = visibleScreen
      }

      // Navigating TO load_menu is always allowed from title_menu /
      // preparation (AC1, AC2) -- the load-menu-empty scenario (PR #1815
      // review, required fix 6) depends on being able to reach the load
      // screen and see "no save" messaging there. Only the actual load
      // action (confirm-load, below) is gated on a save existing.
      const canLoad = actions.canLoadGame()
      openLoadMenuTitleButton.disabled = state.loopPhase !== 'title_menu'
      openLoadMenuPreparationButton.disabled = state.loopPhase !== 'preparation'
      confirmLoadButton.disabled = state.loopPhase !== 'load_menu' || !canLoad
      backFromLoadMenuButton.disabled = state.loopPhase !== 'load_menu'
      newGameButton.disabled = state.loopPhase !== 'title_menu'
      startSortieButton.disabled = state.loopPhase !== 'preparation'
      saveButton.disabled = state.loopPhase !== 'preparation'
      resetButton.disabled = state.loopPhase !== 'preparation'

      loadAvailabilityField.textContent = canLoad
        ? 'A save is available to load.'
        : 'No save is available yet.'
      // Load-specific feedback lives inside the load screen itself (PR #1815
      // review, required fix 4) instead of a separate persistent panel.
      loadStatusField.textContent = state.loopPhase === 'load_menu' ? state.telemetry.status : ''

      prepResourcesField.textContent = `${state.progress.resources}`
      prepWeaponPowerField.textContent = `${upgradeView?.weaponPower ?? state.progress.weaponPower}`

      if (upgradeView) {
        upgradeWeaponButton.disabled = upgradeView.buttonDisabled
        prepUpgradeCostField.textContent = `Cost: ${upgradeView.cost}`
        prepUpgradeStatusField.textContent = upgradeView.statusCopy
          ? `${upgradeView.statusCopy.status} ${upgradeView.statusCopy.summary}`
          : ''
      } else {
        upgradeWeaponButton.disabled = true
        prepUpgradeCostField.textContent = ''
        prepUpgradeStatusField.textContent = ''
      }
    },
  }
}

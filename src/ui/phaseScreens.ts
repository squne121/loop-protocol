import type { GameState, LoopPhase, SortieResult } from '../state'
import type { HudUpgradeViewModel } from './HudController'
import { RewardSystem } from '../systems/RewardSystem'

/**
 * Named phase screens rendered inside the battle-stage `screen` overlay layer
 * (`battle-screen-layer`, Issue #1374 PR #1815 review fix, extended by Issue
 * #1376 to also own `result` and `pause`). Exactly one screen (or none) is
 * visible at a time. Visibility is derived solely from `(state.loopPhase,
 * isPaused)` (AC2) — this module never introduces a separate mutable
 * screen-id state (no independent "ui screen" field).
 */
export type PhaseScreenId = 'title' | 'load' | 'preparation' | 'result' | 'pause'

const PHASE_SCREEN_BY_LOOP_PHASE: Partial<Record<LoopPhase, PhaseScreenId>> = {
  title_menu: 'title',
  load_menu: 'load',
  preparation: 'preparation',
  result: 'result',
}

/**
 * Returns the phase screen that should be visible for `(loopPhase,
 * isPaused)` (AC2, AC3): `isPaused` takes priority over `loopPhase` so the
 * pause dialog interrupts whichever phase is active (in practice always
 * `running`, since pause entry is gated on `loopPhase === 'running'`).
 * `running` (unpaused) maps to `null` — no phase screen; the large title /
 * preparation / result panels are hidden and inert and are excluded from the
 * keyboard tab order.
 */
export function getVisiblePhaseScreen(loopPhase: LoopPhase, isPaused: boolean): PhaseScreenId | null {
  if (isPaused) {
    return 'pause'
  }
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
 * Player-facing outcome copy for the result screen (AC8, AC9). Mirrors
 * `HudController.ts`'s private `getOutcomeCopy()` mapping table but reads a
 * `SortieResult['outcome']` directly since the result screen's reward
 * summary is built from `RewardSystem.calculate()`'s own `outcome` field.
 */
function resultOutcomeLabel(outcome: SortieResult['outcome']): string {
  switch (outcome) {
    case 'victory':
      return 'Victory'
    case 'defeat':
      return 'Defeat'
    case 'timeout':
      return '戦闘終了'
  }
}

interface ResultRewardView {
  outcomeLabel: string
  kills: string
  duration: string
  base: string
  killBonus: string
  hpBonus: string
  delta: string
}

/**
 * Builds the result screen's reward summary view model (AC8) from
 * `RewardSystem.calculate(state.sortie.result)`'s return value ONLY — never
 * re-derives or re-computes reward numbers locally, and never mutates
 * `state` (render-time read, matching `src/ui`'s read-only contract over
 * `src/state`). Returns `null` when there is no terminal result yet (should
 * not happen while the result screen is visible, but this keeps `render()`
 * defensive against being called before a sortie has ended).
 */
function buildResultRewardView(state: GameState): ResultRewardView | null {
  const result = state.sortie.result
  if (result === null) {
    return null
  }

  const quote = RewardSystem.calculate(result)

  return {
    outcomeLabel: resultOutcomeLabel(quote.outcome),
    kills: `${result.kills}`,
    duration: `${(result.durationMs / 1000).toFixed(1)}s`,
    base: `${quote.base}`,
    killBonus: `${quote.killBonus}`,
    hpBonus: `${quote.hpBonus}`,
    delta: `${quote.delta}`,
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
  /** Player pressed "Return to hangar" on the result screen (AC1, AC5, AC7, AC9; moved from HudController in Issue #1376). */
  onConfirmResult(): void
  /** Player pressed "Resume" on the pause dialog (AC3, AC6). */
  onResume(): void
}

export interface PhaseScreenController {
  /**
   * Renders the phase screens. Called every frame like `HudController.render()`.
   * `isPaused` is the runtime-local product pause flag (AC2, AC3) — the
   * pause dialog's visibility is derived from it, never from a separate
   * mutable screen-id state.
   */
  render(state: GameState, isPaused: boolean, upgradeView?: HudUpgradeViewModel): void
}

/** Options for `createPhaseScreenController()` (Issue #1376). */
export interface PhaseScreenControllerOptions {
  /**
   * The battle-stage Canvas element. Used as the AC6 resume-focus fallback
   * when a pause dialog closes with no valid invoker to restore focus to
   * (e.g. auto-pause via `visibilitychange`, which records no invoker).
   */
  canvas?: HTMLElement | null
  /**
   * Returns the element that invoked the current pause dialog (AC6). Called
   * once, at the moment the pause screen opens (`null` -> `'pause'`
   * transition), instead of capturing `document.activeElement` the way
   * every other screen does -- `src/main.ts`'s `enterProductPause()` is the
   * single source of truth for which element counts as "the invoker"
   * (`null` for triggers with no meaningful invoking control, e.g. auto-pause
   * via `visibilitychange`).
   */
  getPauseInvoker?: () => HTMLElement | null
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
 * Resolves the element that should receive focus when `screen` becomes the
 * active phase screen (AC7). The pause dialog is the sole exception to the
 * "focus the tabindex=-1 heading" convention: its initial focus target is
 * the Resume button (AC3, AC6), never its heading, since Resume is the
 * single actionable control the player needs immediately.
 */
function getInitialFocusTarget(container: HTMLElement, screen: PhaseScreenId): HTMLElement | null {
  const screenEl = queryScreen(container, screen)
  if (screen === 'pause') {
    return screenEl.querySelector<HTMLElement>('[data-action="resume"]')
  }
  return screenEl.querySelector<HTMLElement>('[id$="-heading"]')
}

/**
 * Creates the phase screen controller. `container` must be the battle-stage
 * `screen` overlay layer (`battle-screen-layer`, `data-battle-layer="screen"`
 * in `src/main.ts`) — never the HUD layer (PR #1815 review, required fix 1).
 * `title` / `load` / `preparation` / `result` / `pause` render as a single
 * centered menu/dialog overlay panel here (Issue #1376: result and pause
 * joined title/load/preparation, replacing the temporary
 * `data-legacy-result-surface` / ad hoc pause-entry call sites);
 * `HudController` (the HUD layer) keeps only the minimal running-time
 * combat HUD and the temporary legacy debrief surface.
 */
export function createPhaseScreenController(
  container: HTMLElement,
  actions: PhaseScreenActions,
  options: PhaseScreenControllerOptions = {},
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
      <p
        class="status-copy"
        data-field="prep-status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      ></p>
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
    <div
      class="phase-screen-panel panel"
      data-phase-screen="result"
      role="dialog"
      aria-modal="true"
      aria-labelledby="phase-screen-result-heading"
      hidden
      inert
    >
      <p class="eyebrow" id="phase-screen-result-heading" tabindex="-1" data-field="result-outcome"></p>
      <dl class="stat-grid">
        <div><dt>Kills</dt><dd data-field="result-kills"></dd></div>
        <div><dt>Duration</dt><dd data-field="result-duration"></dd></div>
      </dl>
      <dl class="stat-grid">
        <div><dt>Base reward</dt><dd data-field="reward-base"></dd></div>
        <div><dt>Kill bonus</dt><dd data-field="reward-kill-bonus"></dd></div>
        <div><dt>HP bonus</dt><dd data-field="reward-hp-bonus"></dd></div>
        <div><dt>Total reward</dt><dd data-field="reward-delta"></dd></div>
      </dl>
      <button type="button" data-action="confirm-result" data-battle-interactive="true">Return to hangar</button>
    </div>
    <div
      class="phase-screen-panel panel"
      data-phase-screen="pause"
      role="dialog"
      aria-modal="true"
      aria-labelledby="phase-screen-pause-heading"
      hidden
      inert
    >
      <p class="eyebrow" id="phase-screen-pause-heading" tabindex="-1">Paused</p>
      <p class="status-copy">Simulation frozen. Rendering and HUD continue.</p>
      <button type="button" data-action="resume" data-battle-interactive="true">Resume</button>
    </div>
  `

  const titleScreen = queryScreen(container, 'title')
  const loadScreen = queryScreen(container, 'load')
  const preparationScreen = queryScreen(container, 'preparation')
  const resultScreen = queryScreen(container, 'result')
  const pauseScreen = queryScreen(container, 'pause')

  const newGameButton = queryAction(container, 'new-game')
  const openLoadMenuTitleButton = queryAction(container, 'open-load-menu-title')
  const confirmLoadButton = queryAction(container, 'confirm-load')
  const backFromLoadMenuButton = queryAction(container, 'back-from-load-menu')
  const startSortieButton = queryAction(container, 'start-sortie')
  const saveButton = queryAction(container, 'save')
  const openLoadMenuPreparationButton = queryAction(container, 'open-load-menu-preparation')
  const resetButton = queryAction(container, 'reset')
  const upgradeWeaponButton = queryAction(container, 'upgrade-weapon')
  const confirmResultButton = queryAction(container, 'confirm-result')
  const resumeButton = queryAction(container, 'resume')

  const loadAvailabilityField = queryField(container, 'load-availability')
  const loadStatusField = queryField(container, 'load-status')
  const prepResourcesField = queryField(container, 'prep-resources')
  const prepWeaponPowerField = queryField(container, 'prep-weapon-power')
  const prepUpgradeCostField = queryField(container, 'prep-upgrade-cost')
  const prepUpgradeStatusField = queryField(container, 'prep-upgrade-status')
  const prepStatusField = queryField(container, 'prep-status')
  const resultOutcomeField = queryField(container, 'result-outcome')
  const resultKillsField = queryField(container, 'result-kills')
  const resultDurationField = queryField(container, 'result-duration')
  const rewardBaseField = queryField(container, 'reward-base')
  const rewardKillBonusField = queryField(container, 'reward-kill-bonus')
  const rewardHpBonusField = queryField(container, 'reward-hp-bonus')
  const rewardDeltaField = queryField(container, 'reward-delta')

  newGameButton.addEventListener('click', actions.onNewGame)
  openLoadMenuTitleButton.addEventListener('click', () => actions.onOpenLoadMenu('title_menu'))
  openLoadMenuPreparationButton.addEventListener('click', () => actions.onOpenLoadMenu('preparation'))
  backFromLoadMenuButton.addEventListener('click', actions.onBackFromLoadMenu)
  confirmLoadButton.addEventListener('click', actions.onConfirmLoad)
  startSortieButton.addEventListener('click', actions.onStartSortie)
  saveButton.addEventListener('click', actions.onSave)
  resetButton.addEventListener('click', actions.onReset)
  upgradeWeaponButton.addEventListener('click', actions.onUpgradeWeapon)
  // AC1, AC5, AC7, AC9 (Issue #1376): Return to hangar moved here from the
  // temporary HudController legacy result surface. `confirmResultButton`'s
  // own `disabled` state (set in render() below) is the exactly-once guard
  // (AC10) — `confirmResult()` (src/systems/SortieSystem.ts) is itself a
  // no-op outside `result` phase, so even a rapid double-activation before
  // the next render() lands cannot double-claim/double-save.
  confirmResultButton.addEventListener('click', actions.onConfirmResult)
  resumeButton.addEventListener('click', actions.onResume)

  // WAI-ARIA APG dialog (modal) focus management (PR #1815 review, required
  // fix 4): move focus into the screen when it opens, trap Tab within it,
  // and return focus to the control that opened it when it closes (AC6:
  // for the pause dialog specifically, fall back to the Canvas when no
  // valid invoker is available, e.g. auto-pause via visibilitychange).
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
    render(state, isPaused, upgradeView) {
      const visibleScreen = getVisiblePhaseScreen(state.loopPhase, isPaused)

      // The outer screen-layer container itself is hidden/inert unless a
      // phase screen is active, so it never intercepts pointer/keyboard
      // input over the Canvas during `running` (AC3, AC6).
      setPhaseScreenVisibility(container, visibleScreen !== null)
      setPhaseScreenVisibility(titleScreen, visibleScreen === 'title')
      setPhaseScreenVisibility(loadScreen, visibleScreen === 'load')
      setPhaseScreenVisibility(preparationScreen, visibleScreen === 'preparation')
      setPhaseScreenVisibility(resultScreen, visibleScreen === 'result')
      setPhaseScreenVisibility(pauseScreen, visibleScreen === 'pause')

      // Disabled-state gating must run BEFORE the focus-transition block
      // below: a disabled button cannot receive focus, and on the very
      // frame a screen opens, its primary control's `disabled` value is
      // still whatever the PREVIOUS render left it as (e.g. Resume is
      // `disabled` while unpaused) -- getInitialFocusTarget()'s `.focus()`
      // call would silently no-op if it ran first (AC3, AC6, AC7).
      const canLoad = actions.canLoadGame()
      openLoadMenuTitleButton.disabled = state.loopPhase !== 'title_menu'
      openLoadMenuPreparationButton.disabled = state.loopPhase !== 'preparation'
      confirmLoadButton.disabled = state.loopPhase !== 'load_menu' || !canLoad
      backFromLoadMenuButton.disabled = state.loopPhase !== 'load_menu'
      newGameButton.disabled = state.loopPhase !== 'title_menu'
      startSortieButton.disabled = state.loopPhase !== 'preparation'
      saveButton.disabled = state.loopPhase !== 'preparation'
      resetButton.disabled = state.loopPhase !== 'preparation'
      // AC9, AC10: Return to hangar is enabled only in `result` phase — this
      // is also the AC10 rapid-activation exactly-once guard (see the click
      // listener comment above).
      confirmResultButton.disabled = state.loopPhase !== 'result'
      // AC3: Resume is only meaningful while actually paused.
      resumeButton.disabled = !isPaused

      if (visibleScreen !== lastVisibleScreen) {
        if (visibleScreen !== null && lastVisibleScreen === null) {
          // Opening: remember the invoker so it can be restored on close.
          // AC6: the pause dialog uses the externally-supplied invoker
          // (`src/main.ts`'s `enterProductPause()` is the single source of
          // truth for what counts as "the invoker") instead of capturing
          // `document.activeElement` the way every other screen does.
          invokerElement =
            visibleScreen === 'pause' && options.getPauseInvoker
              ? options.getPauseInvoker()
              : document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null
          getInitialFocusTarget(container, visibleScreen)?.focus()
        } else if (visibleScreen === null && lastVisibleScreen !== null) {
          // Closing: return focus to whatever opened the screen (AC6). The
          // pause dialog additionally falls back to the Canvas when no
          // invoker is restorable (e.g. auto-pause via visibilitychange
          // records no invoker at all).
          if (invokerElement && document.contains(invokerElement)) {
            invokerElement.focus()
          } else if (lastVisibleScreen === 'pause' && options.canvas) {
            options.canvas.focus()
          }
          invokerElement = null
        } else if (visibleScreen !== null) {
          // Switched directly between two screens (e.g. load -> preparation,
          // or result -> preparation after Return to hangar, AC7).
          getInitialFocusTarget(container, visibleScreen)?.focus()
        }
        lastVisibleScreen = visibleScreen
      }

      loadAvailabilityField.textContent = canLoad
        ? 'A save is available to load.'
        : 'No save is available yet.'
      // Load-specific feedback lives inside the load screen itself (PR #1815
      // review, required fix 4) instead of a separate persistent panel.
      loadStatusField.textContent = state.loopPhase === 'load_menu' ? state.telemetry.status : ''

      // Preparation's Save / Reset / New Game player-facing feedback (In
      // Scope, Issue #1375): rendered here, inside the preparation phase
      // screen itself, instead of `HudController`'s HUD layer -- the HUD
      // layer sits BEHIND this modal overlay in paint order, so feedback
      // rendered there was invisible while this screen is open.
      prepStatusField.textContent =
        state.loopPhase === 'preparation'
          ? `${state.telemetry.status} ${state.telemetry.lastCommandSummary}`.trim()
          : ''

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

      // Result screen: reward summary built ONLY from
      // RewardSystem.calculate(state.sortie.result)'s return value (AC8).
      const rewardView = buildResultRewardView(state)
      if (rewardView) {
        resultOutcomeField.textContent = rewardView.outcomeLabel
        resultKillsField.textContent = rewardView.kills
        resultDurationField.textContent = rewardView.duration
        rewardBaseField.textContent = rewardView.base
        rewardKillBonusField.textContent = rewardView.killBonus
        rewardHpBonusField.textContent = rewardView.hpBonus
        rewardDeltaField.textContent = rewardView.delta
      } else {
        resultOutcomeField.textContent = ''
        resultKillsField.textContent = ''
        resultDurationField.textContent = ''
        rewardBaseField.textContent = ''
        rewardKillBonusField.textContent = ''
        rewardHpBonusField.textContent = ''
        rewardDeltaField.textContent = ''
      }
    },
  }
}

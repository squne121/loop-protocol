import type { GameState } from '../state'
import type { UpgradePurchaseFailureReason } from '../systems/UpgradeSystem'
import { buildCombatHudViewModel, COMBAT_HUD_MARKUP, type CombatHudViewModel } from './combatHud'

/**
 * Running-time HUD (Issue #1375, narrowed further by Issue #1376): `HudController`
 * now owns two DOM roots inside `battle-hud-layer`:
 *
 * - `data-combat-hud` (`src/ui/combatHud.ts`'s markup + view model): the
 *   single compact player-facing surface visible only during `running`
 *   (Hull, Kills, Elapsed, Weapon readiness, Assist allies, Pause). Becomes
 *   `inert` (but stays visible) while `productPause.isPaused` is true, so it
 *   is excluded from the tab order behind the pause dialog (Issue #1376 AC4)
 *   without losing its live Hull/Kills/Elapsed readout.
 * - `data-legacy-debrief-surface`: a temporary compatibility surface that
 *   keeps the pre-#1375 legacy debrief action surface (`Collect payout` /
 *   `Prepare next sortie`) alive for the `debrief_pending_reward` /
 *   `debrief_reward_claimed` phases only. `Return to hangar` and all other
 *   result responsibilities moved to `phaseScreens.ts`'s single
 *   `battle-screen-layer` result screen in Issue #1376 — this surface is
 *   hidden and inert everywhere else (never visible during `running` or the
 *   normal `result` flow).
 *
 * Title / load / preparation / result / pause are owned by `phaseScreens.ts`'s
 * `createPhaseScreenController()`, rendered into the separate
 * `battle-screen-layer` overlay, never this layer (Issue #1374 PR #1815,
 * Issue #1376).
 */
export interface HudActions {
  onAssistPlayerCommand?(): void
  onClaimReward(): void
  onNextSortie(): void
  /**
   * Called when the pause/resume button is clicked (AC1, AC3). `invoker` is
   * the button element itself, threaded through to the caller so the shared
   * pause-entry seam (`enterProductPause()` in `src/main.ts`) can record it
   * as the element to restore focus to on resume (AC6).
   */
  onTogglePause(invoker: HTMLElement): void
}

/**
 * Player-facing copy for a purchase outcome (Issue #1282, AC4). `status` is a
 * short headline; `summary` is the longer explanation. Internal enum values
 * (`UpgradeFailureReason` / `UpgradePurchaseFailureReason`) must never reach
 * the DOM directly — this table is the single translation boundary.
 */
export interface HudUpgradeStatusCopy {
  status: string
  summary: string
}

/** All outcomes an upgrade purchase attempt can report to the HUD. */
export type HudUpgradeOutcomeReason = 'ok' | UpgradePurchaseFailureReason

const UPGRADE_STATUS_COPY_BY_REASON: Record<HudUpgradeOutcomeReason, HudUpgradeStatusCopy> = {
  ok: {
    status: 'Upgrade installed.',
    summary: 'Weapon Power increased. Resources were saved.',
  },
  'insufficient-resources': {
    status: 'Not enough resources.',
    summary: 'Earn 100 resources before upgrading.',
  },
  'already-purchased': {
    status: 'Upgrade already installed.',
    summary: 'Weapon Power is already upgraded.',
  },
  'not-preparation': {
    status: 'Upgrade available in hangar.',
    summary: 'Return to preparation before upgrading.',
  },
  'write-error': {
    status: 'Upgrade not saved.',
    summary: 'No resources were spent. Check browser storage and try again.',
  },
  'storage-unavailable': {
    status: 'Upgrade not saved.',
    summary: 'No resources were spent. Check browser storage and try again.',
  },
  'invalid-definition': {
    status: 'Upgrade unavailable.',
    summary: 'Current upgrade data could not be applied.',
  },
  'invalid-state': {
    status: 'Upgrade unavailable.',
    summary: 'Current upgrade data could not be applied.',
  },
}

/**
 * Translates a `quoteUpgrade()` / `purchaseUpgrade()` outcome reason (or
 * `'ok'`) into player-facing copy (AC4). This is the single lookup table for
 * upgrade purchase feedback so the mapping cannot drift between callers.
 * Consumed by both `HudController` (none, since Upgrade moved to the
 * preparation phase screen) and `phaseScreens.ts`'s preparation screen.
 */
export function getUpgradeStatusCopy(reason: HudUpgradeOutcomeReason): HudUpgradeStatusCopy {
  return UPGRADE_STATUS_COPY_BY_REASON[reason]
}

/**
 * View model shared with `phaseScreens.ts`'s preparation screen (AC2, AC3,
 * AC6). Built by the caller (main.ts) from `quoteUpgrade()` so the UI never
 * re-derives purchase eligibility itself (AC3: `quoteUpgrade()` result is the
 * authority, not a HUD-local phase check).
 */
export interface HudUpgradeViewModel {
  definitionId: string
  cost: number
  weaponPower: number
  buttonDisabled: boolean
  statusCopy: HudUpgradeStatusCopy | null
}

export interface HudController {
  /**
   * Render the HUD. `isPaused` is the runtime-local product pause flag
   * (AC1, AC3, AC4). `activeFixedDeltaMs` is the fixed simulation timestep
   * currently driving `state.sortie.elapsedTicks` (AC4) — supplied by the
   * caller (`src/main.ts`), never read from `GameState` (Out of Scope:
   * `GameState.sortie` does not carry `fixedDeltaMs`).
   */
  render(state: GameState, isPaused: boolean, activeFixedDeltaMs: number): void
}

function getSortieStatusCopy(state: GameState): string {
  switch (state.sortie.status) {
    case 'idle':
      return state.loopPhase === 'preparation' ? 'Ready' : 'Standing by'
    case 'running':
      return 'In Progress'
    case 'victory':
      return 'Area secured'
    case 'defeat':
      return 'Defeat'
    case 'timeout':
      return '戦闘終了'
    case 'ended':
      return 'Review ready'
  }
}

function getOutcomeCopy(state: GameState): string {
  if (state.sortie.result === null) {
    return 'Awaiting outcome'
  }

  switch (state.sortie.result.outcome) {
    case 'victory':
      return 'Victory'
    case 'defeat':
      return 'Defeat'
    case 'timeout':
      return '戦闘終了'
  }
}

/**
 * Writes `next` into `node.textContent` only when it differs from `previous`
 * (AC6, Issue #1375 PR #1925 review P1-1: `render()` previously reassigned
 * `textContent` unconditionally every frame even when the value had not
 * changed, which is unnecessary DOM churn / style recalculation). Returns
 * `next` so callers can thread it straight into the "previous" snapshot for
 * the following frame.
 */
function patchText(node: HTMLElement, previous: string | undefined, next: string): string {
  if (previous !== next) {
    node.textContent = next
  }
  return next
}

/**
 * Sets `hidden` and `inert` together (AC1) so a HUD root is excluded from
 * the accessibility tree and the keyboard tab order when it is not the
 * active surface for the current phase — mirrors
 * `phaseScreens.ts`'s `setPhaseScreenVisibility()` (same non-negotiable
 * pair: `pointer-events: none` alone cannot exclude keyboard focus).
 */
function setHudRootVisibility(
  element: HTMLElement,
  visible: boolean,
  previousVisible: boolean | undefined,
): void {
  // Issue #1375 PR #1925 review (P1-1): skip re-setting hidden/inert/
  // aria-hidden when the phase-driven visible/hidden state has not changed
  // since the last render — this attribute set previously ran every frame
  // even while the HUD stayed in the same phase.
  if (previousVisible === visible) {
    return
  }

  element.hidden = !visible
  if (visible) {
    element.removeAttribute('inert')
    element.removeAttribute('aria-hidden')
  } else {
    element.setAttribute('inert', '')
    element.setAttribute('aria-hidden', 'true')
  }
}

/**
 * Sets only `inert`/`aria-hidden` (never `hidden`) so the combat HUD stays
 * visible-but-non-interactive while a pause dialog is open (AC4, Issue
 * #1376): Hull/Kills/Elapsed/Weapon readouts must keep updating behind the
 * modal pause overlay, but the surface must drop out of the keyboard tab
 * order and accessibility tree while the pause dialog owns focus.
 */
function setCombatHudInertOnly(element: HTMLElement, inert: boolean, previousInert: boolean | undefined): void {
  if (previousInert === inert) {
    return
  }

  if (inert) {
    element.setAttribute('inert', '')
    element.setAttribute('aria-hidden', 'true')
  } else {
    element.removeAttribute('inert')
    element.removeAttribute('aria-hidden')
  }
}

function queryRoot(container: HTMLElement, attribute: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(`[${attribute}]`)

  if (!element) {
    throw new Error(`HUD root "${attribute}" is missing.`)
  }

  return element
}

function queryAction(container: HTMLElement, name: string): HTMLButtonElement {
  const element = container.querySelector<HTMLButtonElement>(`[data-action="${name}"]`)

  if (!element) {
    throw new Error(`HUD action "${name}" is missing.`)
  }

  return element
}

function queryField(container: HTMLElement, name: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(`[data-field="${name}"]`)

  if (!element) {
    throw new Error(`HUD field "${name}" is missing.`)
  }

  return element
}

/** Phases the temporary legacy debrief surface remains visible for (Issue #1376). */
function isLegacyDebriefPhase(state: GameState): boolean {
  return state.loopPhase === 'debrief_pending_reward' || state.loopPhase === 'debrief_reward_claimed'
}

export function createHudController(
  container: HTMLElement,
  actions: HudActions,
): HudController {
  container.innerHTML = `
    ${COMBAT_HUD_MARKUP}
    <div class="legacy-debrief-surface" data-legacy-debrief-surface hidden inert>
      <section class="panel">
        <p class="eyebrow">Sortie</p>
        <dl class="stat-grid">
          <div><dt>Mission phase</dt><dd data-field="loop-phase"></dd></div>
          <div><dt>Mission status</dt><dd data-field="sortie-status"></dd></div>
          <div><dt>Kills</dt><dd data-field="sortie-kills"></dd></div>
          <div><dt>Duration</dt><dd data-field="sortie-duration"></dd></div>
          <div><dt>Outcome</dt><dd data-field="sortie-result"></dd></div>
        </dl>
      </section>
      <section class="panel">
        <p class="eyebrow">Pilot updates</p>
        <p class="status-copy" data-field="status" role="status" aria-live="polite"></p>
        <p class="status-copy status-copy--muted" data-field="command"></p>
      </section>
      <section class="panel panel--actions">
        <button type="button" data-action="claim-reward" data-battle-interactive="true">Collect payout</button>
        <button type="button" data-action="next-sortie" data-battle-interactive="true">Prepare next sortie</button>
      </section>
    </div>
  `

  const combatHudRoot = queryRoot(container, 'data-combat-hud')
  const legacyDebriefSurface = queryRoot(container, 'data-legacy-debrief-surface')

  if (actions.onAssistPlayerCommand) {
    combatHudRoot
      .querySelector<HTMLButtonElement>('[data-action="assist-player"]')
      ?.addEventListener('click', actions.onAssistPlayerCommand)
  }
  const togglePauseButtonEl = combatHudRoot.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
  togglePauseButtonEl?.addEventListener('click', () => {
    // AC3, AC6: thread the invoking button through so the pause-entry seam
    // can restore focus to it on resume.
    actions.onTogglePause(togglePauseButtonEl)
  })

  legacyDebriefSurface
    .querySelector<HTMLButtonElement>('[data-action="claim-reward"]')
    ?.addEventListener('click', actions.onClaimReward)
  legacyDebriefSurface
    .querySelector<HTMLButtonElement>('[data-action="next-sortie"]')
    ?.addEventListener('click', actions.onNextSortie)

  const combatHudHull = queryField(combatHudRoot, 'combat-hud-hull')
  const combatHudKills = queryField(combatHudRoot, 'combat-hud-kills')
  const combatHudElapsed = queryField(combatHudRoot, 'combat-hud-elapsed')
  const combatHudWeapon = queryField(combatHudRoot, 'combat-hud-weapon')
  const combatHudAssistStatus = queryField(combatHudRoot, 'combat-hud-assist-status')
  const combatHudCritical = queryField(combatHudRoot, 'combat-hud-critical')
  const assistPlayerButton = queryAction(combatHudRoot, 'assist-player')
  const togglePauseButton = queryAction(combatHudRoot, 'toggle-pause')

  const status = queryField(legacyDebriefSurface, 'status')
  const command = queryField(legacyDebriefSurface, 'command')
  const loopPhaseField = queryField(legacyDebriefSurface, 'loop-phase')
  const sortieStatus = queryField(legacyDebriefSurface, 'sortie-status')
  const sortieKills = queryField(legacyDebriefSurface, 'sortie-kills')
  const sortieDuration = queryField(legacyDebriefSurface, 'sortie-duration')
  const sortieResult = queryField(legacyDebriefSurface, 'sortie-result')
  const claimRewardButton = queryAction(legacyDebriefSurface, 'claim-reward')
  const nextSortieButton = queryAction(legacyDebriefSurface, 'next-sortie')

  // AC3: the Pause button's visible label is a fixed constant, so it only
  // needs to be set once at construction time (Issue #1375 PR #1925 review
  // P1-1) rather than reassigned every render().
  togglePauseButton.textContent = 'Pause'

  // Issue #1375 PR #1925 review (P1-1): previous-frame snapshots so
  // render() can skip DOM writes for unchanged values (AC6). `null` means
  // "no previous render yet" so the first render always writes every field.
  let previousIsRunning: boolean | undefined
  let previousLegacyDebriefSurfaceVisible: boolean | undefined
  let previousCombatHudPausedInert: boolean | undefined
  let previousCombatHudViewModel: CombatHudViewModel | null = null

  return {
    render(state, isPaused, activeFixedDeltaMs) {
      // AC1: exactly one of the two HUD roots is the active player-facing
      // surface for the current phase; the other is hidden + inert.
      const isRunning = state.loopPhase === 'running'
      const legacyDebriefSurfaceVisible = isLegacyDebriefPhase(state)
      setHudRootVisibility(combatHudRoot, isRunning, previousIsRunning)
      // Issue #1376: the legacy debrief surface's visibility is now derived
      // from loopPhase directly (debrief_pending_reward / debrief_reward_claimed
      // only), not the "everything except running" predicate it used before
      // #1376 moved the result screen to phaseScreens.ts.
      setHudRootVisibility(legacyDebriefSurface, legacyDebriefSurfaceVisible, previousLegacyDebriefSurfaceVisible)
      // Issue #1375 PR #1925 owner playtest (P0, iteration 4): reset scroll
      // position exactly once, on the transition into visible, so a leftover
      // scroll offset from a previous visit never hides the action buttons
      // below the fold on first paint. Guarded so it only runs on a
      // false -> true visibility transition (AC6: no unconditional
      // per-frame writes).
      if (legacyDebriefSurfaceVisible && previousLegacyDebriefSurfaceVisible !== true) {
        legacyDebriefSurface.scrollTop = 0
      }
      previousIsRunning = isRunning
      previousLegacyDebriefSurfaceVisible = legacyDebriefSurfaceVisible

      // AC4 (Issue #1376): while running AND paused, the combat HUD stays
      // visible (Hull/Kills/Elapsed/Weapon keep updating) but becomes inert
      // so it drops out of the tab order behind the pause dialog. Never
      // hidden here — hidden is exclusively driven by isRunning above. Only
      // touches the inert attribute while isRunning: when !isRunning,
      // setHudRootVisibility() above already owns hidden+inert together and
      // this must not clobber it (previousCombatHudPausedInert is left
      // untouched so re-entering `running` always re-evaluates correctly).
      if (isRunning) {
        setCombatHudInertOnly(combatHudRoot, isPaused, previousCombatHudPausedInert)
        previousCombatHudPausedInert = isPaused
      } else {
        previousCombatHudPausedInert = undefined
      }

      // -----------------------------------------------------------------
      // Combat HUD (data-combat-hud, running only) — AC2, AC3, AC4, AC6
      // -----------------------------------------------------------------
      const combatHudViewModel = buildCombatHudViewModel(state, isPaused, activeFixedDeltaMs)
      const previous = previousCombatHudViewModel

      patchText(combatHudHull, previous?.hullLabel, combatHudViewModel.hullLabel)
      patchText(combatHudKills, previous ? `${previous.kills}` : undefined, `${combatHudViewModel.kills}`)
      // AC4: derived from elapsedTicks * activeFixedDeltaMs — never masked
      // for VRT (Timer / Volatile Text Policy: this IS the display
      // authority, unlike the legacy elapsedTicks/60 approximation below).
      patchText(combatHudElapsed, previous?.elapsedLabel, combatHudViewModel.elapsedLabel)
      combatHudElapsed.removeAttribute('data-visual-mask')
      patchText(combatHudWeapon, previous?.weaponLabel, combatHudViewModel.weaponLabel)

      // AC1, AC6: critical warning is text/shape/icon driven (never
      // color-only) and only toggles the ARIA-live paragraph's hidden
      // attribute (thus only announces) on an actual state transition, not
      // every frame while already critical.
      if (previous === null || previous.isCritical !== combatHudViewModel.isCritical) {
        combatHudCritical.hidden = !combatHudViewModel.isCritical
      }

      assistPlayerButton.disabled = combatHudViewModel.assistDisabled
      // AC6: Assist is the sole live-region field in the combat HUD; Hull /
      // Kills / Elapsed / Weapon are updated in place, not inside a
      // role="status" region.
      patchText(combatHudAssistStatus, previous?.assistStatus, combatHudViewModel.assistStatus)

      // AC3: aria-pressed represents the toggle state (visible label stays
      // fixed, see the one-time assignment above).
      togglePauseButton.setAttribute('aria-pressed', combatHudViewModel.paused ? 'true' : 'false')
      togglePauseButton.disabled = combatHudViewModel.pauseDisabled

      previousCombatHudViewModel = combatHudViewModel

      // -----------------------------------------------------------------
      // Legacy debrief compatibility surface — visible only for
      // debrief_pending_reward / debrief_reward_claimed (temporary until
      // those legacy phases are retired; Issue #1376 removed result
      // responsibility from this surface entirely).
      // -----------------------------------------------------------------
      loopPhaseField.textContent = missionPhaseLabel(state.loopPhase)

      // claim-reward: legacy debrief_pending_reward phase only
      claimRewardButton.disabled = state.loopPhase !== 'debrief_pending_reward'
      // next-sortie: only for legacy debrief_reward_claimed phase
      nextSortieButton.disabled = state.loopPhase !== 'debrief_reward_claimed'

      // In Scope: preparation's Save/Reset (and New Game) player-facing
      // feedback is ADDITIONALLY rendered by phaseScreens.ts's own
      // preparation screen field (`prep-status`) now, fixing the pre-#1375
      // bug where this feedback rendered only here, in the HUD layer that
      // paints BEHIND the modal preparation phase-screen overlay (so it was
      // invisible while the player was looking at that screen). This
      // surface keeps rendering `state.telemetry` unconditionally (as
      // before #1375) so other phases/consumers are unaffected, even though
      // it is hidden + inert outside the two legacy debrief phases.
      status.textContent = state.telemetry.status
      command.textContent = state.telemetry.lastCommandSummary

      // Sortie status display
      const s = state.sortie
      sortieStatus.textContent = getSortieStatusCopy(state)

      // Kills
      if (s.result !== null) {
        sortieKills.textContent = `${s.result.kills}`
      } else {
        // Count defeated enemies for live kills display during running
        const kills = state.enemies.filter((e) => e.defeated).length
        sortieKills.textContent = `${kills}`
      }

      // Duration
      // Terminal: use result.durationMs; running: use elapsedTicks-derived ticks
      if (s.result !== null) {
        const durationSec = (s.result.durationMs / 1000).toFixed(1)
        sortieDuration.textContent = `${durationSec}s`
        // Timer / Volatile Text Policy (Issue #1385): result.durationMs is the
        // authoritative duration source here, so it is not masked for VRT.
        sortieDuration.removeAttribute('data-visual-mask')
      } else {
        // running or idle: elapsedTicks / 60 Hz approximation (display only)
        const approxSec = (s.elapsedTicks / 60).toFixed(1)
        sortieDuration.textContent = `${approxSec}s`
        // Timer / Volatile Text Policy (Issue #1385): this approximation is
        // NOT the elapsedTicks * fixedDeltaMs display authority, so it is
        // masked from VRT captures (see tests/e2e/visual.freeze.css) until
        // the running-duration display is derived from that authority.
        sortieDuration.setAttribute('data-visual-mask', 'true')
      }

      // Result: both Canvas overlay and this legacy surface use result.outcome as authority
      sortieResult.textContent = getOutcomeCopy(state)
    },
  }
}

/**
 * Player-facing mission phase copy (AC contract: raw `LoopPhase` never
 * reaches overlay text). Kept minimal here since title/load/preparation/
 * result/pause own their own headings now (`phaseScreens.ts`).
 */
function missionPhaseLabel(loopPhase: GameState['loopPhase']): string {
  switch (loopPhase) {
    case 'title_menu':
      return 'Launch setup'
    case 'load_menu':
      return 'Restore briefing'
    case 'preparation':
      return 'Pre-launch'
    case 'running':
      return 'Sortie active'
    case 'result':
      return 'Mission review'
    case 'debrief_pending_reward':
      return 'Debrief in progress'
    case 'debrief_reward_claimed':
      return 'Debrief complete'
  }
}

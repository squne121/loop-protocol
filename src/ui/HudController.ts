import type { GameState } from '../state'
import type { UpgradePurchaseFailureReason } from '../systems/UpgradeSystem'
import { buildCombatHudViewModel, COMBAT_HUD_MARKUP } from './combatHud'

/**
 * Running-time HUD (Issue #1375): `HudController` now owns two DOM roots
 * inside `battle-hud-layer`:
 *
 * - `data-combat-hud` (`src/ui/combatHud.ts`'s markup + view model): the
 *   single compact player-facing surface visible only during `running`
 *   (Hull, Kills, Elapsed, Weapon readiness, Assist allies, Pause).
 * - `data-legacy-result-surface`: a temporary compatibility surface that
 *   keeps the pre-#1375 result/debrief action surface (`Return to hangar` /
 *   `Collect payout` / `Prepare next sortie`) and sortie/telemetry copy
 *   alive until #1376 replaces it with a dedicated result/pause overlay.
 *   Visible everywhere EXCEPT `running` (never visible/focusable together
 *   with the combat HUD).
 *
 * Title / load / preparation are owned by `phaseScreens.ts`'s
 * `createPhaseScreenController()`, rendered into the separate
 * `battle-screen-layer` overlay, never this layer (Issue #1374 PR #1815).
 */
export interface HudActions {
  onAssistPlayerCommand?(): void
  onClaimReward(): void
  /** Confirm result and return to preparation (AC5). */
  onConfirmResult?(): void
  onNextSortie(): void
  /** Called when the pause/resume button is clicked (AC1). */
  onTogglePause(): void
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
   * (AC1, AC4). `activeFixedDeltaMs` is the fixed simulation timestep
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
 * Sets `hidden` and `inert` together (AC1) so a HUD root is excluded from
 * the accessibility tree and the keyboard tab order when it is not the
 * active surface for the current phase — mirrors
 * `phaseScreens.ts`'s `setPhaseScreenVisibility()` (same non-negotiable
 * pair: `pointer-events: none` alone cannot exclude keyboard focus).
 */
function setHudRootVisibility(element: HTMLElement, visible: boolean): void {
  element.hidden = !visible
  if (visible) {
    element.removeAttribute('inert')
    element.removeAttribute('aria-hidden')
  } else {
    element.setAttribute('inert', '')
    element.setAttribute('aria-hidden', 'true')
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

export function createHudController(
  container: HTMLElement,
  actions: HudActions,
): HudController {
  container.innerHTML = `
    ${COMBAT_HUD_MARKUP}
    <div class="legacy-result-surface" data-legacy-result-surface hidden inert>
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
        <button type="button" data-action="confirm-result" data-battle-interactive="true">Return to hangar</button>
        <button type="button" data-action="next-sortie" data-battle-interactive="true">Prepare next sortie</button>
      </section>
    </div>
  `

  const combatHudRoot = queryRoot(container, 'data-combat-hud')
  const legacyResultSurface = queryRoot(container, 'data-legacy-result-surface')

  if (actions.onAssistPlayerCommand) {
    combatHudRoot
      .querySelector<HTMLButtonElement>('[data-action="assist-player"]')
      ?.addEventListener('click', actions.onAssistPlayerCommand)
  }
  combatHudRoot
    .querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    ?.addEventListener('click', actions.onTogglePause)

  legacyResultSurface
    .querySelector<HTMLButtonElement>('[data-action="claim-reward"]')
    ?.addEventListener('click', actions.onClaimReward)
  if (actions.onConfirmResult) {
    legacyResultSurface
      .querySelector<HTMLButtonElement>('[data-action="confirm-result"]')
      ?.addEventListener('click', actions.onConfirmResult)
  }
  legacyResultSurface
    .querySelector<HTMLButtonElement>('[data-action="next-sortie"]')
    ?.addEventListener('click', actions.onNextSortie)

  const combatHudHull = queryField(combatHudRoot, 'combat-hud-hull')
  const combatHudKills = queryField(combatHudRoot, 'combat-hud-kills')
  const combatHudElapsed = queryField(combatHudRoot, 'combat-hud-elapsed')
  const combatHudWeapon = queryField(combatHudRoot, 'combat-hud-weapon')
  const combatHudAssistStatus = queryField(combatHudRoot, 'combat-hud-assist-status')
  const assistPlayerButton = queryAction(combatHudRoot, 'assist-player')
  const togglePauseButton = queryAction(combatHudRoot, 'toggle-pause')

  const status = queryField(legacyResultSurface, 'status')
  const command = queryField(legacyResultSurface, 'command')
  const loopPhaseField = queryField(legacyResultSurface, 'loop-phase')
  const sortieStatus = queryField(legacyResultSurface, 'sortie-status')
  const sortieKills = queryField(legacyResultSurface, 'sortie-kills')
  const sortieDuration = queryField(legacyResultSurface, 'sortie-duration')
  const sortieResult = queryField(legacyResultSurface, 'sortie-result')
  const claimRewardButton = queryAction(legacyResultSurface, 'claim-reward')
  const confirmResultButton = queryAction(legacyResultSurface, 'confirm-result')
  const nextSortieButton = queryAction(legacyResultSurface, 'next-sortie')

  return {
    render(state, isPaused, activeFixedDeltaMs) {
      // AC1: exactly one of the two HUD roots is the active player-facing
      // surface for the current phase; the other is hidden + inert.
      const isRunning = state.loopPhase === 'running'
      setHudRootVisibility(combatHudRoot, isRunning)
      setHudRootVisibility(legacyResultSurface, !isRunning)

      // -----------------------------------------------------------------
      // Combat HUD (data-combat-hud, running only) — AC2, AC3, AC4, AC6
      // -----------------------------------------------------------------
      const combatView = buildCombatHudViewModel(state, isPaused, activeFixedDeltaMs)

      combatHudHull.textContent = combatView.hullLabel
      combatHudKills.textContent = `${combatView.kills}`
      // AC4: derived from elapsedTicks * activeFixedDeltaMs — never masked
      // for VRT (Timer / Volatile Text Policy: this IS the display
      // authority, unlike the legacy elapsedTicks/60 approximation below).
      combatHudElapsed.textContent = combatView.elapsedLabel
      combatHudElapsed.removeAttribute('data-visual-mask')
      combatHudWeapon.textContent = combatView.weaponLabel

      assistPlayerButton.disabled = combatView.assistDisabled
      // AC6: Assist is the sole live-region field in the combat HUD; Hull /
      // Kills / Elapsed / Weapon are updated in place, not inside a
      // role="status" region.
      combatHudAssistStatus.textContent = combatView.assistStatus

      // AC3: visible label is fixed ("Pause"); aria-pressed represents the
      // toggle state instead of swapping the accessible name.
      togglePauseButton.textContent = 'Pause'
      togglePauseButton.setAttribute('aria-pressed', combatView.paused ? 'true' : 'false')
      togglePauseButton.disabled = combatView.pauseDisabled

      // -----------------------------------------------------------------
      // Legacy result/debrief compatibility surface — hidden during
      // running (temporary until #1376 replaces it with a dedicated
      // result/pause overlay).
      // -----------------------------------------------------------------
      loopPhaseField.textContent = missionPhaseLabel(state.loopPhase)

      // claim-reward: legacy debrief_pending_reward phase only (AC5: result uses confirm-result)
      claimRewardButton.disabled = state.loopPhase !== 'debrief_pending_reward'
      // confirm-result: only in result phase (AC5)
      confirmResultButton.disabled = state.loopPhase !== 'result'
      // next-sortie: only for legacy debrief_reward_claimed phase
      nextSortieButton.disabled = state.loopPhase !== 'debrief_reward_claimed'

      // In Scope: preparation's Save/Reset (and New Game) player-facing
      // feedback is ADDITIONALLY rendered by phaseScreens.ts's own
      // preparation screen field (`prep-status`) now, fixing the pre-#1375
      // bug where this feedback rendered only here, in the HUD layer that
      // paints BEHIND the modal preparation phase-screen overlay (so it was
      // invisible while the player was looking at that screen). This
      // surface keeps rendering `state.telemetry` unconditionally (as
      // before #1375) so other phases/consumers are unaffected.
      status.textContent = state.telemetry.status
      command.textContent = state.telemetry.lastCommandSummary

      // Sortie status display (AC4, AC10)
      const s = state.sortie
      sortieStatus.textContent = getSortieStatusCopy(state)

      // Kills (AC10)
      if (s.result !== null) {
        sortieKills.textContent = `${s.result.kills}`
      } else {
        // Count defeated enemies for live kills display during running
        const kills = state.enemies.filter((e) => e.defeated).length
        sortieKills.textContent = `${kills}`
      }

      // Duration (AC10, AC11)
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

      // Result (AC9, AC10): both Canvas overlay and HUD use result.outcome as authority
      sortieResult.textContent = getOutcomeCopy(state)
    },
  }
}

/**
 * Player-facing mission phase copy (AC contract: raw `LoopPhase` never
 * reaches overlay text). Kept minimal here since title/load/preparation own
 * their own headings now (`phaseScreens.ts`).
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

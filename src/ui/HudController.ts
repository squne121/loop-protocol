import type { GameState } from '../state'
import { formatCombatNumber } from '../render/renderUtils'
import type { UpgradePurchaseFailureReason } from '../systems/UpgradeSystem'

export interface HudActions {
  onNewGame?(): void
  onStartSortie(): void
  onAssistPlayerCommand?(): void
  onClaimReward(): void
  /** Confirm result and return to preparation (AC5). */
  onConfirmResult?(): void
  onNextSortie(): void
  /** Save operation (AC2, AC8): caller must gate to preparation phase only. */
  onSave?(): void
  /** Load Game (AC3, AC9): only valid from title_menu / load_menu. */
  onLoadGame?(): void
  onReset(): void
  /** Returns true if a loadable snapshot is available (AC3, AC9). */
  canLoadGame?(): boolean
  /** Called when the pause/resume button is clicked (AC1). */
  onTogglePause(): void
  /** Upgrade weapon purchase action (Issue #1282). Caller owns quoteUpgrade/purchaseUpgrade orchestration. */
  onUpgradeWeapon?(): void
  // ---------------------------------------------------------------------------
  // Backward-compat aliases (deprecated — use onSave / onLoadGame / canLoadGame)
  // ---------------------------------------------------------------------------
  /** @deprecated Use onSave() instead. */
  onQuickSave?(): void
  /** @deprecated Use onLoadGame() instead. */
  onQuickLoad?(): void
  /** @deprecated Use canLoadGame() instead. */
  canQuickLoad?(): boolean
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
 */
export function getUpgradeStatusCopy(reason: HudUpgradeOutcomeReason): HudUpgradeStatusCopy {
  return UPGRADE_STATUS_COPY_BY_REASON[reason]
}

/**
 * View model handed to `HudController.render()` describing the current
 * upgrade purchase surface (AC2, AC3, AC6). Built by the caller (main.ts) from
 * `quoteUpgrade()` so the HUD never re-derives purchase eligibility itself
 * (AC3: `quoteUpgrade()` result is the authority, not a HUD-local phase check).
 */
export interface HudUpgradeViewModel {
  definitionId: string
  cost: number
  weaponPower: number
  buttonDisabled: boolean
  statusCopy: HudUpgradeStatusCopy | null
}

export interface HudController {
  /** Render the HUD. isPaused is the runtime-local product pause flag (AC1, AC4). */
  render(state: GameState, isPaused: boolean, upgradeView?: HudUpgradeViewModel): void
}

function getMissionPhaseCopy(loopPhase: GameState['loopPhase']): string {
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

function getAssistStatusCopy(state: GameState): string {
  if (state.loopPhase !== 'running' || state.sortie.status !== 'running') {
    return 'Assist is available during sortie.'
  }

  if (state.allies.length === 0) {
    return 'No ally available.'
  }

  const hasLivingEnemy = state.enemies.some((enemy) => !enemy.defeated)
  const hasAssignedTarget = state.allies.some((ally) => ally.targetEntityId !== null)

  if (state.commandIntentRuntime.activeIntent === 'assist_player') {
    if (hasAssignedTarget) {
      return 'Allies covering you.'
    }
    return hasLivingEnemy ? 'Assist signal sent.' : 'No target to assist.'
  }

  return hasLivingEnemy ? 'Assist ready.' : 'No target to assist.'
}

export function createHudController(
  container: HTMLElement,
  actions: HudActions,
): HudController {
  container.innerHTML = `
    <section class="panel panel--accent">
      <p class="eyebrow">Pilot feed</p>
      <h1>LOOP_PROTOCOL</h1>
      <p class="lede">Canvas battle sandbox with DOM-side command surfaces.</p>
    </section>
    <section class="panel">
      <p class="eyebrow">Progress</p>
      <dl class="stat-grid">
        <div><dt>Hull</dt><dd data-field="hp"></dd></div>
        <div><dt>Resources</dt><dd data-field="resources"></dd></div>
        <div><dt>Shots</dt><dd data-field="shots"></dd></div>
        <div><dt>Cooldown</dt><dd data-field="cooldown"></dd></div>
      </dl>
    </section>
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
      <p class="eyebrow">Wingmates</p>
      <button type="button" data-action="assist-player" aria-label="Assist allies">Assist allies</button>
      <p
        class="status-copy"
        data-field="assist-status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      ></p>
    </section>
    <section class="panel">
      <p class="eyebrow">Armory</p>
      <dl class="stat-grid">
        <div><dt>Weapon Power</dt><dd data-field="weapon-power"></dd></div>
      </dl>
      <button type="button" data-action="upgrade-weapon">Upgrade weapon</button>
      <p class="status-copy status-copy--muted" data-field="upgrade-cost"></p>
      <p
        class="status-copy"
        data-field="upgrade-status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      ></p>
    </section>
    <section class="panel">
      <p class="eyebrow">Pilot updates</p>
      <p class="status-copy" data-field="status" role="status" aria-live="polite"></p>
      <p class="status-copy status-copy--muted" data-field="command"></p>
    </section>
    <section class="panel panel--pause-status">
      <p class="status-copy" data-field="pause-status" role="status" aria-live="polite" aria-atomic="true"></p>
    </section>
    <section class="panel panel--actions">
      <button type="button" data-action="new-game">Begin new run</button>
      <button type="button" data-action="start-sortie">Launch sortie</button>
      <button type="button" data-action="claim-reward">Collect payout</button>
      <button type="button" data-action="confirm-result">Return to hangar</button>
      <button type="button" data-action="next-sortie">Prepare next sortie</button>
      <button type="button" data-action="save">Save progress</button>
      <button type="button" data-action="load-game">Open save</button>
      <button
        type="button"
        data-action="reset"
        title="Reset sortie is a destructive boundary and is only available during preparation."
      >
        Reset sortie
      </button>
      <button
        type="button"
        data-action="toggle-pause"
        aria-pressed="false"
        aria-label="Pause simulation"
        title="Pause or resume simulation. Also toggled by Escape."
      >Pause</button>
    </section>
  `

  if (actions.onNewGame) {
    container
      .querySelector<HTMLButtonElement>('[data-action="new-game"]')
      ?.addEventListener('click', actions.onNewGame)
  }
  container
    .querySelector<HTMLButtonElement>('[data-action="start-sortie"]')
    ?.addEventListener('click', actions.onStartSortie)
  if (actions.onAssistPlayerCommand) {
    container
      .querySelector<HTMLButtonElement>('[data-action="assist-player"]')
      ?.addEventListener('click', actions.onAssistPlayerCommand)
  }
  container
    .querySelector<HTMLButtonElement>('[data-action="claim-reward"]')
    ?.addEventListener('click', actions.onClaimReward)
  if (actions.onConfirmResult) {
    container
      .querySelector<HTMLButtonElement>('[data-action="confirm-result"]')
      ?.addEventListener('click', actions.onConfirmResult)
  }
  container
    .querySelector<HTMLButtonElement>('[data-action="next-sortie"]')
    ?.addEventListener('click', actions.onNextSortie)
  const saveHandler = actions.onSave ?? actions.onQuickSave
  if (saveHandler) {
    container
      .querySelector<HTMLButtonElement>('[data-action="save"]')
      ?.addEventListener('click', saveHandler)
  }
  const loadGameHandler = actions.onLoadGame ?? actions.onQuickLoad
  if (loadGameHandler) {
    container
      .querySelector<HTMLButtonElement>('[data-action="load-game"]')
      ?.addEventListener('click', loadGameHandler)
  }
  container
    .querySelector<HTMLButtonElement>('[data-action="reset"]')
    ?.addEventListener('click', actions.onReset)
  container
    .querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    ?.addEventListener('click', actions.onTogglePause)
  if (actions.onUpgradeWeapon) {
    container
      .querySelector<HTMLButtonElement>('[data-action="upgrade-weapon"]')
      ?.addEventListener('click', actions.onUpgradeWeapon)
  }

  const hp = queryField(container, 'hp')
  const resources = queryField(container, 'resources')
  const weaponPowerField = queryField(container, 'weapon-power')
  const shots = queryField(container, 'shots')
  const cooldown = queryField(container, 'cooldown')
  const assistStatus = queryField(container, 'assist-status')
  const status = queryField(container, 'status')
  const command = queryField(container, 'command')
  const pauseStatus = queryField(container, 'pause-status')
  const loopPhase = queryField(container, 'loop-phase')
  const sortieStatus = queryField(container, 'sortie-status')
  const sortieKills = queryField(container, 'sortie-kills')
  const sortieDuration = queryField(container, 'sortie-duration')
  const sortieResult = queryField(container, 'sortie-result')
  const upgradeCostField = queryField(container, 'upgrade-cost')
  const upgradeStatusField = queryField(container, 'upgrade-status')
  const newGameButton = queryAction(container, 'new-game')
  const startSortieButton = queryAction(container, 'start-sortie')
  const assistPlayerButton = queryAction(container, 'assist-player')
  const claimRewardButton = queryAction(container, 'claim-reward')
  const confirmResultButton = queryAction(container, 'confirm-result')
  const nextSortieButton = queryAction(container, 'next-sortie')
  const saveButton = queryAction(container, 'save')
  const loadGameButton = queryAction(container, 'load-game')
  const resetButton = queryAction(container, 'reset')
  const togglePauseButton = queryAction(container, 'toggle-pause')
  const upgradeWeaponButton = queryAction(container, 'upgrade-weapon')

  return {
    render(state, isPaused, upgradeView) {
      hp.textContent = `${formatCombatNumber(state.player.hp)}/${formatCombatNumber(state.player.maxHp)}`
      resources.textContent = `${state.progress.resources}`
      weaponPowerField.textContent = `${upgradeView?.weaponPower ?? state.progress.weaponPower}`
      shots.textContent = `${state.player.shotsFired}`
      cooldown.textContent = `${Math.ceil(state.player.weaponCooldownMs)} ms`
      assistStatus.textContent = getAssistStatusCopy(state)
      status.textContent = state.telemetry.status
      command.textContent = state.telemetry.lastCommandSummary
      loopPhase.textContent = getMissionPhaseCopy(state.loopPhase)

      // Button enable policy derived from phase state machine (AC2, AC3, AC7, AC8, AC9)
      const isMenuPhase = state.loopPhase === 'title_menu' || state.loopPhase === 'load_menu'
      // new-game: only in title_menu (AC1)
      newGameButton.disabled = state.loopPhase !== 'title_menu'
      startSortieButton.disabled = state.loopPhase !== 'preparation'
      assistPlayerButton.disabled = state.loopPhase !== 'running'
      // claim-reward: legacy debrief_pending_reward phase only (AC5: result uses confirm-result)
      claimRewardButton.disabled = state.loopPhase !== 'debrief_pending_reward'
      // confirm-result: only in result phase (AC5)
      confirmResultButton.disabled = state.loopPhase !== 'result'
      // next-sortie: only for legacy debrief_reward_claimed phase
      nextSortieButton.disabled = state.loopPhase !== 'debrief_reward_claimed'
      // save: preparation only (AC2, AC8)
      saveButton.disabled = state.loopPhase !== 'preparation'
      // load-game: title_menu or load_menu only (AC3, AC9)
      // canLoadGame is preferred; falls back to deprecated canQuickLoad for backward compat
      const canLoad = (actions.canLoadGame ?? actions.canQuickLoad)?.() ?? false
      loadGameButton.disabled = !isMenuPhase || !canLoad
      resetButton.disabled = state.loopPhase !== 'preparation'

      // Upgrade weapon (AC2, AC3, AC6): `quoteUpgrade()` result (via upgradeView.buttonDisabled)
      // is the sole authority for purchase eligibility — this render step never
      // re-derives eligibility from state.loopPhase itself.
      if (upgradeView) {
        upgradeWeaponButton.disabled = upgradeView.buttonDisabled
        upgradeCostField.textContent = `Cost: ${upgradeView.cost}`
        upgradeStatusField.textContent = upgradeView.statusCopy
          ? `${upgradeView.statusCopy.status} ${upgradeView.statusCopy.summary}`
          : ''
      } else {
        upgradeWeaponButton.disabled = true
        upgradeCostField.textContent = ''
        upgradeStatusField.textContent = ''
      }

      // AC1: aria-pressed reflects current pause state; label is fixed to avoid ARIA conflict
      // aria-label updates to describe the current action (not current state)
      togglePauseButton.setAttribute('aria-pressed', isPaused ? 'true' : 'false')
      togglePauseButton.setAttribute(
        'aria-label',
        isPaused ? 'Resume simulation' : 'Pause simulation',
      )
      // AC16: aria-pressed reflects current pause state
      // BLOCKER 1: pause button is disabled when not in running phase and not already paused
      togglePauseButton.disabled = state.loopPhase !== 'running' && !isPaused

      // AC6: live region shows "Paused" status for screen readers (AC16)
      pauseStatus.textContent = isPaused ? 'Paused' : ''

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
      } else {
        // running or idle: elapsedTicks / 60 Hz approximation (display only)
        const approxSec = (s.elapsedTicks / 60).toFixed(1)
        sortieDuration.textContent = `${approxSec}s`
      }

      // Result (AC9, AC10): both Canvas overlay and HUD use result.outcome as authority
      sortieResult.textContent = getOutcomeCopy(state)
    },
  }
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

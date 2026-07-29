import type { GameState } from '../state'
import { formatCombatNumber } from '../render/renderUtils'
import type { UpgradePurchaseFailureReason } from '../systems/UpgradeSystem'

/**
 * Minimal running-time HUD (Issue #1374 PR #1815 review fix 1, fix 2): this
 * controller owns only the `battle-hud-layer` (a narrow right-aligned strip)
 * and renders only what a player needs *during* a sortie plus the legacy
 * debrief/result action surface (Out of Scope for this Issue: a dedicated
 * result screen — see Issue #1374 "Out of Scope"). Title / load / preparation
 * are owned by `phaseScreens.ts`'s `createPhaseScreenController()`, rendered
 * into the separate `battle-screen-layer` overlay, never this layer.
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
  /** Render the HUD. isPaused is the runtime-local product pause flag (AC1, AC4). */
  render(state: GameState, isPaused: boolean): void
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
    <section class="panel">
      <p class="eyebrow">Hull</p>
      <dl class="stat-grid">
        <div><dt>Hull</dt><dd data-field="hp"></dd></div>
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
      <button type="button" data-action="assist-player" data-battle-interactive="true" aria-label="Assist allies">Assist allies</button>
      <p
        class="status-copy"
        data-field="assist-status"
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
      <button type="button" data-action="claim-reward" data-battle-interactive="true">Collect payout</button>
      <button type="button" data-action="confirm-result" data-battle-interactive="true">Return to hangar</button>
      <button type="button" data-action="next-sortie" data-battle-interactive="true">Prepare next sortie</button>
      <button
        type="button"
        data-action="toggle-pause"
        data-battle-interactive="true"
        aria-pressed="false"
        aria-label="Pause simulation"
        title="Pause or resume simulation. Also toggled by Escape."
      >Pause</button>
    </section>
  `

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
  container
    .querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    ?.addEventListener('click', actions.onTogglePause)

  const hp = queryField(container, 'hp')
  const shots = queryField(container, 'shots')
  const cooldown = queryField(container, 'cooldown')
  const assistStatus = queryField(container, 'assist-status')
  const status = queryField(container, 'status')
  const command = queryField(container, 'command')
  const pauseStatus = queryField(container, 'pause-status')
  const loopPhaseField = queryField(container, 'loop-phase')
  const sortieStatus = queryField(container, 'sortie-status')
  const sortieKills = queryField(container, 'sortie-kills')
  const sortieDuration = queryField(container, 'sortie-duration')
  const sortieResult = queryField(container, 'sortie-result')
  const assistPlayerButton = queryAction(container, 'assist-player')
  const claimRewardButton = queryAction(container, 'claim-reward')
  const confirmResultButton = queryAction(container, 'confirm-result')
  const nextSortieButton = queryAction(container, 'next-sortie')
  const togglePauseButton = queryAction(container, 'toggle-pause')

  return {
    render(state, isPaused) {
      hp.textContent = `${formatCombatNumber(state.player.hp)}/${formatCombatNumber(state.player.maxHp)}`
      shots.textContent = `${state.player.shotsFired}`
      cooldown.textContent = `${Math.ceil(state.player.weaponCooldownMs)} ms`
      assistStatus.textContent = getAssistStatusCopy(state)
      status.textContent = state.telemetry.status
      command.textContent = state.telemetry.lastCommandSummary
      loopPhaseField.textContent = missionPhaseLabel(state.loopPhase)

      assistPlayerButton.disabled = state.loopPhase !== 'running'
      // claim-reward: legacy debrief_pending_reward phase only (AC5: result uses confirm-result)
      claimRewardButton.disabled = state.loopPhase !== 'debrief_pending_reward'
      // confirm-result: only in result phase (AC5)
      confirmResultButton.disabled = state.loopPhase !== 'result'
      // next-sortie: only for legacy debrief_reward_claimed phase
      nextSortieButton.disabled = state.loopPhase !== 'debrief_reward_claimed'

      // AC1: aria-pressed reflects current pause state; label is fixed to avoid ARIA conflict
      // aria-label updates to describe the current action (not current state)
      togglePauseButton.setAttribute('aria-pressed', isPaused ? 'true' : 'false')
      togglePauseButton.setAttribute(
        'aria-label',
        isPaused ? 'Resume simulation' : 'Pause simulation',
      )
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

import type { GameState } from '../state'
import { formatCombatNumber } from '../render/renderUtils'

export interface HudActions {
  onNewGame?(): void
  onStartSortie(): void
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

export interface HudController {
  /** Render the HUD. isPaused is the runtime-local product pause flag (AC1, AC4). */
  render(state: GameState, isPaused: boolean): void
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
      <p class="eyebrow">Status</p>
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
        <div><dt>Loop Phase</dt><dd data-field="loop-phase"></dd></div>
        <div><dt>Sortie Status</dt><dd data-field="sortie-status"></dd></div>
        <div><dt>Kills</dt><dd data-field="sortie-kills"></dd></div>
        <div><dt>Duration</dt><dd data-field="sortie-duration"></dd></div>
        <div><dt>Result</dt><dd data-field="sortie-result"></dd></div>
      </dl>
    </section>
    <section class="panel">
      <p class="eyebrow">Telemetry</p>
      <p class="status-copy" data-field="status" role="status" aria-live="polite"></p>
      <p class="status-copy status-copy--muted" data-field="command"></p>
    </section>
    <section class="panel panel--pause-status">
      <p class="status-copy" data-field="pause-status" role="status" aria-live="polite" aria-atomic="true"></p>
    </section>
    <section class="panel panel--actions">
      <button type="button" data-action="new-game">New Game</button>
      <button type="button" data-action="start-sortie">Start sortie</button>
      <button type="button" data-action="claim-reward">Claim reward</button>
      <button type="button" data-action="confirm-result">Confirm result</button>
      <button type="button" data-action="next-sortie">Next sortie</button>
      <button type="button" data-action="save">Save</button>
      <button type="button" data-action="load-game">Load Game</button>
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

  const hp = queryField(container, 'hp')
  const resources = queryField(container, 'resources')
  const shots = queryField(container, 'shots')
  const cooldown = queryField(container, 'cooldown')
  const status = queryField(container, 'status')
  const command = queryField(container, 'command')
  const pauseStatus = queryField(container, 'pause-status')
  const loopPhase = queryField(container, 'loop-phase')
  const sortieStatus = queryField(container, 'sortie-status')
  const sortieKills = queryField(container, 'sortie-kills')
  const sortieDuration = queryField(container, 'sortie-duration')
  const sortieResult = queryField(container, 'sortie-result')
  const newGameButton = queryAction(container, 'new-game')
  const startSortieButton = queryAction(container, 'start-sortie')
  const claimRewardButton = queryAction(container, 'claim-reward')
  const confirmResultButton = queryAction(container, 'confirm-result')
  const nextSortieButton = queryAction(container, 'next-sortie')
  const saveButton = queryAction(container, 'save')
  const loadGameButton = queryAction(container, 'load-game')
  const resetButton = queryAction(container, 'reset')
  const togglePauseButton = queryAction(container, 'toggle-pause')

  return {
    render(state, isPaused) {
      hp.textContent = `${formatCombatNumber(state.player.hp)}/${formatCombatNumber(state.player.maxHp)}`
      resources.textContent = `${state.progress.resources}`
      shots.textContent = `${state.player.shotsFired}`
      cooldown.textContent = `${Math.ceil(state.player.weaponCooldownMs)} ms`
      status.textContent = state.telemetry.status
      command.textContent = state.telemetry.lastCommandSummary

      switch (state.loopPhase) {
        case 'title_menu':
          loopPhase.textContent = 'Title Menu'
          break
        case 'load_menu':
          loopPhase.textContent = 'Load Menu'
          break
        case 'preparation':
          loopPhase.textContent = 'Preparation'
          break
        case 'running':
          loopPhase.textContent = 'Sortie running'
          break
        case 'result':
          loopPhase.textContent = 'Result'
          break
        case 'debrief_pending_reward':
          loopPhase.textContent = 'Debrief: reward pending'
          break
        case 'debrief_reward_claimed':
          loopPhase.textContent = 'Debrief: reward claimed'
          break
      }

      // Button enable policy derived from phase state machine (AC2, AC3, AC7, AC8, AC9)
      const isMenuPhase = state.loopPhase === 'title_menu' || state.loopPhase === 'load_menu'
      // new-game: only in title_menu (AC1)
      newGameButton.disabled = state.loopPhase !== 'title_menu'
      startSortieButton.disabled = state.loopPhase !== 'preparation'
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
      switch (s.status) {
        case 'idle':
          sortieStatus.textContent = 'Idle'
          break
        case 'running':
          sortieStatus.textContent = 'In Progress'
          break
        case 'victory':
          sortieStatus.textContent = 'Victory'
          break
        case 'defeat':
          sortieStatus.textContent = 'Defeat'
          break
        case 'timeout':
          sortieStatus.textContent = '戦闘終了'
          break
        case 'ended':
          sortieStatus.textContent = 'Ended'
          break
      }

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
      if (s.result !== null) {
        switch (s.result.outcome) {
          case 'victory':
            sortieResult.textContent = 'Victory'
            break
          case 'defeat':
            sortieResult.textContent = 'Defeat'
            break
          case 'timeout':
            sortieResult.textContent = '戦闘終了'
            break
        }
      } else {
        sortieResult.textContent = '—'
      }
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

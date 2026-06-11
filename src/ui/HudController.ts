import type { GameState } from '../state'
import { formatCombatNumber } from '../render/renderUtils'

export interface HudActions {
  onStartSortie(): void
  onClaimReward(): void
  onNextSortie(): void
  onQuickSave(): void
  onQuickLoad(): void
  onReset(): void
  canQuickLoad(): boolean
  /** Called when the pause/resume button is clicked (AC1). */
  onTogglePause(): void
}

export interface HudController {
  /** Render the HUD. isPaused is the runtime-local debug pause flag (AC1, AC4). */
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
    <section class="panel panel--actions">
      <button type="button" data-action="start-sortie">Start sortie</button>
      <button type="button" data-action="claim-reward">Claim reward</button>
      <button type="button" data-action="next-sortie">Next sortie</button>
      <button type="button" data-action="quick-save">Quick Save</button>
      <button type="button" data-action="quick-load">Quick Load</button>
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
        title="Pause or resume simulation. Also toggled by Escape."
      >Pause</button>
    </section>
  `

  container
    .querySelector<HTMLButtonElement>('[data-action="start-sortie"]')
    ?.addEventListener('click', actions.onStartSortie)
  container
    .querySelector<HTMLButtonElement>('[data-action="claim-reward"]')
    ?.addEventListener('click', actions.onClaimReward)
  container
    .querySelector<HTMLButtonElement>('[data-action="next-sortie"]')
    ?.addEventListener('click', actions.onNextSortie)
  container
    .querySelector<HTMLButtonElement>('[data-action="quick-save"]')
    ?.addEventListener('click', actions.onQuickSave)
  container
    .querySelector<HTMLButtonElement>('[data-action="quick-load"]')
    ?.addEventListener('click', actions.onQuickLoad)
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
  const loopPhase = queryField(container, 'loop-phase')
  const sortieStatus = queryField(container, 'sortie-status')
  const sortieKills = queryField(container, 'sortie-kills')
  const sortieDuration = queryField(container, 'sortie-duration')
  const sortieResult = queryField(container, 'sortie-result')
  const startSortieButton = queryAction(container, 'start-sortie')
  const claimRewardButton = queryAction(container, 'claim-reward')
  const nextSortieButton = queryAction(container, 'next-sortie')
  const quickSaveButton = queryAction(container, 'quick-save')
  const quickLoadButton = queryAction(container, 'quick-load')
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
        case 'preparation':
          loopPhase.textContent = 'Preparation'
          break
        case 'running':
          loopPhase.textContent = 'Sortie running'
          break
        case 'debrief_pending_reward':
          loopPhase.textContent = 'Debrief: reward pending'
          break
        case 'debrief_reward_claimed':
          loopPhase.textContent = 'Debrief: reward claimed'
          break
      }

      startSortieButton.disabled = state.loopPhase !== 'preparation'
      claimRewardButton.disabled = state.loopPhase !== 'debrief_pending_reward'
      nextSortieButton.disabled = state.loopPhase !== 'debrief_reward_claimed'
      quickSaveButton.disabled = state.loopPhase !== 'preparation'
      quickLoadButton.disabled = state.loopPhase !== 'preparation' || !actions.canQuickLoad()
      resetButton.disabled = state.loopPhase !== 'preparation'

      // AC1: pause button label reflects current pause state
      togglePauseButton.textContent = isPaused ? 'Resume' : 'Pause'

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

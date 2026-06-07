import type { GameState } from '../state'

export interface HudActions {
  onQuickSave(): void
  onReset(): void
}

export interface HudController {
  render(state: GameState): void
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
        <div><dt>Sortie Status</dt><dd data-field="sortie-status"></dd></div>
        <div><dt>Kills</dt><dd data-field="sortie-kills"></dd></div>
        <div><dt>Duration</dt><dd data-field="sortie-duration"></dd></div>
        <div><dt>Result</dt><dd data-field="sortie-result"></dd></div>
      </dl>
    </section>
    <section class="panel">
      <p class="eyebrow">Telemetry</p>
      <p class="status-copy" data-field="status"></p>
      <p class="status-copy status-copy--muted" data-field="command"></p>
    </section>
    <section class="panel panel--actions">
      <button type="button" data-action="save">Quick save</button>
      <button type="button" data-action="reset">Reset sortie</button>
    </section>
  `

  container
    .querySelector<HTMLButtonElement>('[data-action="save"]')
    ?.addEventListener('click', actions.onQuickSave)
  container
    .querySelector<HTMLButtonElement>('[data-action="reset"]')
    ?.addEventListener('click', actions.onReset)

  const hp = queryField(container, 'hp')
  const resources = queryField(container, 'resources')
  const shots = queryField(container, 'shots')
  const cooldown = queryField(container, 'cooldown')
  const status = queryField(container, 'status')
  const command = queryField(container, 'command')
  const sortieStatus = queryField(container, 'sortie-status')
  const sortieKills = queryField(container, 'sortie-kills')
  const sortieDuration = queryField(container, 'sortie-duration')
  const sortieResult = queryField(container, 'sortie-result')

  return {
    render(state) {
      hp.textContent = `${state.player.hp}/${state.player.maxHp}`
      resources.textContent = `${state.progress.resources}`
      shots.textContent = `${state.player.shotsFired}`
      cooldown.textContent = `${Math.ceil(state.player.weaponCooldownMs)} ms`
      status.textContent = state.telemetry.status
      command.textContent = state.telemetry.lastCommandSummary

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

function queryField(container: HTMLElement, name: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(`[data-field="${name}"]`)

  if (!element) {
    throw new Error(`HUD field "${name}" is missing.`)
  }

  return element
}

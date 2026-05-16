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

  return {
    render(state) {
      hp.textContent = `${state.player.hp}/${state.player.maxHp}`
      resources.textContent = `${state.progress.resources}`
      shots.textContent = `${state.player.shotsFired}`
      cooldown.textContent = `${Math.ceil(state.player.weaponCooldownMs)} ms`
      status.textContent = state.telemetry.status
      command.textContent = state.telemetry.lastCommandSummary
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

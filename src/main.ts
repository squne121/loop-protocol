import './style.css'

import { bindInput, createInputState, mapInputToCommands } from './input'
import { createCanvasRenderer } from './render'
import {
  createGameSnapshot,
  createInitialGameState,
  defaultSimulationConfig,
} from './state'
import { createLocalGameStorage } from './storage'
import {
  advanceSimulationLoop,
  clampPlayerToArena,
  runSortieSimulationStep,
  startSortie,
} from './systems'
import { createHudController } from './ui'

const app = document.querySelector<HTMLDivElement>('#app')

if (!app) {
  throw new Error('#app element is missing.')
}

app.innerHTML = `
  <div class="app-shell">
    <section class="battle-stage">
      <div class="battle-stage__header">
        <div>
          <p class="eyebrow">MVP battle loop</p>
          <h1>LOOP_PROTOCOL</h1>
        </div>
        <p class="battle-stage__copy">WASD to reposition. Hold pointer down to pressure the firing lane.</p>
      </div>
      <canvas class="battle-stage__canvas" aria-label="Battle arena"></canvas>
    </section>
    <aside class="command-rail" aria-label="Command rail"></aside>
  </div>
`

const canvas = app.querySelector<HTMLCanvasElement>('.battle-stage__canvas')
const commandRail = app.querySelector<HTMLElement>('.command-rail')

if (!canvas || !commandRail) {
  throw new Error('Application shell is incomplete.')
}

const storage = createLocalGameStorage()
let state = createInitialGameState(storage.load() ?? undefined)
const renderer = createCanvasRenderer(canvas)
const hud = createHudController(commandRail, {
  onQuickSave() {
    storage.save(createGameSnapshot(state))
  },
  onReset() {
    state = createInitialGameState()
    startSortie(state, defaultSimulationConfig.fixedDeltaMs)
    resizeArena(state)
  },
})
const inputState = createInputState()

bindInput(canvas, inputState, () => state.arena)
resizeArena(state)
window.addEventListener('resize', () => resizeArena(state))

// M2 bootstrap: start sortie once after initialisation (AC12)
startSortie(state, defaultSimulationConfig.fixedDeltaMs)

// E2E compile-time fixture overrides — only active in VITE_E2E_MODE builds.
// These are injected via page.addInitScript() in Playwright tests before the
// page script runs. Window flags are read once at initialisation time.
if (import.meta.env.VITE_E2E_MODE === 'true') {
  const e2eFlags = window as Window & {
    __E2E_SHORT_SORTIE__?: boolean
    __E2E_PLAYER_HP_OVERRIDE__?: number
  }
  // Short sortie: override targetTicks to ~0.5s for deterministic victory E2E
  if (e2eFlags.__E2E_SHORT_SORTIE__ === true && state.sortie.status === 'running') {
    state.sortie.targetTicks = Math.ceil(500 / defaultSimulationConfig.fixedDeltaMs)
  }
  // Player HP override: set hp/maxHp for deterministic defeat E2E
  if (typeof e2eFlags.__E2E_PLAYER_HP_OVERRIDE__ === 'number') {
    state.player.hp = e2eFlags.__E2E_PLAYER_HP_OVERRIDE__
    state.player.maxHp = e2eFlags.__E2E_PLAYER_HP_OVERRIDE__
  }
}

let accumulatorMs = 0
let previousFrameTime = performance.now()

function frame(now: number): void {
  const deltaMs = now - previousFrameTime
  previousFrameTime = now

  const result = advanceSimulationLoop(
    accumulatorMs,
    deltaMs,
    defaultSimulationConfig,
    stepSimulation,
  )
  accumulatorMs = result.accumulatorMs

  hud.render(state)
  renderer.render(state)
  window.requestAnimationFrame(frame)
}

window.requestAnimationFrame(frame)

function stepSimulation(fixedDeltaMs: number): void {
  const commands = mapInputToCommands(inputState)
  runSortieSimulationStep(state, commands, fixedDeltaMs)
}

function resizeArena(currentState: typeof state): void {
  const safeSidebar = window.innerWidth > 980 ? 380 : 32
  const width = Math.min(960, Math.max(640, window.innerWidth - safeSidebar))
  currentState.arena.width = width
  currentState.arena.height = Math.round(width * 0.5625)
  // Re-clamp player after arena resize to prevent out-of-bounds position.
  clampPlayerToArena(currentState)
}

// ---------------------------------------------------------------------------
// E2E observability hook (AC12)
// - ONLY active when VITE_E2E_MODE === 'true' (tree-shaken in production builds)
// - Read-only: returns a shallow snapshot (spread copy), never exposes live state
// - Production build MUST NOT contain '__LOOP_E2E__'
// ---------------------------------------------------------------------------

/** Minimal snapshot type exposed to E2E tests. */
interface LoopE2ESnapshot {
  tick: number
  elapsedMs: number
  player: {
    x: number
    y: number
    aimX: number
    aimY: number
    hp: number
    maxHp: number
  }
  projectiles: Array<{
    id: number
    x: number
    y: number
    ageMs: number
  }>
  input: {
    primaryPressed: boolean
    activePointerId: number | null
  }
  enemies: Array<{
    id: number
    x: number
    y: number
    hp: number
    maxHp: number
    defeatedAtTick: number | null
  }>
  sortie: {
    status: 'idle' | 'running' | 'victory' | 'defeat' | 'ended'
    elapsedTicks: number
    result: 'victory' | 'defeat' | null
  }
  arena: {
    width: number
    height: number
  }
}

if (import.meta.env.VITE_E2E_MODE === 'true') {
  ;(
    window as Window &
      typeof globalThis & {
        __LOOP_E2E__: { getState: () => LoopE2ESnapshot }
      }
  ).__LOOP_E2E__ = {
    /** Returns a shallow snapshot copy of the current game + input state. Read-only. */
    getState(): LoopE2ESnapshot {
      return {
        tick: state.tick,
        elapsedMs: state.elapsedMs,
        player: {
          x: state.player.x,
          y: state.player.y,
          aimX: state.player.aimX,
          aimY: state.player.aimY,
          hp: state.player.hp,
          maxHp: state.player.maxHp,
        },
        projectiles: state.projectiles.map((p) => ({
          id: p.id,
          x: p.x,
          y: p.y,
          ageMs: p.ageMs,
        })),
        input: {
          primaryPressed: inputState.primaryPressed,
          activePointerId: inputState.activePointerId,
        },
        enemies: state.enemies.map((e) => ({
          id: e.id,
          x: e.x,
          y: e.y,
          hp: e.hp,
          maxHp: e.maxHp,
          defeatedAtTick: e.defeatedAtTick,
        })),
        sortie: {
          status: state.sortie.status,
          elapsedTicks: state.sortie.elapsedTicks,
          result:
            state.sortie.result != null ? state.sortie.result.outcome : null,
        },
        arena: {
          width: state.arena.width,
          height: state.arena.height,
        },
      }
    },
  }
}

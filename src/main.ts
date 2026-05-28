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
  runCollisionSystem,
  runCombatSystem,
  runMovementSystem,
  runProjectileSystem,
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
    resizeArena(state)
  },
})
const inputState = createInputState()

bindInput(canvas, inputState, () => state.arena)
resizeArena(state)
window.addEventListener('resize', () => resizeArena(state))

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

function stepSimulation(deltaMs: number): void {
  const commands = mapInputToCommands(inputState)
  runMovementSystem(state, commands, deltaMs)
  runCombatSystem(state, commands, deltaMs)
  runProjectileSystem(state, commands, deltaMs)
  runCollisionSystem(state)
  state.tick += 1
  state.elapsedMs += deltaMs
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
        player: { ...state.player },
        projectiles: state.projectiles.map((p) => ({ ...p })),
        input: {
          primaryPressed: inputState.primaryPressed,
          activePointerId: inputState.activePointerId,
        },
      }
    },
  }
}

import './style.css'

import { createInputState, mapInputToCommands } from './input'
import { createCanvasRenderer } from './render'
import {
  createGameSnapshot,
  createInitialGameState,
  defaultSimulationConfig,
} from './state'
import { createLocalGameStorage } from './storage'
import { runCollisionSystem, runCombatSystem, runMovementSystem } from './systems'
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

bindInput(canvas, inputState, () => state)
resizeArena(state)
window.addEventListener('resize', () => resizeArena(state))

let accumulatorMs = 0
let previousFrameTime = performance.now()

function frame(now: number): void {
  const deltaMs = now - previousFrameTime
  previousFrameTime = now
  accumulatorMs += deltaMs

  let frameSkips = 0
  while (
    accumulatorMs >= defaultSimulationConfig.fixedDeltaMs &&
    frameSkips < defaultSimulationConfig.maxFrameSkip
  ) {
    stepSimulation(defaultSimulationConfig.fixedDeltaMs)
    accumulatorMs -= defaultSimulationConfig.fixedDeltaMs
    frameSkips += 1
  }

  hud.render(state)
  renderer.render(state)
  window.requestAnimationFrame(frame)
}

window.requestAnimationFrame(frame)

function stepSimulation(deltaMs: number): void {
  const commands = mapInputToCommands(inputState)
  runMovementSystem(state, commands, deltaMs)
  runCombatSystem(state, commands, deltaMs)
  runCollisionSystem(state)
  state.tick += 1
  state.elapsedMs += deltaMs
}

function bindInput(
  canvasElement: HTMLCanvasElement,
  input: ReturnType<typeof createInputState>,
  getState: () => typeof state,
): void {
  type MovementKey = 'moveUp' | 'moveDown' | 'moveLeft' | 'moveRight'

  const keyMap = new Map<string, MovementKey>([
    ['w', 'moveUp'],
    ['ArrowUp', 'moveUp'],
    ['s', 'moveDown'],
    ['ArrowDown', 'moveDown'],
    ['a', 'moveLeft'],
    ['ArrowLeft', 'moveLeft'],
    ['d', 'moveRight'],
    ['ArrowRight', 'moveRight'],
  ])

  window.addEventListener('keydown', (event) => {
    const key = keyMap.get(event.key)
    if (key) {
      input[key] = true
    }
  })

  window.addEventListener('keyup', (event) => {
    const key = keyMap.get(event.key)
    if (key) {
      input[key] = false
    }
  })

  const updatePointer = (event: PointerEvent) => {
    const bounds = canvasElement.getBoundingClientRect()
    const currentState = getState()
    input.pointerX =
      ((event.clientX - bounds.left) / bounds.width) * currentState.arena.width
    input.pointerY =
      ((event.clientY - bounds.top) / bounds.height) * currentState.arena.height
  }

  canvasElement.addEventListener('pointermove', updatePointer)
  canvasElement.addEventListener('pointerdown', (event) => {
    updatePointer(event)
    input.primaryPressed = true
  })
  window.addEventListener('pointerup', () => {
    input.primaryPressed = false
  })
}

function resizeArena(currentState: typeof state): void {
  const safeSidebar = window.innerWidth > 980 ? 380 : 32
  const width = Math.min(960, Math.max(640, window.innerWidth - safeSidebar))
  currentState.arena.width = width
  currentState.arena.height = Math.round(width * 0.5625)
}

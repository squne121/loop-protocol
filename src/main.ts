import './style.css'

import { initPlaytestEvidencePanel } from './ui/playtestEvidence'
import { bindInput, createInputState, mapInputToCommands } from './input'
import { createCanvasRenderer } from './render'
import {
  createGameSnapshot,
  createInitialGameState,
  defaultSimulationConfig,
} from './state'
import {
  createLocalGameStorage,
  type LoadResult,
  type SaveResult,
} from './storage'
import {
  advanceSimulationLoop,
  clampPlayerToArena,
  claimPendingReward,
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

// AC2: opt-in evidence panel — visible only when ?playtest_evidence=1
initPlaytestEvidencePanel(document.body, window.location.search)

const canvas = app.querySelector<HTMLCanvasElement>('.battle-stage__canvas')
const commandRail = app.querySelector<HTMLElement>('.command-rail')

if (!canvas || !commandRail) {
  throw new Error('Application shell is incomplete.')
}

const storage = createLocalGameStorage()
const loadResult = storage.load()
let hasLoadableSnapshot = loadResult.ok && loadResult.snapshot !== null
let state = createInitialGameState(loadResult.ok ? loadResult.snapshot ?? undefined : undefined)
const renderer = createCanvasRenderer(canvas)
const hud = createHudController(commandRail, {
  onStartSortie() {
    if (state.loopPhase !== 'preparation') {
      return
    }

    startSortie(state, defaultSimulationConfig.fixedDeltaMs)
    setHudFeedback('Sortie started.', 'Preparation controls are now locked until debrief.')
  },
  onClaimReward() {
    if (state.loopPhase !== 'debrief_pending_reward') {
      return
    }

    const claimResult = claimPendingReward(state)

    if (claimResult.ok) {
      setHudFeedback(
        'Reward claimed for this session.',
        'Persistence will be handled by issue #739.',
      )
      return
    }

    switch (claimResult.reason) {
      case 'already-claimed':
        setHudFeedback(
          'Reward already claimed for this session.',
          'Persistence will be handled by issue #739.',
        )
        return
      case 'no-pending-reward':
        setHudFeedback('No pending reward to claim.', 'Current state unchanged.')
        return
      case 'claimed-phase-ledger-miss':
        setHudFeedback('Reward claim ledger mismatch.', 'Current state unchanged.')
        return
    }
  },
  onNextSortie() {
    if (state.loopPhase !== 'debrief_reward_claimed') {
      return
    }

    startSortie(state, defaultSimulationConfig.fixedDeltaMs)
    setHudFeedback(
      'Next sortie started.',
      'Claimed reward remains available only in this in-memory session until saved.',
    )
  },
  onQuickSave() {
    if (state.loopPhase !== 'preparation') {
      return
    }

    const saveResult = storage.save(createGameSnapshot(state))
    if (!saveResult.ok) {
      reportStorageFailure('save', saveResult)
      setHudFeedback('Quick Save failed.', 'Current state unchanged.')
      return
    }

    hasLoadableSnapshot = true
    setHudFeedback('Quick Save complete.', 'Progression snapshot is ready for Quick Load.')
  },
  onQuickLoad() {
    if (state.loopPhase !== 'preparation') {
      return
    }

    if (!hasLoadableSnapshot) {
      setHudFeedback('Quick Load failed.', 'Current state unchanged.')
      return
    }

    const quickLoadResult = storage.load()
    if (!quickLoadResult.ok) {
      reportStorageFailure('load', quickLoadResult)
      setHudFeedback('Quick Load failed.', 'Current state unchanged.')
      return
    }

    if (quickLoadResult.snapshot === null) {
      hasLoadableSnapshot = false
      setHudFeedback('Quick Load failed.', 'Current state unchanged.')
      return
    }

    state = createInitialGameState(quickLoadResult.snapshot)
    resizeArena(state)
    hasLoadableSnapshot = true
    setHudFeedback('Quick Load complete.', 'Progression snapshot restored for preparation.')
  },
  onReset() {
    if (state.loopPhase !== 'preparation') {
      return
    }

    state = createInitialGameState()
    resizeArena(state)
    setHudFeedback(
      'Reset sortie complete.',
      'Reset sortie is a destructive boundary. Preparation only.',
    )
  },
  canQuickLoad() {
    return hasLoadableSnapshot
  },
})

if (!loadResult.ok) {
  reportStorageFailure('load', loadResult)
  setHudFeedback('Quick Load unavailable on startup.', 'A fresh preparation state was created.')
}
const inputState = createInputState()

bindInput(canvas, inputState, () => state.arena)
resizeArena(state)
window.addEventListener('resize', () => resizeArena(state))

maybeAutoStartRuntime()


// E2E compile-time fixture overrides — only active in VITE_E2E_MODE builds.
// These are injected via page.addInitScript() in Playwright tests before the
// page script runs. Window flags are read once at initialisation time.
if (import.meta.env.VITE_E2E_MODE === 'true') {
  const e2eFlags = window as Window & {
    __E2E_SHORT_SORTIE__?: boolean
    __E2E_PLAYER_HP_OVERRIDE__?: number
  }
  // Short sortie: override targetTicks to ~0.5s for deterministic timeout E2E
  if (e2eFlags.__E2E_SHORT_SORTIE__ === true && state.loopPhase === 'running' && state.sortie.status === 'running') {
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

function maybeAutoStartRuntime(): void {
  if (import.meta.env.VITE_E2E_MODE !== 'true') {
    return
  }

  if (state.loopPhase === 'preparation' && state.sortie.status === 'idle') {
    startSortie(state, defaultSimulationConfig.fixedDeltaMs)
  }
}

function setHudFeedback(status: string, summary: string): void {
  state.telemetry.status = status
  state.telemetry.lastCommandSummary = summary
}

function resizeArena(currentState: typeof state): void {
  const safeSidebar = window.innerWidth > 980 ? 380 : 32
  const width = Math.min(960, Math.max(640, window.innerWidth - safeSidebar))
  currentState.arena.width = width
  currentState.arena.height = Math.round(width * 0.5625)
  // Re-clamp player after arena resize to prevent out-of-bounds position.
  clampPlayerToArena(currentState)
}

function reportStorageFailure(
  operation: 'load' | 'save',
  result: Extract<LoadResult | SaveResult, { ok: false }>,
): void {
  console.warn(`[storage] ${operation} failed`, {
    reason: result.reason,
    errorName: result.errorName,
  })
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
  loopPhase: 'preparation' | 'running' | 'debrief_pending_reward' | 'debrief_reward_claimed'
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
    status: 'idle' | 'running' | 'victory' | 'defeat' | 'timeout' | 'ended'
    elapsedTicks: number
    result: 'victory' | 'defeat' | 'timeout' | null
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
        __LOOP_E2E__: { getState: () => LoopE2ESnapshot; startSortie: () => void }
      }
  ).__LOOP_E2E__ = {
    startSortie() {
      startSortie(state, defaultSimulationConfig.fixedDeltaMs)
    },
    /** Returns a shallow snapshot copy of the current game + input state. Read-only. */
    getState(): LoopE2ESnapshot {
      return {
        tick: state.tick,
        elapsedMs: state.elapsedMs,
        loopPhase: state.loopPhase,
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

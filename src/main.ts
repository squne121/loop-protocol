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
import {
  createDebugPauseState,
  toggleDebugPause,
  resetInputOnPause,
} from './ui/debugPause'

// Re-export for testing convenience (tests import from src/ui/debugPause directly)
export type { DebugPauseState } from './ui/debugPause'
export { createDebugPauseState, toggleDebugPause, resetInputOnPause } from './ui/debugPause'

type ProgressionSaveReason = 'reward-claim' | 'quick-save'

type ProgressionSaveFailureFeedback = {
  hasLoadableSnapshot: boolean
  status: string
  summary: string
  readbackAttempted: false
}

export function resolveProgressionSaveFailureFeedback(
  reason: ProgressionSaveReason,
  hadLoadableSnapshot: boolean,
): ProgressionSaveFailureFeedback {
  if (reason === 'reward-claim') {
    return {
      hasLoadableSnapshot: hadLoadableSnapshot,
      status: 'Result confirmed; progress not saved.',
      summary: hadLoadableSnapshot
        ? 'Previous local save is still available for Quick Load.'
        : 'No local save is currently available for Quick Load.',
      readbackAttempted: false,
    }
  }

  return {
    hasLoadableSnapshot: hadLoadableSnapshot,
    status: 'Quick Save failed.',
    summary: hadLoadableSnapshot
      ? 'Previous local save is still available for Quick Load.'
      : 'No local save is currently available for Quick Load.',
    readbackAttempted: false,
  }
}

// ---------------------------------------------------------------------------
// App shell
// ---------------------------------------------------------------------------

const isTestRuntime = import.meta.env.MODE === 'test'
const app = isTestRuntime ? null : document.querySelector<HTMLDivElement>('#app')

if (!app && !isTestRuntime) {
  throw new Error('#app element is missing.')
}

if (app) {
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
}

// AC2: opt-in evidence panel — visible only when ?playtest_evidence=1
if (app) {
  initPlaytestEvidencePanel(document.body, window.location.search)
}

const canvas = app?.querySelector<HTMLCanvasElement>('.battle-stage__canvas') ?? null
const commandRail = app?.querySelector<HTMLElement>('.command-rail') ?? null

if ((!canvas || !commandRail) && !isTestRuntime) {
  throw new Error('Application shell is incomplete.')
}

const storage = createLocalGameStorage()
const loadResult = storage.load()
let hasLoadableSnapshot = loadResult.ok && loadResult.snapshot !== null
let state = createInitialGameState(loadResult.ok ? loadResult.snapshot ?? undefined : undefined)
const renderer = canvas ? createCanvasRenderer(canvas) : null

// Debug pause state (runtime-local, not persisted)
const debugPause = createDebugPauseState()
const inputState = createInputState()

/** Toggle pause and reset firing state to prevent held-fire bleed (AC7). */
function handleTogglePause(): void {
  if (debugPause.isPaused) {
    // AC7: clear firing/pointer active state accumulated during pause, then resume
    resetInputOnPause(inputState)
    toggleDebugPause(debugPause)
    setHudFeedback('Resumed', 'Simulation resumed.')
    return
  }

  // BLOCKER 1: pause entry is only allowed during running phase
  if (state.loopPhase !== 'running') return

  toggleDebugPause(debugPause)
  // AC7: clear firing/pointer active state on pause entry
  resetInputOnPause(inputState)
  setHudFeedback('Paused', 'Simulation frozen. Rendering and HUD continue.')
}

const hud = commandRail ? createHudController(commandRail, {
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
      persistProgressionSnapshot('reward-claim')
      return
    }

    switch (claimResult.reason) {
      case 'already-claimed':
        setHudFeedback(
          'Reward already claimed for this session.',
          'Result already confirmed.',
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

    persistProgressionSnapshot('quick-save')
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
    // BLOCKER 1: reset debug pause on state transition to preparation
    debugPause.isPaused = false
    setHudFeedback('Quick Load complete.', 'Progression snapshot restored for preparation.')
  },
  onReset() {
    if (state.loopPhase !== 'preparation') {
      return
    }

    state = createInitialGameState()
    resizeArena(state)
    // BLOCKER 1: reset debug pause on state transition to preparation
    debugPause.isPaused = false
    setHudFeedback(
      'Reset sortie complete.',
      'Reset sortie is a destructive boundary. Preparation only.',
    )
  },
  canQuickLoad() {
    return hasLoadableSnapshot
  },
  onTogglePause() {
    handleTogglePause()
  },
}) : null

if (!loadResult.ok) {
  reportStorageFailure('load', loadResult)
  setHudFeedback('Quick Load unavailable on startup.', 'A fresh preparation state was created.')
}

if (canvas) {
  bindInput(canvas, inputState, () => state.arena)
  resizeArena(state)
  window.addEventListener('resize', () => resizeArena(state))
}

// AC2: Escape key toggles pause/resume; event.repeat guard prevents multi-toggle on held key
if (app) {
  window.addEventListener('keydown', (event: KeyboardEvent) => {
    if (event.key === 'Escape' && !event.repeat) {
      handleTogglePause()
    }
  })

  maybeAutoStartRuntime()
}


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
  if (!hud || !renderer) {
    return
  }

  const deltaMs = now - previousFrameTime
  previousFrameTime = now

  // AC3, AC5: while paused, do not advance the simulation accumulator.
  // Pass deltaMs=0 so advanceSimulationLoop executes 0 steps.
  // The accumulator is also reset on pause entry (below) to prevent catch-up.
  if (!debugPause.isPaused) {
    const result = advanceSimulationLoop(
      accumulatorMs,
      deltaMs,
      defaultSimulationConfig,
      stepSimulation,
    )
    accumulatorMs = result.accumulatorMs
  } else {
    // AC5: reset accumulator each frame while paused so wall-clock duration
    // does not build up and trigger catch-up steps on resume.
    accumulatorMs = 0
  }

  // AC4: render and HUD continue regardless of pause state
  hud.render(state, debugPause.isPaused)
  renderer.render(state)
  window.requestAnimationFrame(frame)
}

if (app) {
  window.requestAnimationFrame(frame)
}

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

function persistProgressionSnapshot(
  reason: ProgressionSaveReason,
): void {
  const hadLoadableSnapshot = hasLoadableSnapshot
  const saveResult = storage.save(createGameSnapshot(state))

  if (!saveResult.ok) {
    reportStorageFailure('save', saveResult)
    const failureFeedback = resolveProgressionSaveFailureFeedback(reason, hadLoadableSnapshot)
    hasLoadableSnapshot = failureFeedback.hasLoadableSnapshot
    setHudFeedback(failureFeedback.status, failureFeedback.summary)
    return
  }

  hasLoadableSnapshot = true

  if (reason === 'reward-claim') {
    setHudFeedback('Result confirmed.', 'Progress saved locally.')
    return
  }

  setHudFeedback('Quick Save complete.', 'Progression snapshot is ready for Quick Load.')
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

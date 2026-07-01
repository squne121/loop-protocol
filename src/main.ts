import './style.css'

import { initPlaytestEvidencePanel } from './ui/playtestEvidence'
import {
  getPlaytestEvidenceSnapshot,
  setSelfExplanationResponse,
} from './playtest/assistPlayerEventLog'
import {
  bindInput,
  createInputState,
  mapInputToCommands,
  type InputState,
} from './input'
import { createCanvasRenderer } from './render'
import {
  createGameSnapshot,
  createInitialGameState,
  defaultSimulationConfig,
  type GameState,
  type LoopPhase,
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
  confirmResult,
  type PhaseTransitionIntent,
  resolvePhaseTransition,
  runSortieSimulationStep,
  startSortie,
} from './systems'
import { createHudController } from './ui'
import {
  createProductPauseState,
  toggleProductPause,
  resetInputOnPause,
} from './ui/productPause'

// Re-export deprecated debugPause aliases for backward compatibility (AC11)
// Tests and external code that import from src/ui/debugPause continue to work
// via the deprecated wrapper in debugPause.ts
export type { DebugPauseState } from './ui/debugPause'
export { createDebugPauseState, toggleDebugPause, resetInputOnPause } from './ui/debugPause'

// Re-export product pause API (AC11: product-facing naming)
export type { ProductPauseState } from './ui/productPause'
export { createProductPauseState, toggleProductPause } from './ui/productPause'

type ProgressionSaveReason = 'reward-claim' | 'save'

type ProgressionSaveFailureFeedback = {
  hasLoadableSnapshot: boolean
  status: string
  summary: string
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
        ? 'Previous local save is still available; this result may be lost after reload.'
        : 'No local save is available; this result may be lost after reload.',
    }
  }

  return {
    hasLoadableSnapshot: hadLoadableSnapshot,
    status: 'Save failed.',
    summary: hadLoadableSnapshot
      ? 'Previous local save is still available; current progression was not written.'
      : 'No local save is available; current progression was not written.',
  }
}

type ProgressionSaveSeam = {
  storage: Pick<ReturnType<typeof createLocalGameStorage>, 'save' | 'load'>
  createSnapshot: () => ReturnType<typeof createGameSnapshot>
  reportSaveFailure: (result: Extract<SaveResult, { ok: false }>) => void
  setHudFeedback: (status: string, summary: string) => void
}

export function runProgressionSave(
  reason: ProgressionSaveReason,
  hadLoadableSnapshot: boolean,
  seam: ProgressionSaveSeam,
): boolean {
  const saveResult = seam.storage.save(seam.createSnapshot())

  if (!saveResult.ok) {
    seam.reportSaveFailure(saveResult)
    const failureFeedback = resolveProgressionSaveFailureFeedback(reason, hadLoadableSnapshot)
    seam.setHudFeedback(failureFeedback.status, failureFeedback.summary)
    return failureFeedback.hasLoadableSnapshot
  }

  if (reason === 'reward-claim') {
    seam.setHudFeedback('Result confirmed.', 'Progress saved locally.')
    return true
  }

  seam.setHudFeedback('Save complete.', 'Progression snapshot saved locally.')
  return true
}

export type NextSortieHandlerSeam = {
  setHudFeedback: (status: string, summary: string) => void
}

/**
 * Testable seam for the onNextSortie handler (B5, Issue #859).
 * If loopPhase !== 'debrief_reward_claimed', returns false without side effects.
 * Otherwise transitions state to 'preparation' and calls setHudFeedback with
 * 'Returned to preparation.' / 'Use Start sortie to begin the next sortie.'.
 */
export function runNextSortieHandler(
  state: GameState,
  seam: NextSortieHandlerSeam,
): boolean {
  if (state.loopPhase !== 'debrief_reward_claimed') {
    return false
  }
  const transition = resolvePhaseTransition(state.loopPhase, 'legacy_next_sortie')
  if (!transition.ok) {
    return false
  }
  state.loopPhase = transition.to
  seam.setHudFeedback(
    'Returned to preparation.',
    'Use Start sortie to begin the next sortie.',
  )
  return true
}

/**
 * Testable seam for the onConfirmResult handler (AC1–AC4, Issue #858).
 * Mirrors the production handler: if phase !== 'result', returns null without saving.
 * Otherwise calls confirmResult(state) then runProgressionSave('reward-claim', ...).
 */
export type ConfirmResultHandlerSeam = ProgressionSaveSeam & {
  resetDebugPause: () => void
}

export function runConfirmResultHandler(
  state: GameState,
  hadLoadableSnapshot: boolean,
  seam: ConfirmResultHandlerSeam,
): boolean | null {
  const confirmed = confirmResult(state)
  if (!confirmed) {
    return null
  }
  seam.resetDebugPause()
  return runProgressionSave('reward-claim', hadLoadableSnapshot, seam)
}

export type LoadGameSeam = {
  storage: { load(): LoadResult }
  reportLoadFailure(result: Extract<LoadResult, { ok: false }>): void
  setHudFeedback(status: string, summary: string): void
  onTitleMenuTransition(): void
  onLoadSuccess(snapshot: NonNullable<Extract<LoadResult, { ok: true }>['snapshot']>): void
  onLoadFail(): void
}

/**
 * AC3 testable seam: load game phase dispatch logic.
 * Extracted from onLoadGame() closure to allow actual storage.load() spy tests.
 */
export function runLoadGame(
  phase: LoopPhase,
  hasLoadableSnap: boolean,
  seam: LoadGameSeam,
): void {
  if (phase === 'title_menu') {
    seam.onTitleMenuTransition()
    return
  }
  if (phase === 'load_menu') {
    if (!hasLoadableSnap) {
      seam.setHudFeedback('Load Game failed.', 'No save data available.')
      return
    }
    const result = seam.storage.load()
    if (!result.ok) {
      seam.reportLoadFailure(result)
      seam.setHudFeedback('Load Game failed.', 'Current state unchanged.')
      return
    }
    if (result.snapshot === null) {
      seam.onLoadFail()
      seam.setHudFeedback('Load Game failed.', 'No save data found.')
      return
    }
    seam.onLoadSuccess(result.snapshot)
    seam.setHudFeedback('Load Game complete.', 'Progression snapshot restored.')
  }
}

export function queueAssistPlayerCommand(
  phase: LoopPhase,
  inputState: Pick<InputState, 'assistPlayerRisingEdge'>,
): boolean {
  if (phase !== 'running') {
    return false
  }
  inputState.assistPlayerRisingEdge = true
  return true
}

export function createTransitionedInitialGameState(
  currentPhase: LoopPhase,
  intent: 'new_game' | 'load_success' | 'reset_sortie',
  snapshot?: Parameters<typeof createInitialGameState>[0],
): GameState | null {
  const transition = resolvePhaseTransition(currentPhase, intent)
  if (!transition.ok) {
    return null
  }

  const nextState = createInitialGameState(snapshot)
  nextState.loopPhase = transition.to
  return nextState
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
      <canvas class="battle-stage__canvas" aria-label="Battle arena" tabindex="0"></canvas>
    </section>
    <aside class="command-rail" aria-label="Command rail"></aside>
  </div>
`
}

const canvas = app?.querySelector<HTMLCanvasElement>('.battle-stage__canvas') ?? null
const commandRail = app?.querySelector<HTMLElement>('.command-rail') ?? null

if ((!canvas || !commandRail) && !isTestRuntime) {
  throw new Error('Application shell is incomplete.')
}

const storage = createLocalGameStorage()
// B1: No auto-load on startup. Probe storage once to know if a snapshot exists,
// but do NOT apply it to state. Load is only triggered via Load Game button.
const startupProbe = storage.load()
let hasLoadableSnapshot = startupProbe.ok && startupProbe.snapshot !== null
let state: GameState = createInitialGameState()
// B1: Start in title_menu phase (not preparation).
const bootstrapTransition = resolvePhaseTransition(state.loopPhase, 'bootstrap_title_menu')
if (!bootstrapTransition.ok) {
  throw new Error(`Invalid bootstrap loop-phase transition: ${state.loopPhase}`)
}
state.loopPhase = bootstrapTransition.to

function transitionByIntent(intent: PhaseTransitionIntent): boolean {
  const transition = resolvePhaseTransition(state.loopPhase, intent)
  if (!transition.ok) {
    return false
  }
  state.loopPhase = transition.to
  return true
}

// AC2 / B1 (#987): opt-in evidence panel — visible only when ?playtest_evidence=1.
// The panel reads the live state-scoped evidence runtime via injected callbacks
// (no module-global store). `state` is reassigned on load, so the closures read
// the current binding each time.
if (app) {
  initPlaytestEvidencePanel(document.body, {
    search: window.location.search,
    getSnapshot: () => getPlaytestEvidenceSnapshot(state.playtestEvidenceRuntime),
    onSaveExplanation: (response) =>
      setSelfExplanationResponse(state.playtestEvidenceRuntime, response),
  })
}

const renderer = canvas ? createCanvasRenderer(canvas) : null

// Product pause state (runtime-local, not persisted) — AC10, AC11
const productPause = createProductPauseState()
const inputState = createInputState()

/** Toggle pause and reset firing state to prevent held-fire bleed (AC5). */
function handleTogglePause(): void {
  if (productPause.isPaused) {
    // AC5: clear firing/pointer active state accumulated during pause, then resume
    resetInputOnPause(inputState)
    toggleProductPause(productPause)
    setHudFeedback('Resumed', 'Simulation resumed.')
    return
  }

  // BLOCKER 1: pause entry is only allowed during running phase
  if (state.loopPhase !== 'running') return

  toggleProductPause(productPause)
  // AC5: clear firing/pointer active state on pause entry
  resetInputOnPause(inputState)
  setHudFeedback('Paused', 'Simulation frozen. Rendering and HUD continue.')
}

const hud = commandRail ? createHudController(commandRail, {
  onNewGame() {
    // AC1: title_menu → preparation (New Game). Separate from onStartSortie (AC2).
    const nextState = createTransitionedInitialGameState(state.loopPhase, 'new_game')
    if (nextState === null) {
      return
    }
    state = nextState
    resizeArena(state)
    productPause.isPaused = false
    setHudFeedback('New Game started.', 'Preparation phase. Start sortie when ready.')
  },
  onStartSortie() {
    // AC2: preparation only. title_menu New Game is handled by onNewGame().
    const started = startSortie(state, defaultSimulationConfig.fixedDeltaMs)
    if (!started) {
      return
    }
    setHudFeedback('Sortie started.', 'Preparation controls are now locked until result.')
  },
  onAssistPlayerCommand() {
    queueAssistPlayerCommand(state.loopPhase, inputState)
  },
  onClaimReward() {
    // Supports legacy debrief_pending_reward phase only.
    // In result phase, reward is auto-claimed by confirmResult() (B3).
    // AC5: result phase uses confirmResult() which auto-claims. Claim reward is legacy debrief only.
    if (state.loopPhase !== 'debrief_pending_reward') {
      return
    }
    const claimResult = claimPendingReward(state)

    if (claimResult.ok) {
      // B2: Do NOT call storage.save() here (result phase).
      // For legacy debrief_pending_reward, save happens when transitioning to preparation.
      setHudFeedback('Reward claimed.', 'Confirm result to save and return to preparation.')
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
  onConfirmResult() {
    // B3: confirm result auto-claims pending reward, then transitions to preparation and saves.
    if (!confirmResult(state)) {
      return
    }

    // Reset product pause on state transition to preparation
    productPause.isPaused = false
    // B2/B3: storage.save() called after preparation transition (AC2, AC8 compliant)
    // persistProgressionSnapshot sets HUD feedback internally (success or failure).
    // Do NOT call setHudFeedback() unconditionally here — that would overwrite a
    // save-failure message with a false success copy (AC6 fix).
    // Use 'reward-claim' reason so feedback reads "Result confirmed." on success (AC5).
    persistProgressionSnapshot('reward-claim')
  },
  onNextSortie() {
    // B5: legacy debrief_reward_claimed → preparation (not directly to running).
    // startSortie() only accepts preparation phase, so transition to preparation first.
    // Delegates to runNextSortieHandler seam for testability (Issue #859).
    if (!runNextSortieHandler(state, { setHudFeedback })) {
      return
    }
    resizeArena(state)
    productPause.isPaused = false
  },
  onSave() {
    // AC2, AC8: Save only allowed in preparation phase
    if (!transitionByIntent('save_progress')) {
      return
    }

    persistProgressionSnapshot('save')
  },
  onLoadGame() {
    // Delegated to runLoadGame() seam for testability (AC3).
    runLoadGame(state.loopPhase, hasLoadableSnapshot, {
      storage,
      reportLoadFailure(result) { reportStorageFailure('load', result) },
      setHudFeedback,
      onTitleMenuTransition() {
        if (!transitionByIntent('open_load_menu')) {
          return
        }
        setHudFeedback('Load Menu.', 'Select a save slot to load.')
      },
      onLoadSuccess(snapshot) {
        const nextState = createTransitionedInitialGameState(state.loopPhase, 'load_success', snapshot)
        if (nextState === null) {
          return
        }
        state = nextState
        resizeArena(state)
        hasLoadableSnapshot = true
        productPause.isPaused = false
      },
      onLoadFail() {
        hasLoadableSnapshot = false
      },
    })
  },
  onReset() {
    const nextState = createTransitionedInitialGameState(state.loopPhase, 'reset_sortie')
    if (nextState === null) {
      return
    }
    state = nextState
    resizeArena(state)
    // Reset product pause on state transition to preparation
    productPause.isPaused = false
    setHudFeedback(
      'Reset sortie complete.',
      'Reset sortie is a destructive boundary. Preparation only.',
    )
  },
  canLoadGame() {
    return hasLoadableSnapshot
  },
  onTogglePause() {
    handleTogglePause()
  },
}) : null

// B1: startup probe failure is non-fatal — title_menu state is always the starting point.
if (!startupProbe.ok) {
  reportStorageFailure('load', startupProbe)
}

if (canvas) {
  bindInput(canvas, inputState, () => state.arena)
  resizeArena(state)
  window.addEventListener('resize', () => resizeArena(state))
}

// AC2: Escape key toggles pause/resume; event.repeat guard prevents multi-toggle on held key
// AC3: P key only when canvas has focus (document.activeElement === canvas) — WCAG 2.1.4
// AC8: visibilitychange hidden → auto-pause during running phase only; visible → no auto-resume
// AC12: visibilitychange hidden auto-pauses only during running phase; visible does NOT auto-resume
if (app) {
  window.addEventListener('keydown', (event: KeyboardEvent) => {
    // AC2: Escape toggles pause regardless of focus
    if (event.key === 'Escape' && !event.repeat) {
      handleTogglePause()
      return
    }
    // AC3, AC15: KeyP (P key) only when canvas is active element (WCAG 2.1.4 Character Key Shortcuts)
    // event.code === 'KeyP' uses physical key position (layout-agnostic)
    if (event.code === 'KeyP') {
      if (!event.repeat && canvas && document.activeElement === canvas) {
        handleTogglePause()
      }
    }
  })

  // AC8, AC12: auto-pause on tab/window hide during running phase only
  // visible restore does NOT auto-resume (intentional: user must explicitly resume)
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && state.loopPhase === 'running' && !productPause.isPaused) {
      toggleProductPause(productPause)
      resetInputOnPause(inputState)
      setHudFeedback('Paused', 'Simulation paused: window hidden.')
    }
    // AC8: visible restoration does NOT auto-resume
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

  // AC4: while paused, do not advance the simulation accumulator.
  // Pass deltaMs=0 so advanceSimulationLoop executes 0 steps.
  // The accumulator is also reset on pause entry (below) to prevent catch-up.
  if (!productPause.isPaused) {
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
  hud.render(state, productPause.isPaused)
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

  // E2E: auto-transition from title_menu to preparation, then start sortie
  if (state.loopPhase === 'title_menu') {
    transitionByIntent('new_game')
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
  hasLoadableSnapshot = runProgressionSave(reason, hasLoadableSnapshot, {
    storage,
    createSnapshot: () => createGameSnapshot(state),
    reportSaveFailure: (result) => reportStorageFailure('save', result),
    setHudFeedback,
  })
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
  loopPhase: 'title_menu' | 'load_menu' | 'preparation' | 'running' | 'result' | 'debrief_pending_reward' | 'debrief_reward_claimed'
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
  commandIntent: {
    activeIntent: 'none' | 'assist_player'
    bufferedIntentExpiresAtTick: number | null
  }
  allies: Array<{
    id: number
    x: number
    y: number
    targetEntityId: string | null
    behaviorState: GameState['allies'][number]['behaviorState']
  }>
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
        commandIntent: {
          activeIntent: state.commandIntentRuntime.activeIntent,
          bufferedIntentExpiresAtTick:
            state.commandIntentRuntime.bufferedIntent?.expiresAtTick ?? null,
        },
        allies: state.allies.map((ally) => ({
          id: ally.id,
          x: ally.x,
          y: ally.y,
          targetEntityId: ally.targetEntityId,
          behaviorState: ally.behaviorState,
        })),
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

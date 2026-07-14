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
  purchaseUpgrade,
  quoteUpgrade,
  type PhaseTransitionIntent,
  resolvePhaseTransition,
  runSortieSimulationStep,
  startSortie,
} from './systems'
import { upgradeDefinitions } from './data/upgrades'
import { SORTIE_DURATION_MS } from './systems/SortieSystem'
import {
  configureBattleOverlayFoundation,
  createHudController,
  getUpgradeStatusCopy,
  resolveBattleOverlayElements,
  syncBattleOverlayPlaceholderRail,
  type HudUpgradeStatusCopy,
  type HudUpgradeViewModel,
} from './ui'
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

export type UpgradeWeaponHandlerSeam = {
  /** Stores the player-facing copy for the most recent purchase attempt (AC4, AC5). */
  setUpgradeStatusCopy: (copy: HudUpgradeStatusCopy) => void
  /**
   * Marks a loadable snapshot as available (AC6). Must be invoked, when the
   * purchase succeeded, BEFORE `renderHud()` so `hasLoadableSnapshot` is
   * already true by the time the synchronous render reads it — the caller
   * must not update this state only after receiving this function's return
   * value.
   */
  markLoadableSnapshot: () => void
  /**
   * Renders the HUD synchronously (AC6). Must be invoked before this function
   * returns so resources/weaponPower/hasLoadableSnapshot land in the same
   * render pass — the caller must not wait for the next requestAnimationFrame.
   */
  renderHud: () => void
}

/**
 * Testable seam for the onUpgradeWeapon handler (Issue #1282, AC3, AC4, AC6).
 *
 * `quoteUpgrade()` is the sole purchase-eligibility authority (AC3): this
 * function never re-derives eligibility from `state.loopPhase` itself.
 * `purchaseUpgrade()` — the atomic save seam — is invoked only when the quote
 * is `ok`. Returns `true` iff the purchase succeeded so the caller can update
 * `hasLoadableSnapshot` in the same synchronous pass (AC6). `seam.renderHud()`
 * is always invoked exactly once, synchronously, before this function returns.
 */
export function runUpgradeWeaponHandler(
  state: GameState,
  definition: (typeof upgradeDefinitions)[number],
  storage: Pick<ReturnType<typeof createLocalGameStorage>, 'save'>,
  seam: UpgradeWeaponHandlerSeam,
): boolean {
  const quote = quoteUpgrade(state, definition.definitionId, definition)
  if (!quote.ok) {
    seam.setUpgradeStatusCopy(getUpgradeStatusCopy(quote.reason))
    seam.renderHud()
    return false
  }

  const result = purchaseUpgrade(state, definition.definitionId, definition, storage)
  if (result.ok) {
    seam.setUpgradeStatusCopy(getUpgradeStatusCopy('ok'))
    // AC6: mark the loadable snapshot BEFORE renderHud() so the synchronous
    // render below observes hasLoadableSnapshot === true in the same pass —
    // the caller must not defer this until after this function returns.
    seam.markLoadableSnapshot()
  } else {
    seam.setUpgradeStatusCopy(getUpgradeStatusCopy(result.reason))
  }

  // AC6: render synchronously here (do not wait for the next requestAnimationFrame).
  seam.renderHud()
  return result.ok
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
      <!-- Interactive HUD descendants opt in via data-battle-interactive="true". -->
      <div class="battle-ui-layer" data-battle-ui-root>
        <div class="battle-hud-layer" data-battle-layer="hud"></div>
        <div class="battle-screen-layer" data-battle-layer="screen" hidden inert></div>
      </div>
    </section>
    <aside class="command-rail" aria-label="Command rail"></aside>
  </div>
`
}

const battleOverlay = app ? resolveBattleOverlayElements(app) : null
if (battleOverlay) {
  configureBattleOverlayFoundation(battleOverlay)
}

const canvas = battleOverlay?.canvas ?? null
const commandRail = battleOverlay?.commandRail ?? null
const battleHudLayer = battleOverlay?.hudLayer ?? null
const battleScreenLayer = battleOverlay?.screenLayer ?? null

function syncBattleOverlayLayout(): void {
  if (!commandRail) return
  syncBattleOverlayPlaceholderRail({ commandRail })
}

if ((!canvas || !commandRail || !battleHudLayer || !battleScreenLayer) && !isTestRuntime) {
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

// Issue #1282: M4 minimal upgrade catalog only has a single definition
// (weapon_power_plus_1). HUD upgrade purchase surface targets it directly;
// a definitionId selector is out of scope here (single-item catalog).
const upgradeDefinition = upgradeDefinitions[0]

/**
 * Player-facing feedback for the most recent upgrade purchase attempt
 * (Issue #1282, AC4/AC5). `null` until the player first interacts with the
 * upgrade surface. Never holds a raw internal enum value — only the
 * translated HudUpgradeStatusCopy pair.
 */
let upgradeStatusCopy: HudUpgradeStatusCopy | null = null

/**
 * Builds the upgrade purchase view model passed to hud.render() (AC2, AC3,
 * AC6). Re-evaluates `quoteUpgrade()` against the live `state` on every call
 * so the button's disabled state is always derived from the atomic purchase
 * core's own eligibility check — never from a HUD-local phase check.
 */
function buildUpgradeView(): HudUpgradeViewModel {
  const quote = quoteUpgrade(state, upgradeDefinition.definitionId, upgradeDefinition)
  return {
    definitionId: upgradeDefinition.definitionId,
    cost: upgradeDefinition.cost,
    weaponPower: state.progress.weaponPower,
    buttonDisabled: !quote.ok,
    statusCopy: upgradeStatusCopy,
  }
}

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

const hud = battleHudLayer ? createHudController(battleHudLayer, {
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
  onUpgradeWeapon() {
    // Delegated to runUpgradeWeaponHandler seam for testability (Issue #1282).
    // AC6: markLoadableSnapshot() is invoked by the seam BEFORE renderHud()
    // so hasLoadableSnapshot is already true when the synchronous render below
    // reads it — do NOT gate this on the handler's return value after the
    // fact (that would run one render pass too late).
    runUpgradeWeaponHandler(state, upgradeDefinition, storage, {
      setUpgradeStatusCopy(copy) {
        upgradeStatusCopy = copy
      },
      markLoadableSnapshot() {
        // purchaseUpgrade() is the atomic save seam (state.progress is only
        // committed after storage.save() succeeds), so no additional
        // runProgressionSave()/persistProgressionSnapshot() call is needed.
        hasLoadableSnapshot = true
      },
      renderHud() {
        syncBattleOverlayLayout()
        hud?.render(state, productPause.isPaused, buildUpgradeView())
      },
    })
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

// ---------------------------------------------------------------------------
// Visual scenario fixture (E2E/VRT only, Issue #1385)
// - Canonical fixture shape: `tests/e2e/visual-utils.ts` type-imports
//   `VisualScenarioFixture` FROM this module (single source of truth), so
//   `pnpm typecheck:e2e` catches drift instead of relying on a manually
//   kept-in-sync mirror.
// - Honored ONLY when import.meta.env.VITE_E2E_MODE === 'true' — tree-shaken
//   out of production builds. Production dist/** MUST NOT contain
//   '__LOOP_VISUAL_SCENARIO__'.
// - Detected and applied BEFORE `maybeAutoStartRuntime()` (further down):
//   running maybeAutoStartRuntime() first would call startSortie() and
//   mutate tick/HP/reward/evidence state that a fixture would then only
//   partially overwrite (Issue #1385 review, additional指摘6).
// - While a fixture is active, `frame()` (further down) freezes
//   `advanceSimulationLoop()` entirely via `visualScenarioActive`, so the
//   normal RAF simulation loop cannot tick a running fixture's deliberately
//   -set `elapsedTicks` past `targetTicks` and self-transition running ->
//   result on the very next frame (Issue #1385 review, Blocker 1).
// - Takes explicit precedence over legacy __E2E_SHORT_SORTIE__ /
//   __E2E_PLAYER_HP_OVERRIDE__: when a visual scenario fixture is present,
//   the legacy flags are ignored (a console warning documents the conflict
//   instead of silently mixing both fixture sources).
// ---------------------------------------------------------------------------

/** Named viewport labels a visual scenario fixture may request. `tests/e2e/visual-utils.ts`'s `VIEWPORT_LABELS` is typed `Record<VisualScenarioViewportLabel, ...>`, so adding/removing a label here is a compile error there until kept in sync. */
export type VisualScenarioViewportLabel = 'desktop-1280x720'

const VISUAL_SCENARIO_VIEWPORT_LABELS: readonly VisualScenarioViewportLabel[] = [
  'desktop-1280x720',
]

/** Fixed-step sortie state for the 'running' loopPhase visual scenarios. */
export interface RunningVisualSortie {
  status: 'running'
  elapsedTicks: number
  fixedDeltaMs: number
}

/** Terminal sortie state for the 'result' loopPhase visual scenarios. */
export interface TimeoutVisualSortie {
  status: 'timeout'
  elapsedTicks: number
  fixedDeltaMs: number
  /** Terminal duration authority (Timer / Volatile Text Policy). */
  durationMs: number
  kills: number
}

interface VisualScenarioFixtureCommon {
  paused: boolean
  player: { hp: number; maxHp: number }
  progress: { resources: number; weaponPower: number }
  /** Transient HUD copy (status line / last command summary). */
  telemetry: { status: string; summary: string }
  /** Fixed label describing the intended capture viewport. */
  viewportLabel: VisualScenarioViewportLabel
}

/**
 * Deterministic visual scenario fixture, installed as
 * `window.__LOOP_VISUAL_SCENARIO__` before the app's first render (see
 * `installVisualScenario()` in `tests/e2e/visual-utils.ts`, which
 * type-imports this interface — this module is the single source of truth
 * for the fixture contract, Issue #1385 review additional指摘9). A
 * discriminated union on `name` (additional指摘7) so an invalid
 * name/loopPhase/sortie-status combination is a compile-time error for
 * fixture authors, not just a runtime one.
 */
export type VisualScenarioFixture =
  | (VisualScenarioFixtureCommon & {
      name: 'running-hud'
      loopPhase: 'running'
      sortie: RunningVisualSortie
    })
  | (VisualScenarioFixtureCommon & {
      name: 'running-hud-paused'
      loopPhase: 'running'
      sortie: RunningVisualSortie
    })
  | (VisualScenarioFixtureCommon & {
      name: 'result-timeout'
      loopPhase: 'result'
      sortie: TimeoutVisualSortie
    })
  | (VisualScenarioFixtureCommon & {
      name: 'final-no-command-rail'
      loopPhase: 'result'
      sortie: TimeoutVisualSortie
    })

const VISUAL_SCENARIO_NAMES = [
  'running-hud',
  'running-hud-paused',
  'result-timeout',
  'final-no-command-rail',
] as const

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/**
 * Runtime validator (Issue #1385 review, additional指摘7): `window.__LOOP_VISUAL_SCENARIO__`
 * is caller-controlled test-only data, so its compile-time type is not
 * trusted. Fails closed (throws) on any unknown scenario name, mismatched
 * loopPhase/sortie shape, or unknown viewportLabel rather than silently
 * proceeding with a partially-applied or ambiguous fixture.
 */
function parseVisualScenarioFixture(raw: unknown): VisualScenarioFixture {
  if (typeof raw !== 'object' || raw === null) {
    throw new Error('[visual-scenario] fixture must be an object.')
  }
  const fixture = raw as Record<string, unknown>

  if (!VISUAL_SCENARIO_NAMES.includes(fixture.name as (typeof VISUAL_SCENARIO_NAMES)[number])) {
    throw new Error(`[visual-scenario] unknown scenario name: ${String(fixture.name)}`)
  }
  const name = fixture.name as (typeof VISUAL_SCENARIO_NAMES)[number]

  if (typeof fixture.paused !== 'boolean') {
    throw new Error('[visual-scenario] "paused" must be a boolean.')
  }
  if (
    !VISUAL_SCENARIO_VIEWPORT_LABELS.includes(fixture.viewportLabel as VisualScenarioViewportLabel)
  ) {
    throw new Error(`[visual-scenario] unknown viewportLabel: ${String(fixture.viewportLabel)}`)
  }
  const viewportLabel = fixture.viewportLabel as VisualScenarioViewportLabel

  const player = fixture.player as Record<string, unknown> | undefined
  if (!player || !isFiniteNumber(player.hp) || !isFiniteNumber(player.maxHp)) {
    throw new Error('[visual-scenario] "player.hp"/"player.maxHp" must be finite numbers.')
  }

  const progress = fixture.progress as Record<string, unknown> | undefined
  if (!progress || !isFiniteNumber(progress.resources) || !isFiniteNumber(progress.weaponPower)) {
    throw new Error(
      '[visual-scenario] "progress.resources"/"progress.weaponPower" must be finite numbers.',
    )
  }

  const telemetry = fixture.telemetry as Record<string, unknown> | undefined
  if (!telemetry || typeof telemetry.status !== 'string' || typeof telemetry.summary !== 'string') {
    throw new Error('[visual-scenario] "telemetry.status"/"telemetry.summary" must be strings.')
  }

  const sortie = fixture.sortie as Record<string, unknown> | undefined
  if (!sortie || !isFiniteNumber(sortie.elapsedTicks) || !isFiniteNumber(sortie.fixedDeltaMs)) {
    throw new Error(
      '[visual-scenario] "sortie.elapsedTicks"/"sortie.fixedDeltaMs" must be finite numbers.',
    )
  }

  const common: VisualScenarioFixtureCommon = {
    paused: fixture.paused as boolean,
    player: { hp: player.hp as number, maxHp: player.maxHp as number },
    progress: {
      resources: progress.resources as number,
      weaponPower: progress.weaponPower as number,
    },
    telemetry: { status: telemetry.status as string, summary: telemetry.summary as string },
    viewportLabel,
  }

  if (name === 'running-hud' || name === 'running-hud-paused') {
    if (sortie.status !== 'running') {
      throw new Error(`[visual-scenario] scenario "${name}" requires sortie.status "running".`)
    }
    return {
      ...common,
      name,
      loopPhase: 'running',
      sortie: {
        status: 'running',
        elapsedTicks: sortie.elapsedTicks as number,
        fixedDeltaMs: sortie.fixedDeltaMs as number,
      },
    }
  }

  if (sortie.status !== 'timeout') {
    throw new Error(`[visual-scenario] scenario "${name}" requires sortie.status "timeout".`)
  }
  if (!isFiniteNumber(sortie.durationMs) || !isFiniteNumber(sortie.kills)) {
    throw new Error(
      '[visual-scenario] "sortie.durationMs"/"sortie.kills" must be finite numbers for a timeout scenario.',
    )
  }
  return {
    ...common,
    name,
    loopPhase: 'result',
    sortie: {
      status: 'timeout',
      elapsedTicks: sortie.elapsedTicks as number,
      fixedDeltaMs: sortie.fixedDeltaMs as number,
      durationMs: sortie.durationMs as number,
      kills: sortie.kills as number,
    },
  }
}

/**
 * Applies a deterministic visual scenario fixture to game state (AC3).
 * Fixes loopPhase, pause state, sortie state, hull (player hp/maxHp),
 * resources/upgrades (progress), and transient telemetry copy in a single
 * synchronous pass. `targetTicks` is set with headroom above both
 * `elapsedTicks` and the production sortie duration (`SORTIE_DURATION_MS`)
 * so a running fixture's structural invariant (`elapsedTicks < targetTicks`)
 * cannot be violated by rounding (Issue #1385 review, Blocker 1); the caller
 * additionally freezes the RAF simulation loop while a visual scenario is
 * active (`visualScenarioActive`, see `frame()` below) so `elapsedTicks`
 * never advances past the fixture's deliberately-set value in the first
 * place — the `targetTicks` headroom is defense-in-depth, not the only
 * safeguard.
 */
function applyVisualScenarioFixture(fixture: VisualScenarioFixture): void {
  state.loopPhase = fixture.loopPhase
  productPause.isPaused = fixture.paused
  state.player.hp = fixture.player.hp
  state.player.maxHp = fixture.player.maxHp
  state.progress.resources = fixture.progress.resources
  state.progress.weaponPower = fixture.progress.weaponPower
  state.telemetry.status = fixture.telemetry.status
  state.telemetry.lastCommandSummary = fixture.telemetry.summary

  if (fixture.sortie.status === 'timeout') {
    state.sortie = {
      status: 'timeout',
      elapsedTicks: fixture.sortie.elapsedTicks,
      targetTicks: fixture.sortie.elapsedTicks,
      result: {
        outcome: 'timeout',
        endReason: 'timeout',
        durationMs: fixture.sortie.durationMs,
        kills: fixture.sortie.kills,
        shotsFired: state.player.shotsFired,
        playerHpRemaining: fixture.player.hp,
      },
    }
    return
  }

  state.sortie = {
    status: 'running',
    elapsedTicks: fixture.sortie.elapsedTicks,
    targetTicks: Math.max(
      fixture.sortie.elapsedTicks + 1,
      Math.ceil(SORTIE_DURATION_MS / fixture.sortie.fixedDeltaMs),
    ),
    result: null,
  }
}

// Detected BEFORE maybeAutoStartRuntime() (below) so a visual scenario
// fixture — not the normal E2E auto-start — is the sole authority over
// loopPhase/sortie/player/progress/telemetry state (Issue #1385 review,
// additional指摘6).
let visualScenarioActive = false

if (import.meta.env.VITE_E2E_MODE === 'true') {
  const e2eScenarioFlag = window as Window & { __LOOP_VISUAL_SCENARIO__?: unknown }

  if (e2eScenarioFlag.__LOOP_VISUAL_SCENARIO__ !== undefined) {
    const legacyFlags = window as Window & {
      __E2E_SHORT_SORTIE__?: boolean
      __E2E_PLAYER_HP_OVERRIDE__?: number
    }
    if (
      legacyFlags.__E2E_SHORT_SORTIE__ === true ||
      typeof legacyFlags.__E2E_PLAYER_HP_OVERRIDE__ === 'number'
    ) {
      // Documented precedence (Issue #1385): __LOOP_VISUAL_SCENARIO__ fully
      // determines phase/sortie/player/progress/telemetry state, so legacy
      // E2E flags are ignored rather than silently combined.
      console.warn(
        '[visual-scenario] __LOOP_VISUAL_SCENARIO__ takes precedence over ' +
          '__E2E_SHORT_SORTIE__ / __E2E_PLAYER_HP_OVERRIDE__; legacy flags ignored.',
      )
    }
    const fixture = parseVisualScenarioFixture(e2eScenarioFlag.__LOOP_VISUAL_SCENARIO__)
    visualScenarioActive = true
    applyVisualScenarioFixture(fixture)
  }
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

  if (visualScenarioActive) {
    // The visual scenario fixture (detected above, before any auto-start
    // side effect) is already applied — do not auto-start a sortie or
    // double-apply legacy E2E overrides on top of it (Issue #1385 review,
    // additional指摘6).
  } else {
    maybeAutoStartRuntime()

    // E2E compile-time legacy fixture overrides — only active in
    // VITE_E2E_MODE builds, and only when no visual scenario fixture is
    // present. Only meaningful once maybeAutoStartRuntime() has possibly
    // transitioned to 'running'.
    if (import.meta.env.VITE_E2E_MODE === 'true') {
      const legacyFlags = window as Window & {
        __E2E_SHORT_SORTIE__?: boolean
        __E2E_PLAYER_HP_OVERRIDE__?: number
      }
      // Short sortie: override targetTicks to ~0.5s for deterministic timeout E2E
      if (
        legacyFlags.__E2E_SHORT_SORTIE__ === true &&
        state.loopPhase === 'running' &&
        state.sortie.status === 'running'
      ) {
        state.sortie.targetTicks = Math.ceil(500 / defaultSimulationConfig.fixedDeltaMs)
      }
      // Player HP override: set hp/maxHp for deterministic defeat E2E
      if (typeof legacyFlags.__E2E_PLAYER_HP_OVERRIDE__ === 'number') {
        state.player.hp = legacyFlags.__E2E_PLAYER_HP_OVERRIDE__
        state.player.maxHp = legacyFlags.__E2E_PLAYER_HP_OVERRIDE__
      }
    }
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
  // BLOCKER 1 (Issue #1385 review): also freeze while a visual scenario
  // fixture is active — a running fixture's `elapsedTicks` is deliberately
  // set and must not be advanced by the ordinary RAF simulation loop.
  if (!productPause.isPaused && !visualScenarioActive) {
    const result = advanceSimulationLoop(
      accumulatorMs,
      deltaMs,
      defaultSimulationConfig,
      stepSimulation,
    )
    accumulatorMs = result.accumulatorMs
  } else {
    // AC5: reset accumulator each frame while paused or visual-scenario
    // -frozen so wall-clock duration does not build up and trigger
    // catch-up steps on resume.
    accumulatorMs = 0
  }

  // AC4: render and HUD continue regardless of pause state
  syncBattleOverlayLayout()
  hud.render(state, productPause.isPaused, buildUpgradeView())
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

/**
 * E2E pre-bootstrap fixture (Issue #1283, read-only navigation-time config —
 * not a live mutation API). When `window.__LOOP_E2E_BOOTSTRAP__.autoStart` is
 * explicitly `false`, `maybeAutoStartRuntime()` becomes a no-op and the app
 * remains at `title_menu`, letting an E2E scenario drive the normal
 * player-facing Load Game / New Game / Launch sortie navigation instead of
 * the legacy auto-start shortcut. This flag must be set via
 * `page.addInitScript()` before navigation — it is read exactly once here,
 * at module init, and is never re-read or mutated afterward.
 */
function isE2EAutoStartDisabled(): boolean {
  const bootstrap = (
    window as Window & { __LOOP_E2E_BOOTSTRAP__?: { autoStart?: boolean } }
  ).__LOOP_E2E_BOOTSTRAP__
  return bootstrap?.autoStart === false
}

function maybeAutoStartRuntime(): void {
  if (import.meta.env.VITE_E2E_MODE !== 'true') {
    return
  }

  if (isE2EAutoStartDisabled()) {
    // Issue #1283: bootstrap fixture requests no auto-start — stay at
    // title_menu so the scenario can drive Load Game / New Game explicitly.
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
//   or a mutation method (Issue #1283, AC10 / Design Constraints: this hook is
//   observation-only — `startSortie()` and any other state-mutating method
//   MUST NOT be exposed here; navigation-time bootstrap control lives in
//   `window.__LOOP_E2E_BOOTSTRAP__` / `isE2EAutoStartDisabled()` instead, and
//   player-facing actions are driven through DOM buttons / canvas pointer
//   input, never through this hook).
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
  /** Read-only progression snapshot (Issue #1283, AC1, AC3, AC5). */
  progress: {
    resources: number
    weaponPower: number
  }
  projectiles: Array<{
    id: number
    x: number
    y: number
    ageMs: number
    /** Damage snapshot taken from state.progress.weaponPower at fire time (Issue #1283, AC6). */
    damage: number
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
        __LOOP_E2E__: { getState: () => LoopE2ESnapshot }
      }
  ).__LOOP_E2E__ = {
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
        progress: {
          resources: state.progress.resources,
          weaponPower: state.progress.weaponPower,
        },
        projectiles: state.projectiles.map((p) => ({
          id: p.id,
          x: p.x,
          y: p.y,
          ageMs: p.ageMs,
          damage: p.damage,
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

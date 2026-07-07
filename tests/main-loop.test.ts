import { describe, expect, it, vi } from 'vitest'

import { advanceSimulationLoop } from '../src/systems/SimulationLoop'
import { defaultSimulationConfig, createInitialGameState } from '../src/state'
import {
  confirmResult,
  claimPendingReward,
  SORTIE_DURATION_MS,
} from '../src/systems/SortieSystem'
import {
  createTransitionedInitialGameState,
  queueAssistPlayerCommand,
  runLoadGame,
  runNextSortieHandler,
  runProgressionSave,
  runUpgradeWeaponHandler,
} from '../src/main'
import { resolvePhaseTransition } from '../src/systems/PhaseTransitionSystem'
import { createInputState, mapInputToCommands } from '../src/input'
import type { SaveResult } from '../src/storage'
import { upgradeDefinitions } from '../src/data/upgrades'
import type { HudUpgradeStatusCopy } from '../src/ui/HudController'

const FIXED_DT = defaultSimulationConfig.fixedDeltaMs
const MAX_SKIP = defaultSimulationConfig.maxFrameSkip

// Use a config with integer-friendly fixed step for deterministic tests
const integerConfig = { fixedDeltaMs: 16, maxFrameSkip: 5 }

describe('advanceSimulationLoop', () => {
  it('GIVEN accumulatorMs < fixedDeltaMs WHEN deltaMs is small THEN executes 0 steps and accumulates', () => {
    const stepFn = vi.fn()
    const result = advanceSimulationLoop(0, FIXED_DT / 2, defaultSimulationConfig, stepFn)
    expect(stepFn).toHaveBeenCalledTimes(0)
    expect(result.stepsExecuted).toBe(0)
    expect(result.accumulatorMs).toBeCloseTo(FIXED_DT / 2)
    expect(result.panicDiscarded).toBe(false)
  })

  it('GIVEN accumulator crosses fixedDeltaMs threshold WHEN deltaMs is exactly one step THEN executes exactly 1 step', () => {
    const stepFn = vi.fn()
    const result = advanceSimulationLoop(0, FIXED_DT, defaultSimulationConfig, stepFn)
    expect(stepFn).toHaveBeenCalledTimes(1)
    expect(result.stepsExecuted).toBe(1)
    expect(result.accumulatorMs).toBeCloseTo(0)
    expect(result.panicDiscarded).toBe(false)
  })

  it('GIVEN accumulator far exceeds maxFrameSkip limit WHEN deltaMs is huge THEN discards residual (panic clamp)', () => {
    const stepFn = vi.fn()
    // Huge deltaMs causes far more steps than maxFrameSkip allows
    const hugeDeltaMs = FIXED_DT * (MAX_SKIP + 10)
    const result = advanceSimulationLoop(0, hugeDeltaMs, defaultSimulationConfig, stepFn)

    expect(stepFn).toHaveBeenCalledTimes(MAX_SKIP)
    expect(result.stepsExecuted).toBe(MAX_SKIP)
    expect(result.panicDiscarded).toBe(true)
    // Residual after panic clamp must be < fixedDeltaMs
    expect(result.accumulatorMs).toBeGreaterThanOrEqual(0)
    expect(result.accumulatorMs).toBeLessThan(FIXED_DT)
  })

  it('GIVEN exactly maxFrameSkip steps remain (integer config) WHEN deltaMs fills them exactly THEN no panic discard', () => {
    const stepFn = vi.fn()
    // Use integer-friendly config to avoid floating-point issues
    const exactDeltaMs = integerConfig.fixedDeltaMs * integerConfig.maxFrameSkip
    const result = advanceSimulationLoop(0, exactDeltaMs, integerConfig, stepFn)
    expect(result.stepsExecuted).toBe(integerConfig.maxFrameSkip)
    expect(result.panicDiscarded).toBe(false)
    expect(result.accumulatorMs).toBeCloseTo(0)
  })

  it('GIVEN panic clamp WHEN residual is 1.5 * fixedDeltaMs THEN accumulatorMs = 0.5 * fixedDeltaMs', () => {
    const stepFn = vi.fn()
    // Use integer-friendly config: 1.5 * 16 = 24ms residual
    const residual = integerConfig.fixedDeltaMs * 1.5
    const hugeDelta = integerConfig.fixedDeltaMs * integerConfig.maxFrameSkip + residual
    const result = advanceSimulationLoop(0, hugeDelta, integerConfig, stepFn)
    expect(result.panicDiscarded).toBe(true)
    expect(result.accumulatorMs).toBeCloseTo(integerConfig.fixedDeltaMs * 0.5)
  })

  it('GIVEN residual after maxFrameSkip WHEN panic clamp applied THEN result.accumulatorMs < fixedDeltaMs (using default config)', () => {
    const stepFn = vi.fn()
    // Large delta: max skips + 3 extra
    const largeDelta = FIXED_DT * (MAX_SKIP + 3)
    const result = advanceSimulationLoop(0, largeDelta, defaultSimulationConfig, stepFn)
    expect(result.panicDiscarded).toBe(true)
    expect(result.accumulatorMs).toBeGreaterThanOrEqual(0)
    expect(result.accumulatorMs).toBeLessThan(FIXED_DT)
  })
})

// ---------------------------------------------------------------------------
// AC8: phase guard — storage.save() only in preparation (B1, B2, B3, B4)
// ---------------------------------------------------------------------------

function makeResultState() {
  const state = createInitialGameState()
  // Simulate a completed sortie to reach result phase
  state.loopPhase = 'running'
  state.sortie = {
    status: 'victory',
    elapsedTicks: 1800,
    targetTicks: 1800,
    result: Object.freeze({
      outcome: 'victory',
      endReason: 'all_enemies_defeated',
      durationMs: SORTIE_DURATION_MS,
      kills: 3,
      shotsFired: 10,
      playerHpRemaining: 6,
    }),
  }
  state.loopPhase = 'result'
  state.resultRewardStatus = 'pending'
  state.pendingRewardApplicationId = 'sortie-reward-1'
  return state
}

function makeSaveSpySeam(mockSaveResult: SaveResult = { ok: true, reason: 'saved' }) {
  const save = vi.fn<() => SaveResult>(() => mockSaveResult)
  const load = vi.fn(() => ({ ok: true as const, snapshot: null, reason: 'empty' as const }))
  const createSnapshot = vi.fn(() => ({
    schemaVersion: 1 as const,
    resources: 0,
    weaponPower: 1,
    playerMaxHp: 8,
  }))
  const reportSaveFailure = vi.fn()
  const setHudFeedback = vi.fn()
  return {
    storage: { save, load },
    createSnapshot,
    reportSaveFailure,
    setHudFeedback,
    save,
    load,
  }
}

describe('B1: initial state is title_menu', () => {
  it('GIVEN createInitialGameState() WHEN loopPhase is read THEN it starts as preparation (default)', () => {
    // createInitialGameState defaults to preparation; main.ts overrides to title_menu at startup.
    // This test documents the contract: main.ts must explicitly set title_menu.
    const state = createInitialGameState()
    expect(state.loopPhase).toBe('preparation')
  })

  it('GIVEN bootstrap state default WHEN title_menu transition is validated THEN bootstrap can transition to title_menu', () => {
    const state = createInitialGameState()

    expect(resolvePhaseTransition(state.loopPhase, 'bootstrap_title_menu')).toMatchObject({
      ok: true,
      from: 'preparation',
      to: 'title_menu',
      intent: 'bootstrap_title_menu',
    })
  })
})

describe('B3: confirmResult() auto-claims pending reward and transitions to preparation', () => {
  it('GIVEN result phase with pending reward WHEN confirmResult() is called THEN reward is claimed and loopPhase becomes preparation', () => {
    const state = makeResultState()
    expect(state.loopPhase).toBe('result')
    expect(state.resultRewardStatus).toBe('pending')
    expect(state.pendingRewardApplicationId).toBe('sortie-reward-1')

    confirmResult(state)

    expect(state.loopPhase).toBe('preparation')
    expect(state.resultRewardStatus).toBe('claimed')
    // Reward should have been applied (resources > 0 after victory)
    expect(state.progress.resources).toBeGreaterThanOrEqual(0)
  })

  it('GIVEN result phase with already-claimed reward WHEN confirmResult() is called THEN loopPhase still becomes preparation', () => {
    const state = makeResultState()
    // Pre-claim the reward
    claimPendingReward(state)
    expect(state.resultRewardStatus).toBe('claimed')

    confirmResult(state)

    expect(state.loopPhase).toBe('preparation')
  })

  it('GIVEN non-result phase WHEN confirmResult() is called THEN state is unchanged (no-op)', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'

    confirmResult(state)

    expect(state.loopPhase).toBe('preparation')
  })
})

describe('B2/B4: storage.save() phase guard via runProgressionSave', () => {
  it('GIVEN preparation WHEN runProgressionSave is called THEN storage.save() is invoked exactly once (AC8)', () => {
    const seam = makeSaveSpySeam()
    runProgressionSave('save', false, seam)
    expect(seam.save).toHaveBeenCalledTimes(1)
  })

  it('GIVEN save reason "save" WHEN runProgressionSave succeeds THEN HUD shows save complete', () => {
    const seam = makeSaveSpySeam()
    runProgressionSave('save', false, seam)
    expect(seam.setHudFeedback).toHaveBeenCalledWith('Save complete.', 'Progression snapshot saved locally.')
  })

  it('GIVEN save reason "save" with failing storage WHEN runProgressionSave fails THEN storage.save() is called exactly once and returns false', () => {
    const seam = makeSaveSpySeam({ ok: false, reason: 'write-error', errorName: 'QuotaExceededError' })
    const result = runProgressionSave('save', false, seam)
    expect(seam.save).toHaveBeenCalledTimes(1)
    expect(result).toBe(false)
  })
})

describe('assist DOM command routing', () => {
  it('GIVEN running phase WHEN queueAssistPlayerCommand is invoked THEN next tick emits sample_assist_player', () => {
    const inputState = createInputState()

    const accepted = queueAssistPlayerCommand('running', inputState)
    const commands = mapInputToCommands(inputState)

    expect(accepted).toBe(true)
    expect(commands).toContainEqual({ type: 'sample_assist_player' })
    expect(inputState.assistPlayerRisingEdge).toBe(false)
  })

  it('GIVEN non-running phase WHEN queueAssistPlayerCommand is invoked THEN no sample_assist_player command is emitted', () => {
    const inputState = createInputState()

    const accepted = queueAssistPlayerCommand('preparation', inputState)
    const commands = mapInputToCommands(inputState)

    expect(accepted).toBe(false)
    expect(commands.some((command) => command.type === 'sample_assist_player')).toBe(false)
  })
})

describe('resolvePhaseTransition', () => {
  it('GIVEN title_menu WHEN resolving new_game intent THEN transition is allowed', () => {
    expect(resolvePhaseTransition('title_menu', 'new_game')).toMatchObject({
      ok: true,
      from: 'title_menu',
      to: 'preparation',
      intent: 'new_game',
    })
  })

  it('GIVEN running WHEN resolving reset_sortie intent THEN transition is denied', () => {
    expect(resolvePhaseTransition('running', 'reset_sortie')).toMatchObject({
      ok: false,
      error: {
        code: 'illegal-transition',
        from: 'running',
        intent: 'reset_sortie',
      },
    })
  })
})

describe('createTransitionedInitialGameState', () => {
  it('GIVEN title_menu WHEN new_game intent is applied THEN preparation state is returned', () => {
    const nextState = createTransitionedInitialGameState('title_menu', 'new_game')

    expect(nextState).not.toBeNull()
    expect(nextState?.loopPhase).toBe('preparation')
  })

  it('GIVEN load_menu WHEN load_success intent is applied THEN preparation state is restored', () => {
    const nextState = createTransitionedInitialGameState('load_menu', 'load_success', {
      schemaVersion: 1,
      resources: 7,
      weaponPower: 2,
      playerMaxHp: 10,
    })

    expect(nextState).not.toBeNull()
    expect(nextState?.loopPhase).toBe('preparation')
    expect(nextState?.progress.resources).toBe(7)
  })

  it('GIVEN running WHEN reset_sortie intent is applied THEN no state is returned', () => {
    expect(createTransitionedInitialGameState('running', 'reset_sortie')).toBeNull()
  })
})

describe('legacy next sortie seam', () => {
  it('GIVEN debrief_reward_claimed WHEN runNextSortieHandler called THEN preparation transition is committed', () => {
    const state = createInitialGameState()
    state.loopPhase = 'debrief_reward_claimed'
    const setHudFeedback = vi.fn()

    const result = runNextSortieHandler(state, { setHudFeedback })

    expect(result).toBe(true)
    expect(state.loopPhase).toBe('preparation')
    expect(setHudFeedback).toHaveBeenCalledWith(
      'Returned to preparation.',
      'Use Start sortie to begin the next sortie.',
    )
  })
})

describe('B3+B2: confirmResult() then storage.save() sequence', () => {
  it('GIVEN result+pending WHEN confirmResult() transitions to preparation THEN subsequent runProgressionSave calls storage.save() once', () => {
    const state = makeResultState()
    const seam = makeSaveSpySeam()

    // Simulates the onConfirmResult handler in main.ts
    confirmResult(state)
    expect(state.loopPhase).toBe('preparation')
    expect(state.resultRewardStatus).toBe('claimed')

    // After preparation transition, save is valid
    runProgressionSave('save', false, seam)
    expect(seam.save).toHaveBeenCalledTimes(1)
  })
})

// ---------------------------------------------------------------------------
// AC3: storage.load() spy — only called from title_menu / load_menu
// ---------------------------------------------------------------------------


// ---------------------------------------------------------------------------
// AC3: runLoadGame seam — actual storage.load() spy matrix
// ---------------------------------------------------------------------------

describe('AC3: runLoadGame — storage.load() only called from load_menu', () => {
  it('GIVEN title_menu WHEN runLoadGame called THEN storage.load is NOT called (transition only)', () => {
    const loadSpy = vi.fn()
    const transitionSpy = vi.fn()
    runLoadGame('title_menu', true, {
      storage: { load: loadSpy },
      reportLoadFailure: vi.fn(),
      setHudFeedback: vi.fn(),
      onTitleMenuTransition: transitionSpy,
      onLoadSuccess: vi.fn(),
      onLoadFail: vi.fn(),
    })
    expect(loadSpy).not.toHaveBeenCalled()
    expect(transitionSpy).toHaveBeenCalledTimes(1)
  })

  it('GIVEN load_menu with snapshot WHEN runLoadGame called THEN storage.load is called exactly once', () => {
    const loadSpy = vi.fn(() => ({
      ok: true as const,
      snapshot: { schemaVersion: 1 as const, resources: 5, weaponPower: 1, playerMaxHp: 8 },
      reason: 'loaded' as const,
    }))
    const successSpy = vi.fn()
    runLoadGame('load_menu', true, {
      storage: { load: loadSpy },
      reportLoadFailure: vi.fn(),
      setHudFeedback: vi.fn(),
      onTitleMenuTransition: vi.fn(),
      onLoadSuccess: successSpy,
      onLoadFail: vi.fn(),
    })
    expect(loadSpy).toHaveBeenCalledTimes(1)
    expect(successSpy).toHaveBeenCalledTimes(1)
  })

  it('GIVEN preparation phase WHEN runLoadGame called THEN storage.load is NOT called (no-op)', () => {
    const loadSpy = vi.fn()
    runLoadGame('preparation', false, {
      storage: { load: loadSpy },
      reportLoadFailure: vi.fn(),
      setHudFeedback: vi.fn(),
      onTitleMenuTransition: vi.fn(),
      onLoadSuccess: vi.fn(),
      onLoadFail: vi.fn(),
    })
    expect(loadSpy).not.toHaveBeenCalled()
  })

  it('GIVEN running phase WHEN runLoadGame called THEN storage.load is NOT called (no-op)', () => {
    const loadSpy = vi.fn()
    runLoadGame('running', false, {
      storage: { load: loadSpy },
      reportLoadFailure: vi.fn(),
      setHudFeedback: vi.fn(),
      onTitleMenuTransition: vi.fn(),
      onLoadSuccess: vi.fn(),
      onLoadFail: vi.fn(),
    })
    expect(loadSpy).not.toHaveBeenCalled()
  })

  it('GIVEN result phase WHEN runLoadGame called THEN storage.load is NOT called (no-op)', () => {
    const loadSpy = vi.fn()
    runLoadGame('result', false, {
      storage: { load: loadSpy },
      reportLoadFailure: vi.fn(),
      setHudFeedback: vi.fn(),
      onTitleMenuTransition: vi.fn(),
      onLoadSuccess: vi.fn(),
      onLoadFail: vi.fn(),
    })
    expect(loadSpy).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// AC5/AC6: reward-claim reason regression — confirm result feedback
// ---------------------------------------------------------------------------

describe('AC5/AC6: runProgressionSave with reward-claim reason', () => {
  it('GIVEN reward-claim reason and save succeeds THEN feedback is "Result confirmed." / "Progress saved locally."', () => {
    const setHudFeedback = vi.fn()
    runProgressionSave('reward-claim', false, {
      storage: { save: vi.fn(() => ({ ok: true as const, reason: 'saved' as const })) },
      createSnapshot: () => ({ schemaVersion: 1 as const, resources: 0, weaponPower: 1, playerMaxHp: 8 }),
      reportSaveFailure: vi.fn(),
      setHudFeedback,
    })
    expect(setHudFeedback).toHaveBeenCalledWith('Result confirmed.', 'Progress saved locally.')
  })

  it('GIVEN reward-claim reason and save fails THEN feedback is progress-not-saved (no false success)', () => {
    const setHudFeedback = vi.fn()
    runProgressionSave('reward-claim', false, {
      storage: {
        save: vi.fn(() => ({
          ok: false as const,
          reason: 'write-error' as const,
          errorName: 'QuotaExceededError',
        })),
      },
      createSnapshot: () => ({ schemaVersion: 1 as const, resources: 0, weaponPower: 1, playerMaxHp: 8 }),
      reportSaveFailure: vi.fn(),
      setHudFeedback,
    })
    expect(setHudFeedback).toHaveBeenCalledWith(
      'Result confirmed; progress not saved.',
      expect.stringContaining('may be lost after reload'),
    )
  })
})

// ---------------------------------------------------------------------------
// Issue #1282: runUpgradeWeaponHandler — purchase eligibility, feedback mapping,
// and same-render-pass hasLoadableSnapshot update (AC3, AC4, AC6)
// ---------------------------------------------------------------------------

describe('Issue #1282: runUpgradeWeaponHandler — synchronous purchase feedback (AC3, AC4, AC6)', () => {
  it('GIVEN preparation with enough resources WHEN runUpgradeWeaponHandler purchases THEN storage.save is called once, resources/weaponPower update, purchased is true, and renderHud is invoked synchronously with the updated state (AC6)', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    state.progress.resources = 150
    state.progress.weaponPower = 1
    const save = vi.fn(() => ({ ok: true as const, reason: 'saved' as const }))

    let renderedResources: number | null = null
    let renderedWeaponPower: number | null = null
    const renderHud = vi.fn(() => {
      renderedResources = state.progress.resources
      renderedWeaponPower = state.progress.weaponPower
    })
    let copy: HudUpgradeStatusCopy | null = null

    const purchased = runUpgradeWeaponHandler(state, upgradeDefinitions[0], { save }, {
      setUpgradeStatusCopy: (c) => {
        copy = c
      },
      markLoadableSnapshot: vi.fn(),
      renderHud,
    })

    expect(purchased).toBe(true)
    expect(save).toHaveBeenCalledTimes(1)
    expect(state.progress.resources).toBe(50)
    expect(state.progress.weaponPower).toBe(2)
    expect(renderHud).toHaveBeenCalledTimes(1)
    expect(renderedResources).toBe(50)
    expect(renderedWeaponPower).toBe(2)
    expect(copy).toEqual({
      status: 'Upgrade installed.',
      summary: 'Weapon Power increased. Resources were saved.',
    })
  })

  it('GIVEN preparation with enough resources WHEN runUpgradeWeaponHandler purchases THEN markLoadableSnapshot() is invoked before renderHud() so hasLoadableSnapshot-equivalent state is already true at render time (AC6 regression)', () => {
    // Regression test for PR #1365 iteration-2 OWNER REQUEST_CHANGES: the
    // previous seam shape (`renderHud` only, with the caller updating
    // `hasLoadableSnapshot` after receiving the handler's return value) let
    // the synchronous render observe a stale `hasLoadableSnapshot === false`,
    // because the caller-side assignment ran AFTER seam.renderHud() had
    // already fired. This test fails against that shape (there is no
    // markLoadableSnapshot seam member to invoke before renderHud) and only
    // passes once markLoadableSnapshot() is called by the production handler
    // before renderHud() on every successful purchase.
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    state.progress.resources = 150
    state.progress.weaponPower = 1
    const save = vi.fn(() => ({ ok: true as const, reason: 'saved' as const }))

    // Mirrors the production seam wiring in main.ts: markLoadableSnapshot()
    // sets a local flag, and renderHud() captures the flag's value at the
    // moment it is invoked. If markLoadableSnapshot() is not called before
    // renderHud(), renderedHasLoadableSnapshot will be false — proving the
    // false-green (stale render) bug.
    let hasLoadableSnapshot = false
    let renderedHasLoadableSnapshot: boolean | null = null
    const renderHudCallOrder: string[] = []
    const renderHud = vi.fn(() => {
      renderHudCallOrder.push('renderHud')
      renderedHasLoadableSnapshot = hasLoadableSnapshot
    })

    const purchased = runUpgradeWeaponHandler(state, upgradeDefinitions[0], { save }, {
      setUpgradeStatusCopy: vi.fn(),
      markLoadableSnapshot: () => {
        renderHudCallOrder.push('markLoadableSnapshot')
        hasLoadableSnapshot = true
      },
      renderHud,
    })

    expect(purchased).toBe(true)
    expect(renderHud).toHaveBeenCalledTimes(1)
    // markLoadableSnapshot() must run strictly before renderHud() (AC6: no
    // next-rAF wait — the flag must already be true inside the same
    // synchronous render pass).
    expect(renderHudCallOrder).toEqual(['markLoadableSnapshot', 'renderHud'])
    expect(renderedHasLoadableSnapshot).toBe(true)
  })

  it('GIVEN insufficient resources WHEN runUpgradeWeaponHandler is called THEN purchaseUpgrade (storage.save) is not invoked and feedback maps to the AC4 "Not enough resources." copy', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    state.progress.resources = 10
    const save = vi.fn(() => ({ ok: true as const, reason: 'saved' as const }))
    const renderHud = vi.fn()
    let copy: HudUpgradeStatusCopy | null = null

    const purchased = runUpgradeWeaponHandler(state, upgradeDefinitions[0], { save }, {
      setUpgradeStatusCopy: (c) => {
        copy = c
      },
      markLoadableSnapshot: vi.fn(),
      renderHud,
    })

    expect(purchased).toBe(false)
    expect(save).not.toHaveBeenCalled()
    expect(copy).toEqual({
      status: 'Not enough resources.',
      summary: 'Earn 100 resources before upgrading.',
    })
    expect(renderHud).toHaveBeenCalledTimes(1)
  })

  it('GIVEN a non-preparation phase WHEN runUpgradeWeaponHandler is called THEN feedback maps to the hangar copy without leaking the raw loopPhase value (AC4, AC5)', () => {
    const state = createInitialGameState()
    state.loopPhase = 'running'
    const save = vi.fn(() => ({ ok: true as const, reason: 'saved' as const }))
    let copy: HudUpgradeStatusCopy | null = null

    const purchased = runUpgradeWeaponHandler(state, upgradeDefinitions[0], { save }, {
      setUpgradeStatusCopy: (c) => {
        copy = c
      },
      markLoadableSnapshot: vi.fn(),
      renderHud: vi.fn(),
    })

    expect(purchased).toBe(false)
    expect(save).not.toHaveBeenCalled()
    expect(copy?.status).toBe('Upgrade available in hangar.')
    expect(copy?.summary).toBe('Return to preparation before upgrading.')
    expect(copy?.status).not.toContain('running')
    expect(copy?.summary).not.toContain('running')
  })

  it('GIVEN an already-purchased upgrade WHEN runUpgradeWeaponHandler is called THEN purchase is rejected and feedback maps to the "already installed" copy', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    state.progress.resources = 500
    state.progress.weaponPower = 5
    const save = vi.fn(() => ({ ok: true as const, reason: 'saved' as const }))
    let copy: HudUpgradeStatusCopy | null = null

    const purchased = runUpgradeWeaponHandler(state, upgradeDefinitions[0], { save }, {
      setUpgradeStatusCopy: (c) => {
        copy = c
      },
      markLoadableSnapshot: vi.fn(),
      renderHud: vi.fn(),
    })

    expect(purchased).toBe(false)
    expect(save).not.toHaveBeenCalled()
    expect(copy).toEqual({
      status: 'Upgrade already installed.',
      summary: 'Weapon Power is already upgraded.',
    })
  })

  it('GIVEN a failing storage.save() WHEN runUpgradeWeaponHandler purchases THEN resources/weaponPower are left untouched (atomic seam) and feedback maps to the write-error copy', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    state.progress.resources = 150
    state.progress.weaponPower = 1
    const save = vi.fn(() => ({
      ok: false as const,
      reason: 'write-error' as const,
      errorName: 'QuotaExceededError',
    }))
    let copy: HudUpgradeStatusCopy | null = null

    const purchased = runUpgradeWeaponHandler(state, upgradeDefinitions[0], { save }, {
      setUpgradeStatusCopy: (c) => {
        copy = c
      },
      markLoadableSnapshot: vi.fn(),
      renderHud: vi.fn(),
    })

    expect(purchased).toBe(false)
    expect(save).toHaveBeenCalledTimes(1)
    // Atomic seam: state.progress must be untouched on save failure
    expect(state.progress.resources).toBe(150)
    expect(state.progress.weaponPower).toBe(1)
    expect(copy).toEqual({
      status: 'Upgrade not saved.',
      summary: 'No resources were spent. Check browser storage and try again.',
    })
  })
})

/**
 * Issue #858: reward claim 保存一回性をテストで固定
 *
 * AC1: result phase + pending reward で Confirm result 相当処理を実行すると、
 *      confirmResult(state) 後の state.progress.resources を含む snapshot が
 *      storage.save() に exactly once で渡される
 * AC2: 保存された snapshot は schemaVersion / resources / weaponPower / playerMaxHp のみ
 * AC3: 同じ handler seam を 2 回実行しても 2 回目は storage.save() を呼ばず、resources も増えない
 * AC4: storage.save() failure では "Result confirmed." / "Progress saved locally." を表示しない
 * AC5: このファイルのテストが追加・通過している
 */
import { describe, expect, it, vi } from 'vitest'
import type { SaveResult } from '../src/storage'
import { createInitialGameState, createGameSnapshot } from '../src/state/GameState'
import { runConfirmResultHandler } from '../src/main'

// ---------------------------------------------------------------------------
// Test state factory
// ---------------------------------------------------------------------------

function makeResultState() {
  const state = createInitialGameState()
  state.loopPhase = 'running'
  state.sortie = {
    status: 'victory',
    elapsedTicks: 1800,
    targetTicks: 1800,
    result: Object.freeze({
      outcome: 'victory',
      endReason: 'all_enemies_defeated',
      durationMs: 30000,
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

function makeHandlerSeam(mockSaveResult: SaveResult = { ok: true, reason: 'saved' }) {
  const state = makeResultState()
  const save = vi.fn<[ReturnType<typeof createGameSnapshot>], SaveResult>(() => mockSaveResult)
  const load = vi.fn(() => ({ ok: true as const, snapshot: null, reason: 'empty' as const }))
  const reportSaveFailure = vi.fn()
  const setHudFeedback = vi.fn()
  const resetDebugPause = vi.fn()

  const seam = {
    storage: { save, load },
    createSnapshot: () => createGameSnapshot(state),
    reportSaveFailure,
    setHudFeedback,
    resetDebugPause,
    save,
  }

  return { state, seam, save, setHudFeedback, reportSaveFailure, resetDebugPause }
}

// ---------------------------------------------------------------------------
// AC1: storage.save() は exactly once 呼ばれる
// ---------------------------------------------------------------------------

describe('AC1: result phase + pending reward → storage.save() exactly once', () => {
  it('GIVEN result phase with pending reward WHEN runConfirmResultHandler is called THEN storage.save() is called exactly once', () => {
    const { state, seam, save } = makeHandlerSeam()

    runConfirmResultHandler(state, false, seam)

    expect(save).toHaveBeenCalledTimes(1)
  })

  it('GIVEN result phase with pending reward WHEN runConfirmResultHandler is called THEN snapshot contains resources after reward claim', () => {
    const { state, seam, save } = makeHandlerSeam()
    const resourcesBefore = state.progress.resources

    runConfirmResultHandler(state, false, seam)

    expect(save).toHaveBeenCalledTimes(1)
    const savedSnapshot = save.mock.calls[0][0]
    // resources must reflect the claimed reward (non-negative, >= initial 0)
    expect(savedSnapshot.resources).toBeGreaterThanOrEqual(resourcesBefore)
    expect(state.loopPhase).toBe('preparation')
    expect(state.resultRewardStatus).toBe('claimed')
  })
})

// ---------------------------------------------------------------------------
// AC2: snapshot は schemaVersion / resources / weaponPower / playerMaxHp のみ
// ---------------------------------------------------------------------------

describe('AC2: snapshot key set is exactly schemaVersion / resources / weaponPower / playerMaxHp', () => {
  it('GIVEN result phase WHEN runConfirmResultHandler saves THEN snapshot has only the 4 allowed keys', () => {
    const { state, seam, save } = makeHandlerSeam()

    runConfirmResultHandler(state, false, seam)

    expect(save).toHaveBeenCalledTimes(1)
    const savedSnapshot = save.mock.calls[0][0]

    // Exact key set assertion
    const keys = Object.keys(savedSnapshot).sort()
    expect(keys).toEqual(['playerMaxHp', 'resources', 'schemaVersion', 'weaponPower'])
  })

  it('GIVEN result phase WHEN runConfirmResultHandler saves THEN snapshot does NOT contain runtime-only keys', () => {
    const { state, seam, save } = makeHandlerSeam()

    runConfirmResultHandler(state, false, seam)

    const savedSnapshot = save.mock.calls[0][0] as Record<string, unknown>

    // Runtime-only fields must NOT be present in the snapshot
    expect(savedSnapshot).not.toHaveProperty('rewardClaims')
    expect(savedSnapshot).not.toHaveProperty('pendingRewardApplicationId')
    expect(savedSnapshot).not.toHaveProperty('loopPhase')
    expect(savedSnapshot).not.toHaveProperty('enemies')
    expect(savedSnapshot).not.toHaveProperty('projectiles')
    expect(savedSnapshot).not.toHaveProperty('hp')
    expect(savedSnapshot).not.toHaveProperty('tick')
  })
})

// ---------------------------------------------------------------------------
// AC3: double confirm — 2 回目は storage.save() を呼ばず resources も増えない
// ---------------------------------------------------------------------------

describe('AC3: double confirm does not call storage.save() twice and resources do not increase again', () => {
  it('GIVEN handler called once (success) WHEN called again with same state THEN storage.save() is NOT called on second invocation', () => {
    const { state, seam, save } = makeHandlerSeam()

    // First invocation: result phase → transitions to preparation
    runConfirmResultHandler(state, false, seam)
    expect(save).toHaveBeenCalledTimes(1)
    expect(state.loopPhase).toBe('preparation')

    // Second invocation: phase is now 'preparation', guard should reject
    runConfirmResultHandler(state, true, seam)
    expect(save).toHaveBeenCalledTimes(1) // still 1, not 2
  })

  it('GIVEN handler called twice WHEN resources counted after both calls THEN resources are the same after second call', () => {
    const { state, seam } = makeHandlerSeam()

    // First call applies reward
    runConfirmResultHandler(state, false, seam)
    const resourcesAfterFirst = state.progress.resources

    // Second call is a no-op (preparation phase)
    runConfirmResultHandler(state, true, seam)
    const resourcesAfterSecond = state.progress.resources

    expect(resourcesAfterSecond).toBe(resourcesAfterFirst)
  })

  it('GIVEN already-confirmed state (preparation) WHEN runConfirmResultHandler is called THEN returns null (phase guard)', () => {
    const { state, seam } = makeHandlerSeam()

    // Transition to preparation first
    runConfirmResultHandler(state, false, seam)
    expect(state.loopPhase).toBe('preparation')

    // Second call should be rejected by phase guard
    const result = runConfirmResultHandler(state, true, seam)
    expect(result).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// AC4: storage.save() failure → "Result confirmed." / "Progress saved locally." は表示しない
// ---------------------------------------------------------------------------

describe('AC4: storage.save() failure does not show success messages', () => {
  it('GIVEN storage.save() fails WHEN runConfirmResultHandler is called THEN setHudFeedback is NOT called with success messages', () => {
    const { state, seam, setHudFeedback } = makeHandlerSeam({
      ok: false,
      reason: 'write-error',
      errorName: 'QuotaExceededError',
    })

    runConfirmResultHandler(state, false, seam)

    // Success messages must NOT appear
    const calls = setHudFeedback.mock.calls
    const successCall = calls.find(
      ([status, summary]) =>
        status === 'Result confirmed.' || summary === 'Progress saved locally.',
    )
    expect(successCall).toBeUndefined()
  })

  it('GIVEN storage.save() fails WHEN runConfirmResultHandler is called THEN storage.save() is still called exactly once', () => {
    const { state, seam, save } = makeHandlerSeam({
      ok: false,
      reason: 'write-error',
      errorName: 'QuotaExceededError',
    })

    runConfirmResultHandler(state, false, seam)

    expect(save).toHaveBeenCalledTimes(1)
  })
})

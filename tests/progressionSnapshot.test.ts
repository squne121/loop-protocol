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
import { claimPendingReward } from '../src/systems/SortieSystem'

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
    const { state, seam, save, resetDebugPause } = makeHandlerSeam()

    runConfirmResultHandler(state, false, seam)

    expect(save).toHaveBeenCalledTimes(1)
    expect(resetDebugPause).toHaveBeenCalledTimes(1)
  })

  it('GIVEN result phase with pending reward WHEN runConfirmResultHandler is called THEN snapshot contains resources after reward claim', () => {
    const { state, seam, save } = makeHandlerSeam()
    const resourcesBefore = state.progress.resources

    runConfirmResultHandler(state, false, seam)

    expect(save).toHaveBeenCalledTimes(1)
    const savedSnapshot = save.mock.calls[0][0]
    // reward が実際に加算された（victory: base=100 + kills*bonus + hpBonus > 0）
    expect(state.progress.resources).toBeGreaterThan(resourcesBefore)
    // snapshot が post-claim state と一致する
    expect(savedSnapshot.resources).toBe(state.progress.resources)
    expect(savedSnapshot.weaponPower).toBe(state.progress.weaponPower)
    expect(savedSnapshot.playerMaxHp).toBe(state.player.maxHp)
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

  it('GIVEN result phase already claimed via claimPendingReward WHEN runConfirmResultHandler is called THEN resources are not added again and saved snapshot matches claimed state', () => {
    const { state, seam, save } = makeHandlerSeam()

    // 事前に手動 claim（result phase のまま留まる）
    const claim = claimPendingReward(state)
    expect(claim.ok).toBe(true)
    expect(state.loopPhase).toBe('result')
    expect(state.resultRewardStatus).toBe('claimed')

    const resourcesAfterPreClaim = state.progress.resources

    // confirmResult handler は already-claimed でも save を 1 回だけ呼ぶ
    runConfirmResultHandler(state, false, seam)

    expect(save).toHaveBeenCalledTimes(1)
    // resources は変化しない（二重加算なし）
    expect(state.progress.resources).toBe(resourcesAfterPreClaim)
    expect(save.mock.calls[0][0].resources).toBe(resourcesAfterPreClaim)
    expect(state.loopPhase).toBe('preparation')
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

  it('GIVEN storage.save() fails with hadLoadableSnapshot=true THEN failure feedback preserves loadable snapshot state', () => {
    const { state, seam, setHudFeedback, reportSaveFailure } = makeHandlerSeam({
      ok: false,
      reason: 'write-error',
      errorName: 'QuotaExceededError',
    })

    const result = runConfirmResultHandler(state, true, seam)

    // reportSaveFailure が呼ばれる
    expect(reportSaveFailure).toHaveBeenCalledTimes(1)
    // failure feedback の具体値を確認
    expect(setHudFeedback).toHaveBeenCalledWith(
      'Result confirmed; progress not saved.',
      'Previous local save is still available; this result may be lost after reload.',
    )
    // hadLoadableSnapshot=true のとき戻り値は true
    expect(result).toBe(true)
  })

  it('GIVEN storage.save() fails with hadLoadableSnapshot=false THEN failure feedback reports no available save', () => {
    const { state, seam, setHudFeedback, reportSaveFailure } = makeHandlerSeam({
      ok: false,
      reason: 'write-error',
      errorName: 'QuotaExceededError',
    })

    const result = runConfirmResultHandler(state, false, seam)

    expect(reportSaveFailure).toHaveBeenCalledTimes(1)
    expect(setHudFeedback).toHaveBeenCalledWith(
      'Result confirmed; progress not saved.',
      'No local save is available; this result may be lost after reload.',
    )
    // hadLoadableSnapshot=false のとき戻り値は false
    expect(result).toBe(false)
  })
})

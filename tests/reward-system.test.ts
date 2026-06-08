import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { createInitialGameState, type GameState, type SortieResult } from '../src/state/GameState'
import {
  PER_SORTIE_REWARD_CAP,
  RESOURCE_CAP,
  REWARD_FORMULA_VERSION,
  RewardSystem,
} from '../src/systems/RewardSystem'

function createResult(overrides: Partial<SortieResult> = {}): SortieResult {
  return {
    outcome: 'victory',
    endReason: 'all_enemies_defeated',
    durationMs: 30_000,
    kills: 3,
    shotsFired: 12,
    playerHpRemaining: 5,
    ...overrides,
  } as SortieResult
}

describe('RewardSystem.calculate', () => {
  it('GIVEN a victory result WHEN calculate is called THEN it returns the exact reward formula', () => {
    expect(RewardSystem.calculate(createResult())).toEqual({
      formulaVersion: REWARD_FORMULA_VERSION,
      outcome: 'victory',
      base: 100,
      killBonus: 15,
      hpBonus: 5,
      delta: 120,
    })
  })

  it('GIVEN a defeat result WHEN calculate is called THEN it excludes hp bonus', () => {
    expect(
      RewardSystem.calculate(
        createResult({
          outcome: 'defeat',
          endReason: 'player_hp_zero',
          kills: 2,
          playerHpRemaining: 99,
        }),
      ),
    ).toEqual({
      formulaVersion: REWARD_FORMULA_VERSION,
      outcome: 'defeat',
      base: 10,
      killBonus: 10,
      hpBonus: 0,
      delta: 20,
    })
  })

  it('GIVEN a timeout result WHEN calculate is called THEN it uses the timeout base reward', () => {
    expect(
      RewardSystem.calculate(
        createResult({
          outcome: 'timeout',
          endReason: 'timeout',
          kills: 4,
          playerHpRemaining: 7,
        }),
      ),
    ).toEqual({
      formulaVersion: REWARD_FORMULA_VERSION,
      outcome: 'timeout',
      base: 30,
      killBonus: 20,
      hpBonus: 0,
      delta: 50,
    })
  })

  it('GIVEN an oversized reward WHEN calculate is called THEN it clamps to the per-sortie cap', () => {
    expect(
      RewardSystem.calculate(
        createResult({
          kills: 200,
          playerHpRemaining: 999,
        }),
      ).delta,
    ).toBe(PER_SORTIE_REWARD_CAP)
  })

  it('GIVEN poisoned numeric fields WHEN calculate is called THEN invalid values fall back to zero', () => {
    const result = {
      outcome: 'victory',
      endReason: 'all_enemies_defeated',
      durationMs: 30_000,
      kills: Number.NaN,
      shotsFired: 0,
      playerHpRemaining: Number.POSITIVE_INFINITY,
    } as unknown as SortieResult

    expect(RewardSystem.calculate(result)).toEqual({
      formulaVersion: REWARD_FORMULA_VERSION,
      outcome: 'victory',
      base: 100,
      killBonus: 0,
      hpBonus: 0,
      delta: 100,
    })
  })

  it.each([
    {
      name: 'fractional kills are invalid',
      result: createResult({ kills: 2.9 as unknown as number }),
      expected: { killBonus: 0, hpBonus: 5, delta: 105 },
    },
    {
      name: 'fractional hp is invalid',
      result: createResult({ playerHpRemaining: 10.5 as unknown as number }),
      expected: { killBonus: 15, hpBonus: 0, delta: 115 },
    },
    {
      name: 'unsafe integer kills are invalid',
      result: createResult({ kills: (Number.MAX_SAFE_INTEGER + 1) as unknown as number }),
      expected: { killBonus: 0, hpBonus: 5, delta: 105 },
    },
    {
      name: 'negative infinity kills are invalid',
      result: createResult({ kills: Number.NEGATIVE_INFINITY }),
      expected: { killBonus: 0, hpBonus: 5, delta: 105 },
    },
  ])('GIVEN $name WHEN calculate is called THEN it falls back to zero for that operand', ({ result, expected }) => {
    expect(RewardSystem.calculate(result)).toMatchObject({
      formulaVersion: REWARD_FORMULA_VERSION,
      outcome: 'victory',
      base: 100,
      ...expected,
    })
  })

  it('GIVEN the same sortie result WHEN calculate is called THEN it stays pure', () => {
    const result = createResult()
    const snapshot = JSON.parse(JSON.stringify(result))

    RewardSystem.calculate(result)

    expect(result).toEqual(snapshot)
  })
})

describe('RewardSystem.claim', () => {
  it('GIVEN a fresh applicationId WHEN claim is called THEN it applies resources exactly once', () => {
    const state = createInitialGameState({ resources: 10 })

    const claim = RewardSystem.claim(state, 'sortie-1', createResult())

    expect(claim).toEqual({
      ok: true,
      quote: {
        formulaVersion: REWARD_FORMULA_VERSION,
        outcome: 'victory',
        base: 100,
        killBonus: 15,
        hpBonus: 5,
        delta: 120,
      },
      resourcesBefore: 10,
      resourcesAfter: 130,
    })
    expect(state.progress.resources).toBe(130)
    expect(state.rewardClaims.claimedApplicationIds['sortie-1']).toBe(true)
  })

  it('GIVEN the same applicationId twice WHEN claim is repeated THEN the second call is rejected', () => {
    const state = createInitialGameState({ resources: 0 })

    expect(RewardSystem.claim(state, 'sortie-1', createResult()).ok).toBe(true)
    expect(RewardSystem.claim(state, 'sortie-1', createResult())).toEqual({
      ok: false,
      reason: 'already-claimed',
    })
    expect(state.progress.resources).toBe(120)
  })

  it('GIVEN the same payload with different applicationIds WHEN claim is called THEN both rewards are applied', () => {
    const state = createInitialGameState({ resources: 0 })
    const result = createResult()

    RewardSystem.claim(state, 'sortie-1', result)
    RewardSystem.claim(state, 'sortie-2', result)

    expect(state.progress.resources).toBe(240)
  })

  it('GIVEN invalid resourcesBefore WHEN claim is called THEN it falls back to zero and clamps to RESOURCE_CAP', () => {
    const state = createInitialGameState({ resources: 1 })
    state.progress.resources = Number.NaN

    const invalidClaim = RewardSystem.claim(
      state,
      'sortie-invalid',
      createResult({ kills: 0, playerHpRemaining: 0 }),
    )

    expect(invalidClaim).toEqual({
      ok: true,
      quote: {
        formulaVersion: REWARD_FORMULA_VERSION,
        outcome: 'victory',
        base: 100,
        killBonus: 0,
        hpBonus: 0,
        delta: 100,
      },
      resourcesBefore: 0,
      resourcesAfter: 100,
    })

    const nearCapState = createInitialGameState({ resources: RESOURCE_CAP - 5 })
    const cappedClaim = RewardSystem.claim(
      nearCapState,
      'sortie-cap',
      createResult({ kills: 200, playerHpRemaining: 999 }),
    )

    expect(cappedClaim).toMatchObject({
      ok: true,
      resourcesBefore: RESOURCE_CAP - 5,
      resourcesAfter: RESOURCE_CAP,
    })
    expect(nearCapState.progress.resources).toBe(RESOURCE_CAP)
  })

  it('GIVEN fractional resourcesBefore WHEN claim is called THEN it falls back to zero before applying reward', () => {
    const state = createInitialGameState({ resources: 0 })
    state.progress.resources = 2.5

    expect(RewardSystem.claim(state, 'fractional-resources', createResult({ kills: 0, playerHpRemaining: 0 }))).toMatchObject({
      ok: true,
      resourcesBefore: 0,
      resourcesAfter: 100,
    })
  })

  it('GIVEN a non-terminal outcome pair WHEN claim is called THEN it rejects the result', () => {
    const state = createInitialGameState()
    const invalidResult = {
      outcome: 'victory',
      endReason: 'timeout',
      durationMs: 1,
      kills: 0,
      shotsFired: 0,
      playerHpRemaining: 1,
    } as unknown as SortieResult

    expect(RewardSystem.claim(state, 'broken', invalidResult)).toEqual({
      ok: false,
      reason: 'not-terminal',
    })
  })

  it('GIVEN coercive terminal-looking objects WHEN claim is called THEN it rejects them without mutating state', () => {
    const state = createInitialGameState({ resources: 7 })
    const forged = {
      outcome: { toString: () => 'victory' },
      endReason: { toString: () => 'all_enemies_defeated' },
      durationMs: 1,
      kills: 0,
      shotsFired: 0,
      playerHpRemaining: 1,
    } as unknown as SortieResult

    expect(RewardSystem.claim(state, 'coercive-terminal', forged)).toEqual({
      ok: false,
      reason: 'not-terminal',
    })
    expect(state.progress.resources).toBe(7)
    expect(state.rewardClaims.claimedApplicationIds['coercive-terminal']).toBeUndefined()
  })

  it.each(['toString', 'constructor', '__proto__'])(
    'GIVEN a prototype-looking application id %s WHEN claim is called THEN it is treated as a normal id',
    (applicationId) => {
      const state = createInitialGameState({ resources: 0 })
      const result = createResult({ kills: 0, playerHpRemaining: 0 })

      expect(RewardSystem.claim(state, applicationId, result)).toMatchObject({
        ok: true,
        resourcesAfter: 100,
      })
      expect(RewardSystem.claim(state, applicationId, result)).toEqual({
        ok: false,
        reason: 'already-claimed',
      })
    },
  )

  it('GIVEN a legacy state without rewardClaims WHEN claim is called THEN it initializes the runtime ledger defensively', () => {
    const state = createInitialGameState({ resources: 0 }) as GameState & {
      rewardClaims?: GameState['rewardClaims']
    }
    delete state.rewardClaims

    expect(RewardSystem.claim(state as GameState, 'legacy-state', createResult({ kills: 0, playerHpRemaining: 0 }))).toMatchObject({
      ok: true,
      resourcesAfter: 100,
    })
    expect(state.rewardClaims?.claimedApplicationIds['legacy-state']).toBe(true)
  })
})

describe('RewardSystem module boundary', () => {
  it('GIVEN the RewardSystem source WHEN inspected THEN it does not import DOM, Canvas, or localStorage APIs', () => {
    const dirnameValue = dirname(fileURLToPath(import.meta.url))
    const source = readFileSync(resolve(dirnameValue, '../src/systems/RewardSystem.ts'), 'utf8')

    expect(source).not.toMatch(/localStorage|document|window|Canvas/i)
  })
})

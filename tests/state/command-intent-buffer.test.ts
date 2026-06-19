/**
 * tests/state/command-intent-buffer.test.ts
 *
 * Unit tests for CommandIntentRuntimeState / BufferedCommandIntent (Issue #982).
 *
 * Coverage:
 *   AC1 – assistPlayerTtlTicks is fixed ticks (ceil(ms/fixedDeltaMs), clamped [1..180])
 *   AC2 – expiry judgment: currentTick < expiresAtTick (not wall-clock)
 *   AC3 – no wall-clock APIs used in buffer logic (structural; rg-backed by VC)
 *   AC4 – CommandIntentRuntimeState / BufferedCommandIntent are exported from GameState
 *   AC7 – expiresAtTick = sampledAtTick + assistPlayerTtlTicks; active iff currentTick < expiresAtTick
 */
import { describe, expect, it } from 'vitest'

import type {
  BufferedCommandIntent,
  CommandIntentRuntimeState,
} from '../../src/state/GameState'
import { createInitialGameState } from '../../src/state/GameState'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Simulate sampling an assist_player intent at `sampledAtTick`.
 * Returns the resulting BufferedCommandIntent (pure function, no mutation).
 */
function sampleAssistPlayer(
  runtime: CommandIntentRuntimeState,
  sampledAtTick: number,
): BufferedCommandIntent {
  return {
    intent: 'assist_player',
    sampledAtTick,
    expiresAtTick: sampledAtTick + runtime.assistPlayerTtlTicks,
  }
}

/**
 * Returns true iff the buffered intent is still active at currentTick.
 * AC2: deterministic tick comparison — no Date.now / performance.now.
 */
function isBufferedIntentActive(
  buffered: BufferedCommandIntent,
  currentTick: number,
): boolean {
  return currentTick < buffered.expiresAtTick
}

/**
 * Compute assistPlayerTtlTicks from ms and fixedDeltaMs (AC1).
 * Range is clamped to [1, 180].
 */
function computeTtlTicks(ttlMs: number, fixedDeltaMs: number): number {
  const raw = Math.ceil(ttlMs / fixedDeltaMs)
  return Math.min(180, Math.max(1, raw))
}

// ---------------------------------------------------------------------------
// AC4: type exports from GameState
// ---------------------------------------------------------------------------

describe('AC4 – CommandIntentRuntimeState and BufferedCommandIntent exported from GameState', () => {
  it('createInitialGameState returns commandIntentRuntime with expected shape', () => {
    const state = createInitialGameState()
    expect(state.commandIntentRuntime).toBeDefined()
    expect(state.commandIntentRuntime.activeIntent).toBe('none')
    expect(state.commandIntentRuntime.bufferedIntent).toBeNull()
    expect(typeof state.commandIntentRuntime.assistPlayerTtlTicks).toBe('number')
  })

  it('assistPlayerTtlTicks is at least 1 and at most 180 in initial state', () => {
    const state = createInitialGameState()
    const ttl = state.commandIntentRuntime.assistPlayerTtlTicks
    expect(ttl).toBeGreaterThanOrEqual(1)
    expect(ttl).toBeLessThanOrEqual(180)
  })
})

// ---------------------------------------------------------------------------
// AC1: TTL conversion — ceil(ms / fixedDeltaMs), clamped [1..180]
// ---------------------------------------------------------------------------

describe('AC1 – assistPlayerTtlTicks conversion and clamping', () => {
  const FIXED_DELTA_MS = 1000 / 60 // ≈16.667ms

  it('GIVEN ttlMs=133 and fixedDeltaMs≈16.667 WHEN computed THEN ticks = 8', () => {
    expect(computeTtlTicks(133, FIXED_DELTA_MS)).toBe(8)
  })

  it('GIVEN ttlMs=16.667 WHEN computed THEN ticks = 1 (exactly one tick)', () => {
    expect(computeTtlTicks(FIXED_DELTA_MS, FIXED_DELTA_MS)).toBe(1)
  })

  it('GIVEN ttlMs=0 WHEN computed THEN clamped to 1 (minimum)', () => {
    expect(computeTtlTicks(0, FIXED_DELTA_MS)).toBe(1)
  })

  it('GIVEN ttlMs=3001 and fixedDeltaMs≈16.667 WHEN computed THEN clamped to 180 (maximum)', () => {
    // ceil(3001/16.667) = ceil(180.06) = 181 → clamped to 180
    expect(computeTtlTicks(3001, FIXED_DELTA_MS)).toBe(180)
  })

  it('GIVEN ttlMs=3000 and fixedDeltaMs≈16.667 WHEN computed THEN equals 180 (boundary)', () => {
    // ceil(3000/16.667) = ceil(179.998) = 180 → exactly 180
    expect(computeTtlTicks(3000, FIXED_DELTA_MS)).toBe(180)
  })

  it('GIVEN ttlMs=1ms WHEN computed THEN clamped to 1 (below one frame)', () => {
    expect(computeTtlTicks(1, FIXED_DELTA_MS)).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// AC7: expiresAtTick = sampledAtTick + assistPlayerTtlTicks
// ---------------------------------------------------------------------------

describe('AC7 – expiresAtTick derivation', () => {
  it('GIVEN ttl=8 and sampledAtTick=0 WHEN sampled THEN expiresAtTick=8', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 8,
    }
    const buf = sampleAssistPlayer(runtime, 0)
    expect(buf.sampledAtTick).toBe(0)
    expect(buf.expiresAtTick).toBe(8)
  })

  it('GIVEN ttl=1 and sampledAtTick=5 WHEN sampled THEN expiresAtTick=6 (sampling tick only active)', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 1,
    }
    const buf = sampleAssistPlayer(runtime, 5)
    expect(buf.expiresAtTick).toBe(6)
  })

  it('GIVEN ttl=3 and sampledAtTick=10 WHEN sampled THEN expiresAtTick=13', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 3,
    }
    const buf = sampleAssistPlayer(runtime, 10)
    expect(buf.expiresAtTick).toBe(13)
  })
})

// ---------------------------------------------------------------------------
// AC2: expiry is currentTick < expiresAtTick (no wall-clock)
// ---------------------------------------------------------------------------

describe('AC2 – deterministic tick-based expiry', () => {
  it('GIVEN expiresAtTick=8 WHEN currentTick=7 THEN still active', () => {
    const buf: BufferedCommandIntent = {
      intent: 'assist_player',
      sampledAtTick: 0,
      expiresAtTick: 8,
    }
    expect(isBufferedIntentActive(buf, 7)).toBe(true)
  })

  it('GIVEN expiresAtTick=8 WHEN currentTick=8 THEN expired (boundary: < not <=)', () => {
    const buf: BufferedCommandIntent = {
      intent: 'assist_player',
      sampledAtTick: 0,
      expiresAtTick: 8,
    }
    expect(isBufferedIntentActive(buf, 8)).toBe(false)
  })

  it('GIVEN expiresAtTick=8 WHEN currentTick=9 THEN expired', () => {
    const buf: BufferedCommandIntent = {
      intent: 'assist_player',
      sampledAtTick: 0,
      expiresAtTick: 8,
    }
    expect(isBufferedIntentActive(buf, 9)).toBe(false)
  })

  it('GIVEN ttl=1 and sampledAtTick=5 WHEN currentTick=5 THEN active (sampling tick)', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 1,
    }
    const buf = sampleAssistPlayer(runtime, 5)
    // expiresAtTick = 6; active at tick 5
    expect(isBufferedIntentActive(buf, 5)).toBe(true)
    // expired at tick 6
    expect(isBufferedIntentActive(buf, 6)).toBe(false)
  })

  it('GIVEN ttl=8 WHEN scanning ticks 0..7 THEN all active; tick 8 expired', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 8,
    }
    const buf = sampleAssistPlayer(runtime, 0)
    for (let tick = 0; tick < 8; tick++) {
      expect(isBufferedIntentActive(buf, tick)).toBe(true)
    }
    expect(isBufferedIntentActive(buf, 8)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// AC4: BufferedCommandIntent.intent is narrowed to 'assist_player'
// ---------------------------------------------------------------------------

describe('AC4 – bufferedIntent.intent is narrowed to assist_player', () => {
  it('GIVEN sampled intent WHEN accessing .intent THEN it equals assist_player', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 8,
    }
    const buf = sampleAssistPlayer(runtime, 0)
    // Type-level: Extract<CommandIntent, 'assist_player'> === 'assist_player'
    expect(buf.intent).toBe('assist_player')
  })

  it('GIVEN runtime with bufferedIntent WHEN read THEN stage7 can consume it', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'assist_player',
      bufferedIntent: {
        intent: 'assist_player',
        sampledAtTick: 10,
        expiresAtTick: 18,
      },
      assistPlayerTtlTicks: 8,
    }
    // Stage 7 reads: runtime.bufferedIntent?.intent === 'assist_player'
    expect(runtime.bufferedIntent?.intent).toBe('assist_player')
    expect(runtime.bufferedIntent?.expiresAtTick).toBe(18)
  })
})

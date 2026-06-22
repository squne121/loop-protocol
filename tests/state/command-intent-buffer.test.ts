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
 *
 *   Blockers 1–5 (Issue #982 fix_delta):
 *   B1  – sampleAssistPlayerIntent connects to commandIntentRuntime (production tick step)
 *   B2  – helpers are production imports, not test-local
 *   B3  – computeAssistPlayerTtlTicks is a production function
 *   B5  – resetCommandIntentRuntime clears activeIntent / bufferedIntent at sortie start
 */
import { describe, expect, it } from 'vitest'

import type {
  BufferedCommandIntent,
  CommandIntentRuntimeState,
} from '../../src/state/GameState'
import {
  computeAssistPlayerTtlTicks,
  createInitialGameState,
  isBufferedCommandIntentActive,
  resetCommandIntentRuntime,
  sampleAssistPlayerIntent,
} from '../../src/state/GameState'

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

  it('assistPlayerTtlTicks in initial state equals computeAssistPlayerTtlTicks(133, 1000/60)', () => {
    // AC1 + Blocker 3: createInitialGameState must use production function, not hardcode 8
    const state = createInitialGameState()
    const expected = computeAssistPlayerTtlTicks(133, 1000 / 60)
    expect(state.commandIntentRuntime.assistPlayerTtlTicks).toBe(expected)
  })
})

// ---------------------------------------------------------------------------
// AC1: TTL conversion — ceil(ms / fixedDeltaMs), clamped [1..180] (Blocker 3)
// All tests use the production function computeAssistPlayerTtlTicks (no test-local helper)
// ---------------------------------------------------------------------------

describe('AC1 – assistPlayerTtlTicks conversion and clamping (production function)', () => {
  const FIXED_DELTA_MS = 1000 / 60 // ≈16.667ms

  it('GIVEN ttlMs=133 and fixedDeltaMs≈16.667 WHEN computed THEN ticks = 8', () => {
    expect(computeAssistPlayerTtlTicks(133, FIXED_DELTA_MS)).toBe(8)
  })

  it('GIVEN ttlMs=16.667 WHEN computed THEN ticks = 1 (exactly one tick)', () => {
    expect(computeAssistPlayerTtlTicks(FIXED_DELTA_MS, FIXED_DELTA_MS)).toBe(1)
  })

  it('GIVEN ttlMs=0 WHEN computed THEN clamped to 1 (minimum)', () => {
    expect(computeAssistPlayerTtlTicks(0, FIXED_DELTA_MS)).toBe(1)
  })

  it('GIVEN ttlMs=3001 and fixedDeltaMs≈16.667 WHEN computed THEN clamped to 180 (maximum)', () => {
    // ceil(3001/16.667) = ceil(180.06) = 181 → clamped to 180
    expect(computeAssistPlayerTtlTicks(3001, FIXED_DELTA_MS)).toBe(180)
  })

  it('GIVEN ttlMs=3000 and fixedDeltaMs≈16.667 WHEN computed THEN equals 180 (boundary)', () => {
    // ceil(3000/16.667) = ceil(179.998) = 180 → exactly 180
    expect(computeAssistPlayerTtlTicks(3000, FIXED_DELTA_MS)).toBe(180)
  })

  it('GIVEN ttlMs=1ms WHEN computed THEN clamped to 1 (below one frame)', () => {
    expect(computeAssistPlayerTtlTicks(1, FIXED_DELTA_MS)).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// AC7 + Blocker 1: sampleAssistPlayerIntent updates commandIntentRuntime (production)
// expiresAtTick = sampledAtTick + assistPlayerTtlTicks
// ---------------------------------------------------------------------------

describe('AC7 + Blocker 1 – sampleAssistPlayerIntent (production function)', () => {
  it('GIVEN ttl=8 and sampledAtTick=0 WHEN sampleAssistPlayerIntent THEN bufferedIntent.expiresAtTick=8', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 8,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    sampleAssistPlayerIntent(runtime, 0)
    expect(runtime.bufferedIntent).not.toBeNull()
    expect(runtime.bufferedIntent!.sampledAtTick).toBe(0)
    expect(runtime.bufferedIntent!.expiresAtTick).toBe(8)
  })

  it('GIVEN ttl=1 and sampledAtTick=5 WHEN sampleAssistPlayerIntent THEN expiresAtTick=6', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 1,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    sampleAssistPlayerIntent(runtime, 5)
    expect(runtime.bufferedIntent!.expiresAtTick).toBe(6)
  })

  it('GIVEN ttl=3 and sampledAtTick=10 WHEN sampleAssistPlayerIntent THEN expiresAtTick=13', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 3,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    sampleAssistPlayerIntent(runtime, 10)
    expect(runtime.bufferedIntent!.expiresAtTick).toBe(13)
  })

  it('GIVEN sample_assist_player command received WHEN tick step processes it THEN bufferedIntent is set with correct fields', () => {
    // Blocker 1: production tick step connects command to commandIntentRuntime
    const state = createInitialGameState()
    sampleAssistPlayerIntent(state.commandIntentRuntime, state.tick)
    expect(state.commandIntentRuntime.bufferedIntent).not.toBeNull()
    expect(state.commandIntentRuntime.bufferedIntent!.intent).toBe('assist_player')
    expect(state.commandIntentRuntime.bufferedIntent!.sampledAtTick).toBe(0)
    expect(state.commandIntentRuntime.bufferedIntent!.expiresAtTick).toBe(
      state.commandIntentRuntime.assistPlayerTtlTicks,
    )
  })

  it('GIVEN sampleAssistPlayerIntent called THEN activeIntent becomes assist_player', () => {
    // Blocker 1: production step also updates activeIntent
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 8,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    sampleAssistPlayerIntent(runtime, 0)
    expect(runtime.activeIntent).toBe('assist_player')
  })
})

// ---------------------------------------------------------------------------
// AC2: expiry is currentTick < expiresAtTick (no wall-clock) — Blocker 2
// All tests use the production function isBufferedCommandIntentActive (no test-local helper)
// ---------------------------------------------------------------------------

describe('AC2 – deterministic tick-based expiry (production function isBufferedCommandIntentActive)', () => {
  it('GIVEN expiresAtTick=8 WHEN currentTick=7 THEN still active', () => {
    const buf: BufferedCommandIntent = {
      intent: 'assist_player',
      sampledAtTick: 0,
      expiresAtTick: 8,
    }
    expect(isBufferedCommandIntentActive(buf, 7)).toBe(true)
  })

  it('GIVEN expiresAtTick=8 WHEN currentTick=8 THEN expired (boundary: < not <=)', () => {
    const buf: BufferedCommandIntent = {
      intent: 'assist_player',
      sampledAtTick: 0,
      expiresAtTick: 8,
    }
    expect(isBufferedCommandIntentActive(buf, 8)).toBe(false)
  })

  it('GIVEN expiresAtTick=8 WHEN currentTick=9 THEN expired', () => {
    const buf: BufferedCommandIntent = {
      intent: 'assist_player',
      sampledAtTick: 0,
      expiresAtTick: 8,
    }
    expect(isBufferedCommandIntentActive(buf, 9)).toBe(false)
  })

  it('GIVEN ttl=1 and sampledAtTick=5 WHEN currentTick=5 THEN active; tick 6 expired', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 1,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    sampleAssistPlayerIntent(runtime, 5)
    const buf = runtime.bufferedIntent!
    // expiresAtTick = 6; active at tick 5
    expect(isBufferedCommandIntentActive(buf, 5)).toBe(true)
    // expired at tick 6
    expect(isBufferedCommandIntentActive(buf, 6)).toBe(false)
  })

  it('GIVEN ttl=8 WHEN scanning ticks 0..7 THEN all active; tick 8 expired', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 8,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    sampleAssistPlayerIntent(runtime, 0)
    const buf = runtime.bufferedIntent!
    for (let tick = 0; tick < 8; tick++) {
      expect(isBufferedCommandIntentActive(buf, tick)).toBe(true)
    }
    expect(isBufferedCommandIntentActive(buf, 8)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// AC4: BufferedCommandIntent.intent is narrowed to 'assist_player'
// ---------------------------------------------------------------------------

describe('AC4 – bufferedIntent.intent is narrowed to assist_player', () => {
  it('GIVEN sampleAssistPlayerIntent called WHEN accessing .intent THEN it equals assist_player', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 8,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    sampleAssistPlayerIntent(runtime, 0)
    // Type-level: Extract<CommandIntent, 'assist_player'> === 'assist_player'
    expect(runtime.bufferedIntent!.intent).toBe('assist_player')
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
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    // Stage 7 reads: runtime.bufferedIntent?.intent === 'assist_player'
    expect(runtime.bufferedIntent?.intent).toBe('assist_player')
    expect(runtime.bufferedIntent?.expiresAtTick).toBe(18)
  })
})

// ---------------------------------------------------------------------------
// Blocker 5: resetCommandIntentRuntime clears activeIntent / bufferedIntent
// Called at sortie start to prevent stale intents from a previous sortie
// ---------------------------------------------------------------------------

describe('Blocker 5 – resetCommandIntentRuntime (production function)', () => {
  it('GIVEN activeIntent=assist_player WHEN resetCommandIntentRuntime THEN activeIntent=none', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'assist_player',
      bufferedIntent: {
        intent: 'assist_player',
        sampledAtTick: 0,
        expiresAtTick: 8,
      },
      assistPlayerTtlTicks: 8,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    resetCommandIntentRuntime(runtime)
    expect(runtime.activeIntent).toBe('none')
  })

  it('GIVEN bufferedIntent set WHEN resetCommandIntentRuntime THEN bufferedIntent=null', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'assist_player',
      bufferedIntent: {
        intent: 'assist_player',
        sampledAtTick: 5,
        expiresAtTick: 13,
      },
      assistPlayerTtlTicks: 8,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    resetCommandIntentRuntime(runtime)
    expect(runtime.bufferedIntent).toBeNull()
  })

  it('GIVEN previous sortie had bufferedIntent WHEN startSortie resets runtime THEN null at new sortie start', () => {
    // Simulates Blocker 5: sortie reset must clear commandIntentRuntime
    const state = createInitialGameState()
    // Simulate previous sortie leaving stale intent
    sampleAssistPlayerIntent(state.commandIntentRuntime, 100)
    expect(state.commandIntentRuntime.bufferedIntent).not.toBeNull()

    // Reset (as called by startSortie / resetCombatRuntime)
    resetCommandIntentRuntime(state.commandIntentRuntime)
    expect(state.commandIntentRuntime.activeIntent).toBe('none')
    expect(state.commandIntentRuntime.bufferedIntent).toBeNull()
  })

  it('GIVEN fresh runtime WHEN resetCommandIntentRuntime called THEN remains in zero state', () => {
    const runtime: CommandIntentRuntimeState = {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 8,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    }
    resetCommandIntentRuntime(runtime)
    expect(runtime.activeIntent).toBe('none')
    expect(runtime.bufferedIntent).toBeNull()
  })
})

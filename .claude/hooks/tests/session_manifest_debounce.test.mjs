/**
 * session_manifest_debounce.test.mjs
 *
 * Node.js unit tests for the flushLoop DI core (`runFlushLoopCore`) exported
 * from session_manifest_debounce.mjs.
 *
 * Uses node:test + node:assert/strict (no external dependencies).
 * Requires Node 18+ (node:test); pinned to Node 22 in CI (AC6).
 *
 * AC4 coverage:
 *   - Events within < WINDOW_MS of the last event do not trigger a flush
 *     (quiet-window check: sleeps instead of flushing).
 *   - Exactly one flush occurs after >= WINDOW_MS of quiet time.
 *   - A second event at WINDOW_MS - 1 extends the deadline.
 *   - A gap of >= WINDOW_MS between two bursts produces two separate flush
 *     calls (two batches).
 *   - Forced flush (force=true) calls producer immediately, skipping window.
 *   - Empty event list produces zero producer calls.
 */

import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { existsSync } from 'node:fs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const MJS_PATH = resolve(__dirname, '..', 'session_manifest_debounce.mjs')

// Guard: the production module must exist.
if (!existsSync(MJS_PATH)) {
  throw new Error(`session_manifest_debounce.mjs not found at: ${MJS_PATH}`)
}

// Import the DI core exported by the production module.
const mod = await import(MJS_PATH)

const runFlushLoopCore = mod.runFlushLoopCore
if (typeof runFlushLoopCore !== 'function') {
  throw new Error(
    'session_manifest_debounce.mjs does not export `runFlushLoopCore`.\n' +
      'DI extraction is required by Issue #1141 AC4.',
  )
}

// ---------------------------------------------------------------------------
// Test harness factory
// ---------------------------------------------------------------------------

/**
 * Build a minimal DI harness for runFlushLoopCore.
 *
 * @param {object} opts
 * @param {number} opts.windowMs       - Debounce window in ms.
 * @param {number[]} opts.eventTimestamps - Timestamps (ms) of initially pending events.
 * @param {number} opts.startNowMs     - Initial virtual clock value.
 */
function makeHarness({ windowMs = 400, eventTimestamps = [], startNowMs = 0 } = {}) {
  let currentNowMs = startNowMs
  // Mutable event list: {timestamp, index} per event.
  const events = eventTimestamps.map((ts, i) => ({ timestamp: ts, index: i }))
  const producerCalls = []
  const sleepLog = []

  const deps = {
    now: () => currentNowMs,

    // Virtual sleep: records the duration and advances the clock.
    sleep: (ms) => {
      sleepLog.push(ms)
      currentNowMs += ms
      return Promise.resolve()
    },

    // List events as {timestamp} objects (for quiet-window check).
    listEvents: () => [...events],

    // Read all events and return payloads (for aggregation).
    readEvents: () =>
      events.map((e) => ({
        session_manifest_delta: [
          { mutation_type: 'write', relative_paths: [`docs/dev/file-${e.index}.md`] },
        ],
      })),

    // Run producer: record the call (does NOT clear events; removeEvents does).
    runProducer: (payload) => {
      producerCalls.push({ ...payload })
    },

    // Remove all events: called by runFlushLoopCore after producer call.
    removeEvents: () => {
      events.splice(0, events.length)
    },

    windowMs,
  }

  // Helper to advance the clock without sleeping.
  const advanceClock = (ms) => {
    currentNowMs += ms
  }

  // Helper to add new events (simulating arriving events in gap tests).
  const addEvents = (timestamps) => {
    const startIndex = events.length
    timestamps.forEach((ts, i) => events.push({ timestamp: ts, index: startIndex + i }))
  }

  return { deps, producerCalls, sleepLog, events, advanceClock, addEvents }
}

// ---------------------------------------------------------------------------
// Tests: quiet-window boundary (AC4)
// ---------------------------------------------------------------------------

describe('flushLoop DI core — quiet-window boundary (AC4)', () => {
  it('sleeps instead of flushing when newest event is < WINDOW_MS old', async () => {
    // Newest event at t=350; now=400; WINDOW_MS=400.
    // quietForMs = 400 - 350 = 50 < 400 => sleep(350), then on next iteration
    // events will be cleared (simulating external clearing).
    const { deps, producerCalls, sleepLog, events } = makeHarness({
      windowMs: 400,
      eventTimestamps: [0, 100, 200, 350],
      startNowMs: 400,
    })

    // After the first sleep, clear events so the loop terminates cleanly.
    const originalSleep = deps.sleep
    deps.sleep = async (ms) => {
      await originalSleep(ms)
      // Simulate events being consumed externally (or window expired and we
      // want the loop to exit after one flush attempt).
      // Since now is 400+350=750 and newest=350, quietForMs=400 >= WINDOW_MS,
      // the loop will flush on next iteration. Let it do so by not clearing.
    }

    await runFlushLoopCore(deps, { force: false, maxIterations: 10 })

    // The loop must have slept at least once before (or instead of) flushing.
    assert.ok(sleepLog.length >= 1, `expected at least one sleep, got ${sleepLog.length}`)
    // First sleep should be WINDOW_MS - quietForMs = 400 - 50 = 350.
    assert.strictEqual(sleepLog[0], 350, `expected first sleep = 350ms, got ${sleepLog[0]}ms`)
  })

  it('flushes exactly once after >= WINDOW_MS quiet time, 10 events', async () => {
    // All 10 events at t=0; now=500ms; WINDOW_MS=400ms.
    // quietForMs = 500 >= 400 => flush immediately.
    const { deps, producerCalls } = makeHarness({
      windowMs: 400,
      eventTimestamps: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
      startNowMs: 500,
    })

    await runFlushLoopCore(deps, { force: false, maxIterations: 20 })

    assert.strictEqual(producerCalls.length, 1, `expected 1 producer call, got ${producerCalls.length}`)
    assert.strictEqual(
      producerCalls[0].debounce_event_count,
      10,
      `expected debounce_event_count=10, got ${producerCalls[0].debounce_event_count}`,
    )
    // All 10 distinct file paths must appear in the aggregated delta.
    const allPaths = producerCalls[0].session_manifest_delta.flatMap((d) => d.relative_paths)
    for (let i = 0; i < 10; i++) {
      assert.ok(allPaths.includes(`docs/dev/file-${i}.md`), `missing path: docs/dev/file-${i}.md`)
    }
  })

  it('extends deadline when event arrives at WINDOW_MS - 1 (< WINDOW_MS gap)', async () => {
    // Events: t=0 and t=WINDOW_MS-1=399.
    // At now=400: newest=399, quietForMs=1 < 400 => sleep(399).
    // After sleep(399): now=799, quietForMs=799-399=400 >= WINDOW_MS => flush.
    const WINDOW_MS = 400
    const { deps, producerCalls, sleepLog } = makeHarness({
      windowMs: WINDOW_MS,
      eventTimestamps: [0, WINDOW_MS - 1],
      startNowMs: WINDOW_MS, // now=400
    })

    await runFlushLoopCore(deps, { force: false, maxIterations: 20 })

    // Deadline was extended: first sleep = WINDOW_MS - quietForMs = 400 - 1 = 399.
    assert.ok(sleepLog.length >= 1, 'expected at least one sleep (deadline extension)')
    assert.strictEqual(sleepLog[0], 399, `expected sleep(399) for deadline extension, got sleep(${sleepLog[0]})`)
    // After sleep, exactly one flush.
    assert.strictEqual(producerCalls.length, 1, `expected 1 producer call after deadline extension, got ${producerCalls.length}`)
  })

  it('produces two separate flush calls when gap between bursts >= WINDOW_MS', async () => {
    // Burst 1: event at t=0; now=500 (quiet=500 >= WINDOW_MS=400) -> flush.
    // After flush + removeEvents, inject burst 2 at t=1000 and advance clock
    // to now=1500 (quiet=1500-1000=500 >= 400) -> second flush.
    const WINDOW_MS = 400
    const { deps, producerCalls, addEvents } = makeHarness({
      windowMs: WINDOW_MS,
      eventTimestamps: [0],
      startNowMs: 500,
    })

    // Override removeEvents to inject burst 2 after the first clear.
    const originalRemoveEvents = deps.removeEvents
    let removeCount = 0
    deps.removeEvents = () => {
      originalRemoveEvents()
      removeCount++
      if (removeCount === 1) {
        // Burst 1 has been removed; inject burst 2 at t=1000, clock=1500.
        addEvents([1000])
        let nowVal = 1500
        deps.now = () => nowVal
      }
      // After remove 2, events are empty and loop exits.
    }

    await runFlushLoopCore(deps, { force: false, maxIterations: 30 })

    assert.strictEqual(
      producerCalls.length,
      2,
      `expected 2 producer calls (2 bursts), got ${producerCalls.length}`,
    )
    // First call: 1 event (burst 1).
    assert.strictEqual(producerCalls[0].debounce_event_count, 1, 'burst 1 should have 1 event')
    // Second call: 1 event (burst 2).
    assert.strictEqual(producerCalls[1].debounce_event_count, 1, 'burst 2 should have 1 event')
  })

  it('forced flush calls producer immediately, skipping quiet-window check', async () => {
    // Events are very recent (now=10ms, WINDOW_MS=400ms).
    // With force=true, producer must be called without sleeping.
    const { deps, producerCalls, sleepLog } = makeHarness({
      windowMs: 400,
      eventTimestamps: [5, 6, 7],
      startNowMs: 10,
    })

    await runFlushLoopCore(deps, { force: true, maxIterations: 20 })

    assert.strictEqual(
      producerCalls.length,
      1,
      `forced flush should call producer once, got ${producerCalls.length}`,
    )
    assert.strictEqual(
      producerCalls[0].debounce_event_count,
      3,
      `expected debounce_event_count=3, got ${producerCalls[0].debounce_event_count}`,
    )
    assert.strictEqual(sleepLog.length, 0, 'forced flush must not sleep')
    assert.strictEqual(producerCalls[0].debounce_flush_reason, 'forced_flush')
  })

  it('exits immediately with no producer call when event list is empty', async () => {
    const { deps, producerCalls } = makeHarness({
      windowMs: 400,
      eventTimestamps: [],
      startNowMs: 1000,
    })

    await runFlushLoopCore(deps, { force: false, maxIterations: 5 })

    assert.strictEqual(producerCalls.length, 0, 'empty event list must not call producer')
  })
})

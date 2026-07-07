#!/usr/bin/env node
/**
 * test-codex-single-composite.mjs
 *
 * Tests for the Codex composite hook stdout policy (AC3, AC7).
 *
 * Verifies:
 * - Stop / SubagentStop allow path: stdout is valid JSON {"continue": true/false}
 * - Stop / SubagentStop session recording failure: stdout is {"continue": true}
 * - PreToolUse allow path: stdout is empty or valid JSON with hookSpecificOutput
 * - PreToolUse block path: stdout is the OpenAI Codex Hooks PreToolUse deny schema
 *
 * This script is a standalone manual smoke (Issue #1354). It is not spawned
 * from pnpm test / tests/hooks/hooks-stdout-policy.test.ts; the cases above
 * are covered there as direct Vitest assertions (AC2-AC8). Run this script
 * manually during PR review to smoke-test the composite hook end to end.
 *
 * These tests spawn the composite hook as a child process, feed stdin, and
 * capture stdout/stderr. They are manual-only and are not nested inside pnpm test.
 */

/* global process */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')
const compositeHook = resolve(repoRoot, '.codex', 'hooks', 'session-recording-composite.mjs')
const fixturesDir = resolve(repoRoot, 'tests', 'fixtures', 'hooks')

// spawnSync timeout for the composite hook subprocess invoked below (Issue #1354).
const COMPOSITE_HOOK_SPAWN_TIMEOUT_MS = 10000

let passed = 0
let failed = 0
const errors = []

function assert(description, condition, detail = '') {
  if (condition) {
    process.stdout.write(`  PASS: ${description}\n`)
    passed++
  } else {
    process.stdout.write(`  FAIL: ${description}${detail ? ` — ${detail}` : ''}\n`)
    failed++
    errors.push(`${description}${detail ? ` — ${detail}` : ''}`)
  }
}

/**
 * Run the composite hook with a given event and stdin payload.
 * Returns { stdout, stderr, exitCode, parsedJson }.
 */
function runHook(event, stdinContent) {
  const result = spawnSync(process.execPath, [compositeHook, '--event', event], {
    input: typeof stdinContent === 'string' ? stdinContent : JSON.stringify(stdinContent),
    encoding: 'utf8',
    timeout: COMPOSITE_HOOK_SPAWN_TIMEOUT_MS,
    env: {
      ...process.env,
      // Override producer script to a no-op for tests
      CODEX_SESSION_RECORDING_PRODUCER: resolve(repoRoot, 'tests', 'hooks', '_stub-producer.mjs'),
    },
  })
  let parsedJson = null
  if (result.stdout && result.stdout.trim()) {
    try {
      parsedJson = JSON.parse(result.stdout.trim())
    } catch {
      // not JSON
    }
  }
  return {
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    exitCode: result.status ?? -1,
    parsedJson,
  }
}

// ---------------------------------------------------------------------------
// Test suite: Stop event allow path
// ---------------------------------------------------------------------------
process.stdout.write('\n[Suite] Stop event — allow path (no guard violation)\n')
{
  const fixture = readFileSync(resolve(fixturesDir, 'codex-stop-allow.json'), 'utf8')
  const r = runHook('Stop', fixture)

  assert(
    'exit 0',
    r.exitCode === 0,
    `got exitCode=${r.exitCode}`,
  )
  assert(
    'stdout is non-empty (Stop requires JSON output)',
    r.stdout.trim().length > 0,
    `stdout was empty`,
  )
  assert(
    'stdout is valid JSON',
    r.parsedJson !== null,
    `stdout: ${r.stdout.trim().slice(0, 120)}`,
  )
  assert(
    'stdout has "continue" field',
    r.parsedJson !== null && typeof r.parsedJson.continue === 'boolean',
    `parsedJson: ${JSON.stringify(r.parsedJson)}`,
  )
}

// ---------------------------------------------------------------------------
// Test suite: SubagentStop event allow path
// ---------------------------------------------------------------------------
process.stdout.write('\n[Suite] SubagentStop event — allow path\n')
{
  const fixture = readFileSync(resolve(fixturesDir, 'codex-subagent-stop-allow.json'), 'utf8')
  const r = runHook('SubagentStop', fixture)

  assert('exit 0', r.exitCode === 0, `got exitCode=${r.exitCode}`)
  assert('stdout is valid JSON', r.parsedJson !== null, `stdout: ${r.stdout.trim().slice(0, 120)}`)
  assert(
    'stdout has "continue" field',
    r.parsedJson !== null && typeof r.parsedJson.continue === 'boolean',
    `parsedJson: ${JSON.stringify(r.parsedJson)}`,
  )
}

// ---------------------------------------------------------------------------
// Test suite: Stop event — malformed payload (session recording failure path)
// AC3: session recording failure must return {"continue": true}
// ---------------------------------------------------------------------------
process.stdout.write('\n[Suite] Stop event — malformed payload (recording failure)\n')
{
  const r = runHook('Stop', 'INVALID_JSON_NOT_VALID_PAYLOAD')

  assert('exit 0 on malformed payload', r.exitCode === 0, `got exitCode=${r.exitCode}`)
  assert(
    'stdout is valid JSON {"continue": true} on recording failure',
    r.parsedJson !== null && r.parsedJson.continue === true,
    `stdout: ${r.stdout.trim().slice(0, 120)}`,
  )
}

// ---------------------------------------------------------------------------
// Test suite: SubagentStop event — malformed payload (recording failure)
// ---------------------------------------------------------------------------
process.stdout.write('\n[Suite] SubagentStop event — malformed payload (recording failure)\n')
{
  const r = runHook('SubagentStop', 'INVALID_JSON_NOT_VALID_PAYLOAD')

  assert('exit 0 on malformed payload', r.exitCode === 0, `got exitCode=${r.exitCode}`)
  assert(
    'stdout is {"continue": true} on recording failure (AC3)',
    r.parsedJson !== null && r.parsedJson.continue === true,
    `stdout: ${r.stdout.trim().slice(0, 120)}`,
  )
}

// ---------------------------------------------------------------------------
// Test suite: PreToolUse event — allow path (no violation)
// Codex Hooks stdout policy: empty on allow path
// ---------------------------------------------------------------------------
process.stdout.write('\n[Suite] PreToolUse event — allow path\n')
{
  const safePayload = {
    hook_event_name: 'PreToolUse',
    tool_name: 'Bash',
    tool_input: { command: 'pnpm typecheck' },
    secrets_mode: 'none',
  }
  const r = runHook('PreToolUse', safePayload)

  assert('exit 0', r.exitCode === 0, `got exitCode=${r.exitCode}`)
  // PreToolUse allow path: stdout should be empty (no output = allow)
  assert(
    'stdout is empty on allow path (PreToolUse AC1 policy)',
    r.stdout.trim() === '',
    `stdout was: "${r.stdout.trim().slice(0, 120)}"`,
  )
}

// ---------------------------------------------------------------------------
// Test suite: PreToolUse event — block path (forbidden path)
// Codex Hooks stdout policy: event-specific JSON on block path
// ---------------------------------------------------------------------------
process.stdout.write('\n[Suite] PreToolUse event — block path (forbidden path)\n')
{
  const forbiddenPayload = {
    hook_event_name: 'PreToolUse',
    tool_name: 'Bash',
    tool_input: { command: 'cat assets/forbidden.png' },
    secrets_mode: 'none',
  }
  const r = runHook('PreToolUse', forbiddenPayload)

  assert('exit 0 on block path', r.exitCode === 0, `got exitCode=${r.exitCode}`)
  assert(
    'stdout is valid JSON on block path (AC2)',
    r.parsedJson !== null,
    `stdout: ${r.stdout.trim().slice(0, 120)}`,
  )
  assert(
    'stdout has hookSpecificOutput on deny (AC2)',
    r.parsedJson !== null && r.parsedJson.hookSpecificOutput != null,
    `parsedJson: ${JSON.stringify(r.parsedJson)}`,
  )
  assert(
    'deny decision is "deny"',
    r.parsedJson?.hookSpecificOutput?.permissionDecision === 'deny',
    `parsedJson: ${JSON.stringify(r.parsedJson)}`,
  )
  assert(
    'hookEventName is PreToolUse',
    r.parsedJson?.hookSpecificOutput?.hookEventName === 'PreToolUse',
    `parsedJson: ${JSON.stringify(r.parsedJson)}`,
  )
  assert(
    'permissionDecisionReason is string',
    typeof r.parsedJson?.hookSpecificOutput?.permissionDecisionReason === 'string',
    `parsedJson: ${JSON.stringify(r.parsedJson)}`,
  )
  assert(
    'stdout does not leak Stop common fields',
    r.parsedJson !== null &&
      !Object.prototype.hasOwnProperty.call(r.parsedJson, 'continue') &&
      !Object.prototype.hasOwnProperty.call(r.parsedJson, 'stopReason') &&
      !Object.prototype.hasOwnProperty.call(r.parsedJson, 'suppressOutput'),
    `parsedJson: ${JSON.stringify(r.parsedJson)}`,
  )
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
process.stdout.write(`\n[Result] ${passed} passed, ${failed} failed\n`)
if (errors.length > 0) {
  process.stdout.write('[Failures]\n')
  for (const e of errors) {
    process.stdout.write(`  - ${e}\n`)
  }
  process.exit(1)
}
process.exit(0)

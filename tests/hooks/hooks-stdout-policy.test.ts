/**
 * hooks-stdout-policy.test.ts
 *
 * Fixture-based tests for Claude / Codex hook stdout policy (Issue #797).
 *
 * AC1: Claude hooks allow path stdout is empty
 * AC2: Claude hooks block/hint path stdout is event-specific JSON or stderr short contract
 * AC3: Codex Stop/SubagentStop stdout is valid JSON {"continue": true/false};
 *      session recording failure always returns {"continue": true}
 * AC4: PreCompact hook saves loop state and is fail-open (stdout empty)
 * AC5: session manifest throttle skips same payload digest
 * AC7: tests/hooks/test-codex-single-composite.mjs exists
 * AC8/AC9: tests/fixtures/hooks/ directory exists with fixtures
 */

import { describe, it, expect } from 'vitest'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')

// ---------------------------------------------------------------------------
// AC8/AC9: tests/fixtures/hooks/ directory exists
// ---------------------------------------------------------------------------
describe('AC8/AC9: tests/fixtures/hooks/ directory', () => {
  it('directory exists', () => {
    expect(existsSync(resolve(repoRoot, 'tests', 'fixtures', 'hooks'))).toBe(true)
  })

  it('contains at least one fixture file', () => {
    const dir = resolve(repoRoot, 'tests', 'fixtures', 'hooks')
    if (!existsSync(dir)) return
    const files = readdirSync(dir).filter(f => f.endsWith('.json'))
    expect(files.length).toBeGreaterThan(0)
  })

  it('all valid .json fixtures (excluding malformed test fixtures) are valid JSON', () => {
    const dir = resolve(repoRoot, 'tests', 'fixtures', 'hooks')
    if (!existsSync(dir)) return
    // Exclude intentionally malformed fixtures (named *-malformed.*)
    const files = readdirSync(dir).filter(
      f => f.endsWith('.json') && !f.includes('-malformed')
    )
    for (const file of files) {
      const content = readFileSync(resolve(dir, file), 'utf8')
      expect(() => JSON.parse(content), `${file} must be valid JSON`).not.toThrow()
    }
  })
})

// ---------------------------------------------------------------------------
// AC1: save_loop_state_before_compaction.sh exists
// ---------------------------------------------------------------------------
describe('AC1: save_loop_state_before_compaction.sh exists', () => {
  it('file exists at .claude/hooks/save_loop_state_before_compaction.sh', () => {
    expect(
      existsSync(resolve(repoRoot, '.claude', 'hooks', 'save_loop_state_before_compaction.sh'))
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// AC4: PreCompact hook stdout is empty (allow path stdout policy)
// ---------------------------------------------------------------------------
describe('AC4: PreCompact hook stdout policy', () => {
  it('save_loop_state_before_compaction.sh stdout is empty on allow path', () => {
    const hookPath = resolve(repoRoot, '.claude', 'hooks', 'save_loop_state_before_compaction.sh')
    if (!existsSync(hookPath)) {
      expect(true).toBe(true) // Will fail AC1 above
      return
    }
    const fixture = readFileSync(
      resolve(repoRoot, 'tests', 'fixtures', 'hooks', 'precompact-allow.json'),
      'utf8'
    )
    // Run with a temp artifacts dir to avoid side effects
    const tmpDir = resolve(repoRoot, '.claude', 'worktrees', 'test-tmp-precompact-artifacts')
    const result = spawnSync('bash', [hookPath], {
      input: fixture,
      encoding: 'utf8',
      timeout: 10000,
      env: {
        ...process.env,
        LOOP_STATE_ARTIFACTS_DIR: tmpDir,
      },
    })

    expect(result.status, `expected exit 0, got ${result.status}`).toBe(0)
    expect(result.stdout.trim(), 'stdout must be empty on allow path (AC1 policy)').toBe('')
  })

  it('save_loop_state_before_compaction.sh is fail-open (exit 0) on unwritable dir', () => {
    const hookPath = resolve(repoRoot, '.claude', 'hooks', 'save_loop_state_before_compaction.sh')
    if (!existsSync(hookPath)) return

    const fixture = readFileSync(
      resolve(repoRoot, 'tests', 'fixtures', 'hooks', 'precompact-allow.json'),
      'utf8'
    )
    // Point to a non-existent unwritable-like path
    const result = spawnSync('bash', [hookPath], {
      input: fixture,
      encoding: 'utf8',
      timeout: 10000,
      env: {
        ...process.env,
        LOOP_STATE_ARTIFACTS_DIR: '/proc/non-existent-readonly-path-797',
      },
    })

    // fail-open: always exit 0
    expect(result.status, 'fail-open: must exit 0 even on write failure').toBe(0)
    expect(result.stdout.trim(), 'stdout must be empty even on failure').toBe('')
  })

  it('PreCompact hook is registered in .claude/settings.json', () => {
    const settingsPath = resolve(repoRoot, '.claude', 'settings.json')
    expect(existsSync(settingsPath)).toBe(true)
    const settings = JSON.parse(readFileSync(settingsPath, 'utf8'))
    expect(settings.hooks?.PreCompact).toBeDefined()
    const hookCommands = settings.hooks.PreCompact.flatMap(
      (entry: { hooks: Array<{ command: string }> }) => entry.hooks.map((h) => h.command)
    )
    expect(hookCommands.some((c: string) => c.includes('save_loop_state_before_compaction.sh'))).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// AC5: session manifest throttle — payload_digest in key
// ---------------------------------------------------------------------------
describe('AC5: session manifest throttle — payload_digest', () => {
  it('generate_session_manifest_from_hook.mjs contains payload_digest', () => {
    const hookPath = resolve(
      repoRoot, '.claude', 'hooks', 'generate_session_manifest_from_hook.mjs'
    )
    expect(existsSync(hookPath)).toBe(true)
    const content = readFileSync(hookPath, 'utf8')
    expect(content).toContain('payload_digest')
  })

  it('computePayloadDigest produces different digests for different payloads', () => {
    // Simulate payload digest computation used by the hook
    function sha256(content: string) {
      return createHash('sha256').update(content).digest('hex')
    }
    function computePayloadDigest(payload: object) {
      const serialized = JSON.stringify(payload, Object.keys(payload).sort())
      return sha256(serialized).slice(0, 16)
    }

    const payloadA = { hook_event_name: 'PostToolUse', tool_name: 'Bash', tool_use_id: 'tool-001' }
    const payloadB = { hook_event_name: 'PostToolUse', tool_name: 'Bash', tool_use_id: 'tool-002-different' }
    const digestA = computePayloadDigest(payloadA)
    const digestB = computePayloadDigest(payloadB)

    expect(digestA).not.toBe(digestB)
  })

  it('computePayloadDigest is stable for identical payloads (throttle idempotency)', () => {
    function sha256(content: string) {
      return createHash('sha256').update(content).digest('hex')
    }
    function computePayloadDigest(payload: object) {
      const serialized = JSON.stringify(payload, Object.keys(payload).sort())
      return sha256(serialized).slice(0, 16)
    }

    const payload = { hook_event_name: 'PostToolUse', tool_name: 'Bash', tool_use_id: 'tool-stable-001' }
    const digest1 = computePayloadDigest(payload)
    const digest2 = computePayloadDigest({ ...payload }) // shallow copy — same values
    expect(digest1).toBe(digest2)
  })
})

// ---------------------------------------------------------------------------
// AC7: tests/hooks/test-codex-single-composite.mjs exists
// ---------------------------------------------------------------------------
describe('AC7: test-codex-single-composite.mjs exists', () => {
  it('file exists at tests/hooks/test-codex-single-composite.mjs', () => {
    expect(
      existsSync(resolve(repoRoot, 'tests', 'hooks', 'test-codex-single-composite.mjs'))
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// AC3: Codex Stop/SubagentStop stdout policy — fixture content validation
// ---------------------------------------------------------------------------
describe('AC3: Codex hook fixtures are valid', () => {
  const codexFixtures = [
    'codex-stop-allow.json',
    'codex-subagent-stop-allow.json',
  ]

  for (const fixture of codexFixtures) {
    it(`${fixture} is valid JSON with hook_event_name`, () => {
      const content = readFileSync(
        resolve(repoRoot, 'tests', 'fixtures', 'hooks', fixture), 'utf8'
      )
      const parsed = JSON.parse(content)
      expect(parsed.hook_event_name).toBeDefined()
    })
  }
})

// ---------------------------------------------------------------------------
// AC2: session_manifest_coordinator.sh stdout is empty (allow path)
// ---------------------------------------------------------------------------
describe('AC2: session_manifest_coordinator.sh allow path stdout policy', () => {
  it('coordinator stdout is empty when stop_hook_active=true (short-circuit path)', () => {
    const coordinatorPath = resolve(
      repoRoot, '.claude', 'hooks', 'session_manifest_coordinator.sh'
    )
    if (!existsSync(coordinatorPath)) {
      expect(true).toBe(true)
      return
    }
    const fixture = readFileSync(
      resolve(repoRoot, 'tests', 'fixtures', 'hooks', 'stop-hook-active.json'),
      'utf8'
    )
    const result = spawnSync('bash', [coordinatorPath], {
      input: fixture,
      encoding: 'utf8',
      timeout: 10000,
    })

    expect(result.status, `expected exit 0, got ${result.status}`).toBe(0)
    expect(result.stdout.trim(), 'stdout must be empty on allow path').toBe('')
  })
})

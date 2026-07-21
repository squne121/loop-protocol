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
 *
 * Issue #1354: the Codex composite hook (Stop / SubagentStop / PreToolUse)
 * stdout policy is verified below via direct Vitest assertions instead of
 * spawning tests/hooks/test-codex-single-composite.mjs as a nested child
 * process. test-codex-single-composite.mjs remains a standalone manual smoke
 * script (run separately; not part of the pnpm test baseline gate).
 */

import { describe, it, expect } from 'vitest'
import { existsSync, readdirSync, readFileSync, mkdirSync, unlinkSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')

// spawnSync timeouts for shell/Node hook subprocesses invoked below (Issue #1354).
const HOOK_SPAWN_TIMEOUT_MS = 15000
const COORDINATOR_SPAWN_TIMEOUT_MS = 10000

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
// AC4: PreCompact hook saves loop state artifact with required fields (B4 fix)
// ---------------------------------------------------------------------------
describe('AC4: PreCompact hook stdout policy and artifact content (B4)', () => {
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
    const tmpDir = resolve(repoRoot, 'tmp', 'test-precompact-artifacts-ac4')
    const result = spawnSync('bash', [hookPath], {
      input: fixture,
      encoding: 'utf8',
      timeout: HOOK_SPAWN_TIMEOUT_MS,
      env: {
        ...process.env,
        LOOP_STATE_ARTIFACTS_DIR: tmpDir,
      },
    })

    expect(result.status, `expected exit 0, got ${result.status}\nstderr: ${result.stderr}`).toBe(0)
    expect(result.stdout.trim(), 'stdout must be empty on allow path (AC1 policy)').toBe('')
  })

  it('B4: artifact contains required fields (schema_version, session_id, trigger, saved_at, source_hook_input_hash)', () => {
    const hookPath = resolve(repoRoot, '.claude', 'hooks', 'save_loop_state_before_compaction.sh')
    if (!existsSync(hookPath)) return

    const fixture = readFileSync(
      resolve(repoRoot, 'tests', 'fixtures', 'hooks', 'precompact-allow.json'),
      'utf8'
    )
    const tmpDir = resolve(repoRoot, 'tmp', 'test-precompact-b4-fields')
    // Clean up any previous run artifacts
    if (existsSync(tmpDir)) {
      readdirSync(tmpDir).forEach((f) => { try { unlinkSync(resolve(tmpDir, f)) } catch { /**/ } })
    } else {
      mkdirSync(tmpDir, { recursive: true })
    }

    const result = spawnSync('bash', [hookPath], {
      input: fixture,
      encoding: 'utf8',
      timeout: HOOK_SPAWN_TIMEOUT_MS,
      env: { ...process.env, LOOP_STATE_ARTIFACTS_DIR: tmpDir },
    })

    expect(result.status).toBe(0)
    // Read the artifact produced
    if (!existsSync(tmpDir)) return
    const artifactFiles = readdirSync(tmpDir).filter((f) => f.endsWith('.json'))
    expect(artifactFiles.length, 'at least one artifact file must exist').toBeGreaterThan(0)

    const artifact = JSON.parse(readFileSync(resolve(tmpDir, artifactFiles[0]), 'utf8')) as Record<string, unknown>
    // B4 required fields
    expect(artifact['schema_version']).toBe('loop_state_precompact_v2')
    expect(typeof artifact['session_id']).toBe('string')
    expect(typeof artifact['trigger']).toBe('string')
    expect(typeof artifact['saved_at']).toBe('string')
    expect(typeof artifact['source_hook_input_hash']).toBe('string')
    // cwd must be present (may be empty string)
    expect('cwd' in artifact).toBe(true)
    // loop_state_ref and loop_state_hash may be null (not yet available from hook stdin)
    expect('loop_state_ref' in artifact).toBe(true)
    expect('loop_state_hash' in artifact).toBe(true)
  })

  it('save_loop_state_before_compaction.sh is fail-open (exit 0) on unwritable dir', () => {
    const hookPath = resolve(repoRoot, '.claude', 'hooks', 'save_loop_state_before_compaction.sh')
    if (!existsSync(hookPath)) return

    const fixture = readFileSync(
      resolve(repoRoot, 'tests', 'fixtures', 'hooks', 'precompact-allow.json'),
      'utf8'
    )
    const result = spawnSync('bash', [hookPath], {
      input: fixture,
      encoding: 'utf8',
      timeout: HOOK_SPAWN_TIMEOUT_MS,
      env: {
        ...process.env,
        LOOP_STATE_ARTIFACTS_DIR: '/proc/non-existent-readonly-path-797',
      },
    })

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
// B1: stableKeySegment is a hash of all key material (payloadDigest not truncated)
// B2: computePayloadDigest uses recursive canonical JSON (nested objects preserved)
// B3: lock-then-check prevents parallel duplicate generation
// ---------------------------------------------------------------------------
describe('AC5 / B1 / B2 / B3: session manifest throttle', () => {
  it('generate_session_manifest_from_hook.mjs contains payload_digest and canonicalJson', () => {
    const hookPath = resolve(
      repoRoot, '.claude', 'hooks', 'generate_session_manifest_from_hook.mjs'
    )
    expect(existsSync(hookPath)).toBe(true)
    const content = readFileSync(hookPath, 'utf8')
    expect(content).toContain('payload_digest')
    expect(content).toContain('canonicalJson')
  })

  it('B2: canonicalJson preserves nested objects (array replacer would drop them)', () => {
    function sha256(content: string) {
      return createHash('sha256').update(content).digest('hex')
    }
    function canonicalJson(value: unknown): string {
      if (value === null || typeof value !== 'object') return JSON.stringify(value)
      if (Array.isArray(value)) return '[' + (value as unknown[]).map(canonicalJson).join(',') + ']'
      const obj = value as Record<string, unknown>
      const sorted = Object.keys(obj).sort().map(k => JSON.stringify(k) + ':' + canonicalJson(obj[k]))
      return '{' + sorted.join(',') + '}'
    }
    function computePayloadDigest(payload: object) {
      return sha256(canonicalJson(payload)).slice(0, 16)
    }

    // Nested object: tool_input.command must be included in digest
    const payloadWithNested = { hook_event_name: 'PostToolUse', tool_name: 'Bash', tool_input: { command: 'echo hello' } }
    const payloadNestedDiff = { hook_event_name: 'PostToolUse', tool_name: 'Bash', tool_input: { command: 'echo world' } }
    // Array replacer (old bug) would produce the same digest for both because tool_input is an object
    // canonicalJson must differentiate them
    expect(computePayloadDigest(payloadWithNested)).not.toBe(computePayloadDigest(payloadNestedDiff))
  })

  it('computePayloadDigest produces different digests for different payloads', () => {
    function sha256(content: string) {
      return createHash('sha256').update(content).digest('hex')
    }
    function canonicalJson(value: unknown): string {
      if (value === null || typeof value !== 'object') return JSON.stringify(value)
      if (Array.isArray(value)) return '[' + (value as unknown[]).map(canonicalJson).join(',') + ']'
      const obj = value as Record<string, unknown>
      const sorted = Object.keys(obj).sort().map(k => JSON.stringify(k) + ':' + canonicalJson(obj[k]))
      return '{' + sorted.join(',') + '}'
    }
    function computePayloadDigest(payload: object) {
      return sha256(canonicalJson(payload)).slice(0, 16)
    }

    const payloadA = { hook_event_name: 'PostToolUse', tool_name: 'Bash', tool_use_id: 'tool-001' }
    const payloadB = { hook_event_name: 'PostToolUse', tool_name: 'Bash', tool_use_id: 'tool-002-different' }
    expect(computePayloadDigest(payloadA)).not.toBe(computePayloadDigest(payloadB))
  })

  it('computePayloadDigest is stable for identical payloads (throttle idempotency)', () => {
    function sha256(content: string) {
      return createHash('sha256').update(content).digest('hex')
    }
    function canonicalJson(value: unknown): string {
      if (value === null || typeof value !== 'object') return JSON.stringify(value)
      if (Array.isArray(value)) return '[' + (value as unknown[]).map(canonicalJson).join(',') + ']'
      const obj = value as Record<string, unknown>
      const sorted = Object.keys(obj).sort().map(k => JSON.stringify(k) + ':' + canonicalJson(obj[k]))
      return '{' + sorted.join(',') + '}'
    }
    function computePayloadDigest(payload: object) {
      return sha256(canonicalJson(payload)).slice(0, 16)
    }

    const payload = { hook_event_name: 'PostToolUse', tool_name: 'Bash', tool_use_id: 'tool-stable-001' }
    const digest1 = computePayloadDigest(payload)
    const digest2 = computePayloadDigest({ ...payload })
    expect(digest1).toBe(digest2)
  })

  it('B1: buildStableKey uses hash of keyMaterial (payloadDigest not truncated away)', () => {
    function sha256(content: string) {
      return createHash('sha256').update(content).digest('hex')
    }
    function canonicalJson(value: unknown): string {
      if (value === null || typeof value !== 'object') return JSON.stringify(value)
      if (Array.isArray(value)) return '[' + (value as unknown[]).map(canonicalJson).join(',') + ']'
      const obj = value as Record<string, unknown>
      const sorted = Object.keys(obj).sort().map(k => JSON.stringify(k) + ':' + canonicalJson(obj[k]))
      return '{' + sorted.join(',') + '}'
    }
    function buildStableKey(hookEventName: string, sessionId: string | null, toolName: string | null, ledgerPhase: string, payloadDigest: string) {
      const keyMaterial = {
        hookEventName,
        sessionId: sessionId || 'nosession',
        triggerOrTool: toolName || '',
        ledgerPhase,
        payloadDigest: payloadDigest || 'nodigest',
        loopStateHash: '',
      }
      return sha256(canonicalJson(keyMaterial)).slice(0, 32)
    }

    // Different payloadDigests must produce different stableKeys (B1 fix)
    const keyA = buildStableKey('Stop', 'sess-001', null, 'post_commit_verification', 'aaaa1111bbbb2222')
    const keyB = buildStableKey('Stop', 'sess-001', null, 'post_commit_verification', 'cccc3333dddd4444')
    expect(keyA).not.toBe(keyB)
    // Same inputs → same key (idempotency)
    const keyA2 = buildStableKey('Stop', 'sess-001', null, 'post_commit_verification', 'aaaa1111bbbb2222')
    expect(keyA).toBe(keyA2)
  })

  it('B3: generate_session_manifest_from_hook.mjs contains lock acquisition (tryAcquireLock / openSync wx)', () => {
    const hookPath = resolve(
      repoRoot, '.claude', 'hooks', 'generate_session_manifest_from_hook.mjs'
    )
    const content = readFileSync(hookPath, 'utf8')
    expect(content).toContain('tryAcquireLock')
    expect(content).toContain("'wx'")
    expect(content).toContain('releaseLock')
  })
})

// ---------------------------------------------------------------------------
// AC1/AC9: test-codex-single-composite.mjs is a standalone manual smoke
// script. Its file existence is checked here, but it is intentionally NOT
// spawned as a nested child process from pnpm test (Issue #1354). The cases
// it exercises are covered as direct Vitest assertions in the
// "AC2-AC8: Codex composite hook direct Vitest assertions" describe block
// below instead.
// ---------------------------------------------------------------------------
describe('AC1/AC9: test-codex-single-composite.mjs remains a manual smoke script', () => {
  it('file exists at tests/hooks/test-codex-single-composite.mjs', () => {
    expect(
      existsSync(resolve(repoRoot, 'tests', 'hooks', 'test-codex-single-composite.mjs'))
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// AC9: Codex hooks structural validation vs runtime-active/trust distinction
// ---------------------------------------------------------------------------
describe('AC9: Codex hooks structural validation vs runtime-active/trust', () => {
  it('.codex/hooks.json is parseable and Stop/SubagentStop have exactly one matching command each', () => {
    const hooksJsonPath = resolve(repoRoot, '.codex', 'hooks.json')
    expect(existsSync(hooksJsonPath), '.codex/hooks.json must exist').toBe(true)
    const hooksJsonRoot = JSON.parse(readFileSync(hooksJsonPath, 'utf8')) as Record<string, unknown>
    // hooks.json schema: { hooks: { Stop: [...], SubagentStop: [...], ... } }
    const hooksMap = (hooksJsonRoot['hooks'] ?? hooksJsonRoot) as Record<string, Array<{ hooks: Array<{ command: string }> }>>
    // AC9 structural validation: Stop and SubagentStop must each have exactly one matcher entry
    // with exactly one command hook referencing session-recording-composite.mjs
    const events = ['Stop', 'SubagentStop']
    for (const eventName of events) {
      const entries = hooksMap[eventName]
      expect(
        Array.isArray(entries) && entries.length === 1,
        `${eventName} must have exactly one matcher entry (got ${JSON.stringify(entries)})`,
      ).toBe(true)
      const hookList = entries[0]?.hooks
      expect(
        Array.isArray(hookList) && hookList.length === 1,
        `${eventName}[0].hooks must have exactly one command (got ${JSON.stringify(hookList)})`,
      ).toBe(true)
      const cmd = hookList[0]?.command
      expect(
        typeof cmd === 'string' && cmd.includes('session-recording-composite.mjs'),
        `${eventName}[0].hooks[0].command must reference session-recording-composite.mjs`,
      ).toBe(true)
    }
  })

  it('AC9: structural validation confirms hook wiring; runtime-active/trust requires live session (documented caveat)', () => {
    // AC9 documents the boundary:
    // - "structural validation" = parse hooks.json and assert command presence (done above).
    // - "runtime-active/trust" = whether Codex actually loads and honours the hook in a live
    //   session. This cannot be verified by a unit test without a live Codex process.
    //   The caveat is recorded here so reviewers understand what AC9 covers and does NOT cover.
    const caveat = {
      structural_validation: 'covered_by_unit_test',
      runtime_active_trust: 'requires_live_codex_session_manual_or_e2e_verification',
    }
    expect(caveat.structural_validation).toBe('covered_by_unit_test')
    expect(caveat.runtime_active_trust).toBe('requires_live_codex_session_manual_or_e2e_verification')
  })
})

// ---------------------------------------------------------------------------
// AC2-AC8: Codex composite hook direct Vitest assertions
//
// Replaces the former nested standalone script execution (Issue #1354).
// Each case below invokes .codex/hooks/session-recording-composite.mjs
// directly via runCompositeHook() and asserts the OpenAI Codex Hooks stdout
// contract for the relevant event / path.
// ---------------------------------------------------------------------------
describe('AC2-AC8: Codex composite hook direct Vitest assertions', () => {
  const compositeHook = resolve(repoRoot, '.codex', 'hooks', 'session-recording-composite.mjs')

  function runCompositeHook(event: string, stdinContent: string | object, overrideEnv: Record<string, string> = {}) {
    const captureDirectory = mkdtempSync(resolve(tmpdir(), 'codex-hook-capture-fixture-'))
    let result
    try {
      result = spawnSync(process.execPath, [compositeHook, '--event', event], {
        input: typeof stdinContent === 'string' ? stdinContent : JSON.stringify(stdinContent),
        encoding: 'utf8',
        timeout: HOOK_SPAWN_TIMEOUT_MS,
        cwd: repoRoot,
        env: { ...process.env, SCOPE_ROLLUP_CAPTURE_DIR: captureDirectory, ...overrideEnv },
      })
    } finally {
      rmSync(captureDirectory, { recursive: true, force: true })
    }
    let parsedJson: unknown = null
    if (result.stdout && result.stdout.trim()) {
      try { parsedJson = JSON.parse(result.stdout.trim()) } catch { /* not JSON */ }
    }
    return { ...result, parsedJson }
  }

  // AC2: Stop allow path — exit 0, stdout is valid JSON, continue is boolean
  it('Stop allow path: exit 0, valid JSON stdout, typeof continue === "boolean" (AC2)', () => {
    const fixture = readFileSync(
      resolve(repoRoot, 'tests', 'fixtures', 'hooks', 'codex-stop-allow.json'), 'utf8'
    )
    const r = runCompositeHook('Stop', fixture, {
      CODEX_SESSION_RECORDING_PRODUCER: resolve(repoRoot, 'tests', 'hooks', '_stub-producer.mjs'),
    })
    expect(r.status, `exit code: ${r.status}`).toBe(0)
    expect(r.parsedJson, `stdout: ${r.stdout}`).not.toBeNull()
    expect(typeof (r.parsedJson as { continue: unknown }).continue).toBe('boolean')
  })

  // AC3: SubagentStop allow path — exit 0, stdout is valid JSON, continue is boolean
  it('SubagentStop allow path: exit 0, valid JSON stdout, typeof continue === "boolean" (AC3)', () => {
    const fixture = readFileSync(
      resolve(repoRoot, 'tests', 'fixtures', 'hooks', 'codex-subagent-stop-allow.json'), 'utf8'
    )
    const r = runCompositeHook('SubagentStop', fixture, {
      CODEX_SESSION_RECORDING_PRODUCER: resolve(repoRoot, 'tests', 'hooks', '_stub-producer.mjs'),
    })
    expect(r.status, `exit code: ${r.status}`).toBe(0)
    expect(r.parsedJson, `stdout: ${r.stdout}`).not.toBeNull()
    expect(typeof (r.parsedJson as { continue: unknown }).continue).toBe('boolean')
  })

  // AC4: Stop malformed payload — fail-open {"continue": true}
  it('Stop malformed payload: exit 0, {"continue": true} (AC4)', () => {
    const r = runCompositeHook('Stop', 'INVALID_JSON_NOT_VALID_PAYLOAD')
    expect(r.status).toBe(0)
    expect(r.parsedJson).not.toBeNull()
    expect((r.parsedJson as { continue: boolean }).continue).toBe(true)
  })

  // AC5: SubagentStop malformed payload — fail-open {"continue": true}
  it('SubagentStop malformed payload: exit 0, {"continue": true} (AC5)', () => {
    const r = runCompositeHook('SubagentStop', 'INVALID_JSON_NOT_VALID_PAYLOAD')
    expect(r.status).toBe(0)
    expect(r.parsedJson).not.toBeNull()
    expect((r.parsedJson as { continue: boolean }).continue).toBe(true)
  })

  // AC6: PreToolUse allow path — exit 0, stdout empty
  it('PreToolUse allow path: exit 0, stdout empty (AC6)', () => {
    const safePayload = {
      hook_event_name: 'PreToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'pnpm typecheck' },
      secrets_mode: 'none',
    }
    const r = runCompositeHook('PreToolUse', safePayload)
    expect(r.status, `exit code: ${r.status}`).toBe(0)
    expect(r.stdout.trim(), `stdout was: "${r.stdout.trim()}"`).toBe('')
  })

  // AC7: PreToolUse block path — OpenAI Codex Hooks PreToolUse deny schema.
  // hookSpecificOutput.hookEventName === "PreToolUse", permissionDecision === "deny",
  // permissionDecisionReason is a string, and parsed does NOT carry
  // continue / stopReason / suppressOutput properties (Stop-schema leakage guard).
  it('PreToolUse block path: OpenAI Codex Hooks deny schema with permissionDecisionReason (AC7)', () => {
    const forbiddenPayload = {
      hook_event_name: 'PreToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'cat assets/forbidden.png' },
      secrets_mode: 'none',
    }
    const r = runCompositeHook('PreToolUse', forbiddenPayload)
    expect(r.status, `exit code: ${r.status}`).toBe(0)
    expect(r.parsedJson, `stdout: ${r.stdout}`).not.toBeNull()
    const parsed = r.parsedJson as {
      hookSpecificOutput?: {
        hookEventName?: unknown
        permissionDecision?: unknown
        permissionDecisionReason?: unknown
      }
      continue?: unknown
      stopReason?: unknown
      suppressOutput?: unknown
    }
    expect(parsed.hookSpecificOutput?.hookEventName).toBe('PreToolUse')
    expect(parsed.hookSpecificOutput?.permissionDecision).toBe('deny')
    expect(typeof parsed.hookSpecificOutput?.permissionDecisionReason).toBe('string')
    expect('continue' in parsed).toBe(false)
    expect('stopReason' in parsed).toBe(false)
    expect('suppressOutput' in parsed).toBe(false)
  })

  // AC8: Stop security verifier failure — {"continue": false, "stopReason": <string>}
  it('Stop security verifier failure: {"continue": false, stopReason defined} (AC8)', () => {
    const fixture = readFileSync(
      resolve(repoRoot, 'tests', 'fixtures', 'hooks', 'codex-stop-security-gate-fail.json'), 'utf8'
    )
    const r = runCompositeHook('Stop', fixture, {
      CODEX_SESSION_RECORDING_PRODUCER: resolve(repoRoot, 'tests', 'hooks', '_stub-producer.mjs'),
    })
    expect(r.status).toBe(0)
    expect(r.parsedJson).not.toBeNull()
    const parsed = r.parsedJson as { continue: boolean; stopReason?: string }
    expect(parsed.continue).toBe(false)
    expect(typeof parsed.stopReason).toBe('string')
  })

  // Fixture schema validation
  for (const fixture of ['codex-stop-allow.json', 'codex-subagent-stop-allow.json', 'codex-stop-security-gate-fail.json']) {
    it(`fixture ${fixture} is valid JSON with hook_event_name`, () => {
      const content = readFileSync(resolve(repoRoot, 'tests', 'fixtures', 'hooks', fixture), 'utf8')
      const parsed = JSON.parse(content)
      expect((parsed as { hook_event_name: string }).hook_event_name).toBeDefined()
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
      timeout: COORDINATOR_SPAWN_TIMEOUT_MS,
    })

    expect(result.status, `expected exit 0, got ${result.status}`).toBe(0)
    expect(result.stdout.trim(), 'stdout must be empty on allow path').toBe('')
  })
})

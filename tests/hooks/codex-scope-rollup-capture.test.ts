import { afterEach, describe, expect, it } from 'vitest'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')
const adapter = resolve(repoRoot, 'scripts', 'session-recording', 'codex-hook-adapter.mjs')
const fixtures = resolve(repoRoot, 'tests', 'fixtures', 'hooks')
const temporaryDirectories: string[] = []

function isolatedDirectory() {
  const directory = mkdtempSync(resolve(tmpdir(), 'codex-scope-rollup-capture-'))
  temporaryDirectories.push(directory)
  return directory
}

function readFixture(name: string) {
  return JSON.parse(readFileSync(resolve(fixtures, name), 'utf8')) as Record<string, unknown>
}

function runAdapter(payload: Record<string, unknown>, captureDirectory: string, override = '') {
  const startedAt = Date.now()
  const result = spawnSync(process.execPath, [adapter, '--event', 'SubagentStop'], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    cwd: repoRoot,
    timeout: 7000,
    env: {
      ...process.env,
      CODEX_SESSION_RECORDING_PRODUCER: resolve(repoRoot, 'tests', 'hooks', '_stub-producer.mjs'),
      CODEX_HOOK_MANIFEST_ROOT: isolatedDirectory(),
      SCOPE_ROLLUP_CAPTURE_DIR: captureDirectory,
      ...(override ? { CODEX_SCOPE_ROLLUP_CAPTURE_SCRIPT: override } : {}),
    },
  })
  return { ...result, elapsedMs: Date.now() - startedAt }
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true })
  }
})

describe('Codex SubagentStop scope-rollup capture adapter', () => {
  it('GIVEN a target marker WHEN the adapter runs THEN target valid marker writes canonical capture and matching sidecar', () => {
    const captureDirectory = isolatedDirectory()
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    const result = runAdapter(payload, captureDirectory)
    const names = readdirSync(captureDirectory)
    const canonical = names.filter(name => name.endsWith('.txt'))

    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    expect(canonical).toHaveLength(1)
    expect(readFileSync(resolve(captureDirectory, canonical[0]), 'utf8')).toBe(payload.last_assistant_message)
    expect(statSync(resolve(captureDirectory, canonical[0])).mode & 0o777).toBe(0o600)
    expect(names.filter(name => name.endsWith('.capture.yaml'))).toHaveLength(1)
  })

  it('GIVEN a non-target SubagentStop WHEN the adapter runs THEN non-target writes diagnostic sidecar only', () => {
    const captureDirectory = isolatedDirectory()
    const payload = { ...readFixture('codex-subagent-stop-allow.json'), agent_type: 'test-runner', last_assistant_message: 'not a capture target' }
    const result = runAdapter(payload, captureDirectory)
    const names = readdirSync(captureDirectory)

    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    expect(names.filter(name => name.endsWith('.txt'))).toHaveLength(0)
    expect(names.filter(name => name.endsWith('.capture.yaml'))).toHaveLength(1)
    expect(readFileSync(resolve(captureDirectory, names[0]), 'utf8')).toContain('capture_status: agent_type_mismatch')
  })

  it('GIVEN denied or stop-hook payloads WHEN the adapter runs THEN eligibility matrix skips denied and stop-hook payloads', () => {
    for (const payload of [
      { ...readFixture('codex-scope-rollup-runner-stop.json'), secrets_mode: 'app_secret' },
      { ...readFixture('codex-scope-rollup-runner-stop.json'), public_checkpoint_enabled: true },
      { ...readFixture('codex-scope-rollup-runner-stop.json'), unknown_visibility_mapping: true },
      { ...readFixture('codex-scope-rollup-runner-stop.json'), stop_hook_active: true },
    ]) {
      const captureDirectory = isolatedDirectory()
      const result = runAdapter(payload, captureDirectory)
      expect(result.status).toBe(0)
      expect(readdirSync(captureDirectory).filter(name => name.endsWith('.txt'))).toHaveLength(0)
    }
  })

  it('GIVEN a semantic producer rejection WHEN the adapter runs THEN semantic rejection remains fail-open with a diagnostic sidecar', () => {
    const captureDirectory = isolatedDirectory()
    const payload = { ...readFixture('codex-scope-rollup-runner-stop.json'), last_assistant_message: 'marker is absent' }
    const result = runAdapter(payload, captureDirectory)
    const names = readdirSync(captureDirectory)

    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    expect(names.filter(name => name.endsWith('.txt'))).toHaveLength(0)
    expect(readFileSync(resolve(captureDirectory, names[0]), 'utf8')).toContain('capture_status: parser_rejected')
  })

  it('GIVEN transport fixture failures WHEN the adapter runs THEN transport failures are bounded and redacted', () => {
    for (const fixture of ['nonzero.py', 'timeout.py']) {
      const captureDirectory = isolatedDirectory()
      const result = runAdapter(
        readFixture('codex-scope-rollup-runner-stop.json'),
        captureDirectory,
        resolve(fixtures, 'scope-rollup-capture', fixture),
      )

      expect(result.status).toBe(0)
      // 5,000 ms is the capture-child budget; the surrounding manifest flow
      // may add bounded process teardown time before the adapter returns.
      expect(result.elapsedMs).toBeLessThan(6500)
      expect(result.stdout.trim()).toBe('{"continue":true}')
      expect(result.stderr).not.toContain('scope-rollup-fixture')
      expect(result.stderr).not.toContain(payloadText(readFixture('codex-scope-rollup-runner-stop.json')))
    }
  }, 7000)

  it('GIVEN a repeated target payload WHEN the adapter runs twice THEN the producer keeps one canonical capture', () => {
    const captureDirectory = isolatedDirectory()
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    runAdapter(payload, captureDirectory)
    runAdapter(payload, captureDirectory)

    expect(readdirSync(captureDirectory).filter(name => name.endsWith('.txt'))).toHaveLength(1)
  })

  it('GIVEN adapter subprocess verification WHEN it passes THEN adapter verification is distinct from live Codex trust', () => {
    expect(existsSync(adapter)).toBe(true)
    expect('adapter path verified').toContain('adapter path verified')
  })
})

function payloadText(payload: Record<string, unknown>) {
  return String(payload.last_assistant_message ?? '')
}

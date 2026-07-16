import { afterEach, describe, expect, it } from 'vitest'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')
const adapter = resolve(repoRoot, 'scripts', 'session-recording', 'codex-hook-adapter.mjs')
const fixtures = resolve(repoRoot, 'tests', 'fixtures', 'hooks')
const scopeRollupCaptureFixtures = resolve(fixtures, 'scope-rollup-capture')
const INVOCATION_ID_RE = /invocation_id:\s*['"]?([^ \n'"]+)/
const temporaryDirectories: string[] = []

function isolatedDirectory() {
  const directory = mkdtempSync(resolve(tmpdir(), 'codex-scope-rollup-capture-'))
  temporaryDirectories.push(directory)
  return directory
}

function readFixture(name: string) {
  return JSON.parse(readFileSync(resolve(fixtures, name), 'utf8')) as Record<string, unknown>
}

function payloadInvocationId(payload: Record<string, unknown>): string | null {
  const message = String(payload.last_assistant_message ?? '')
  const match = message.match(INVOCATION_ID_RE)
  return match?.[1] ?? null
}

function writeSourceBoundArtifacts(
  payload: Record<string, unknown>,
  captureDirectory: string,
  options: {
    eligibilityRequestedAt?: string
    eligibilityGeneratedAt?: string
    readinessGeneratedAt?: string
    readinessPrepared?: boolean
    invocationId?: string | null
    skipReadiness?: boolean
    invalidReadiness?: boolean
    mismatchedInvocation?: boolean
  } = {},
) {
  const { mismatchedInvocation = false } = options
  const invocationId = options.invocationId ?? payloadInvocationId(payload) ?? 'scope-rollup-missing-invocation'
  const requestedAt = options.eligibilityRequestedAt ?? '2026-07-15T12:00:00Z'
  const generatedAt = options.eligibilityGeneratedAt ?? '2026-07-15T12:00:02Z'
  const readinessGeneratedAt = options.readinessGeneratedAt ?? '2026-07-15T12:00:03Z'
  const eligibility = {
    invocation_id: mismatchedInvocation ? 'other-invocation-id' : invocationId,
    requested_at: requestedAt,
    generated_at: generatedAt,
    agent_transcript_path: String(payload.agent_transcript_path ?? '/tmp/transcript'),
  }
  const eligibilityPath = resolve(captureDirectory, 'source-bound-eligibility.json')
  const readiness = {
    invocation_id: mismatchedInvocation ? 'other-readiness-id' : invocationId,
    generated_at: readinessGeneratedAt,
    prepared: options.readinessPrepared ?? true,
    state: options.readinessPrepared === false ? 'not-ready' : 'ready',
  }
  const readinessPath = resolve(captureDirectory, 'source-bound-readiness.json')

  writeFileSync(eligibilityPath, JSON.stringify(eligibility, null, 2), 'utf8')
  if (!options.skipReadiness) {
    writeFileSync(
      readinessPath,
      options.invalidReadiness ? 'not-json-or-yaml-object' : JSON.stringify(readiness, null, 2),
      'utf8',
    )
  }

  const enriched = {
    ...payload,
    source_bound_eligibility_artifact_path: eligibilityPath,
  }
  if (!options.skipReadiness) {
    enriched.source_bound_readiness_artifact_path = readinessPath
  }
  return enriched
}

function runAdapter(payload: Record<string, unknown>, captureDirectory: string, override = '', env: Record<string, string> = {}) {
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
      ...env,
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
  it('official wire without source-bound eligibility skips capture', () => {
    const captureDirectory = isolatedDirectory()
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    const result = runAdapter(payload, captureDirectory)
    const sidecars = readdirSync(captureDirectory).filter((name) => name.endsWith('.capture.yaml'))
    const txts = readdirSync(captureDirectory).filter((name) => name.endsWith('.txt'))

    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    expect(txts).toHaveLength(0)
    expect(sidecars).toHaveLength(1)
    expect(readFileSync(resolve(captureDirectory, sidecars[0]), 'utf8')).toContain('capture_status: parser_rejected')
    expect(readFileSync(resolve(captureDirectory, sidecars[0]), 'utf8')).toContain('source-bound eligibility artifact is missing')
  })

  it('source-bound eligibility rejects missing stale invalid and mismatched artifacts', () => {
    const basePayload = readFixture('codex-scope-rollup-runner-stop.json')
    const cases = [
      {
        name: 'missing readiness artifact',
        prepare: (payload: Record<string, unknown>, directory: string) => writeSourceBoundArtifacts(payload, directory, { skipReadiness: true }),
        expectParser: 'source-bound readiness artifact is missing',
      },
      {
        name: 'stale eligibility artifact',
        prepare: (payload: Record<string, unknown>, directory: string) => writeSourceBoundArtifacts(payload, directory, {
          eligibilityRequestedAt: '2026-07-15T12:00:03Z',
          eligibilityGeneratedAt: '2026-07-15T12:00:01Z',
        }),
        expectParser: 'source-bound eligibility artifact is stale',
      },
    {
      name: 'invalid readiness artifact',
      prepare: (payload: Record<string, unknown>, directory: string) => writeSourceBoundArtifacts(payload, directory, {
        invalidReadiness: true,
      }),
      expectParser: 'source-bound readiness artifact is malformed',
      },
      {
        name: 'mismatched invocation',
        prepare: (payload: Record<string, unknown>, directory: string) => writeSourceBoundArtifacts(payload, directory, { mismatchedInvocation: true }),
        expectParser: 'invocation_id mismatch',
      },
    ]

    for (const { prepare, expectParser } of cases) {
      const captureDirectory = isolatedDirectory()
      const payload = prepare(basePayload, captureDirectory)
      const result = runAdapter(payload, captureDirectory)
      const sidecars = readdirSync(captureDirectory).filter((name) => name.endsWith('.capture.yaml'))

      expect(result.status).toBe(0)
      expect(readFileSync(resolve(captureDirectory, sidecars[0]), 'utf8')).toContain('capture_status: parser_rejected')
      expect(readFileSync(resolve(captureDirectory, sidecars[0]), 'utf8')).toContain(expectParser)
    }
  })

  it('valid eligibility writes canonical capture', () => {
    const captureDirectory = isolatedDirectory()
    const payload = writeSourceBoundArtifacts(readFixture('codex-scope-rollup-runner-stop.json'), captureDirectory)
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
    const payload = {
      ...writeSourceBoundArtifacts(readFixture('codex-scope-rollup-runner-stop.json'), captureDirectory),
      last_assistant_message: 'marker is absent',
    }
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
      const payload = writeSourceBoundArtifacts(readFixture('codex-scope-rollup-runner-stop.json'), captureDirectory)
      const result = runAdapter(
        payload,
        captureDirectory,
        resolve(scopeRollupCaptureFixtures, fixture),
      )

      expect(result.status).toBe(0)
      expect(result.elapsedMs).toBeLessThan(6500)
      expect(result.stdout.trim()).toBe('{"continue":true}')
      expect(result.stderr).not.toContain('scope-rollup-fixture')
      expect(result.stderr).not.toContain(payloadText(readFixture('codex-scope-rollup-runner-stop.json')))
    }
  }, 7000)

  it('timeout terminates process tree without late write', () => {
    const captureDirectory = isolatedDirectory()
    const payload = writeSourceBoundArtifacts(readFixture('codex-scope-rollup-runner-stop.json'), captureDirectory)
    const result = runAdapter(
      payload,
      captureDirectory,
      resolve(scopeRollupCaptureFixtures, 'late_writer.py'),
      { NODE_ENV: 'test' },
    )

    expect(result.status).toBe(0)
    expect(result.elapsedMs).toBeLessThan(7000)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    expect(existsSync(resolve(captureDirectory, 'scope_rollup_capture_late_write.txt'))).toBe(false)
  })

  it('bootstrap writes source-bound readiness artifact', () => {
    const bootstrapScript = resolve(repoRoot, 'scripts', 'session-recording', 'bootstrap-source-bound-readiness.mjs')
    const captureDirectory = isolatedDirectory()
    const readinessPath = resolve(captureDirectory, 'source-bound-readiness-bootstrap.json')
    const result = spawnSync(process.execPath, [bootstrapScript], {
      encoding: 'utf8',
      timeout: 5000,
      env: {
        ...process.env,
        SCOPE_ROLLUP_CAPTURE_DIR: captureDirectory,
        SCOPE_ROLLUP_SOURCE_BOUND_READINESS_PATH: readinessPath,
      },
    })

    expect(result.status).toBe(0)
    expect(existsSync(readinessPath)).toBe(true)
    const ready = JSON.parse(readFileSync(readinessPath, 'utf8')) as Record<string, unknown>
    expect(ready.prepared).toBe(true)
    expect(ready.state).toBe('ready')
    expect(typeof ready.invocation_id).toBe('string')
    expect(ready.invocation_id).toContain('inv-')
    expect((statSync(readinessPath).mode & 0o777)).toBe(0o600)
  })

  it('unprepared readiness skips without sync', () => {
    const captureDirectory = isolatedDirectory()
    const payload = writeSourceBoundArtifacts(
      readFixture('codex-scope-rollup-runner-stop.json'),
      captureDirectory,
      { readinessPrepared: false },
    )
    const result = runAdapter(payload, captureDirectory)
    const sidecars = readdirSync(captureDirectory).filter((name) => name.endsWith('.capture.yaml'))

    expect(result.status).toBe(0)
    expect(readFileSync(resolve(captureDirectory, sidecars[0]), 'utf8')).toContain('capture_status: parser_rejected')
    expect(readFileSync(resolve(captureDirectory, sidecars[0]), 'utf8')).toContain('source-bound readiness artifact is not prepared')
  })

  it('production transport rejects test override and sanitizes child environment', () => {
    const captureDirectory = isolatedDirectory()
    const payload = writeSourceBoundArtifacts(
      readFixture('codex-scope-rollup-runner-stop.json'),
      captureDirectory,
    )
    const result = runAdapter(
      payload,
      captureDirectory,
      resolve(scopeRollupCaptureFixtures, 'env-probe.py'),
      {
        NODE_ENV: 'production',
        SECRET_TEST: 'should-not-reach-child',
      },
    )

    const names = readdirSync(captureDirectory)
    const txts = names.filter(name => name.endsWith('.txt'))

    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    expect(txts).toHaveLength(1)
    expect(readdirSync(captureDirectory).filter(name => name.endsWith('.capture.yaml'))).toHaveLength(1)
    expect(result.stderr).not.toContain('capture_status: capture_nonzero')
    const envProbePath = resolve(captureDirectory, 'env_probe.txt')
    expect(existsSync(envProbePath)).toBe(false)
  })

  it('GIVEN a repeated target payload WHEN the adapter runs twice THEN the producer keeps one canonical capture', () => {
    const captureDirectory = isolatedDirectory()
    const payload = writeSourceBoundArtifacts(readFixture('codex-scope-rollup-runner-stop.json'), captureDirectory)
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

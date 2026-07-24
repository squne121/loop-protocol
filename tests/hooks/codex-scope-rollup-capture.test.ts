import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { chmodSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { execFileSync, spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')
const adapter = resolve(repoRoot, 'scripts', 'session-recording', 'codex-hook-adapter.mjs')
const bootstrapScript = resolve(repoRoot, 'scripts', 'session-recording', 'bootstrap-source-bound-readiness.mjs')
const producerScript = resolve(repoRoot, '.claude', 'hooks', 'capture_scope_rollup_final_response.py')
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

function sha256Hex(buffer: Buffer) {
  return `sha256:${createHash('sha256').update(buffer).digest('hex')}`
}

function payloadText(payload: Record<string, unknown>) {
  return String(payload.last_assistant_message ?? '')
}

// Resolve a real python3 interpreter once — the adapter's Node-only gate
// requires readiness.interpreter_realpath to be an existing regular file
// (Issue #1527 Scope Delta (2) AC1/AC17).
let realInterpreterPath = ''
let realProducerDigest = ''
let realPolicyDigest = ''
let realSecretPolicyDigest = ''

beforeAll(() => {
  realInterpreterPath = execFileSync('python3', ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' }).trim()
  realProducerDigest = sha256Hex(readFileSync(producerScript))
  realPolicyDigest = sha256Hex(readFileSync(resolve(repoRoot, 'docs', 'dev', 'session-recording-policy.md')))
  realSecretPolicyDigest = sha256Hex(readFileSync(resolve(repoRoot, 'docs', 'dev', 'secret-policy.md')))
})

function writeJsonMode0600(path: string, payload: Record<string, unknown>) {
  writeFileSync(path, `${JSON.stringify(payload, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 })
  chmodSync(path, 0o600)
}

interface FixedLocationOverrides {
  eligibilityGeneratedAt?: string
  eligibilityExpiresAt?: string
  eligibilitySafetyVerdict?: string
  eligibilityRepoRootRealpath?: string
  eligibilityPublicCheckpointPresent?: boolean
  eligibilitySecretsMode?: string
  eligibilityExtraKey?: boolean
  eligibilityMissingKey?: boolean
  skipEligibility?: boolean
  readinessGeneratedAt?: string
  readinessPrepared?: boolean
  readinessProducerDigest?: string
  readinessInterpreterRealpath?: string
  skipReadiness?: boolean
}

/** Write the fixed private eligibility/readiness artifacts the Node-only
 *  adapter gate and the Python producer's independent re-verification both
 *  read exclusively — no hook-payload keys are used any more (Issue #1527
 *  Scope Delta (2) AC12). Returns the env overrides to point both consumers
 *  at the isolated artifact paths. */
function writeFixedLocationArtifacts(directory: string, overrides: FixedLocationOverrides = {}) {
  const eligibilityPath = resolve(directory, 'eligibility.json')
  const readinessPath = resolve(directory, 'readiness.json')
  // AC14: eligibility must be generated BEFORE the marker's generated_at
  // (fixture markers are fixed at 2026-07-15T12:00:01Z) — real production
  // eligibility is minted at session start, well before the final response
  // marker. expires_at is set far in the future so the artifact also
  // remains valid at real wall-clock "now" (the producer's own
  // hook_received_at check uses the real clock, not the fixture's date).
  const nowIso = '2026-07-15T11:00:00Z'
  const expiresIso = '2030-01-01T00:00:00Z'

  if (!overrides.skipEligibility) {
    const eligibility: Record<string, unknown> = {
      schema: 'SESSION_RECORDING_SCOPE_ROLLUP_ELIGIBILITY_V1',
      artifact_version: 1,
      repo_root_realpath: overrides.eligibilityRepoRootRealpath ?? repoRoot,
      head_sha: null,
      policy_digest: realPolicyDigest,
      secret_policy_digest: realSecretPolicyDigest,
      public_checkpoint_present: overrides.eligibilityPublicCheckpointPresent ?? false,
      visibility: 'public',
      secrets_mode: overrides.eligibilitySecretsMode ?? 'none',
      generated_at: overrides.eligibilityGeneratedAt ?? nowIso,
      expires_at: overrides.eligibilityExpiresAt ?? expiresIso,
      safety_verdict: overrides.eligibilitySafetyVerdict ?? 'allow',
    }
    if (overrides.eligibilityExtraKey) eligibility.extra_unexpected_key = true
    if (overrides.eligibilityMissingKey) delete eligibility.secrets_mode
    writeJsonMode0600(eligibilityPath, eligibility)
  }

  if (!overrides.skipReadiness) {
    const readiness = {
      schema: 'SESSION_RECORDING_SCOPE_ROLLUP_READINESS_V1',
      artifact_version: 1,
      repo_root_realpath: repoRoot,
      uv_lock_digest: null,
      python_version_digest: null,
      interpreter_realpath: overrides.readinessInterpreterRealpath ?? realInterpreterPath,
      interpreter_version: 'Python 3.x',
      producer_digest: overrides.readinessProducerDigest ?? realProducerDigest,
      prepared: overrides.readinessPrepared ?? true,
      generated_at: overrides.readinessGeneratedAt ?? nowIso,
    }
    writeJsonMode0600(readinessPath, readiness)
  }

  return {
    SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH: eligibilityPath,
    SCOPE_ROLLUP_READINESS_ARTIFACT_PATH: readinessPath,
  }
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
    const artifactDirectory = isolatedDirectory()
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    const env = writeFixedLocationArtifacts(artifactDirectory, { skipEligibility: true, skipReadiness: true })
    const result = runAdapter(payload, captureDirectory, '', env)
    const sidecars = readdirSync(captureDirectory).filter((name) => name.endsWith('.capture.yaml'))
    const txts = readdirSync(captureDirectory).filter((name) => name.endsWith('.txt'))

    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    // AC1: no valid fixed-location eligibility artifact => the producer is
    // never invoked at all (fixed skip reason, no sidecar either).
    expect(txts).toHaveLength(0)
    expect(sidecars).toHaveLength(0)
    expect(result.stderr).toContain('eligibility_missing')
  })

  const sourceBoundRejectionCases: Array<{ name: string; overrides: FixedLocationOverrides; expectReason: string }> = [
    {
      name: 'missing readiness artifact',
      overrides: { skipReadiness: true },
      expectReason: 'readiness_missing',
    },
    {
      name: 'stale (future) eligibility artifact',
      overrides: { eligibilityGeneratedAt: new Date(Date.now() + 60_000).toISOString() },
      expectReason: 'eligibility_stale_future_generated_at',
    },
    {
      name: 'expired eligibility artifact',
      overrides: {
        eligibilityGeneratedAt: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
        eligibilityExpiresAt: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
      },
      expectReason: 'eligibility_stale_expired',
    },
    {
      name: 'invalid readiness (unprepared)',
      overrides: { readinessPrepared: false },
      expectReason: 'readiness_unprepared',
    },
    {
      name: 'mismatched repo root binding',
      overrides: { eligibilityRepoRootRealpath: '/nonexistent/other/repo' },
      expectReason: 'eligibility_binding_repo_mismatch',
    },
    {
      name: 'unsafe secrets_mode',
      overrides: { eligibilitySecretsMode: 'app_secret' },
      expectReason: 'eligibility_binding_secrets_mode_unsafe',
    },
    {
      name: 'safety_verdict deny',
      overrides: { eligibilitySafetyVerdict: 'deny' },
      expectReason: 'eligibility_binding_safety_verdict_denied',
    },
    {
      name: 'additionalProperties rejected',
      overrides: { eligibilityExtraKey: true },
      expectReason: 'eligibility_invalid_additional_properties',
    },
    {
      name: 'missing required key rejected',
      overrides: { eligibilityMissingKey: true },
      expectReason: 'eligibility_invalid_additional_properties',
    },
  ]

  it.each(sourceBoundRejectionCases)(
    'source-bound eligibility rejects: $name',
    ({ overrides, expectReason }) => {
      const basePayload = readFixture('codex-scope-rollup-runner-stop.json')
      const captureDirectory = isolatedDirectory()
      const artifactDirectory = isolatedDirectory()
      const env = writeFixedLocationArtifacts(artifactDirectory, overrides)
      const result = runAdapter(basePayload, captureDirectory, '', env)

      expect(result.status).toBe(0)
      expect(readdirSync(captureDirectory).filter((name) => name.endsWith('.txt'))).toHaveLength(0)
      expect(result.stderr).toContain(expectReason)
    },
  )

  it('valid eligibility writes canonical capture', () => {
    const captureDirectory = isolatedDirectory()
    const artifactDirectory = isolatedDirectory()
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    const env = writeFixedLocationArtifacts(artifactDirectory)
    const result = runAdapter(payload, captureDirectory, '', env)
    const names = readdirSync(captureDirectory)
    const canonical = names.filter(name => name.endsWith('.txt'))

    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    expect(canonical).toHaveLength(1)
    expect(readFileSync(resolve(captureDirectory, canonical[0]), 'utf8')).toBe(payload.last_assistant_message)
    expect(statSync(resolve(captureDirectory, canonical[0])).mode & 0o777).toBe(0o600)
    const sidecars = names.filter(name => name.endsWith('.capture.yaml'))
    expect(sidecars).toHaveLength(1)
    const sidecar = readFileSync(resolve(captureDirectory, sidecars[0]), 'utf8')
    expect(sidecar).toContain('capture_status: captured')
    expect(sidecar).toContain('parser_status: ok')
    expect(sidecar).toContain('routing_action: continue')
    expect(sidecar).toContain('capture_sha256:')
    expect(sidecar).toContain(`invocation_id: ${payloadInvocationId(payload)}`)
    expect(sidecar).toContain('agent_type: scope-rollup-runner')
    expect(sidecar).toContain('capture_source: last_assistant_message')
    // AC15: sidecar provenance records the verified artifact digests and verdicts.
    expect(sidecar).toContain('eligibility_artifact_digest: sha256:')
    expect(sidecar).toContain('eligibility_verification_reason_code: ok')
    expect(sidecar).toContain('readiness_artifact_digest: sha256:')
    expect(sidecar).toContain('readiness_verification_reason_code: ok')
  })

  it('GIVEN a non-target SubagentStop WHEN the adapter runs THEN non-target writes diagnostic sidecar only', () => {
    const captureDirectory = isolatedDirectory()
    const artifactDirectory = isolatedDirectory()
    // AC5 semantic rejection (agent_type mismatch) is decided INSIDE the
    // producer — the Node-only eligibility gate runs first and must be
    // valid/allowed for the producer to be invoked at all.
    const env = writeFixedLocationArtifacts(artifactDirectory)
    const payload = { ...readFixture('codex-subagent-stop-allow.json'), agent_type: 'test-runner', last_assistant_message: 'not a capture target' }
    const result = runAdapter(payload, captureDirectory, '', env)
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
    const artifactDirectory = isolatedDirectory()
    const env = writeFixedLocationArtifacts(artifactDirectory)
    const payload = {
      ...readFixture('codex-scope-rollup-runner-stop.json'),
      last_assistant_message: 'marker is absent',
    }
    const result = runAdapter(payload, captureDirectory, '', env)
    const names = readdirSync(captureDirectory)

    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    expect(names.filter(name => name.endsWith('.txt'))).toHaveLength(0)
    expect(readFileSync(resolve(captureDirectory, names[0]), 'utf8')).toContain('capture_status: parser_rejected')
  })

  it('GIVEN transport fixture failures WHEN the adapter runs THEN transport failures are bounded and redacted', () => {
    for (const fixture of ['nonzero.py', 'timeout.py']) {
      const captureDirectory = isolatedDirectory()
      const artifactDirectory = isolatedDirectory()
      const env = writeFixedLocationArtifacts(artifactDirectory)
      const payload = readFixture('codex-scope-rollup-runner-stop.json')
      const result = runAdapter(
        payload,
        captureDirectory,
        resolve(scopeRollupCaptureFixtures, fixture),
        { NODE_ENV: 'test', ...env },
      )

      expect(result.status).toBe(0)
      expect(result.elapsedMs).toBeLessThan(6500)
      expect(result.stdout.trim()).toBe('{"continue":true}')
      expect(result.stderr).not.toContain('scope-rollup-fixture')
      expect(result.stderr).not.toContain(payloadText(payload))
    }
  }, 7000)

  it('timeout terminates process tree without late write', () => {
    const captureDirectory = isolatedDirectory()
    const artifactDirectory = isolatedDirectory()
    const env = writeFixedLocationArtifacts(artifactDirectory)
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    const result = runAdapter(
      payload,
      captureDirectory,
      resolve(scopeRollupCaptureFixtures, 'late_writer.py'),
      { NODE_ENV: 'test', ...env },
    )

    expect(result.status).toBe(0)
    expect(result.elapsedMs).toBeLessThan(7000)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    expect(existsSync(resolve(captureDirectory, 'scope_rollup_capture_late_write.txt'))).toBe(false)
  })

  it('timeout waits bounded grace and verifies process group liveness absence', () => {
    const captureDirectory = isolatedDirectory()
    const artifactDirectory = isolatedDirectory()
    const env = writeFixedLocationArtifacts(artifactDirectory)
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    const result = runAdapter(
      payload,
      captureDirectory,
      resolve(scopeRollupCaptureFixtures, 'pgid_liveness.py'),
      { NODE_ENV: 'test', ...env },
    )

    expect(result.status).toBe(0)
    // AC18: the fixture records its own PID/PGID before sleeping 30s; the
    // adapter's 3.5s timeout + bounded grace must have already reaped it.
    const pidFile = resolve(captureDirectory, 'pgid_liveness_pids.json')
    expect(existsSync(pidFile)).toBe(true)
    const { pgid } = JSON.parse(readFileSync(pidFile, 'utf8')) as { pid: number; pgid: number }
    let alive
    try {
      process.kill(-pgid, 0)
      alive = true
    } catch {
      alive = false
    }
    expect(alive).toBe(false)
  }, 8000)

  it('bootstrap writes source-bound readiness artifact', () => {
    const captureDirectory = isolatedDirectory()
    const readinessPath = resolve(captureDirectory, 'scope-rollup-readiness-bootstrap.json')
    const result = spawnSync(process.execPath, [bootstrapScript], {
      encoding: 'utf8',
      timeout: 60_000,
      cwd: repoRoot,
      env: {
        ...process.env,
        SCOPE_ROLLUP_READINESS_ARTIFACT_PATH: readinessPath,
      },
    })

    expect(result.status).toBe(0)
    expect(existsSync(readinessPath)).toBe(true)
    const ready = JSON.parse(readFileSync(readinessPath, 'utf8')) as Record<string, unknown>
    expect(ready.schema).toBe('SESSION_RECORDING_SCOPE_ROLLUP_READINESS_V1')
    expect(ready.prepared).toBe(true)
    expect(ready.repo_root_realpath).toBe(repoRoot)
    expect(typeof ready.interpreter_realpath).toBe('string')
    expect(existsSync(ready.interpreter_realpath as string)).toBe(true)
    expect(typeof ready.producer_digest).toBe('string')
    expect(ready.producer_digest).toBe(realProducerDigest)
    expect((statSync(readinessPath).mode & 0o777)).toBe(0o600)
  }, 65_000)

  it('unprepared readiness skips without sync', () => {
    const captureDirectory = isolatedDirectory()
    const artifactDirectory = isolatedDirectory()
    const env = writeFixedLocationArtifacts(artifactDirectory, { readinessPrepared: false })
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    const result = runAdapter(payload, captureDirectory, '', env)

    expect(result.status).toBe(0)
    expect(readdirSync(captureDirectory).filter((name) => name.endsWith('.txt'))).toHaveLength(0)
    expect(readdirSync(captureDirectory).filter((name) => name.endsWith('.capture.yaml'))).toHaveLength(0)
    expect(result.stderr).toContain('readiness_unprepared')
  })

  it('production transport rejects test override and sanitizes child environment', () => {
    const captureDirectory = isolatedDirectory()
    const artifactDirectory = isolatedDirectory()
    const env = writeFixedLocationArtifacts(artifactDirectory)
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    const result = runAdapter(
      payload,
      captureDirectory,
      resolve(scopeRollupCaptureFixtures, 'env-probe.py'),
      {
        NODE_ENV: 'production',
        SECRET_TEST: 'should-not-reach-child',
        ...env,
      },
    )

    const names = readdirSync(captureDirectory)
    const txts = names.filter(name => name.endsWith('.txt'))

    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe('{"continue":true}')
    // NODE_ENV=production means the test override is ignored — the real
    // producer runs and writes the canonical capture, not the env-probe fixture.
    expect(txts).toHaveLength(1)
    expect(readdirSync(captureDirectory).filter(name => name.endsWith('.capture.yaml'))).toHaveLength(1)
    expect(result.stderr).not.toContain('capture_status: capture_nonzero')
    const envProbePath = resolve(captureDirectory, 'env_probe.txt')
    expect(existsSync(envProbePath)).toBe(false)
  })

  it('GIVEN a repeated target payload WHEN the adapter runs twice THEN the producer keeps one canonical capture', () => {
    const captureDirectory = isolatedDirectory()
    const artifactDirectory = isolatedDirectory()
    const env = writeFixedLocationArtifacts(artifactDirectory)
    const payload = readFixture('codex-scope-rollup-runner-stop.json')
    runAdapter(payload, captureDirectory, '', env)
    runAdapter(payload, captureDirectory, '', env)

    expect(readdirSync(captureDirectory).filter(name => name.endsWith('.txt'))).toHaveLength(1)
  })

  it('GIVEN adapter subprocess verification WHEN it passes THEN adapter verification is distinct from live Codex trust', () => {
    // AC11: adapter subprocess verification never asserts runtime-active /
    // production trust. This test only proves the adapter file exists and
    // is invocable — see docs/dev/session-recording-policy.md's "adapter
    // path verified" boundary language for the executable distinction from
    // live Codex smoke.
    expect(existsSync(adapter)).toBe(true)
    const result = spawnSync(process.execPath, ['--check', adapter], { encoding: 'utf8' })
    expect(result.status).toBe(0)
  })
})
